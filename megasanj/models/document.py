import hashlib
import json
import re
from markupsafe import escape
from odoo import api, fields, models, Command
from odoo.exceptions import UserError, ValidationError
from ..domain import calculate, number
from .common import INTERNAL


MOVEMENTS = [('external_to_warehouse','خرید به انبار'), ('warehouse_to_production','مصرف سفارش تولید'),
             ('external_to_production','خرید مستقیم برای تولید'), ('warehouse_transfer','انتقال داخلی')]


class Document(models.Model):
    _name = 'megasanj.document'
    _description = 'سند باسکول مگاسنج'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'
    name = fields.Char('شماره سند', required=True, copy=False)
    event_id = fields.Char(required=True, index=True, copy=False)
    payload_hash = fields.Char(required=True, copy=False)
    payload = fields.Json(copy=False)
    response = fields.Json(copy=False)
    company_id = fields.Many2one('res.company', required=True, default=lambda s:s.env.company)
    category_id = fields.Many2one('product.category', required=True, ondelete='restrict', string='نوع محموله')
    policy_id = fields.Many2one('megasanj.policy', required=True, ondelete='restrict')
    policy_snapshot = fields.Json()
    movement_type = fields.Selection(MOVEMENTS, string='مسیر توزین', required=True)
    waybill_id = fields.Many2one('megasanj.waybill', string='بارنامه', ondelete='restrict', check_company=True)
    quality_id = fields.Many2one('quality.check', string='کنترل کیفیت', ondelete='restrict', check_company=True)
    picking_id = fields.Many2one('stock.picking', readonly=True, check_company=True, string='گردش انبار')
    production_id = fields.Many2one('mrp.production', check_company=True, string='سفارش تولید')
    source_id = fields.Many2one('stock.location', string='محل مبدأ', check_company=True)
    destination_id = fields.Many2one('stock.location', string='محل مقصد', check_company=True)
    state = fields.Selection([('ready','در انتظار عملیات اودو'), ('pending','در انتظار رفع مغایرت'), ('posted','ثبت‌شده'), ('void','باطل')], string='وضعیت', default='ready', tracking=True)
    effect = fields.Selection([('receipt','رسید انبار'),('transfer','انتقال'),('consumption_recorded','مصرف ثبت‌شده روی MO؛ تکمیل با تولید'),('direct','رسید و تخصیص مستقیم تولید')], string='اثر توزین')
    gross_kg = fields.Float('وزن پر', digits=(16,3))
    tare_kg = fields.Float('وزن خالی', digits=(16,3))
    net_kg = fields.Float('وزن خالص', digits=(16,3))
    original_kg = fields.Float('وزن مبنای مقایسه', digits=(16,3))
    accepted_kg = fields.Float('وزن پذیرفته‌شده', digits=(16,3))
    loss_kg = fields.Float('افت', digits=(16,3))
    rejected_kg = fields.Float('برگشتی', digits=(16,3))
    line_ids = fields.One2many('megasanj.document.line', 'document_id', string='اقلام پذیرفته‌شده')
    move_ids = fields.One2many('stock.move', 'ms_document_id', string='حرکت‌های انبار')
    discrepancy_id = fields.Many2one('megasanj.discrepancy', string='صورت‌جلسه مغایرت', readonly=True, ondelete='restrict')
    posted_at = fields.Datetime(readonly=True)
    error = fields.Text('اقدام موردنیاز')
    void_reason = fields.Text('دلیل ابطال')
    return_picking_id = fields.Many2one('stock.picking', readonly=True, check_company=True)
    _event_unique = models.Constraint('unique(company_id,event_id)', 'رویداد توزین تکراری است.')
    _number_unique = models.Constraint('unique(company_id,name)', 'شماره سند تکراری است.')
    _waybill_unique = models.Constraint('unique(waybill_id)', 'بارنامه قبلاً مصرف شده است.')
    _quality_unique = models.Constraint('unique(quality_id)', 'کیفیت قبلاً مصرف شده است.')

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get('_ms_internal') is not INTERNAL:
            raise UserError('سند باسکول فقط از گردش معتبر توزین ایجاد می‌شود.')
        return super().create(vals_list)

    def write(self, vals):
        if set(vals) - {'void_reason'}:
            raise UserError('سند توزین از گردش عملیات مدیریت می‌شود و قابل ویرایش مستقیم نیست.')
        return super().write(vals)

    def unlink(self):
        raise UserError('اسناد توزین حذف نمی‌شوند؛ از ابطال با دلیل استفاده کنید.')

    def _set(self, vals):
        return super(Document, self).write(vals)

    @api.model
    def ingest(self, data):
        if not self.env.user.has_group('megasanj.group_operator') and self.env.context.get('_ms_complete_staged') is not INTERNAL:
            raise UserError('دسترسی باسکول لازم است.')
        event = data.get('event_id', '')
        if not isinstance(event, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,120}', event):
            raise ValidationError('event_id معتبر لازم است.')
        if data.get('schema_version') != 3 or data.get('company'):
            raise ValidationError('سند نسخه ۳ لازم است و شرکت از رسید خرید تعیین می‌شود.')
        digest = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(',',':'), allow_nan=False).encode()).hexdigest()
        self.env.cr.execute('SELECT pg_advisory_xact_lock(%s, %s)', (74183,self.env.company.id))
        previous = self.search([('company_id','=',self.env.company.id),('event_id','=',event)])
        completing = bool(previous and previous.workflow_version >= 2 and self.env.context.get('_ms_complete_staged') is INTERNAL)
        if previous and not completing and not (previous.state=='draft' and previous.auto_entry_draft):
            if previous.payload_hash != digest:
                raise UserError('شناسه رویداد با محتوای متفاوت تکرار شده است.')
            return dict(previous.response or previous.contract(), duplicate=True, deleted=previous.state == 'void')
        header = data.get('document') or {}
        name = header.get('document_number')
        movement = header.get('movement_type')
        if not isinstance(name,str) or not 1 <= len(name) <= 120 or movement not in dict(MOVEMENTS):
            raise ValidationError('شماره سند یا مسیر توزین معتبر نیست.')
        for key in ('registered_at','finalized_at'):
            from datetime import datetime
            try: datetime.fromisoformat(str(header[key]).replace('Z','+00:00'))
            except (KeyError, ValueError): raise ValidationError('زمان ثبت و نهایی‌سازی لازم است.') from None
        if not (data.get('vehicle') or {}).get('trailer_plate'):
            raise ValidationError('پلاک خودرو لازم است.')
        weighings = data.get('weighings') or []
        if len(weighings) != 2 or {w.get('kind') for w in weighings} != {'gross','tare'} or {w.get('sequence') for w in weighings} != {1,2}:
            raise ValidationError('دو توزین پر و خالی با ترتیب مجزا لازم است.')
        for w in weighings:
            if w.get('source') not in ('manual','device') or not w.get('captured_at') or (w['source'] == 'manual' and not str(w.get('manual_reason') or '').strip()):
                raise ValidationError('روش و زمان توزین و دلیل ثبت دستی لازم است.')
        external = movement.startswith('external_')
        waybill = self.env['megasanj.waybill']
        if external:
            wb = data.get('waybill') or {}
            waybill = self.env['megasanj.waybill'].search([('number','=',int(wb.get('number') or 0)),('company_id','=',self.env.company.id)], limit=1)
            if not waybill:
                raise ValidationError('بارنامه مرتبط با رسید خرید پیدا نشد.')
            self.env.cr.execute('SELECT id FROM megasanj_waybill WHERE id=%s FOR UPDATE',(waybill.id,))
            waybill.invalidate_recordset()
            if previous and previous.waybill_id and previous.waybill_id!=waybill:
                raise UserError('بارنامه سند قابل تغییر نیست.')
            if not completing and waybill.document_ids.filtered(lambda d:d.state!='draft'):
                raise UserError('برای این بارنامه سند وجود دارد؛ همان سند را ادامه دهید.')
            consumed_by_staged = completing and previous.waybill_id == waybill and waybill.state == 'used'
            if (waybill.state not in ('issued','reserved') and not consumed_by_staged) or waybill.gate_state != 'inside':
                raise UserError('بارنامه باید آزاد و خودرو داخل مجموعه باشد.')
            if waybill.state == 'reserved' and waybill.reservation_expires_at > fields.Datetime.now() and waybill.reservation_token != wb.get('reservation_token'):
                raise UserError('بارنامه در رزرو دستگاه دیگری است.')
            if waybill.picking_id.state in ('done','cancel'):
                raise UserError('رسید خرید قابل توزین نیست.')
            category, policy = waybill.category_id, waybill.policy_id
            if data.get('material_type_id') and int(data['material_type_id']) != category.id:
                raise ValidationError('نوع محموله از رسید خرید تعیین می‌شود.')
            eligibility = self.env['megasanj.eligibility'].search([('partner_id','=',waybill.partner_id.id),('category_id','=',category.id),('company_id','=',self.env.company.id)])
            if not eligibility or not waybill.partner_id.active:
                raise UserError('تأمین‌کننده برای این محموله فعال یا مجاز نیست.')
        else:
            if data.get('waybill'):
                raise ValidationError('گردش داخلی بارنامه خرید ندارد.')
            category = self.env['product.category'].browse(int(data.get('material_type_id') or 0)).exists()
            policy = category.ms_policy_id
        if not category.ms_enabled or not policy:
            raise ValidationError('نوع محموله یا سیاست منتشرشده موجود نیست.')
        if data.get('material_policy_id') and int(data['material_policy_id']) != policy.id:
            raise UserError('نسخه سیاست با عملیات برابر نیست.')
        quality = self.env['quality.check']
        code = str(data.get('quality_document_code') or '').translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩','01234567890123456789'))
        rows = data.get('composition')
        if external and code:
            if not policy.quality_enabled:
                raise UserError('کیفیت برای این نوع محموله فعال نیست.')
            quality = self.env['quality.check'].search([('ms_code','=',code),('company_id','=',self.env.company.id)])
            if not quality:
                raise ValidationError('کد کیفیت پیدا نشد.')
            self.env.cr.execute('SELECT id FROM quality_check WHERE id=%s FOR UPDATE',(quality.id,))
            quality.invalidate_recordset()
            quality._ms_validate()
            if quality.ms_state != 'available' or quality.picking_id != waybill.picking_id or quality.ms_category_id != category:
                raise UserError('کیفیت مصرف شده یا متعلق به رسید دیگری است.')
            rows = [{'material_item_id':l.product_id.id,'percentage':l.percentage} for l in quality.ms_line_ids]
        elif external and policy.quality_required:
            raise UserError('کیفیت معتبر این رسید اجباری است.')
        elif not external and code:
            raise ValidationError('گردش داخلی کیفیت محموله خارجی ندارد.')
        weights = {w['kind']:w['weight_kg'] for w in weighings}
        try:
            result = calculate(weights['gross'], weights['tare'], rows, (previous.policy_snapshot or {}).get('calculator',policy.calculator) if completing else policy.calculator, quality.ms_loss or 0, quality.ms_rejected or 0)
        except (ValueError, TypeError, KeyError) as exc:
            raise ValidationError(str(exc)) from exc
        products = self.env['product.product'].browse([int(r['material_item_id']) for r in rows]).exists()
        if len(products) != len(rows) or any(p.categ_id != category or not p.ms_weighable or not p.active or not p.is_storable for p in products):
            raise ValidationError('قلم ترکیب نامعتبر یا متعلق به نوع دیگری است.')
        source = self.env['stock.location'].browse(int(data.get('source_warehouse_id') or 0)).exists()
        destination = self.env['stock.location'].browse(int(data.get('destination_warehouse_id') or 0)).exists()
        for loc in source | destination:
            if loc.company_id != self.env.company or not loc.active or loc.usage != 'internal' or loc.ms_category_id != category:
                raise ValidationError('محل انبار غیرفعال، نامرتبط یا متعلق به شرکت دیگری است.')
        if movement == 'external_to_warehouse' and (not destination or source):
            raise ValidationError('ورود خرید فقط محل مقصد دارد.')
        if movement == 'warehouse_transfer' and (not source or not destination or source == destination):
            raise ValidationError('دو محل داخلی متفاوت لازم است.')
        if movement == 'warehouse_to_production' and (not source or destination):
            raise ValidationError('مصرف تولید فقط محل مبدأ دارد.')
        production = self.env['mrp.production'].browse(int(data.get('production_id') or 0)).exists()
        if movement in ('warehouse_to_production','external_to_production'):
            if not production or production.company_id != self.env.company or production.state not in ('confirmed','progress','to_close'):
                raise UserError('سفارش تولید فعال همان شرکت لازم است.')
            if movement == 'external_to_production':
                if not policy.allow_direct_production or source or destination:
                    raise UserError('ورود مستقیم تولید در این سیاست مجاز نیست یا نباید انبار دستی داشته باشد.')
                # Native production source/staging location; no fabricated warehouse round trip.
                destination = production.location_src_id
            if not set(products.ids).issubset(set(production.move_raw_ids.filtered(lambda m:m.state not in ('done','cancel')).product_id.ids)):
                raise UserError('تمام اقلام مصرف باید در ردیف‌های مواد سفارش تولید وجود داشته باشند.')
        values = {'name':name,'event_id':event,'payload_hash':digest,'payload':data,
            'category_id':category.id,'policy_id':policy.id,'policy_snapshot':policy.contract(),'movement_type':movement,
            'waybill_id':waybill.id,'quality_id':quality.id,'production_id':production.id,'source_id':source.id,'destination_id':destination.id,
            'gross_kg':float(weights['gross']),'tare_kg':float(weights['tare']), 'net_kg':result['net_weight_kg'],
            'original_kg':result['original_load_weight_kg'],'accepted_kg':result['accepted_weight_kg'],
            'loss_kg':result['loss_weight_kg'],'rejected_kg':result['rejected_weight_kg'],
            'line_ids':[Command.create({'product_id':r['material_item_id'],'percentage':r['percentage'],'weight_kg':r['weight_kg'],
                                       'lot_id':r.get('lot_id')}) for r in result['items']]}
        if completing:
            values['state']='pending' if previous.discrepancy_id and previous.discrepancy_id.state=='open' and policy.inventory_requires_discrepancy_closed else 'ready'
            values['policy_snapshot']=previous.policy_snapshot
            previous.with_context(_ms_internal=INTERNAL)._set(values)
            doc=previous.with_context(_ms_internal=INTERNAL)
        else:
            entry=waybill.document_ids.filtered(lambda d:d.state=='draft' and d.auto_entry_draft)[:1] if waybill else self.browse()
            if entry:
                entry.with_context(_ms_internal=INTERNAL)._set(dict(values,state='ready',workflow_version=1))
                doc=entry
            else:doc=self.with_context(_ms_internal=INTERNAL).create(values)
        if quality:
            for line in quality.ms_line_ids:
                line.with_context(_ms_internal=INTERNAL).write({'weight_kg':doc.line_ids.filtered(lambda l:l.product_id == line.product_id).weight_kg})
            quality.with_context(_ms_internal=INTERNAL).write({'ms_document_id':doc.id,'ms_state':'attached'})
        if waybill:
            waybill.with_context(_ms_internal=INTERNAL).write({'state':'used','reservation_token':False,'reservation_expires_at':False})
            if not waybill.no_origin_weighbridge:
                difference = doc.original_kg - waybill.origin_net_weight_kg
                if not doc.discrepancy_id and (difference < -policy.tolerance_kg or (policy.detect_surplus and difference > policy.tolerance_kg)):
                    discrepancy = self.env['megasanj.discrepancy'].with_context(_ms_internal=INTERNAL).create({'document_id':doc.id,'origin_kg':waybill.origin_net_weight_kg,
                        'destination_kg':doc.original_kg,'tolerance_kg':policy.tolerance_kg})
                    doc._set({'discrepancy_id':discrepancy.id,'state':'pending' if policy.inventory_requires_discrepancy_closed else 'ready'})
        if doc.state != 'pending':
            try:
                with self.env.cr.savepoint(): doc.action_post()
            except UserError as exc:
                doc._set({'error':str(exc)})
        response = doc.contract()
        doc._set({'response':response})
        return response

    def _kg_unit(self):
        return self.env.ref('uom.product_uom_kgm')

    def _assert_stock(self, source):
        """Reject negative stock for scale effects, without changing standard Odoo policy globally."""
        kg = self._kg_unit()
        for line in self.line_ids.sorted(lambda l:l.product_id.id):
            self.env.cr.execute('SELECT pg_advisory_xact_lock(%s,%s)', (source.id,line.product_id.id))
            locations = self.env['stock.location'].search([('id','child_of',source.id)])
            self.env.cr.execute('SELECT id FROM stock_quant WHERE product_id=%s AND location_id IN %s ORDER BY id FOR UPDATE',
                                (line.product_id.id,tuple(locations.ids)))
            self.env['stock.quant'].invalidate_model(['quantity','reserved_quantity'])
            available = self.env['stock.quant']._get_available_quantity(line.product_id, source, lot_id=line.lot_id, allow_negative=False)
            available_kg = line.product_id.uom_id._compute_quantity(available, kg)
            owned = self.move_ids.filtered(lambda m:m.product_id == line.product_id and m.location_id == source and m.state not in ('done','cancel'))
            available_kg += sum(m.product_uom._compute_quantity(m.quantity,kg) for m in owned)
            # Existing reservations of this MO can be used by its own weighed consumption.
            if self.production_id:
                own = self.production_id.move_raw_ids.filtered(lambda m:m.product_id == line.product_id and m.location_id == source)
                available_kg += sum(m.product_uom._compute_quantity(m.quantity,kg) for m in own if not m.picked)
            if available_kg + .0001 < line.weight_kg:
                raise UserError('موجودی آزاد برای %s کافی نیست.' % line.product_id.display_name)

    def _record_consumption(self):
        production = self.production_id
        self.env.cr.execute('SELECT id FROM mrp_production WHERE id=%s FOR UPDATE',(production.id,))
        production.invalidate_recordset()
        if production.state not in ('confirmed','progress','to_close'):
            raise UserError('سفارش تولید قابل ثبت مصرف نیست.')
        for line in self.line_ids:
            moves = production.move_raw_ids.filtered(lambda m:m.product_id == line.product_id and m.state not in ('done','cancel'))
            if len(moves) != 1:
                raise UserError('برای هر قلم باید یک ردیف مصرف مشخص در سفارش تولید وجود داشته باشد.')
            move = moves
            if self.source_id and move.location_id != self.source_id:
                raise UserError('محل مبدأ با محل مصرف سفارش تولید برابر نیست؛ ابتدا انتقال آماده‌سازی را ثبت کنید.')
            # Weighed totals are separate from automatically reserved quantities.
            total_kg = move.ms_weighed_kg + line.weight_kg
            qty = self._kg_unit()._compute_quantity(total_kg, move.product_uom)
            if move.product_id.tracking != 'none' and not line.lot_id:
                raise UserError('لات قلم مصرفی را پیش از ثبت تعیین کنید.')
            move.with_context(_ms_internal=INTERNAL).write({'ms_weighed_kg':total_kg, 'manual_consumption':True})
            if move.product_id.tracking == 'none':
                move.quantity = qty
            else:
                if line.lot_id.product_id != line.product_id:
                    raise UserError('لات با قلم برابر نیست.')
                ml = move.move_line_ids.filtered(lambda l:l.lot_id == line.lot_id)
                if len(ml) > 1:
                    raise UserError('ردیف‌های لات مصرفی نیازمند تعیین تکلیف در اودو هستند.')
                if ml: ml.quantity += self._kg_unit()._compute_quantity(line.weight_kg, ml.product_uom_id)
                else:
                    self.env['stock.move.line'].create({'move_id':move.id,'product_id':line.product_id.id,'product_uom_id':move.product_uom.id,
                        'quantity':self._kg_unit()._compute_quantity(line.weight_kg,move.product_uom),'lot_id':line.lot_id.id,
                        'location_id':move.location_id.id,'location_dest_id':move.location_dest_id.id})
            move.picked = True
            line._set({'consumption_move_id':move.id})

    def action_post(self):
        for doc in self:
            self.env.cr.execute('SELECT id FROM megasanj_document WHERE id=%s FOR UPDATE',(doc.id,))
            doc.invalidate_recordset()
            if doc.state == 'posted': continue
            if doc.state == 'void' or (doc.discrepancy_id.state == 'open' and doc.policy_id.inventory_requires_discrepancy_closed):
                raise UserError('ابتدا صورت‌جلسه را تعیین تکلیف کنید.')
            if doc.movement_type == 'warehouse_to_production':
                doc._assert_stock(doc.source_id)
                doc._record_consumption()
                doc._set({'state':'posted','effect':'consumption_recorded','posted_at':fields.Datetime.now(),'error':False})
                continue
            if not doc.picking_id:
                if doc.waybill_id:
                    picking = doc.waybill_id.picking_id
                    original = picking.move_ids.filtered(lambda m:m.purchase_line_id == doc.waybill_id.purchase_line_id and m.state not in ('done','cancel'))
                    if not original:
                        raise UserError('ردیف باز برای رسید این محموله وجود ندارد.')
                    # One load/receipt; unrelated lines must not be auto-validated.
                    other = picking.move_ids.filtered(lambda m:m.state not in ('done','cancel') and m not in original)
                    if other:
                        raise UserError('رسید باید برای این محموله تفکیک شود؛ اقلام نامرتبط در رسید وجود دارد.')
                    original._action_cancel()
                else:
                    picking_type = doc.source_id.warehouse_id.int_type_id
                    if not picking_type:
                        raise UserError('نوع عملیات انتقال محل مبدأ تعیین نشده است.')
                    picking = self.env['stock.picking'].create({'picking_type_id':picking_type.id,'company_id':doc.company_id.id,
                        'location_id':doc.source_id.id,'location_dest_id':doc.destination_id.id,'origin':doc.name})
                doc._set({'picking_id':picking.id})
                for line in doc.line_ids:
                    source = picking.location_id if doc.waybill_id else doc.source_id
                    vals = {'product_id':line.product_id.id,'product_uom':doc._kg_unit().id,'product_uom_qty':line.weight_kg,
                        'picking_id':picking.id,'company_id':doc.company_id.id,'location_id':source.id,
                        'location_dest_id':doc.destination_id.id,'ms_document_id':doc.id,'origin':doc.name}
                    if doc.waybill_id:
                        purchase_line = doc.waybill_id.purchase_line_id
                        vals.update(purchase_line_id=purchase_line.id,
                                    price_unit=purchase_line.product_uom_id._compute_price(purchase_line.price_unit,doc._kg_unit()))
                    move = self.env['stock.move'].with_context(_ms_internal=INTERNAL).create(vals)
                    move._action_confirm(merge=False)
                    move._action_assign()
                    if line.product_id.tracking != 'none':
                        if not line.lot_id:
                            raise UserError('لات/سریال قلم را تعیین کنید.')
                        if line.lot_id.product_id != line.product_id:
                            raise UserError('لات متعلق به قلم دیگری است.')
                        move.move_line_ids.unlink()
                        self.env['stock.move.line'].create({'move_id':move.id,'product_id':line.product_id.id,'product_uom_id':doc._kg_unit().id,
                            'quantity':line.weight_kg,'lot_id':line.lot_id.id,'location_id':source.id,'location_dest_id':doc.destination_id.id})
                    else: move.quantity = line.weight_kg
                    move.picked = True
            if doc.source_id: doc._assert_stock(doc.source_id)
            picking = doc.picking_id
            if picking.state != 'done':
                result = picking.button_validate()
                if picking.state != 'done':
                    # Standard quality/backorder wizards remain actionable in Odoo.
                    doc._set({'state':'ready','error':'عملیات رسید/کیفیت را در اودو تکمیل و سپس ثبت گردش را بزنید.'})
                    if isinstance(result,dict):return result
                    continue
            if doc.movement_type == 'external_to_production':
                doc._assert_stock(doc.production_id.location_src_id)
                doc._record_consumption()
            doc._set({'state':'posted','effect':'direct' if doc.movement_type == 'external_to_production' else ('receipt' if doc.waybill_id else 'transfer'),
                      'error':False,'posted_at':fields.Datetime.now()})
        return True

    def action_void(self):
        if not self.env.user.has_group('megasanj.group_manager'):
            raise UserError('ابطال نیازمند مدیر مگاسنج است.')
        for doc in self:
            self.env.cr.execute('SELECT id FROM megasanj_document WHERE id=%s FOR UPDATE',(doc.id,))
            doc.invalidate_recordset()
            if doc.state == 'void': continue
            if not doc.void_reason or not doc.void_reason.strip():
                raise UserError('دلیل ابطال لازم است.')
            if doc.production_id and doc.production_id.state in ('done','cancel'):
                raise UserError('تولید بسته‌شده ابتدا نیازمند گردش اصلاح/Unbuild استاندارد اودو است.')
            for line in doc.line_ids.filtered('consumption_move_id'):
                move = line.consumption_move_id
                if move.product_id.tracking != 'none':
                    raise UserError('اصلاح مصرف لات‌دار باید ابتدا در سفارش تولید انجام شود.')
                move.with_context(_ms_internal=INTERNAL).write({'ms_weighed_kg':max(0,move.ms_weighed_kg - line.weight_kg)})
                move.quantity = doc._kg_unit()._compute_quantity(move.ms_weighed_kg,move.product_uom)
                move.picked = bool(move.ms_weighed_kg)
            if doc.picking_id.state == 'done':
                if not doc.return_picking_id:
                    wizard = self.env['stock.return.picking'].with_context(active_id=doc.picking_id.id, active_model='stock.picking').create({'picking_id':doc.picking_id.id})
                    for line in wizard.product_return_moves:
                        line.quantity = line._ms_remaining_qty() if line.move_id in doc.move_ids else 0
                    returned = wizard._create_return()
                    returned.move_ids.with_context(_ms_internal=INTERNAL).write({'ms_document_id':doc.id,'to_refund':True})
                    doc._set({'return_picking_id':returned.id})
                returned = doc.return_picking_id
                for move in returned.move_ids:
                    move.quantity = move.product_uom_qty
                    move.picked = True
                returned.button_validate()
                if returned.state != 'done':
                    raise UserError('ابتدا کنترل‌های رسید برگشت را تکمیل کنید.')
            elif doc.picking_id:
                doc.picking_id.action_cancel()
            doc._ms_mark_returned()
            doc.message_post(body='ابطال با حفظ گردش برگشت: '+doc.void_reason)
        return True

    def contract(self):
        self.ensure_one()
        comparison=None
        if self.waybill_id and not self.waybill_id.no_origin_weighbridge:
            diff=self.original_kg-self.waybill_id.origin_net_weight_kg
            comparison={'origin_weight_kg':self.waybill_id.origin_net_weight_kg,'destination_weight_kg':self.original_kg,
                'difference_weight_kg':diff,'shortage_weight_kg':max(0,-diff),'surplus_weight_kg':max(0,diff),
                'tolerance_weight_kg':self.policy_id.tolerance_kg,'matches':not bool(self.discrepancy_id),
                'direction':'shortage' if diff < 0 else ('surplus' if diff > 0 else 'equal')}
        return {'success':True,'duplicate':False,'schema_version':3,'event_id':self.event_id,'id':self.id,
            'transport_operation_id':self.production_id.id or self.id,'material_type_id':self.category_id.id,'material_policy_id':self.policy_id.id,
            'odoo_id':self.id,'odoo_state':self.state,'gross_weight_kg':self.gross_kg,'tare_weight_kg':self.tare_kg,'weight_basis':(self.policy_snapshot or {}).get('weight_basis','legacy_scrap_v1'),'accepted_weight_kg':self.accepted_kg,'net_weight_kg':self.net_kg,'inventory_pending':self.state not in ('posted','void'),
            'effect':self.effect or None,'odoo_picking_id':self.picking_id.id or None,'production_id':self.production_id.id or None,
            'weight_comparison':comparison,
            'weight_discrepancy':self.discrepancy_id.contract() if self.discrepancy_id else None,
            'warehouse':{'source_warehouse_id':self.source_id.id or None,'destination_warehouse_id':self.destination_id.id or None},
            'action_required':self.error or None}


