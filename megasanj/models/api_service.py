from datetime import timedelta
import hashlib
import json
import secrets
from odoo import api, fields, models, Command
from odoo.exceptions import AccessDenied, UserError, ValidationError
from .common import INTERNAL


class Token(models.Model):
    _name = 'megasanj.token'
    _description = 'نشست قابل ابطال مگاسنج'
    user_id = fields.Many2one('res.users', required=True, ondelete='cascade')
    company_id = fields.Many2one('res.company', required=True)
    digest = fields.Char(required=True, index=True)
    expires_at = fields.Datetime(required=True)
    active = fields.Boolean(default=True)
    _digest_unique = models.Constraint('unique(digest)', 'نشست تکراری است.')


class ApiService(models.AbstractModel):
    _name = 'megasanj.api'
    _description = 'قرارداد سازگار مگاسنج برای اودو'

    @api.model
    def _user_contract(self):
        user = self.env.user
        caps = []
        if user.has_group('megasanj.group_operator'): caps += ['weighbridge','processing']
        if user.has_group('megasanj.group_gate'): caps += ['waybills']
        if caps: caps += ['queue']
        if caps: caps += ['settings']
        return {'id':user.id,'name':user.name,'email':user.login,'mobile':user.partner_id.phone or '',
                'avatar':None,'roles':['super_admin'] if user.has_group('megasanj.group_manager') else ['weighbridge_operator'],
                'role_labels':['مگاسنج اودو'],'permissions':[], 'weighbridge_capabilities':caps,
                'edition':'odoo19','capture_workflow':'gross_tare_quality','company_id':self.env.company.id,'currency':'IRR','company_currency':self.env.company.currency_id.name,
                'test_environment':self.env.cr.dbname.startswith('megasanj_test_')}

    @api.model
    def _login(self, data):
        credential = {'type':'password','login':str(data.get('email') or ''),'password':str(data.get('password') or '')}
        info = self.env['res.users'].authenticate(credential, {'interactive':True})
        user = self.env['res.users'].sudo().browse(info['uid'])
        if not user.active or user.share:
            raise AccessDenied()
        service = self.with_user(user).with_company(user.company_id)
        profile = service._user_contract()
        if not profile['weighbridge_capabilities']:
            raise AccessDenied()
        token = secrets.token_urlsafe(48)
        expires = fields.Datetime.now() + timedelta(days=30) if data.get('remember') else fields.Datetime.now() + timedelta(hours=12)
        self.env['megasanj.token'].sudo().create({'user_id':user.id,'company_id':user.company_id.id,
            'digest':hashlib.sha256(token.encode()).hexdigest(),'expires_at':expires})
        return {'success':True,'data':{'user':profile,'token':token,'token_type':'Bearer','expires_at':expires.isoformat()+'Z'}}

    @api.model
    def _company(self, partner, internal=False):
        return {'id':partner.id,'name':partner.name,'code':'PARTNER-%s'%partner.id,'legal_name':partner.name,
                'national_id':partner.vat or '', 'registration_number':partner.company_registry or '',
                'phone':partner.phone or '', 'province':partner.state_id.name or '', 'city':partner.city or '',
                'address':partner.contact_address or '', 'is_active':bool(partner.active),'is_internal_factory':internal,'deleted_at':None}

    @api.model
    def _warehouse(self, loc):
        return {'id':loc.id,'code':'LOC-%s'%loc.id,'name':loc.complete_name,'factory_id':loc.company_id.partner_id.id,
            'material_type_id':loc.ms_category_id.id,'is_active':bool(loc.active),'location':loc.complete_name,'notes':None,
            'factory':self._company(loc.company_id.partner_id,True)}

    @api.model
    def _references(self, known=''):
        categories = self.env['product.category'].search([('ms_enabled','=',True)])
        categories = categories.filtered('ms_policy_id')
        products = self.env['product.product'].with_context(active_test=False).search([('categ_id','in',categories.ids),('ms_weighable','=',True)])
        links = self.env['megasanj.eligibility'].with_context(active_test=False).search([('company_id','=',self.env.company.id)])
        locations = self.env['stock.location'].with_context(active_test=False).search([('company_id','=',self.env.company.id),('ms_category_id','in',categories.ids),('usage','=','internal')])
        companies = links.partner_id | self.env.company.partner_id
        kg = self.env.ref('uom.product_uom_kgm')
        balances = []
        for loc in locations.filtered('active'):
            for product in products.filtered(lambda p:p.categ_id == loc.ms_category_id):
                quantity = self.env['stock.quant']._get_available_quantity(product,loc,allow_negative=True)
                balances.append({'warehouse_id':loc.id,'material_item_id':product.id,
                                 'quantity_g':round(product.uom_id._compute_quantity(quantity,kg)*1000)})
        data = {'material_types':[{'id':c.id,'code':c.ms_code,'name':c.name,'is_active':True,'policy':c.ms_policy_id.contract()} for c in categories],
            'material_items':[{'id':p.id,'material_type_id':p.categ_id.id,'code':'PRODUCT-%s'%p.id,'name':p.name,
                'is_active':p.active,'is_warehouse_storable':p.is_storable,'scrap_type_id':p.id} for p in products],
            'supplier_material_types':[{'id':l.id,'supplier_company_id':l.partner_id.id,'material_type_id':l.category_id.id,'is_active':l.active} for l in links],
            'companies':[self._company(p,p == self.env.company.partner_id) for p in companies],
            'warehouses':[self._warehouse(l) for l in locations],
            'warehouse_receiving_defaults':[{'factory_id':l.company_id.partner_id.id,'material_type_id':l.ms_category_id.id,'warehouse_id':l.id} for l in locations.filtered(lambda l:l.active and l.ms_receiving_default)],
            'warehouse_balances':balances}
        version = hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()
        return {'success':True,'schema_version':3,'party_model':'company','supported_versions':[3],
                'snapshot_version':version,'not_modified':version == known,'data':None if version == known else data}

    @api.model
    def _waybill_contract(self, w):
        return {'id':w.id,'number':w.number,'display_number':w.name,'event_id':w.event_id,'material_type_id':w.category_id.id,
            'material_type':{'id':w.category_id.id,'code':w.category_id.ms_code,'name':w.category_id.name,'is_active':True},
            'material_policy':w.policy_id.contract(),'transport_operation_id':None,'issuance_snapshot':{'company':self._company(w.partner_id)},
            'queue_number':w.queue_number,'queue_day':str(w.queue_day),'status':w.state,
            'origin_weight_status':'not_applicable' if w.no_origin_weighbridge else 'known','origin_net_weight_kg':w.origin_net_weight_kg,
            'driver':{'name':w.driver_name,'phone':w.driver_phone},'driver_name':w.driver_name,'driver_phone':w.driver_phone,
            'trailer_plate':w.trailer_plate,'origin':{'province':w.origin_province,'city':w.origin_city},
            'origin_province':w.origin_province,'origin_city':w.origin_city,'supplier_company_id':w.partner_id.id,'company':self._company(w.partner_id),
            'freight_amount':w.api_freight_irr,'freight_by_contractor':w.freight_by_contractor,'no_origin_weighbridge':w.no_origin_weighbridge,
            'reservation_token':w.reservation_token or None,'reservation_expires_at':w.reservation_expires_at.isoformat()+'Z' if w.reservation_expires_at else None,
            'issued_at':w.create_date.isoformat()+'Z','reserved_at':None,'used_at':None,'deleted_at':w.write_date.isoformat()+'Z' if w.state == 'cancelled' else None,
            'called_at':w.called_at.isoformat()+'Z' if w.called_at else None,
            'entry_at':w.entry_at.isoformat()+'Z' if w.entry_at else None,
            'exit_at':w.exit_at.isoformat()+'Z' if w.exit_at else None,
            'picking_id':w.picking_id.id,'purchase_line_id':w.purchase_line_id.id,'gate_state':w.gate_state,
            'purchase_id':w.parent_purchase_id.id,'purchase_name':w.parent_purchase_id.name,'driver_partner_id':w.driver_partner_id.id,'returned':w.ms_returned}

    @api.model
    def _issue(self, data):
        if not self.env.user.has_group('megasanj.group_gate'):
            raise UserError('دسترسی انتظامات برای صدور بارنامه لازم است.')
        import re
        event = data.get('event_id','')
        if not isinstance(event,str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,120}',event):
            raise ValidationError('شناسه رویداد معتبر لازم است.')
        digest = hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
        self.env.cr.execute('SELECT pg_advisory_xact_lock(%s,%s)',(74182,self.env.company.id))
        existing = self.env['megasanj.waybill'].search([('event_id','=',event),('company_id','=',self.env.company.id)])
        if existing:
            if existing.payload_hash != digest: raise UserError('رویداد بارنامه با محتوای متفاوت تکرار شده است.')
            return {'success':True,'duplicate':True,'data':self._waybill_contract(existing)}
        allowed = ['driver_name','driver_phone','trailer_plate','origin_province','origin_city','origin_net_weight_kg',
                   'no_origin_weighbridge','freight_by_contractor','freight_amount','picking_id','purchase_line_id','driver_partner_id']
        vals = {k:data[k] for k in allowed if k in data}
        line=self.env['purchase.order.line'].browse(int(data.get('purchase_line_id') or 0)).exists()
        if not line or line.company_id != self.env.company:
            raise ValidationError('ردیف خرید معتبر همان شرکت لازم است.')
        contractor=bool(line.order_id.ms_freight_by_contractor)
        vals['freight_by_contractor']=contractor
        amount = 0 if contractor else (data.get('freight_amount',0) or 0)
        if isinstance(amount,bool) or not isinstance(amount,int) or not 0 <= amount <= 2000000000:
            raise ValidationError('کرایه قرارداد اپ باید عدد صحیح نامنفی ریال باشد.')
        rial = self.env.ref('base.IRR')
        if amount and not contractor:
            if self.env.company.currency_id != rial and not rial.rate_ids:
                raise ValidationError('نرخ تبدیل ریال به ارز شرکت را در اودو تعیین کنید.')
            vals['freight_amount'] = rial._convert(amount,self.env.company.currency_id,self.env.company,fields.Date.today())
        vals['api_freight_irr'] = amount
        vals.update(event_id=event,payload_hash=digest)
        w = self.env['megasanj.waybill'].create(vals)
        if data.get('supplier_company_id') and int(data['supplier_company_id']) != w.partner_id.id:
            raise ValidationError('تأمین‌کننده از رسید خرید تعیین می‌شود.')
        if data.get('material_type_id') and int(data['material_type_id']) != w.category_id.id:
            raise ValidationError('نوع محموله از ردیف خرید تعیین می‌شود.')
        return {'success':True,'duplicate':False,'data':self._waybill_contract(w)}

    @api.model
    def _quality_contract(self, q):
        return {'document_code':q.ms_code,'material_kind':q.ms_category_id.ms_code,'material_type_id':q.ms_category_id.id,
            'expert_name':q.user_id.name or q.create_uid.name,'loss_percentage':q.ms_loss,'rejected_percentage':q.ms_rejected,
            'notes':q.additional_note or '', 'picking_id':q.picking_id.id,
            'items':[{'material_reference_id':str(l.product_id.id),'material_name':l.product_id.name,'percentage':l.percentage} for l in q.ms_line_ids]}

    @api.model
    def _receipts(self):
        pickings = self.env['stock.picking'].search([('company_id','=',self.env.company.id),('picking_type_code','=','incoming'),('state','not in',['done','cancel']),('purchase_id','!=',False),('purchase_id.ms_returned','=',False)])
        result = []
        for p in pickings:
            lines = p.move_ids.filtered(lambda m:m.state not in ('done','cancel') and m.product_id.categ_id.ms_enabled).purchase_line_id
            result.append({'id':p.id,'name':p.name,'purchase_id':p.purchase_id.id,'purchase_name':p.purchase_id.name,
                'supplier_company_id':p.partner_id.id,'company':self._company(p.partner_id),'freight_by_contractor':bool(p.purchase_id.ms_freight_by_contractor),
                'lines':[{'id':l.id,'name':l.product_id.display_name,'material_type_id':l.product_id.categ_id.id,
                          'ordered_qty':l.product_qty,'received_qty':l.qty_received,'unit':l.product_uom_id.name} for l in lines]})
        return {'success':True,'data':result}

    @api.model
    def _productions(self):
        mos = self.env['mrp.production'].search([('company_id','=',self.env.company.id),('state','in',['confirmed','progress','to_close'])])
        return {'success':True,'data':[{'id':m.id,'name':m.name,'product':m.product_id.display_name,'bom_id':m.bom_id.id,
            'source_warehouse_id':m.location_src_id.id,'destination_warehouse_id':m.location_dest_id.id,
            'state':m.state,'components':[{'move_id':v.id,'product_id':v.product_id.id,'material_type_id':v.product_id.categ_id.id,
                'name':v.product_id.display_name,'planned_qty':v.product_uom_qty,'unit':v.product_uom.name,'weighed_kg':v.ms_weighed_kg} for v in m.move_raw_ids.filtered(lambda v:v.state not in ('done','cancel'))]} for m in mos]}

    @api.model
    def _queue(self):
        ws = self.env['megasanj.waybill'].search([('company_id','=',self.env.company.id),('gate_state','!=','exited'),('state','!=','cancelled')],order='queue_day,queue_number')
        now = fields.Datetime.now()
        return {'success':True,'data':{'workflow_version':2,'day':str(fields.Date.context_today(self.with_context(tz='Asia/Tehran'))),
            'server_time':now.isoformat()+'Z','estimate_sample_count':0,
            'entries':[{'id':w.id,'waybill_id':w.id,'day':str(w.queue_day),'number':w.queue_number,'state':w.gate_state,
                'driver_name':w.driver_name,'trailer_plate':w.trailer_plate,'waybill_number':w.number,
                'issued_at':w.create_date.isoformat()+'Z',
                'called_at':w.called_at.isoformat()+'Z' if w.called_at else None,
                'entered_at':w.entry_at.isoformat()+'Z' if w.entry_at else None,
                'exited_at':w.exit_at.isoformat()+'Z' if w.exit_at else None,'wait_estimate_seconds':None,
                'inside_elapsed_seconds':max(0,int(((w.exit_at or now)-w.entry_at).total_seconds())) if w.entry_at else None} for w in ws]}}

    @api.model
    def _sync(self, known='', cursor=''):
        import base64
        offset = 0
        if cursor:
            try:
                decoded = json.loads(base64.urlsafe_b64decode(cursor + '='*(-len(cursor)%4)))
                offset = int(decoded['offset'])
                if offset < 0: raise ValueError()
            except Exception: raise ValidationError('cursor نامعتبر است.') from None
        ws = self.env['megasanj.waybill'].search([('company_id','=',self.env.company.id)],order='id')
        docs = self.env['megasanj.document'].search([('company_id','=',self.env.company.id)],order='id') if self.env.user.has_group('megasanj.group_operator') else self.env['megasanj.document']
        # A version is tied to every row, not just max(write_date).
        version = hashlib.sha256(str([(r._name,r.id,str(r.write_date)) for r in ws | self.env['megasanj.waybill']]).encode()+str([(r.id,str(r.write_date)) for r in docs]).encode()).hexdigest()
        if cursor and decoded['version'] != version:
            raise UserError('snapshot تغییر کرده است؛ همگام‌سازی را از ابتدا تکرار کنید.')
        sections = ['waybills'] + (['documents'] if self.env.user.has_group('megasanj.group_operator') else [])
        data = {'waybills':[], 'documents':[]}
        if known != version:
            data['waybills'] = [self._waybill_contract(w) for w in ws[offset:offset+200]]
            for d in docs[offset:offset+200]:
                p = d.payload or {}
                quality = {'accepted_weight_kg':d.accepted_kg,'original_load_weight_kg':d.original_kg,'loss_weight_kg':d.loss_kg,
                           'rejected_weight_kg':d.rejected_kg,'loss_percentage':d.quality_id.ms_loss,'rejected_percentage':d.quality_id.ms_rejected,
                           'expert_name':d.quality_id.user_id.name or ''}
                item = dict(p.get('document') or {}, event_id=d.event_id,document_number=d.name,movement_type=d.movement_type,status='draft' if d.state=='draft' else 'first_weighed' if d.state in ('gross','tare') else 'finalized',workflow_version=d.workflow_version,capture_payload=p if d.workflow_version>=2 else None,
                    odoo_id=d.id,odoo_state=d.state,effect=d.effect,inventory_pending=d.state not in ('posted','void'),action_required=d.error or None,
                    material_type_id=d.category_id.id,material_policy=d.policy_snapshot,production_id=d.production_id.id or None,
                    company=self._company(d.waybill_id.partner_id) if d.waybill_id else None,supplier_company_id=d.waybill_id.partner_id.id or None,
                    driver=p.get('driver'),vehicle=p.get('vehicle'),origin=p.get('origin'),destination='',quality=quality,
                    waybill_snapshot=self._waybill_contract(d.waybill_id) if d.waybill_id else {},entry_at=d.waybill_id.entry_at.isoformat()+'Z' if d.waybill_id.entry_at else None,exit_at=d.waybill_id.exit_at.isoformat()+'Z' if d.waybill_id.exit_at else None,
                    waybill_number=d.waybill_id.number or None,waybill_id=d.waybill_id.number or None,
                    source_warehouse_id=d.source_id.id or None,destination_warehouse_id=d.destination_id.id or None,
                    warehouse_snapshot=[self._warehouse(loc) for loc in d.source_id|d.destination_id if loc.ms_category_id],
                    deleted_at=d.write_date.isoformat()+'Z' if d.state == 'void' else None,
                    weighings=[dict(w,role=w['kind'],stable=True) for w in p.get('weighings',[])],
                    material_composition=[{'material_type_id':d.category_id.id,'material_item_id':l.product_id.id,'material_name':l.product_id.name,
                        'percentage':l.percentage,'weight_kg':l.weight_kg} for l in d.line_ids],
                    composition=[{'scrap_type_id':str(l.product_id.id),'scrap_name':l.product_id.name,'percentage':l.percentage,'weight_kg':l.weight_kg,'notes':''} for l in d.line_ids],
                    weight_discrepancy=d.discrepancy_id.contract() if d.discrepancy_id else None)
                data['documents'].append(item)
        more = known != version and offset+200 < max(len(ws),len(docs))
        next_cursor = base64.urlsafe_b64encode(json.dumps({'offset':offset+200,'version':version}).encode()).decode().rstrip('=') if more else None
        return {'success':True,'data':data,'sync':{'server_time':fields.Datetime.now().isoformat()+'Z', 'cursor':next_cursor,
            'snapshot_version':version,'sections':sections,'not_modified':known == version,'has_more':more,'poll_after_seconds':60}}


