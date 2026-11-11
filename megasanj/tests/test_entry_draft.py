from odoo.tests import tagged
from odoo.exceptions import UserError
from copy import deepcopy
from .test_flow import TestMegaSanj


@tagged('post_install','-at_install')
class TestEntryDraft(TestMegaSanj):
    def test_cancelled_called_waybill_does_not_block_queue(self):
        from ..models.common import INTERNAL
        po,wb,q,payload=self.load()
        cancelled=wb.copy({'event_id':'gate:cancelled'})
        cancelled.action_call()
        cancelled.with_context(_ms_internal=INTERNAL).write({'state':'cancelled'})
        active=wb.copy({'event_id':'gate:active'})
        active.action_call()
        self.assertEqual(active.gate_state,'ready')
        waiting=wb.copy({'event_id':'gate:waiting'})
        with self.assertRaisesRegex(UserError, active.trailer_plate):
            waiting.action_call()
        active.action_enter()
        waiting.action_call()
        self.assertEqual(waiting.gate_state,'ready')

    def test_queue_exposes_real_utc_entry_time_and_elapsed(self):
        from datetime import timedelta
        from odoo import fields
        from ..models.common import INTERNAL
        po,wb,q,payload=self.load()
        self.assertTrue(wb.called_at)
        self.assertTrue(wb.entry_at)
        entered=fields.Datetime.now()-timedelta(hours=1,minutes=2)
        wb.with_context(_ms_internal=INTERNAL).write({'entry_at':entered})
        api=self.env['megasanj.api']
        row=next(r for r in api._queue()['data']['entries'] if r['id']==wb.id)
        self.assertEqual(row['entered_at'],entered.isoformat()+'Z')
        self.assertGreaterEqual(row['inside_elapsed_seconds'],3720)
        self.assertLess(row['inside_elapsed_seconds'],3725)
        snapshot=api._waybill_contract(wb)
        self.assertEqual(snapshot['entry_at'],row['entered_at'])
        wb.action_enter()
        self.assertEqual(wb.entry_at,entered)

    def test_gate_enter_creates_one_draft_with_waybill_snapshot(self):
        po,wb,q,payload=self.load()
        doc=wb.document_ids
        self.assertEqual(len(doc),1)
        self.assertEqual(doc.state,'draft')
        self.assertTrue(doc.auto_entry_draft)
        wb.action_enter();self.assertEqual(wb.document_ids,doc)
        snapshot=self.env['megasanj.api']._sync()['data']['documents']
        item=next(d for d in snapshot if d['odoo_id']==doc.id)
        self.assertEqual(item['status'],'draft')
        self.assertEqual(item['waybill_snapshot']['driver_name'],wb.driver_name)
        self.assertEqual(item['weighings'],[])
        self.assertFalse(doc.move_ids)

    def test_first_and_second_weighing_reuse_admission_draft(self):
        po,wb,q,payload=self.load()
        doc=wb.document_ids
        data=deepcopy(payload)
        data['event_id']=doc.event_id;data['document']['document_number']=doc.name
        data['document'].pop('finalized_at');data.pop('composition');data.pop('quality_document_code')
        data['weighings']=data['weighings'][:1]
        first=self.env['megasanj.document'].stage_capture(data)
        self.assertEqual(first['id'],doc.id);self.assertEqual(doc.state,'gross')
        self.assertFalse(doc.quality_request_ids)
        self.assertTrue(self.env['megasanj.document'].stage_capture(data)['duplicate'])
        data['weighings']=deepcopy(payload['weighings'])
        result=self.env['megasanj.document'].stage_capture(data)
        self.assertEqual(result['id'],doc.id);self.assertEqual(doc.state,'quality')
        self.assertEqual(len(doc.quality_request_ids),1)
        self.assertEqual(po.order_line.qty_received,0)
        self.assertEqual(len(wb.document_ids),1)

    def test_old_client_can_adopt_unweighed_draft_but_not_duplicate_first(self):
        po,wb,q,payload=self.load()
        original=wb.document_ids
        data=deepcopy(payload);data['weighings']=data['weighings'][:1]
        data.pop('quality_document_code');data.pop('composition');data['document'].pop('finalized_at')
        result=self.env['megasanj.document'].stage_capture(data)
        self.assertEqual(result['id'],original.id)
        data['event_id']='another-device:same-waybill'
        with self.assertRaises(UserError):self.env['megasanj.document'].stage_capture(data)
        self.assertEqual(len(wb.document_ids),1)

    def test_admission_identity_cannot_be_reassigned_to_another_waybill(self):
        po,wb,q,payload=self.load()
        doc=wb.document_ids
        other=wb.copy({'event_id':'entry:another-waybill'})
        self.assertFalse(other.document_ids)
        other.action_call()
        admitted=other.document_ids
        self.assertEqual(admitted.state,'draft')
        other.action_enter()
        self.assertEqual(other.document_ids,admitted)
        data=deepcopy(payload);data['event_id']=doc.event_id
        data['document']['document_number']=doc.name;data['document'].pop('finalized_at')
        data['waybill']['number']=other.number;data['weighings']=data['weighings'][:1]
        data.pop('composition');data.pop('quality_document_code')
        with self.assertRaises(UserError):self.env['megasanj.document'].stage_capture(data)
        self.assertEqual(doc.waybill_id,wb)
        self.assertEqual(len(other.document_ids),1)