class DocumentLine(models.Model):
    _name = 'megasanj.document.line'
    _description = 'قلم پذیرفته‌شده توزین'
    document_id = fields.Many2one('megasanj.document', required=True, ondelete='restrict')
    company_id = fields.Many2one(related='document_id.company_id', store=True)
    product_id = fields.Many2one('product.product', required=True, ondelete='restrict')
    percentage = fields.Float('درصد', digits=(7,4))
    weight_kg = fields.Float('وزن (kg)', digits=(16,3))
    lot_id = fields.Many2one('stock.lot', string='لات', check_company=True)
    consumption_move_id = fields.Many2one('stock.move', readonly=True, ondelete='restrict')

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get('_ms_internal') is not INTERNAL:
            raise UserError('اقلام فقط همراه سند توزین ایجاد می‌شوند.')
        return super().create(vals_list)

    def write(self, vals):
        if set(vals) != {'lot_id'} or self.document_id.filtered(lambda d:d.state in ('posted','void')):
            raise UserError('فقط لات سند در انتظار عملیات قابل تکمیل است.')
        return super().write(vals)

    def _set(self, vals):
        return super(DocumentLine,self).write(vals)

    def unlink(self):
        raise UserError('اقلام سند توزین حذف نمی‌شوند.')


class Discrepancy(models.Model):
    _name = 'megasanj.discrepancy'
    _description = 'صورت‌جلسه عدم تطابق وزن'
    _inherit = ['mail.thread']
    name = fields.Char('شماره صورت‌جلسه', readonly=True, default='New', copy=False)
    document_id = fields.Many2one('megasanj.document', required=True, ondelete='restrict')
    company_id = fields.Many2one(related='document_id.company_id', store=True)
    origin_kg = fields.Float('وزن مبدأ', digits=(16,3))
    destination_kg = fields.Float('وزن مقایسه مقصد', digits=(16,3))
    tolerance_kg = fields.Float('تلرانس', digits=(16,3))
    penalty_weight_kg = fields.Integer('وزن جبران‌شده')
    deduction_reason = fields.Text('نتیجه رسیدگی')
    driver_national_id = fields.Char('کد ملی راننده')
    state = fields.Selection([('open','باز'),('closed','بسته')], default='open', tracking=True)
    reviewed_by = fields.Many2one('res.users', readonly=True)
    reviewed_at = fields.Datetime(readonly=True)
    _document_unique = models.Constraint('unique(document_id)', 'صورت‌جلسه این توزین موجود است.')

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get('_ms_internal') is not INTERNAL:
            raise UserError('صورت‌جلسه فقط از محاسبه مغایرت ایجاد می‌شود.')
        for v in vals_list: v['name'] = self.env['ir.sequence'].next_by_code('megasanj.discrepancy')
        return super().create(vals_list)

    def write(self, vals):
        if {'state','reviewed_by','reviewed_at'}.intersection(vals) and self.env.context.get('_ms_internal') is not INTERNAL:
            raise UserError('نتیجه رسیدگی را از دکمه ثبت نتیجه تأیید کنید.')
        if self.filtered(lambda r:r.state == 'closed'):
            raise UserError('صورت‌جلسه بسته‌شده ثابت است.')
        if {'origin_kg','destination_kg','tolerance_kg','document_id','company_id'}.intersection(vals):
            raise UserError('وزن‌های صورت‌جلسه از سند توزین می‌آیند.')
        return super().write(vals)

    def action_close(self):
        if not self.env.user.has_group('megasanj.group_operator'):
            raise UserError('مجوز رسیدگی مغایرت لازم است.')
        for rec in self:
            self.env.cr.execute('SELECT id FROM megasanj_discrepancy WHERE id=%s FOR UPDATE',(rec.id,))
            rec.invalidate_recordset()
            if rec.state == 'closed': continue
            if rec.penalty_weight_kg < 0 or not (rec.deduction_reason or '').strip():
                raise UserError('وزن جبران نامنفی و نتیجه رسیدگی لازم است.')
            rec.with_context(_ms_internal=INTERNAL).write({'state':'closed','reviewed_by':self.env.uid,'reviewed_at':fields.Datetime.now()})
            if rec.document_id.state == 'pending':
                rec.document_id._set({'state':'ready'})
                try:
                    with self.env.cr.savepoint(): rec.document_id.action_post()
                except UserError as exc: rec.document_id._set({'error':str(exc)})

    def comparison(self):
        self.ensure_one()
        diff = self.destination_kg - self.origin_kg
        return {'origin_weight_kg':self.origin_kg,'destination_weight_kg':self.destination_kg,'difference_weight_kg':diff,
                'shortage_weight_kg':max(0,-diff),'surplus_weight_kg':max(0,diff),'tolerance_weight_kg':self.tolerance_kg,
                'matches':False,'direction':'shortage' if diff < 0 else 'surplus'}

    def contract(self):
        self.ensure_one()
        wb = self.document_id.waybill_id
        difference=abs(self.destination_kg-self.origin_kg)
        severity=1 if difference <= 100 else (2 if difference <= 250 else (3 if difference <= 500 else 4))
        return dict(self.comparison(),id=self.id,certificate_number=self.name,status=self.state,penalty_weight_kg=self.penalty_weight_kg,
            severity_grade=severity,severity_label={1:'کم',2:'متوسط',3:'زیاد',4:'بحرانی'}[severity],
            deduction_reason=self.deduction_reason or None,driver_national_id=self.driver_national_id or None,
            driver_name=wb.driver_name,driver_phone=wb.driver_phone,trailer_plate=wb.trailer_plate,waybill_number=wb.name,
            statement_html=str(self._ms_statement_html()),verification_url=self.get_base_url()+'/web#model=megasanj.discrepancy&id='+str(self.id),
            created_at=self.create_date.isoformat()+'Z',reviewed_at=fields.Datetime.to_string(self.reviewed_at) if self.reviewed_at else None)


