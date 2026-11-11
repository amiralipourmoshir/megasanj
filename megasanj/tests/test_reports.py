from html import unescape
from odoo.tests import tagged
from .test_flow import TestMegaSanj


@tagged('post_install','-at_install')
class TestPrintReports(TestMegaSanj):
    def test_minutes_use_filled_fields_and_escape_expert_text(self):
        po,wb,q,payload=self.load(origin=30000)
        result=self.env['megasanj.document'].ingest(payload)
        doc=self.env['megasanj.document'].browse(result['id']);minute=doc.discrepancy_id
        minute.write({'driver_national_id':'1234567890','penalty_weight_kg':0,'deduction_reason':'نظر کارشناس <script>unsafe</script>'})
        minute.action_close()
        text=str(minute._ms_statement_html())
        self.assertIn(wb.driver_name,text)
        self.assertIn('۱۲۳۴۵۶۷۸۹۰',text)
        self.assertIn('۰ کیلوگرم آهن',text)
        self.assertNotIn('................',text)
        self.assertNotIn('<script>',text)
        self.assertEqual(minute.contract()['statement_html'],text)
        report=self.env['ir.actions.report']._render_qweb_html('megasanj.action_report_discrepancy',minute.ids)[0].decode()
        self.assertTrue('صورت‌جلسه رسمی عدم' in unescape(report).replace('\xa0',' '),'minutes heading must match desktop layout')
        self.assertNotIn('data-megasanj-print-source',report)
        self.assertNotIn('نام: ........................................',report)

    def test_missing_national_id_uses_blank_and_surplus_is_not_shortage(self):
        po,wb,q,payload=self.load(origin=20000)
        result=self.env['megasanj.document'].ingest(payload)
        minute=self.env['megasanj.document'].browse(result['id']).discrepancy_id
        # This policy may not detect surplus; explicitly exercise the text on
        # a valid system-owned discrepancy when the policy excludes it.
        if not minute:
            from ..models.common import INTERNAL
            minute=self.env['megasanj.discrepancy'].with_context(_ms_internal=INTERNAL).create({'document_id':result['id'],'origin_kg':20000,'destination_kg':24500})
        text=str(minute._ms_statement_html())
        self.assertIn('................',text)
        self.assertIn('اضافه‌وزن محرز',text)
        self.assertNotIn('کسری محرز',text)

    def test_document_uses_desktop_sections_and_record_values(self):
        po,wb,q,payload=self.load()
        result=self.env['megasanj.document'].ingest(payload)
        doc=self.env['megasanj.document'].browse(result['id'])
        report=self.env['ir.actions.report']._render_qweb_html('megasanj.action_report_document',doc.ids)[0].decode()
        for value in ('سند باسکول مگاسنج','اوزان محموله','ریز توزین‌ها','ورود خودرو',wb.driver_name,wb.trailer_plate):
            self.assertTrue((value if value!=wb.trailer_plate else 'TEST-۱') in report,'missing report field: '+value)
        self.assertNotIn('مگاپالس',report)
        self.assertIn('/megasanj/static/report/logo.png',report)
        self.assertIn('اقلام پذیرفته‌شده',report)
        self.assertNotIn('megasanj',self.env['ir.actions.report']._run_wkhtmltopdf.__module__)
