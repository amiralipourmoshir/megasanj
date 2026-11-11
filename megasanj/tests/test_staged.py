from copy import deepcopy
from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import new_test_user
from .test_flow import TestMegaSanj


@tagged('post_install','-at_install')
class TestStaged(TestMegaSanj):
    def captures(self, origin=25000):
        po,wb,q,payload=self.load(origin)
        payload.pop('quality_document_code')
        payload['material_policy_id']=wb.policy_id.id
        payload['event_type']='weighbridge.capture'
        payload['weighings']=[dict(payload['weighings'][0],sequence=1),dict(payload['weighings'][1],sequence=2)]
        first=deepcopy(payload);first['weighings']=first['weighings'][:1]
        return po,wb,first,payload

    def test_staged_qc_after_weighing(self):
        po,wb,first,second=self.captures()
        model=self.env['megasanj.document']
        a=model.stage_capture(first); doc=model.browse(a['id'])
        self.assertEqual(doc.state,'gross');self.assertEqual(po.picking_ids.state,'assigned')
        self.assertFalse(doc.move_ids);self.assertFalse(doc.quality_request_ids)
        self.assertTrue(model.stage_capture(first)['duplicate'])
        b=model.stage_capture(second)
        self.assertEqual(a['id'],b['id']);self.assertEqual(doc.state,'quality');self.assertFalse(doc.move_ids)
        self.assertEqual(wb.state,'used')
        qc=doc.quality_request_ids
        self.assertEqual(len(qc),1);self.assertEqual(qc.ms_state,'draft')
        with self.assertRaises(UserError):doc.action_post()
        with self.assertRaises(UserError):doc.action_skip_quality()
        qc.write({'ms_loss':1,'ms_rejected':2,'ms_line_ids':[Command.create({'product_id':p.id,'percentage':pct}) for p,pct in zip(self.products,[60,40])]})
        assessor=new_test_user(self.env,login="ms_qc_only",groups="quality.group_quality_user")
        qc.with_user(assessor).action_ms_release()
        self.assertEqual(doc.state,'posted',doc.error)
        self.assertEqual(doc.net_kg,24500);self.assertEqual(doc.original_kg,24500)
        self.assertEqual(doc.accepted_kg,23765);self.assertEqual(doc.loss_kg,245);self.assertEqual(doc.rejected_kg,490)
        self.assertEqual(qc.ms_state,'attached');self.assertEqual(po.order_line.qty_received,23765)
        self.assertTrue(model.stage_capture(second)['duplicate'])
        self.assertEqual(len(doc.quality_request_ids),1)

    def test_capture_immutable_and_negative(self):
        po,wb,first,second=self.captures()
        model=self.env['megasanj.document'];model.stage_capture(first)
        bad=deepcopy(second);bad['weighings'][0]['weight_kg']+=1
        with self.assertRaises(UserError):model.stage_capture(bad)
        bad=deepcopy(second);bad['weighings'][1]['weight_kg']=50000
        with self.assertRaises(ValidationError):model.stage_capture(bad)

    def test_new_capture_must_start_with_gross(self):
        po,wb,first,second=self.captures()
        first['weighings']=[dict(second['weighings'][1],sequence=1)]
        with self.assertRaises(ValidationError):self.env['megasanj.document'].stage_capture(first)
        self.assertFalse(self.env['megasanj.document'].search([('event_id','=',first['event_id'])]))

    def test_report_templates_render_real_record_fields(self):
        po,wb,first,second=self.captures(30000)
        doc=self.env['megasanj.document'].browse(self.env['megasanj.document'].stage_capture(second)['id'])
        report=self.env['ir.actions.report']
        html,kind=report._render_qweb_html('megasanj.action_report_discrepancy',doc.discrepancy_id.ids)
        self.assertEqual(kind,'html')
        self.assertIn(doc.discrepancy_id.name.encode(),html)
        self.assertIn(wb.driver_name.encode(),html)
        html,kind=report._render_qweb_html('megasanj.action_report_document',doc.ids)
        self.assertIn(doc.name.encode(),html)

    def test_discrepancy_notification_once(self):
        po,wb,first,second=self.captures(30000)
        doc=self.env['megasanj.document'].browse(self.env['megasanj.document'].stage_capture(second)['id'])
        discrepancy=doc.discrepancy_id
        self.assertTrue(discrepancy.notification_message_id)
        self.assertEqual(discrepancy.notification_message_id.res_id,self.env.ref('megasanj.discuss_channel').id)
        self.assertEqual(discrepancy.destination_kg,24500)
        mid=discrepancy.notification_message_id.id
        discrepancy._ms_notify();self.env['megasanj.document'].stage_capture(second)
        self.assertEqual(discrepancy.notification_message_id.id,mid)

    def test_group_invites_only_seniors(self):
        operator=new_test_user(self.env,login='ms_stage_operator',groups='megasanj.group_operator')
        target=new_test_user(self.env,login='ms_stage_target',groups='base.group_user')
        channel=self.env.ref('megasanj.discuss_channel')
        channel.add_members(partner_ids=operator.partner_id.ids)
        with self.assertRaises(AccessError):channel.with_user(operator).add_members(partner_ids=target.partner_id.ids)
        with self.assertRaises(AccessError):self.env['discuss.channel.member'].with_user(operator).create({'channel_id':channel.id,'partner_id':target.partner_id.id})
        channel.add_members(partner_ids=target.partner_id.ids)
        self.assertIn(target.partner_id,channel.channel_partner_ids)

    def test_optional_quality_can_be_skipped_without_fake_pass(self):
        policy=self.env['megasanj.policy'].create({'category_id':self.category.id,'version':2,'calculator':'scrap_v1','quality_enabled':True,'quality_required':False})
        policy.action_publish()
        # The single purchase item must be an allowed composition item to bypass QC.
        self.cargo.ms_weighable=True
        po,wb,first,second=self.captures()
        doc=self.env['megasanj.document'].browse(self.env['megasanj.document'].stage_capture(second)['id'])
        self.assertEqual(doc.state,'quality')
        doc.action_skip_quality()
        self.assertEqual(doc.state,'posted',doc.error)
        self.assertEqual(doc.accepted_kg,24500)
        self.assertFalse(doc.quality_request_ids)

    def test_point_template_requested_only_after_full_weight(self):
        point=self.env['quality.point'].create({'title':'Post weigh percentage','test_type_id':self.env.ref('megasanj.test_type_percentage').id,
            'picking_type_ids':[Command.set(self.env['stock.picking.type'].search([('code','=','incoming'),('company_id','=',self.env.company.id)]).ids)],
            'ms_category_id':self.category.id,'ms_loss':1,'ms_rejected':2,
            'ms_line_ids':[Command.create({'product_id':p.id,'percentage':pct}) for p,pct in zip(self.products,[60,40])]})
        po,wb,first,second=self.captures()
        self.assertFalse(self.env['quality.check'].search([('point_id','=',point.id),('picking_id','=',wb.picking_id.id)]))
        doc=self.env['megasanj.document'].browse(self.env['megasanj.document'].stage_capture(first)['id'])
        self.assertFalse(doc.quality_request_ids)
        self.env['megasanj.document'].stage_capture(second)
        self.assertEqual(doc.quality_request_ids.point_id,point)
        self.assertEqual(doc.quality_request_ids.ms_line_ids.mapped('percentage'),[60,40])
        self.assertEqual(doc.quality_request_ids.ms_state,'draft')

    def test_direct_purchase_waits_for_qc_then_allocates_to_bom(self):
        output=self.env['product.product'].create({'name':'MS staged finished','type':'consu','is_storable':True,'uom_id':self.kg.id})
        bom=self.env['mrp.bom'].create({'product_tmpl_id':output.product_tmpl_id.id,'product_qty':1000,'product_uom_id':self.kg.id,
            'bom_line_ids':[Command.create({'product_id':p.id,'product_qty':qty,'product_uom_id':self.kg.id}) for p,qty in zip(self.products,[600,400])]})
        mo=self.env['mrp.production'].create({'product_id':output.id,'bom_id':bom.id,'product_qty':1000,'product_uom_id':self.kg.id,'location_src_id':self.location.id})
        mo.action_confirm()
        po,wb,first,second=self.captures()
        for payload in (first,second):
            payload['document']['movement_type']='external_to_production'
            payload.pop('destination_warehouse_id');payload['production_id']=mo.id
        model=self.env['megasanj.document'];a=model.stage_capture(first);model.stage_capture(second)
        doc=model.browse(a['id'])
        self.assertEqual(doc.state,'quality');self.assertFalse(doc.move_ids)
        self.assertFalse(any(mo.move_raw_ids.mapped('ms_weighed_kg')))
        qc=doc.quality_request_ids
        qc.write({'ms_loss':1,'ms_rejected':2,'ms_line_ids':[Command.create({'product_id':p.id,'percentage':pct}) for p,pct in zip(self.products,[60,40])]})
        qc.action_ms_release()
        self.assertEqual(doc.state,'posted',doc.error)
        self.assertEqual(doc.effect,'direct')
        self.assertEqual(sum(mo.move_raw_ids.mapped('ms_weighed_kg')),23765)
        self.assertNotEqual(mo.state,'done')