class StockMove(models.Model):
    _inherit = 'stock.move'
    ms_document_id = fields.Many2one('megasanj.document', ondelete='restrict', copy=False)
    ms_weighed_kg = fields.Float('مصرف توزین‌شده (kg)', digits=(16,3), copy=False, readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        if any(v.get('ms_document_id') or v.get('ms_weighed_kg') for v in vals_list) and self.env.context.get('_ms_internal') is not INTERNAL:
            raise UserError('ارتباط توزین فقط از گردش مگاسنج ایجاد می‌شود.')
        return super().create(vals_list)

    def _action_done(self, cancel_backorder=False):
        # Odoo normally permits negative stock; only weighbridge effects opt into
        # the stricter policy. Hold product/location locks until the whole posting commits.
        tracked = self.filtered(lambda m:(m.ms_document_id or m.ms_weighed_kg) and m.location_id.usage == 'internal' and m.state != 'done')
        totals = {}
        for move in tracked:
            key = (move.location_id.id,move.product_id.id)
            totals[key] = totals.get(key,0) + move.product_uom._compute_quantity(move.quantity,move.product_id.uom_id)
        for (location,product),quantity in sorted(totals.items()):
            self.env.cr.execute('SELECT pg_advisory_xact_lock(%s,%s)',(location,product))
            locations = self.env['stock.location'].search([('id','child_of',location)])
            self.env.cr.execute('SELECT id FROM stock_quant WHERE product_id=%s AND location_id IN %s ORDER BY id FOR UPDATE',(product,tuple(locations.ids)))
            self.env['stock.quant'].invalidate_model(['quantity','reserved_quantity'])
            quants = self.env['stock.quant'].search([('location_id','child_of',location),('product_id','=',product)])
            if sum(quants.mapped('quantity')) + .0001 < quantity:
                raise UserError('موجودی واقعی برای ثبت گردش توزین کافی نیست.')
        return super()._action_done(cancel_backorder=cancel_backorder)

    def write(self, vals):
        if {'ms_document_id','ms_weighed_kg'}.intersection(vals) and self.env.context.get('_ms_internal') is not INTERNAL:
            raise UserError('ارتباط توزین فقط از گردش مگاسنج ثبت می‌شود.')
        return super().write(vals)


class PurchaseLine(models.Model):
    _inherit = 'purchase.order.line'

    def _get_po_line_moves(self):
        self.ensure_one()
        # Reuse Odoo's receipt, return/refund and accrual calculations. Extend only
        # the product filter for explicitly classified weighbridge receipts.
        moves = super()._get_po_line_moves()
        classified = self.move_ids.filtered(lambda m:m.ms_document_id and m.product_id != self.product_id)
        if self.env.context.get('accrual_entry_date'):
            date = fields.Date.from_string(self.env.context['accrual_entry_date'])
            classified = classified.filtered(lambda m:fields.Date.context_today(m,m.date) <= date)
        return moves | classified
