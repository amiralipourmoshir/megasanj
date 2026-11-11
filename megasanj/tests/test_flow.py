from copy import deepcopy
from odoo import Command
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user
from odoo.exceptions import UserError, ValidationError
from ..domain import calculate


@tagged('post_install', '-at_install')
class TestMegaSanj(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids += cls.env.ref('megasanj.group_manager')
        cls.kg = cls.env.ref('uom.product_uom_kgm')
        cls.category = cls.env['product.category'].create({'name':'MS Test Scrap','ms_enabled':True,'ms_code':'ms_test_scrap'})
        cls.policy = cls.env['megasanj.policy'].create({'category_id':cls.category.id,'calculator':'scrap_v1',
            'quality_enabled':True,'quality_required':True,'allow_direct_production':True})
        cls.policy.action_publish()
        cls.products = cls.env['product.product'].create([{'name':name,'type':'consu','is_storable':True,
            'categ_id':cls.category.id,'uom_id':cls.kg.id,'ms_weighable':True} for name in ['MS Light','MS Heavy']])
        cls.cargo = cls.env['product.product'].create({'name':'MS Cargo','type':'consu','is_storable':True,
            'categ_id':cls.category.id,'uom_id':cls.kg.id})
        cls.vendor = cls.env['res.partner'].create({'name':'MS Vendor','is_company':True,'supplier_rank':1})
        cls.env['megasanj.eligibility'].create({'partner_id':cls.vendor.id,'category_id':cls.category.id})
        cls.location = cls.env['stock.warehouse'].search([('company_id','=',cls.env.company.id)],limit=1).lot_stock_id
        cls.location.write({'ms_category_id':cls.category.id,'ms_receiving_default':True})

    def load(self,origin=25000):
        po = self.env['purchase.order'].create({'partner_id':self.vendor.id,'ms_freight_by_contractor':True,'order_line':[Command.create({
            'product_id':self.cargo.id,'product_qty':30000,'product_uom_id':self.kg.id,'price_unit':10})]})
        po.button_confirm()
        picking = po.picking_ids[:1]
        wb = self.env['megasanj.waybill'].create({'picking_id':picking.id,'purchase_line_id':po.order_line.id,
            'driver_name':'Test Driver','driver_phone':'09120000000','trailer_plate':'TEST-1','origin_province':'Tehran',
            'origin_city':'Tehran','origin_net_weight_kg':origin,'freight_by_contractor':True})
        wb.action_call()
        wb.action_enter()
        quality = self.env['quality.check'].create({'test_type_id':self.env.ref('megasanj.test_type_percentage').id,
            'picking_id':picking.id,'ms_category_id':self.category.id,'ms_loss':1,'ms_rejected':2,
            'ms_line_ids':[Command.create({'product_id':p.id,'percentage':percent}) for p,percent in zip(self.products,[60,40])]})
        quality.action_ms_release()
        payload = {'schema_version':3,'event_id':'test:%s'%wb.id,'material_type_id':self.category.id,
            'material_policy_id':self.policy.id,'destination_warehouse_id':self.location.id,
            'quality_document_code':quality.ms_code,'document':{'document_number':'MS-%s'%wb.id,
            'movement_type':'external_to_warehouse','registered_at':'2026-10-29T10:00:00+03:30','finalized_at':'2026-10-29T11:00:00+03:30'},
            'waybill':{'number':wb.number},'vehicle':{'trailer_plate':'TEST-1'},
            'weighings':[{'sequence':1,'kind':'gross','weight_kg':40000,'source':'device','captured_at':'2026-10-29T10:00:00+03:30'},
                         {'sequence':2,'kind':'tare','weight_kg':15500,'source':'manual','manual_reason':'Test','captured_at':'2026-10-29T11:00:00+03:30'}],
            'composition':[{'material_item_id':p.id,'percentage':percent} for p,percent in zip(self.products,[60,40])]}
        return po,wb,quality,payload

    def test_quality_purchase_receipt_replay(self):
        po,wb,q,payload = self.load()
        result = self.env['megasanj.document'].ingest(payload)
        self.assertEqual(result['accepted_weight_kg'],24250)
        doc = self.env['megasanj.document'].browse(result['id'])
        self.assertEqual(doc.state,'posted',doc.error)
        self.assertEqual(doc.picking_id.state,'done')
        self.assertAlmostEqual(po.order_line.qty_received,24250,places=3)
        self.assertEqual(q.ms_state,'attached')
        self.assertEqual(sum(doc.line_ids.mapped('weight_kg')),24250)
        before = len(doc.move_ids)
        duplicate = self.env['megasanj.document'].ingest(payload)
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(duplicate['id'],doc.id)
        self.assertEqual(len(doc.move_ids),before)
        changed = deepcopy(payload)
        changed['weighings'][0]['weight_kg'] += 1
        with self.assertRaises(UserError): self.env['megasanj.document'].ingest(changed)
        with self.assertRaises(UserError): q.write({'ms_loss':2})

    def test_discrepancy_blocks_inventory_then_posts_once(self):
        self.policy = self.policy.copy({'version':2,'state':'draft','inventory_requires_discrepancy_closed':True})
        self.policy.action_publish()
        po,wb,q,payload = self.load(origin=25500)
        result = self.env['megasanj.document'].ingest(payload)
        doc = self.env['megasanj.document'].browse(result['id'])
        self.assertEqual(doc.state,'pending')
        self.assertEqual(po.order_line.qty_received,0)
        doc.discrepancy_id.write({'penalty_weight_kg':100,'deduction_reason':'Test decision'})
        doc.discrepancy_id.action_close()
        self.assertEqual(doc.state,'posted',doc.error)
        self.assertAlmostEqual(po.order_line.qty_received,24250,places=3)
        doc.discrepancy_id.action_close()
        self.assertAlmostEqual(po.order_line.qty_received,24250,places=3)

    def test_checkpoint_blocks_departure(self):
        point = self.env['megasanj.checkpoint'].create({'name':'Exit inspection'})
        po,wb,q,payload = self.load()
        self.env['megasanj.document'].ingest(payload)
        with self.assertRaises(UserError): wb.action_exit()
        wb.gate_check_ids.write({'state':'pass'})
        wb.action_exit()
        self.assertEqual(wb.gate_state,'exited')

    def test_policy_is_immutable_and_percentage_total(self):
        with self.assertRaises(UserError): self.policy.write({'tolerance_kg':5})
        with self.assertRaises(ValueError):
            calculate(40000,15500,[{'material_item_id':1,'percentage':99}],'scrap_v1',1,2)
        with self.assertRaises(ValueError):
            calculate(40000,15500,[{'material_item_id':1,'percentage':100}],'net',1,0)

    def test_direct_writes_cannot_bypass_gate_or_document(self):
        po,wb,q,payload = self.load()
        with self.assertRaises(UserError): wb.write({'gate_state':'exited'})
        with self.assertRaises(UserError):
            self.env['megasanj.document'].create({'name':'FAKE'})

    def test_transfer_uses_native_moves_without_negative_stock(self):
        po,wb,q,payload = self.load()
        self.env['megasanj.document'].ingest(payload)
        destination = self.env['stock.location'].create({'name':'MS transfer destination','usage':'internal',
            'location_id':self.location.id,'company_id':self.env.company.id,'ms_category_id':self.category.id})
        transfer = deepcopy(payload)
        transfer.update(event_id='transfer:1',source_warehouse_id=self.location.id,destination_warehouse_id=destination.id,quality_document_code=None)
        transfer.pop('waybill')
        transfer['document'].update(document_number='MS-TRANSFER-1',movement_type='warehouse_transfer')
        transfer['weighings'][0]['weight_kg']=16500
        result=self.env['megasanj.document'].ingest(transfer)
        doc=self.env['megasanj.document'].browse(result['id'])
        self.assertEqual(doc.state,'posted',doc.error)
        self.assertEqual(doc.picking_id.state,'done')
        self.assertEqual(doc.effect,'transfer')

    def test_receipt_void_returns_purchase_quantity(self):
        po,wb,q,payload=self.load()
        result=self.env['megasanj.document'].ingest(payload)
        doc=self.env['megasanj.document'].browse(result['id'])
        doc.void_reason='Test duplicate truck corrected through return'
        doc.action_void()
        self.assertEqual(doc.state,'void')
        self.assertEqual(doc.return_picking_id.state,'done')
        self.assertAlmostEqual(po.order_line.qty_received,0,places=3)

    def test_normal_user_receives_purchase_without_superuser(self):
        po,wb,q,payload=self.load()
        user=new_test_user(self.env,login='ms_operator_orm',groups='megasanj.group_manager')
        result=self.env['megasanj.document'].with_user(user).ingest(payload)
        doc=self.env['megasanj.document'].browse(result['id'])
        self.assertEqual(doc.state,'posted',doc.error)
        self.assertEqual(doc.write_uid,user)
        self.assertAlmostEqual(po.order_line.qty_received,24250,places=3)

    def test_percentage_point_uses_native_operation_checks(self):
        point=self.env['quality.point'].create({'title':'Percentage incoming','test_type_id':self.env.ref('megasanj.test_type_percentage').id,
            'picking_type_ids':[Command.set(self.env['stock.picking.type'].search([('code','=','incoming'),('company_id','=',self.env.company.id)]).ids)],
            'measure_on':'operation','ms_category_id':self.category.id,
            'ms_line_ids':[Command.create({'product_id':p.id,'percentage':pct}) for p,pct in zip(self.products,[60,40])]})
        self.assertEqual(point.test_type,'megasanj_percentage')
        with self.assertRaises(ValidationError):
            point.write({'ms_line_ids':[Command.update(point.ms_line_ids[0].id,{'percentage':55})]})

    def test_consumption_targets_mo_without_completing_it(self):
        po,wb,q,payload = self.load()
        self.env['megasanj.document'].ingest(payload)
        output = self.env['product.product'].create({'name':'MS Finished','type':'consu','is_storable':True,'uom_id':self.kg.id})
        bom = self.env['mrp.bom'].create({'product_tmpl_id':output.product_tmpl_id.id,'product_qty':1000,'product_uom_id':self.kg.id,
            'bom_line_ids':[Command.create({'product_id':p.id,'product_qty':qty,'product_uom_id':self.kg.id}) for p,qty in zip(self.products,[600,400])]})
        mo = self.env['mrp.production'].create({'product_id':output.id,'bom_id':bom.id,'product_qty':1000,'product_uom_id':self.kg.id,
            'location_src_id':self.location.id})
        mo.action_confirm()
        consumption = deepcopy(payload)
        consumption.update(event_id='consume:1',production_id=mo.id,source_warehouse_id=self.location.id)
        consumption.pop('destination_warehouse_id')
        consumption.pop('waybill')
        consumption['quality_document_code'] = None
        consumption['document'].update(document_number='MS-CONSUME-1',movement_type='warehouse_to_production')
        consumption['weighings'][0]['weight_kg'] = 16500
        result = self.env['megasanj.document'].ingest(consumption)
        doc = self.env['megasanj.document'].browse(result['id'])
        self.assertEqual(doc.state,'posted',doc.error)
        self.assertEqual(doc.effect,'consumption_recorded')
        self.assertIn(mo.state,('confirmed','progress'))
        self.assertNotEqual(mo.state,'done')
        self.assertEqual(sum(mo.move_raw_ids.mapped('ms_weighed_kg')),1000)
        self.env['megasanj.document'].ingest(consumption)
        self.assertEqual(sum(mo.move_raw_ids.mapped('ms_weighed_kg')),1000)
