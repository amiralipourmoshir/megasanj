import hashlib
import json
import logging
from functools import wraps
from odoo import http, fields
from odoo.http import request
from odoo.exceptions import AccessDenied, AccessError, UserError, ValidationError
from ..models.common import INTERNAL

_logger = logging.getLogger(__name__)


def api_call(capability=None, public=False):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            try:
                if not public:
                    raw = request.httprequest.headers.get('Authorization','')
                    if not raw.startswith('Bearer '): raise AccessDenied()
                    token = request.env['megasanj.token'].sudo().search([('digest','=',hashlib.sha256(raw[7:].encode()).hexdigest()),('active','=',True)],limit=1)
                    if not token or token.expires_at <= fields.Datetime.now() or not token.user_id.active or token.company_id not in token.user_id.company_ids:
                        raise AccessDenied()
                    request.update_env(user=token.user_id.id,context={'allowed_company_ids':[token.company_id.id],'tz':'Asia/Tehran','lang':token.user_id.lang or 'en_US'},su=False)
                    profile = request.env['megasanj.api']._user_contract()
                    if not profile['weighbridge_capabilities'] or (capability and capability not in profile['weighbridge_capabilities']):
                        raise AccessError('دسترسی این عملیات وجود ندارد.')
                with request.env.cr.savepoint():
                    result = fn(*args, **kwargs)
                if isinstance(result,tuple): result,status = result
                else: status = 200
                return request.make_json_response(result,status=status)
            except AccessDenied:
                return request.make_json_response({'success':False,'message':'نشست یا اطلاعات ورود معتبر نیست.'},status=401)
            except AccessError:
                return request.make_json_response({'success':False,'message':'دسترسی این عملیات وجود ندارد.'},status=403)
            except (ValidationError, ValueError, KeyError, TypeError) as exc:
                return request.make_json_response({'success':False,'code':'VALIDATION_FAILED','message':str(exc)},status=422)
            except UserError as exc:
                return request.make_json_response({'success':False,'code':'MATERIAL_DOCUMENT_CONFLICT','message':str(exc)},status=409)
            except Exception:
                _logger.exception('MegaSanj request failed (payload and credentials omitted)')
                return request.make_json_response({'success':False,'message':'ثبت انجام نشد؛ رویداد را با همان شناسه دوباره ارسال کنید.'},status=500)
        return wrapped
    return decorator


def body():
    value = request.httprequest.get_json(silent=True)
    if not isinstance(value,dict): raise ValueError('بدنه JSON object لازم است.')
    return value


def service(): return request.env['megasanj.api']


