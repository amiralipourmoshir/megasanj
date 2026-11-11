from odoo.tests import tagged
from odoo.exceptions import UserError
from .test_flow import TestMegaSanj


@tagged('post_install','-at_install')
class TestPurchaseFreight(TestMegaSanj):
    def test_receipt_contract_and_waybill_take_flag_from_purchase(self):
        po,wb,q,payload=self.load()
        self.assertTrue(po.ms_freight_by_contractor)
        self.assertTrue(wb.freight_by_contractor)
        receipt=next(r for r in self.env['megasanj.api']._receipts()['data'] if r['purchase_id']==po.id)
        self.assertTrue(receipt['freight_by_contractor'])
        with self.assertRaises(UserError):wb.write({'freight_by_contractor':False})
        po.ms_freight_by_contractor=False
        self.assertFalse(wb.freight_by_contractor)
        self.assertFalse(next(r for r in self.env['megasanj.api']._receipts()['data'] if r['purchase_id']==po.id)['freight_by_contractor'])

    def test_api_cannot_override_purchase_or_charge_contractor(self):
        po,wb,q,payload=self.load()
        data={'event_id':'freight:order:1','picking_id':wb.picking_id.id,'purchase_line_id':po.order_line.id,
            'driver_name':'Driver','driver_phone':'09120000000','trailer_plate':'TEST-2','origin_province':'Tehran',
            'origin_city':'Tehran','origin_net_weight_kg':25000,'freight_by_contractor':False,'freight_amount':999999999}
        result=self.env['megasanj.api']._issue(data)['data']
        self.assertTrue(result['freight_by_contractor']);self.assertEqual(result['freight_amount'],0)
        self.assertTrue(self.env['megasanj.api']._issue(data)['duplicate'])
        po.ms_freight_by_contractor=False
        data.update(event_id='freight:order:2',freight_by_contractor=True,freight_amount=0)
        result=self.env['megasanj.api']._issue(data)['data']
        self.assertFalse(result['freight_by_contractor'])

    def test_purchase_freight_is_fixed_after_weighing(self):
        po,wb,q,payload=self.load()
        self.env['megasanj.document'].ingest(payload)
        with self.assertRaises(UserError):po.ms_freight_by_contractor=False
        self.assertTrue(po.ms_freight_by_contractor)
