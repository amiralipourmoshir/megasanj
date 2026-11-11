"""Native QWeb report data and the shared minutes statement; no custom renderer."""
import html
import textwrap,re
from html.parser import HTMLParser
from decimal import Decimal,ROUND_HALF_UP
from datetime import datetime,timezone
from markupsafe import Markup
from odoo import fields,models


def fa(value):
    return str(value if value not in (None,False,'') else '—').translate(str.maketrans('0123456789','۰۱۲۳۴۵۶۷۸۹'))


def number(value):
    if value is None or value is False:return '—'
    amount=Decimal(str(value)).quantize(Decimal('0.001'),rounding=ROUND_HALF_UP)
    text=format(amount,',.3f').rstrip('0').rstrip('.')
    return fa(text).replace(',','٬').replace('.','٫')


def lines(value,width):
    return textwrap.wrap(str(value or '—'),width=width,break_long_words=True,break_on_hyphens=False) or ['—']


def date_time(record,value):
    if not value:return '—'
    value=datetime.fromisoformat(value.replace('Z','+00:00')) if isinstance(value,str) else value
    if value.tzinfo:value=value.astimezone(timezone.utc).replace(tzinfo=None)
    local=fields.Datetime.context_timestamp(record.with_context(tz='Asia/Tehran'),value)
    return fa(local.strftime('%Y/%m/%d %H:%M:%S'))


class Document(models.Model):
    _inherit='megasanj.document'

    def _ms_report_data(self):
        self.ensure_one();wb=self.waybill_id;p=self.payload or {}
        driver=p.get('driver') or {};vehicle=p.get('vehicle') or {};origin=p.get('origin') or {}
        supplier=wb.partner_id
        weighings=[]
        for w in p.get('weighings',[]):
            weighings.append({'kind':{'gross':'وزن پر','tare':'وزن خالی'}.get(w.get('kind') or w.get('role'),'توزین'),
                'weight':number(w.get('weight_kg')),'source':'ثبت دستی' if w.get('source')=='manual' else 'دریافت از باسکول',
                'at':date_time(self,w.get('captured_at')),'reason':w.get('manual_reason') or '—',
                'reason_lines':lines(w.get('manual_reason'),26)})
        return {'driver':wb.driver_name or driver.get('name') or '—','phone':fa(wb.driver_phone or driver.get('phone')),
            'plate':fa(wb.trailer_plate or vehicle.get('trailer_plate')),'origin':'، '.join(x for x in [wb.origin_province or origin.get('province'),wb.origin_city or origin.get('city')] if x) or '—',
            'supplier':supplier.display_name or 'داخلی','supplier_vat':fa(supplier.vat),'supplier_phone':fa(supplier.phone),
            'waybill':wb.name or '—','purchase':wb.parent_purchase_id.name or '—','material':self.category_id.display_name,
            'movement':dict(self._fields['movement_type'].selection).get(self.movement_type,self.movement_type),
            'created':date_time(self,p.get('document',{}).get('registered_at') or self.create_date),
            'entry':date_time(self,wb.entry_at),'exit':date_time(self,wb.exit_at),
            'gross':number(self.gross_kg),'tare':number(self.tare_kg),'net':number(self.net_kg),'accepted':number(self.accepted_kg),
            'origin_weight':('فاقد باسکول مبدأ' if wb.no_origin_weighbridge else number(wb.origin_net_weight_kg)) if wb else '—',
            'comparison':number(self.original_kg),'loss':number(self.loss_kg),'rejected':number(self.rejected_kg),
            'state':dict(self._fields['state']._description_selection(self.env)).get(self.state,self.state),
            'weighings':weighings,'notes':p.get('document',{}).get('notes') or '—','note_lines':lines(p.get('document',{}).get('notes'),90),
            'lines':[{'name':line.product_id.name,'percent':number(line.percentage),'weight':number(line.weight_kg)} for line in self.line_ids]}


