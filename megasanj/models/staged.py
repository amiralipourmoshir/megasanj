import copy
import hashlib
import json
import re
from odoo import api, fields, models
from odoo.fields import Domain
from odoo.exceptions import UserError, ValidationError
from ..domain import number, kg
from .common import INTERNAL


class StagedDocument(models.Model):
    _inherit = 'megasanj.document'
    state = fields.Selection(selection_add=[('gross','در انتظار توزین دوم'),('tare','وزن خالی ثبت‌شده'),('quality','در انتظار کنترل کیفیت')], ondelete={'gross':'set default','tare':'set default','quality':'set default'})
    capture_hashes = fields.Json(copy=False)
    qc_required = fields.Boolean(related='policy_id.quality_required')
    workflow_version = fields.Integer(default=1, readonly=True)
    driver_name = fields.Char(related='waybill_id.driver_name', string='راننده')
    driver_phone = fields.Char(related='waybill_id.driver_phone', string='تلفن راننده')
    trailer_plate = fields.Char(related='waybill_id.trailer_plate', string='پلاک')
    quality_request_ids = fields.One2many('quality.check','ms_weighbridge_id', string='ارزیابی کیفیت محموله')
    weight_note = fields.Text(compute='_compute_weight_note', string='مبنای محاسبه وزن')

    @api.depends('state','workflow_version','gross_kg','tare_kg','net_kg','accepted_kg','loss_kg','rejected_kg')
    def _compute_weight_note(self):
        for doc in self:
            if doc.state=='draft':
                doc.weight_note='پیش‌نویس پس از اجازه ورود خودرو ساخته شده است؛ اطلاعات اولیه و سپس وزن پر را تکمیل کنید.'
            elif doc.state=='gross':
                doc.weight_note='وزن پر ثبت شده است؛ پس از وزن خالی، خالص محاسبه و محموله برای کنترل کیفیت ارسال می‌شود. موجودی ثبت نشده است.'
            elif doc.state=='tare':
                doc.weight_note='فقط وزن خالی ثبت شده است؛ خالص و مقدار قابل دریافت پس از وزن پر و ارزیابی لازم مشخص می‌شوند. موجودی ثبت نشده است.'
            elif doc.state=='quality':
                doc.weight_note='خالص = وزن پر − وزن خالی. وزن پذیرفته‌شده هنوز تعیین نشده و موجودی در انتظار کنترل کیفیت است.'
            elif doc.workflow_version>=2:
                doc.weight_note='خالص و مبنای مقایسه = وزن پر − وزن خالی؛ پذیرفته‌شده = خالص − افت − برگشتی. همه وزن‌ها کیلوگرم هستند.'
            else:
                doc.weight_note='سند گردش قبلی است؛ روش قراضه قدیمی وزن قبل از برگشتی را بازسازی می‌کند. مبنای مقایسه، خالص توزین و وزن پذیرفته‌شده مفاهیم متفاوت‌اند؛ سند قبلی بازنویسی نشده است.'

    @api.model
    def stage_capture(self, data):
        if not self.env.user.has_group('megasanj.group_operator'):
            raise UserError('دسترسی باسکول لازم است.')
        event = data.get('event_id','')
        if not isinstance(event,str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,120}',event):
            raise ValidationError('شناسه سند معتبر لازم است.')
        header, ws = data.get('document') or {}, data.get('weighings') or []
        movement=header.get('movement_type')
        if data.get('schema_version') != 3 or movement not in ('external_to_warehouse','external_to_production'):
            raise ValidationError('گردش مرحله‌ای برای دریافت خرید به انبار یا سفارش تولید است.')
        if len(ws) not in (1,2) or ws[0].get('sequence') != 1 or ws[0].get('kind') not in ('gross','tare'):
            raise ValidationError('ابتدا وزن پر و سپس وزن خالی روی همان سند ثبت شود.')
        first_kind=ws[0]['kind']
        second_kind='tare' if first_kind=='gross' else 'gross'
        complete=len(ws)==2
        if complete and (ws[1].get('sequence') != 2 or ws[1].get('kind') != second_kind):
            raise ValidationError('توزین دوم باید نوع مقابل توزین اول باشد.')
        for w in ws:
            if number(w.get('weight_kg')) <= 0 or w.get('source') not in ('manual','device') or not w.get('captured_at') or (w['source']=='manual' and not str(w.get('manual_reason') or '').strip()):
                raise ValidationError('وزن مثبت، روش و زمان ثبت و علت ثبت دستی لازم است.')
            try:
                from datetime import datetime
                datetime.fromisoformat(str(w['captured_at']).replace('Z','+00:00'))
            except ValueError: raise ValidationError('زمان توزین معتبر نیست.') from None
        name = header.get('document_number')
        if not isinstance(name,str) or not 1 <= len(name) <= 120:
            raise ValidationError('شماره سند لازم است.')
        self.env.cr.execute('SELECT pg_advisory_xact_lock(%s,%s)',(74183,self.env.company.id))
        doc = self.search([('event_id','=',event),('company_id','=',self.env.company.id)])
        if first_kind=='tare' and not (doc and doc.workflow_version==2):
            raise ValidationError('برای سند جدید، توزین اول حتماً وزن پر و توزین دوم وزن خالی است.')
        digest = hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
        phase = str(len(ws))
        if doc and (doc.capture_hashes or {}).get(phase):
            if doc.capture_hashes[phase] != digest:
                raise UserError('توزین ثبت‌شده با محتوای متفاوت قابل جایگزینی نیست.')
            return dict(doc.contract(),duplicate=True)
        if doc and phase=='1' and doc.workflow_version>=2:
            expected=copy.deepcopy(doc.payload)
            expected['weighings']=expected['weighings'][:1]
            # Final QC adds fields to the final payload; compare the original capture.
            expected.pop('quality_document_code',None)
            expected.pop('composition',None)
            expected['document'].pop('finalized_at',None)
            if expected==data:
                doc._set({'capture_hashes':dict(doc.capture_hashes or {},**{'1':digest})})
                return dict(doc.contract(),duplicate=True)
        if doc and (doc.workflow_version < 2 or doc.state not in ('draft','gross','tare')):
            raise UserError('این سند وارد مرحله بعد شده یا نهایی شده است.')
        wb = self.env['megasanj.waybill'].search([('number','=',int((data.get('waybill') or {}).get('number') or 0)),('company_id','=',self.env.company.id)],limit=1)
        if not wb: raise ValidationError('بارنامه پیدا نشد.')
        self.env.cr.execute('SELECT id FROM megasanj_waybill WHERE id=%s FOR UPDATE',(wb.id,))
        wb.invalidate_recordset()
        if doc and doc.waybill_id!=wb:
            raise UserError('پیش‌نویس به همین بارنامه متصل است و قابل جابه‌جایی نیست.')
        linked=self.search([('waybill_id','=',wb.id)],limit=1)
        if not doc and linked:
            if linked.state=='draft' and linked.auto_entry_draft:doc=linked
            else:raise UserError('سند این بارنامه قبلاً ساخته شده است؛ همان سند را ادامه دهید.')
        if wb.gate_state != 'inside' or wb.state not in ('issued','reserved') or wb.picking_id.state in ('done','cancel'):
            raise UserError('رسید باید باز و خودرو داخل مجموعه باشد.')
        if wb.state=='reserved' and wb.reservation_expires_at > fields.Datetime.now() and wb.reservation_token != (data.get('waybill') or {}).get('reservation_token'):
            raise UserError('بارنامه در رزرو دستگاه دیگر است.')
        if data.get('material_type_id') != wb.category_id.id or data.get('material_policy_id') not in (None,False,wb.policy_id.id):
            raise ValidationError('نوع محموله یا نسخه سیاست با بارنامه برابر نیست.')
        if not self.env['megasanj.eligibility'].search_count([('partner_id','=',wb.partner_id.id),('category_id','=',wb.category_id.id),('company_id','=',self.env.company.id),('active','=',True)]) or not wb.partner_id.active:
            raise UserError('تأمین‌کننده مجاز نیست.')
        if not (data.get('vehicle') or {}).get('trailer_plate') or data['vehicle']['trailer_plate'] != wb.trailer_plate:
            raise ValidationError('پلاک باید با بارنامه برابر باشد.')
        production=self.env['mrp.production'].browse(int(data.get('production_id') or 0)).exists()
        if movement=='external_to_production':
            if not wb.policy_id.allow_direct_production or not production or production.company_id!=self.env.company or production.state not in ('confirmed','progress','to_close') or data.get('destination_warehouse_id') or data.get('source_warehouse_id'):
                raise ValidationError('ورود مستقیم باید مجاز و به سفارش تولید فعال همان شرکت مرتبط باشد؛ انبار دستی انتخاب نمی‌شود.')
            destination=production.location_src_id
        else:
            if production or data.get('source_warehouse_id'):raise ValidationError('دریافت خرید به انبار فقط محل مقصد دارد.')
            destination=self.env['stock.location'].browse(int(data.get('destination_warehouse_id') or 0)).exists()
            if not destination or destination.company_id != self.env.company or not destination.active or destination.usage != 'internal' or destination.ms_category_id != wb.category_id:
                raise ValidationError('انبار مقصد معتبر این نوع محموله لازم است.')
        if doc and doc.state!='draft':
            old=doc.payload
            if old['weighings'][0] != ws[0] or doc.waybill_id != wb or doc.name != name or doc.destination_id != destination or doc.production_id!=production or doc.movement_type!=movement:
                raise UserError('توزین اول و اطلاعات پایه ثبت‌شده ثابت هستند؛ برای اصلاح، ابطال کنید.')
        measured={w['kind']:float(kg(w['weight_kg'])) for w in ws}
        gross=measured.get('gross',0);tare=measured.get('tare',0)
        if complete and gross <= tare: raise ValidationError('وزن پر باید از خالی بیشتر باشد.')
        snapshot=dict(wb.policy_id.contract(),calculator='scrap_net' if wb.policy_id.calculator=='scrap_v1' else wb.policy_id.calculator,weight_basis='gross_minus_tare')
        values={'name':name,'event_id':event,'payload_hash':digest,'payload':copy.deepcopy(data),'category_id':wb.category_id.id,
            'policy_id':wb.policy_id.id,'policy_snapshot':snapshot,'movement_type':movement,'waybill_id':wb.id,'production_id':production.id,
            'destination_id':destination.id,'tare_kg':tare,'gross_kg':gross,'net_kg':gross-tare if complete else 0,
            'original_kg':gross-tare if complete else 0,'state':'quality' if complete else first_kind,'workflow_version':3 if first_kind=='gross' else 2,
            'capture_hashes':dict(doc.capture_hashes or {},**{phase:digest}) if doc else {phase:digest},'error':False}
        if doc: doc.with_context(_ms_internal=INTERNAL)._set(values)
        else: doc=self.with_context(_ms_internal=INTERNAL).create(values)
        if complete:
            # Completion owns this waybill even while quality is still outstanding.
            wb.with_context(_ms_internal=INTERNAL).write({'state':'used','reservation_token':False,'reservation_expires_at':False})
            if not wb.no_origin_weighbridge:
                diff=doc.net_kg-wb.origin_net_weight_kg
                if diff < -wb.policy_id.tolerance_kg or (wb.policy_id.detect_surplus and diff > wb.policy_id.tolerance_kg):
                    discrepancy=self.env['megasanj.discrepancy'].with_context(_ms_internal=INTERNAL).create({'document_id':doc.id,'origin_kg':wb.origin_net_weight_kg,'destination_kg':doc.net_kg,'tolerance_kg':wb.policy_id.tolerance_kg})
                    doc._set({'discrepancy_id':discrepancy.id})
            if wb.policy_id.quality_enabled:
                points=self.env['quality.point'].with_context(_ms_after_weighing=INTERNAL)
                domain=points._get_domain(wb.purchase_line_id.product_id,wb.picking_id.picking_type_id,measure_on='operation')
                point=points.search(domain & Domain('test_type','=','megasanj_percentage'),limit=1)
                self.env['quality.check'].create({'test_type_id':self.env.ref('megasanj.test_type_percentage').id,
                    'picking_id':wb.picking_id.id,'company_id':self.env.company.id,'ms_category_id':wb.category_id.id,
                    'ms_weighbridge_id':doc.id,'product_id':wb.purchase_line_id.product_id.id,'point_id':point.id})
            else:
                doc._complete_after_quality()
        return doc.contract()

    def _complete_after_quality(self, quality=None):
        self.ensure_one()
        if self.state != 'quality': raise UserError('تکمیل دو توزین پیش از کنترل کیفیت لازم است.')
        data=copy.deepcopy(self.payload)
        data['document']['finalized_at']=fields.Datetime.now().isoformat()+'Z'
        if quality:
            if quality.ms_weighbridge_id != self or quality.ms_state != 'available': raise UserError('کنترل کیفیت این سند آماده نیست.')
            data['quality_document_code']=quality.ms_code
        else:
            if self.policy_id.quality_required: raise UserError('کنترل کیفیت این محموله اجباری است.')
            product=self.waybill_id.purchase_line_id.product_id
            if not product.ms_weighable: raise UserError('برای ثبت بدون QC، قلم خرید باید قلم مجاز ترکیب باشد.')
            data['composition']=[{'material_item_id':product.id,'percentage':100}]
            data.pop('quality_document_code',None)
            requests=self.quality_request_ids.filtered(lambda q:q.ms_state=='draft')
            if requests:
                self.message_post(body='طبق سیاست اختیاری محموله، ثبت بدون QC انتخاب شد؛ درخواست‌های ارزیابی انجام‌نشده لغو شدند.')
                requests.unlink()
        return self.with_context(_ms_complete_staged=INTERNAL).ingest(data)

    def action_skip_quality(self):
        if not self.env.user.has_group('megasanj.group_operator'): raise UserError('دسترسی باسکول لازم است.')
        for doc in self: doc._complete_after_quality()

    def action_post(self):
        if self.filtered(lambda d:d.state in ('draft','gross','tare','quality')):
            raise UserError('تا تکمیل وزن پر و کنترل کیفیت مجاز، ثبت موجودی انجام نمی‌شود.')
        return super().action_post()