class Processing(models.Model):
    _name = 'megasanj.processing'
    _description = 'فرآوری باسکول با سفارش ساخت اودو'
    name = fields.Char('شماره', readonly=True)
    company_id = fields.Many2one('res.company', default=lambda s:s.env.company,required=True)
    event_id = fields.Char(required=True,copy=False,index=True)
    payload_hash = fields.Char(required=True)
    payload = fields.Json()
    response = fields.Json()
    production_id = fields.Many2one('mrp.production',ondelete='restrict',check_company=True)
    _event_unique = models.Constraint('unique(company_id,event_id)', 'رویداد فرآوری تکراری است.')

    @api.model
    def _ingest(self,data):
        from ..domain import composition, number
        if not self.env.user.has_group('megasanj.group_operator'): raise UserError('دسترسی باسکول لازم است.')
        import re
        event = data.get('event_id','')
        if not isinstance(event,str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,120}',event) or data.get('schema_version') != 1:
            raise ValidationError('شناسه یا نسخه فرآوری معتبر نیست.')
        digest = hashlib.sha256(json.dumps(data,sort_keys=True,allow_nan=False).encode()).hexdigest()
        self.env.cr.execute('SELECT pg_advisory_xact_lock(%s,%s)',(74183,self.env.company.id))
        existing = self.search([('company_id','=',self.env.company.id),('event_id','=',event)])
        if existing:
            if existing.payload_hash != digest: raise UserError('رویداد فرآوری با محتوای متفاوت تکرار شده است.')
            return dict(existing.response,duplicate=True)
        inputs = data.get('inputs') or []
        try: composition([{'material_item_id':r['scrap_type_id'],'percentage':r['percentage']} for r in inputs])
        except (KeyError,ValueError) as exc: raise ValidationError(str(exc)) from exc
        output = self.env['product.product'].browse(int((data.get('output') or {}).get('scrap_type_id') or 0)).exists()
        quantity = float(number((data.get('output') or {}).get('weight_kg'),minimum=__import__('decimal').Decimal('.001')))
        if output.id in [int(r['scrap_type_id']) for r in inputs]: raise ValidationError('خروجی فرآوری نمی‌تواند جزو ورودی‌ها باشد.')
        source = self.env['stock.location'].browse(int(data.get('source_warehouse_id') or 0)).exists()
        dest = self.env['stock.location'].browse(int(data.get('destination_warehouse_id') or 0)).exists()
        if not source or not dest or source.company_id != self.env.company or dest.company_id != self.env.company:
            raise ValidationError('دو محل داخلی همان شرکت لازم است.')
        if source.usage != 'internal' or dest.usage != 'internal' or source.ms_category_id != output.categ_id or dest.ms_category_id != output.categ_id:
            raise ValidationError('محل‌های فرآوری با نوع خروجی برابر نیستند.')
        products = self.env['product.product'].browse([int(r['scrap_type_id']) for r in inputs]).exists()
        if len(products) != len(inputs) or any(not p.ms_weighable or not p.is_storable or p.categ_id != output.categ_id for p in products | output):
            raise ValidationError('اقلام ورودی و خروجی باید از یک نوع و قابل انبارش باشند.')
        capture = data.get('weighing') or {}
        if capture.get('source') not in ('device','manual') or (capture.get('source') == 'manual' and not capture.get('manual_reason')):
            raise ValidationError('روش توزین و دلیل ورود دستی لازم است.')
        bom = self.env['mrp.bom']._bom_find(output,company_id=self.env.company.id,bom_type='normal')[output]
        if not bom or set(bom.bom_line_ids.product_id.ids) != set(products.ids):
            raise UserError('برای خروجی فرآوری، BOM با همین اقلام ورودی در اودو تعریف کنید.')
        kg = self.env.ref('uom.product_uom_kgm')
        mo = self.env['mrp.production'].create({'product_id':output.id,'bom_id':bom.id,'product_qty':kg._compute_quantity(quantity,output.uom_id),
            'product_uom_id':output.uom_id.id,'location_src_id':source.id,'location_dest_id':dest.id,'company_id':self.env.company.id})
        mo.action_confirm()
        mo.qty_producing = mo.product_qty
        for r in inputs:
            move = mo.move_raw_ids.filtered(lambda m:m.product_id.id == int(r['scrap_type_id']))
            if len(move) != 1: raise UserError('ردیف ورودی فرآوری باید یکتا باشد.')
            move.quantity = kg._compute_quantity(quantity*float(r['percentage'])/100,move.product_uom)
            move.with_context(_ms_internal=INTERNAL).write({'ms_weighed_kg':quantity*float(r['percentage'])/100})
            move.manual_consumption = True
            move.picked = True
        record = self.create({'name':mo.name,'event_id':event,'payload_hash':digest,'payload':data,'production_id':mo.id})
        # Keep normal quality/lot/consumption approvals in Odoo. No bypass contexts.
        response = {'success':True,'duplicate':False,'data':{'id':record.id,'event_id':event,'document_number':mo.name,
                    'production_id':mo.id,'inventory_pending':True,'action_required':'کنترل کیفیت و نهایی‌سازی سفارش فرآوری در اودو'}}
        record.response = response
        return response
