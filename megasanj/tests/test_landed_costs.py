from odoo import fields
from odoo.tests import tagged
from odoo.exceptions import UserError
from .test_workflows import TestWorkflows


@tagged('post_install', '-at_install')
class TestNativeFreight(TestWorkflows):
    def test_native_po_freight_replaces_custom_receipt(self):
        po,wb,doc,line=self.paid_load()
        self.assertEqual(line.order_id,po)
        self.assertEqual(line.price_unit,100)
        self.assertTrue(line.product_id.landed_cost_ok)
        self.assertEqual(line.product_id.type,'service')
        self.assertFalse(self.env['megasanj.freight'].search([('document_id','=',doc.id)]))
        self.assertEqual(wb.driver_partner_id.supplier_rank,0)
        wb._ms_sync_purchase_freight()
        self.assertEqual(len(wb.freight_line_ids),1)
        cost=self.env['stock.landed.cost'].search([('ms_waybill_id','=',wb.id)])
        self.assertEqual(len(cost),1)
        self.assertEqual(cost.state,'done')
        self.assertEqual(cost.amount_total,wb.freight_amount)
        self.assertEqual(cost.picking_ids,doc.picking_id)
        self.assertFalse(cost.vendor_bill_id)
        self.assertAlmostEqual(sum(doc.move_ids.mapped('value')),doc.accepted_kg*wb.purchase_line_id.price_unit+wb.freight_amount)

    def test_native_bill_uses_purchase_vendor_and_links_freight(self):
        po,wb,doc,line=self.paid_load()
        po.action_create_invoice()
        bill=po.invoice_ids
        freight=bill.invoice_line_ids.filtered(lambda l:l.purchase_line_id==line)
        self.assertEqual(bill.partner_id,po.partner_id)
        self.assertNotEqual(bill.partner_id,wb.driver_partner_id)
        self.assertTrue(freight.is_landed_costs_line)
        self.assertEqual(freight.price_subtotal,100)
        with self.assertRaises(UserError):wb.write({'freight_amount':120})
        self.env['res.company']._ms_migrate_native_freight()
        self.assertEqual(len(line.invoice_lines),1)

    def test_landed_cost_increases_native_receipt_value_once(self):
        self.category.property_cost_method='average'
        po,wb,doc,line=self.paid_load()
        po.action_create_invoice()
        bill=po.invoice_ids
        bill.invoice_date=fields.Date.today()
        bill.action_post()
        self.assertEqual(len(bill.landed_costs_ids),1)
        action=bill.button_create_landed_costs()
        landed=self.env['stock.landed.cost'].browse(action['res_id'])
        self.assertEqual(landed.picking_ids,doc.picking_id)
        self.assertEqual(landed.amount_total,100)
        self.assertEqual(landed.state,'done')
        self.assertAlmostEqual(sum(landed.valuation_adjustment_lines.mapped('additional_landed_cost')),100)
        original=sum(doc.move_ids.mapped('value'))
        wb._ms_apply_landed_freight()
        doc.action_post()
        doc.move_ids.invalidate_recordset()
        self.assertAlmostEqual(sum(doc.move_ids.mapped('value')),original)
        bill.button_create_landed_costs()
        self.assertEqual(len(bill.landed_costs_ids),1)

    def test_contractor_has_no_added_service(self):
        po,wb,q,payload=self.load()
        self.assertFalse(wb.freight_line_ids)
        contact=wb._ms_driver_partner('Only a driver','09123338877',po.company_id)
        self.assertFalse(contact.supplier_rank)
        result=self.env['megasanj.document'].ingest(payload)
        self.assertEqual(self.env['megasanj.document'].browse(result['id']).state,'posted')
        self.assertFalse(self.env['stock.landed.cost'].search([('ms_waybill_id','=',wb.id)]))

    def test_unbilled_freight_edit_updates_same_po_line(self):
        po,wb,q,payload=self.load()
        if not self.env.ref('base.IRR').rate_ids:
            self.env['res.currency.rate'].create({'currency_id':self.env.ref('base.IRR').id,'company_id':po.company_id.id,'rate':1})
        po.ms_freight_by_contractor=False
        wb.freight_amount=100
        line=wb.freight_line_ids
        wb.freight_amount=120
        self.assertEqual(wb.freight_line_ids,line)
        self.assertEqual(line.price_unit,120)
        po.ms_freight_by_contractor=True
        self.assertEqual(wb.freight_line_ids.product_qty,0)

    def test_legacy_approval_is_disabled(self):
        with self.assertRaises(UserError):self.env['megasanj.freight'].create({})

    def test_driver_vendor_cleanup_preserves_actual_supplier_role(self):
        po,wb,doc,line=self.paid_load()
        driver=wb.driver_partner_id
        driver.supplier_rank=5
        self.env['res.company']._ms_migrate_native_freight()
        self.assertEqual(driver.supplier_rank,0)
        self.env['megasanj.eligibility'].create({'partner_id':driver.id,'category_id':self.category.id})
        driver.supplier_rank=5
        self.env['res.company']._ms_migrate_native_freight()
        self.assertEqual(driver.supplier_rank,5)

    def test_no_cost_before_receipt_and_zero_freight_never_capitalized(self):
        po,wb,q,payload=self.load()
        po.ms_freight_by_contractor=False
        wb._ms_apply_landed_freight()
        self.assertFalse(self.env['stock.landed.cost'].search([('ms_waybill_id','=',wb.id)]))
        result=self.env['megasanj.document'].ingest(payload)
        self.assertEqual(self.env['megasanj.document'].browse(result['id']).state,'posted')
        self.assertFalse(self.env['stock.landed.cost'].search([('ms_waybill_id','=',wb.id)]))
