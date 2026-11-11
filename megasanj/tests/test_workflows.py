from odoo import Command
from odoo.tests import tagged
from odoo.exceptions import UserError, ValidationError
from .test_flow import TestMegaSanj
from ..models.common import INTERNAL


@tagged('post_install','-at_install')
class TestWorkflows(TestMegaSanj):
    def paid_load(self,origin=25000):
        self.category.property_cost_method='average'
        po,wb,q,payload=self.load(origin)
        if not self.env.ref('base.IRR').rate_ids:
            self.env['res.currency.rate'].create({'currency_id':self.env.ref('base.IRR').id,'company_id':po.company_id.id,'rate':1})
        po.ms_freight_by_contractor=False
        wb.write({'freight_amount':100,'driver_phone':'09123338877'})
        result=self.env['megasanj.document'].ingest(payload)
        doc=self.env['megasanj.document'].browse(result['id'])
        freight=wb.freight_line_ids
        return po,wb,doc,freight

    def test_driver_contact_reused_for_international_and_persian_phone(self):
        po,wb,q,payload=self.load()
        partner=wb.driver_partner_id
        self.assertTrue(partner)
        with self.assertRaises(UserError):wb.write({'ms_returned':True})
        other=self.env['megasanj.waybill'].create({'picking_id':wb.picking_id.id,'purchase_line_id':wb.purchase_line_id.id,
            'driver_name':'same driver','driver_phone':'+98 912 000 0000','trailer_plate':'TEST2','origin_province':'T','origin_city':'T','origin_net_weight_kg':1})
        self.assertEqual(other.driver_partner_id,partner)
        po.ms_freight_by_contractor=False
        if not self.env.ref('base.IRR').rate_ids:
            self.env['res.currency.rate'].create({'currency_id':self.env.ref('base.IRR').id,'company_id':po.company_id.id,'rate':1})
        other.write({'driver_phone':'۰۹۱۲۰۰۰۰۰۰۰','freight_amount':12})
        self.assertGreater(other.api_freight_irr,0)
        self.assertIn(wb,po.ms_waybill_ids)
        self.assertEqual(wb.parent_purchase_id,po)
        self.assertEqual(self.env['megasanj.waybill']._ms_driver_partner('driver','۰۹۱۲۰۰۰۰۰۰۰',po.company_id),partner)

    def test_full_purchase_return_keeps_freight_and_zero_receipt(self):
        po,wb,doc,freight=self.paid_load()
        self.assertEqual(freight.order_id,po)
        po.action_ms_return_all()
        self.assertTrue(doc.ms_returned)
        self.assertEqual(doc.state,'void')
        self.assertTrue(wb.ms_returned)
        self.assertEqual(wb.state,'cancelled')
        self.assertTrue(po.ms_returned)
        self.assertEqual(doc.return_picking_id.state,'done')
        self.assertAlmostEqual(wb.purchase_line_id.qty_received,0,places=3)
        self.assertTrue(freight.exists())
        before=len(doc.move_ids)
        po.action_ms_return_all();self.assertEqual(len(doc.move_ids),before)

    def test_native_partial_then_full_return_synchronizes_only_after_full(self):
        po,wb,doc,freight=self.paid_load()
        wizard=self.env['stock.return.picking'].with_context(active_id=doc.picking_id.id,active_model='stock.picking').create({'picking_id':doc.picking_id.id})
        for line in wizard.product_return_moves:line.quantity=line.move_id.quantity/2
        returned=wizard._create_return()
        for move in returned.move_ids:move.quantity=move.product_uom_qty;move.picked=True
        returned.button_validate()
        self.assertFalse(doc.ms_returned)
        po.action_ms_return_all()
        self.assertTrue(doc.ms_returned)
        self.assertAlmostEqual(wb.purchase_line_id.qty_received,0,places=3)
        self.assertTrue(freight.exists())


    def test_notification_authored_by_odoobot(self):
        po,wb,doc,freight=self.paid_load(origin=25500)
        self.assertEqual(doc.discrepancy_id.notification_message_id.author_id,self.env.ref('base.partner_root'))


    def test_return_refuses_missing_physical_stock(self):
        po,wb,doc,freight=self.paid_load()
        for line in doc.line_ids:
            self.env['stock.quant']._update_available_quantity(line.product_id,doc.destination_id,-line.weight_kg)
        with self.assertRaises(UserError),self.env.cr.savepoint():po.action_ms_return_all()
        self.assertEqual(doc.state,'posted')
        self.assertTrue(freight.exists())

    def test_cancel_purchase_cascades_and_keeps_freight(self):
        po,wb,doc,freight=self.paid_load()
        po.button_cancel()
        self.assertEqual(po.state,'cancel')
        self.assertEqual(doc.state,'void')
        self.assertAlmostEqual(wb.purchase_line_id.qty_received,0,places=3)
        self.assertTrue(freight.exists())


    def test_native_warehouse_return_can_sync_without_finance_permissions(self):
        from odoo.tests.common import new_test_user
        po,wb,doc,freight=self.paid_load()
        user=new_test_user(self.env,login='ms_return_warehouse',groups='stock.group_stock_user')
        wizard=self.env['stock.return.picking'].with_user(user).with_context(active_id=doc.picking_id.id,active_model='stock.picking').create({'picking_id':doc.picking_id.id})
        for line in wizard.product_return_moves:line.quantity=line.move_id.quantity
        returned=wizard._create_return()
        for move in returned.move_ids:move.quantity=move.product_uom_qty;move.picked=True
        returned.button_validate()
        self.assertTrue(doc.ms_returned)
        self.assertAlmostEqual(wb.purchase_line_id.qty_received,0,places=3)
        self.assertTrue(freight.exists())

    def test_return_uses_actual_classified_receipt_warehouse(self):
        po,wb,q,payload=self.load()
        warehouse=self.env['stock.warehouse'].create({'name':'MS Separate Destination','code':'MSRT','company_id':po.company_id.id})
        target=warehouse.lot_stock_id
        target.ms_category_id=self.category
        payload['destination_warehouse_id']=target.id
        result=self.env['megasanj.document'].ingest(payload)
        doc=self.env['megasanj.document'].browse(result['id'])
        self.assertEqual(doc.state,'posted')
        self.assertNotEqual(doc.picking_id.location_dest_id,target)
        po.action_ms_return_all()
        self.assertEqual(doc.return_picking_id.location_id,target)
        self.assertEqual(doc.return_picking_id.move_ids.location_id,target)
        for product in self.products:
            self.assertAlmostEqual(self.env['stock.quant']._get_available_quantity(product,target),0,places=3)
        self.assertAlmostEqual(wb.purchase_line_id.qty_received,0,places=3)