class Discrepancy(models.Model):
    _inherit='megasanj.discrepancy'

    def _ms_statement_lines(self):
        self.ensure_one()
        class Text(HTMLParser):
            def __init__(self):super().__init__(convert_charrefs=True);self.parts=[]
            def handle_data(self,value):self.parts.append(value)
        parser=Text();parser.feed(str(self._ms_statement_html()))
        result=[]
        for line in lines(''.join(parser.parts),60):
            tokens=re.split(r'([۰-۹0-9٬٫]+)',line)
            result.append(Markup(''.join('<span class="ms-number" dir="ltr">'+html.escape(t)+'</span>' if re.fullmatch(r'[۰-۹0-9٬٫]+',t or '') else html.escape(t) for t in tokens)))
        return result

    def _ms_statement_html(self):
        self.ensure_one();wb=self.document_id.waybill_id
        def value(v):return html.escape(str(v)) if v not in (False,None,'') else '......................'
        def weight(v):return number(v)
        penalty=weight(self.penalty_weight_kg) if self.state=='closed' or self.penalty_weight_kg or self.deduction_reason else '........................'
        kind='کسری' if self.destination_kg<=self.origin_kg else 'اضافه‌وزن'
        return Markup('بدین‌وسیله صورت‌جلسه می‌گردد آقای <b>%s</b> به شماره ملی <b>%s</b> و شماره همراه <b>%s</b>، راننده وسیله حمل با پلاک <b>%s</b>، '
            'مطابق بارنامه فوق متعهد به تحویل <b>%s کیلوگرم</b> بار بوده است؛ لیکن وزن ثبت‌شده در مقصد <b>%s کیلوگرم</b> و %s محرز برابر <b>%s کیلوگرم</b> است. '
            'بر اساس ضوابط داخلی شرکت %s و مسئولیت راننده در حفاظت از محموله طی مسیر، پس از بررسی کارشناسی، میزان جبران مقرر برای نامبرده معادل <b>%s کیلوگرم آهن</b> تعیین می‌گردد. '
            'تعیین معادل ریالی و نحوه اعمال آن بر مطالبات یا کرایه صرفاً توسط واحد مالی و مطابق مقررات و اسناد قراردادی انجام می‌شود. '
            'علت و مستند تصمیم کارشناس: <b>%s</b>. '
            'راننده با امضای این صورت‌جلسه، صحت مشخصات و اوزان مندرج را تأیید نموده و در حدود قوانین و مقررات لازم‌الاجرا، حق هرگونه اعتراض نسبت به محاسبات ثبت‌شده و اجرای مفاد این صورت‌جلسه را از خود سلب می‌نماید.'
            %(value(wb.driver_name),fa(value(self.driver_national_id)),fa(value(wb.driver_phone)),fa(value(wb.trailer_plate)),
              weight(self.origin_kg),weight(self.destination_kg),kind,weight(abs(self.destination_kg-self.origin_kg)),value(self.company_id.name),penalty,value((self.deduction_reason or '').strip())))


    def _ms_report_data(self):
        self.ensure_one()
        data=self.document_id._ms_report_data()
        data.update(number=self.name,created=date_time(self,self.create_date),reviewed=date_time(self,self.reviewed_at),
            reviewer=self.reviewed_by.display_name or '—',national=fa(self.driver_national_id) if self.driver_national_id else '......................',
            origin_weight=number(self.origin_kg),destination_weight=number(self.destination_kg),
            shortage=number(max(0,self.origin_kg-self.destination_kg)),surplus=number(max(0,self.destination_kg-self.origin_kg)),
            tolerance=number(self.tolerance_kg),penalty=number(self.penalty_weight_kg) if self.state=='closed' or self.penalty_weight_kg or self.deduction_reason else '........................',
            reason=self.deduction_reason or '........................................',
            registrar=(self.reviewed_by or self.document_id.create_uid).display_name)
        return data