class MegaSanj(http.Controller):
    @http.route('/api/login',type='http',auth='none',methods=['POST'],csrf=False,save_session=False,readonly=False)
    @api_call(public=True)
    def login(self): return service()._login(body())

    @http.route('/api/weighbridge/session',type='http',auth='none',methods=['GET'],csrf=False,save_session=False,readonly=False)
    @api_call()
    def session(self): return {'success':True,'data':service()._user_contract()}

    @http.route('/api/logout',type='http',auth='none',methods=['POST'],csrf=False,save_session=False,readonly=False)
    @api_call()
    def logout(self):
        token = request.httprequest.headers['Authorization'][7:]
        request.env['megasanj.token'].sudo().search([('digest','=',hashlib.sha256(token.encode()).hexdigest())]).write({'active':False})
        return {'success':True}

    @http.route('/api/transportation/reference/materials',type='http',auth='none',methods=['GET'],csrf=False,save_session=False,readonly=False)
    @api_call()
    def references(self,known_version='',**kw): return service()._references(known_version)

    @http.route('/api/transportation/webhooks/waybills',type='http',auth='none',methods=['POST'],csrf=False,save_session=False,readonly=False)
    @api_call('waybills')
    def issue(self):
        result = service()._issue(body())
        return result,200 if result['duplicate'] else 201

    @http.route('/api/transportation/waybills/available',type='http',auth='none',methods=['GET'],csrf=False,save_session=False,readonly=False)
    @api_call()
    def available(self,search='',material_type_id='',**kw):
        if not search.isdigit(): return {'success':True,'data':[]}
        domain = [('company_id','=',request.env.company.id),('state','in',['issued','reserved'])]
        if material_type_id: domain.append(('category_id','=',int(material_type_id)))
        ws = request.env['megasanj.waybill'].search(domain,order='number').filtered(lambda w:str(w.number).startswith(search))[:12]
        return {'success':True,'data':[service()._waybill_contract(w) for w in ws]}

    def _waybill(self,number):
        w = request.env['megasanj.waybill'].search([('number','=',number),('company_id','=',request.env.company.id)],limit=1)
        if not w: raise ValidationError('بارنامه موجود نیست.')
        return w

    @http.route('/api/transportation/waybills/<int:number>',type='http',auth='none',methods=['GET'],csrf=False,save_session=False,readonly=False)
    @api_call()
    def waybill(self,number): return {'success':True,'data':service()._waybill_contract(self._waybill(number))}

    @http.route('/api/transportation/waybills/<int:number>/reserve',type='http',auth='none',methods=['POST'],csrf=False,save_session=False,readonly=False)
    @api_call('weighbridge')
    def reserve(self,number): return {'success':True,'data':service()._waybill_contract(self._waybill(number).reserve())}

    @http.route('/api/transportation/waybills/<int:number>/release',type='http',auth='none',methods=['POST'],csrf=False,save_session=False,readonly=False)
    @api_call('weighbridge')
    def release(self,number):
        w = self._waybill(number)
        request.env.cr.execute('SELECT id FROM megasanj_waybill WHERE id=%s FOR UPDATE',(w.id,))
        w.invalidate_recordset()
        if w.state == 'reserved' and w.reservation_token == body().get('reservation_token'):
            w.with_context(_ms_internal=INTERNAL).write({'state':'issued','reservation_token':False,'reservation_expires_at':False})
        else: raise UserError('رزرو متعلق به این دستگاه نیست.')
        return {'success':True}

    @http.route(['/api/transportation/v3/weighbridge','/api/transportation/webhooks/weighbridge'],type='http',auth='none',methods=['POST'],csrf=False,save_session=False,readonly=False)
    @api_call('weighbridge')
    def weighbridge(self):
        payload=body()
        model=request.env['megasanj.document']
        result = model.stage_capture(payload) if payload.get('event_type') == 'weighbridge.capture' else model.ingest(payload)
        return result,200 if result['duplicate'] else 201

    @http.route('/api/transportation/weighbridge-quality',type='http',auth='none',methods=['GET'],csrf=False,save_session=False,readonly=False)
    @api_call('weighbridge')
    def qualities(self,search='',material_kind='',picking_id='',**kw):
        domain = [('company_id','=',request.env.company.id),('test_type','=','megasanj_percentage'),('ms_state','=','available')]
        if material_kind: domain.append(('ms_category_id.ms_code','=',material_kind))
        if picking_id: domain.append(('picking_id','=',int(picking_id)))
        if search: domain.append(('ms_code','ilike',search))
        qs = request.env['quality.check'].search(domain,limit=20)
        return {'success':True,'data':[service()._quality_contract(q) for q in qs]}

    @http.route('/api/transportation/weighbridge-quality/<string:code>',type='http',auth='none',methods=['GET'],csrf=False,save_session=False,readonly=False)
    @api_call('weighbridge')
    def quality(self,code):
        code = code.translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩','01234567890123456789'))
        q = request.env['quality.check'].search([('company_id','=',request.env.company.id),('ms_code','=',code)],limit=1)
        if not q: return {'success':False,'code':'QUALITY_DOCUMENT_NOT_FOUND'},404
        if q.ms_state != 'available': return {'success':False,'code':'QUALITY_DOCUMENT_UNAVAILABLE'},409
        return {'success':True,'data':service()._quality_contract(q)}

    @http.route('/api/transportation/truck-queue',type='http',auth='none',methods=['GET'],csrf=False,save_session=False,readonly=False)
    @api_call()
    def queue(self,**kw): return service()._queue()

    @http.route('/api/transportation/truck-queue/<int:ident>',type='http',auth='none',methods=['POST'],csrf=False,save_session=False,readonly=False)
    @api_call('waybills')
    def queue_action(self,ident):
        w = request.env['megasanj.waybill'].search([('id','=',ident),('company_id','=',request.env.company.id)])
        if not w: raise ValidationError('نوبت موجود نیست.')
        w.gate_action(body().get('action'))
        return {'success':True,'data':service()._waybill_contract(w)}

    @http.route('/api/transportation/desktop-sync',type='http',auth='none',methods=['GET'],csrf=False,save_session=False,readonly=False)
    @api_call()
    def sync(self,known_version='',cursor='',**kw): return service()._sync(known_version,cursor)

    @http.route('/api/megasanj/receipts',type='http',auth='none',methods=['GET'],csrf=False,save_session=False,readonly=False)
    @api_call()
    def receipts(self,**kw): return service()._receipts()

    @http.route('/api/megasanj/productions',type='http',auth='none',methods=['GET'],csrf=False,save_session=False,readonly=False)
    @api_call('weighbridge')
    def productions(self,**kw): return service()._productions()

    @http.route('/api/transportation/weighbridge/<string:event>/weight-discrepancy',type='http',auth='none',methods=['GET'],csrf=False,save_session=False,readonly=False)
    @api_call('weighbridge')
    def discrepancy(self,event):
        d = request.env['megasanj.document'].search([('company_id','=',request.env.company.id),('event_id','=',event)],limit=1)
        if not d.discrepancy_id: return {'success':False,'message':'صورت‌جلسه موجود نیست.'},404
        return {'success':True,'data':d.discrepancy_id.contract()}

    @http.route('/api/transportation/weighbridge/<string:event>/weight-discrepancy/finalize',type='http',auth='none',methods=['POST'],csrf=False,save_session=False,readonly=False)
    @api_call('weighbridge')
    def close_discrepancy(self,event):
        d = request.env['megasanj.document'].search([('company_id','=',request.env.company.id),('event_id','=',event)],limit=1).discrepancy_id
        if not d: return {'success':False},404
        data = body()
        vals = {k:data[k] for k in ['penalty_weight_kg','deduction_reason','driver_national_id'] if k in data}
        request.env.cr.execute('SELECT id FROM megasanj_discrepancy WHERE id=%s FOR UPDATE',(d.id,))
        d.invalidate_recordset()
        if d.state == 'closed':
            if any((getattr(d,k) or '') != (v or '') for k,v in vals.items()): raise UserError('تصمیم متفاوت برای صورت‌جلسه بسته مجاز نیست.')
        else:
            d.write(vals)
            d.action_close()
        return {'success':True,'data':d.contract()}

    @http.route('/api/webhooks/weighbridge/scrap-processing',type='http',auth='none',methods=['POST'],csrf=False,save_session=False,readonly=False)
    @api_call('processing')
    def processing(self):
        result = request.env['megasanj.processing']._ingest(body())
        return result,200 if result['duplicate'] else 201
