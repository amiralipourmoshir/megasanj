from datetime import timedelta
import uuid
from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from .common import INTERNAL


class Company(models.Model):
    _inherit = 'res.company'
    ms_freight_product_id = fields.Many2one('product.product', string='خدمت کرایه باسکول', domain=[('type', '=', 'service')])


class Waybill(models.Model):
    _name = 'megasanj.waybill'
    _description = 'بارنامه و نوبت مگاسنج'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'
    name = fields.Char('شماره بارنامه', readonly=True, copy=False, default='New')
    number = fields.Integer(readonly=True, copy=False, index=True)
    event_id = fields.Char(index=True, copy=False)
    payload_hash = fields.Char(copy=False)
    company_id = fields.Many2one('res.company', required=True, default=lambda s: s.env.company)
    picking_id = fields.Many2one('stock.picking', string='رسید خرید', required=True, ondelete='restrict', check_company=True,
                                 domain="[('picking_type_code', '=', 'incoming'), ('state', 'not in', ['done','cancel'])]")
    purchase_line_id = fields.Many2one('purchase.order.line', string='ردیف خرید محموله', required=True, ondelete='restrict', check_company=True)
    partner_id = fields.Many2one('res.partner', related='picking_id.partner_id', store=True, string='تأمین‌کننده')
    parent_purchase_id = fields.Many2one('purchase.order', related='picking_id.purchase_id')
    category_id = fields.Many2one('product.category', related='purchase_line_id.product_id.categ_id', store=True, string='نوع محموله')
    policy_id = fields.Many2one('megasanj.policy', readonly=True, ondelete='restrict')
    driver_name = fields.Char('راننده', required=True)
    driver_phone = fields.Char('تلفن راننده', required=True)
    driver_partner_id = fields.Many2one('res.partner', string='ذی‌نفع کرایه')
    trailer_plate = fields.Char('پلاک', required=True)
    origin_province = fields.Char('استان مبدأ', required=True)
    origin_city = fields.Char('شهر مبدأ', required=True)
    origin_net_weight_kg = fields.Float('وزن مبدأ (kg)', digits=(16, 3))
    no_origin_weighbridge = fields.Boolean('فاقد وزن مبدأ')
    freight_by_contractor = fields.Boolean('کرایه با پیمانکار', readonly=True)
    freight_amount = fields.Monetary('کرایه', currency_field='currency_id')
    api_freight_irr = fields.Integer('کرایه قرارداد اپ (ریال)', readonly=True, copy=False)
    currency_id = fields.Many2one(related='company_id.currency_id')
    state = fields.Selection([('issued', 'صادرشده'), ('reserved', 'رزروشده'), ('used', 'توزین‌شده'), ('cancelled', 'لغوشده')], default='issued', tracking=True)
    reservation_token = fields.Char(copy=False)
    reservation_expires_at = fields.Datetime(copy=False)
    queue_number = fields.Integer('نوبت', readonly=True)
    queue_day = fields.Date('روز نوبت', readonly=True)
    gate_state = fields.Selection([('waiting', 'در صف'), ('ready', 'فراخوان‌شده'), ('inside', 'داخل مجموعه'), ('exited', 'خارج‌شده')], default='waiting', tracking=True)
    called_at = fields.Datetime(readonly=True)
    entry_at = fields.Datetime(readonly=True)
    exit_at = fields.Datetime(readonly=True)
    document_ids = fields.One2many('megasanj.document', 'waybill_id', string='اسناد باسکول')
    gate_check_ids = fields.One2many('megasanj.gate.check', 'waybill_id', string='بررسی انتظامات')
    _event_unique = models.Constraint('unique(company_id,event_id)', 'رویداد بارنامه تکراری است.')
    _number_unique = models.Constraint('unique(number)', 'شماره بارنامه تکراری است.')
    _queue_unique = models.Constraint('unique(company_id,queue_day,queue_number)', 'نوبت روز تکراری است.')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            company = self.env['res.company'].browse(vals.get('company_id') or self.env.company.id)
            self.env.cr.execute('SELECT pg_advisory_xact_lock(%s, %s)', (74182, company.id))
            line = self.env['purchase.order.line'].browse(vals.get('purchase_line_id')).exists()
            vals['freight_by_contractor']=bool(line.order_id.ms_freight_by_contractor)
            vals['driver_partner_id']=self._ms_driver_partner(vals.get('driver_name'),vals.get('driver_phone'),company).id
            policy = line.product_id.categ_id.ms_policy_id
            if not policy:
                raise ValidationError('برای نوع محموله رسید، سیاست منتشرشده لازم است.')
            day = fields.Datetime.context_timestamp(self.with_context(tz='Asia/Tehran'), fields.Datetime.now()).date()
            last = self.search([('company_id', '=', company.id), ('queue_day', '=', day)], order='queue_number desc', limit=1)
            if last.queue_number >= 999:
                raise ValidationError('ظرفیت نوبت روز تکمیل شده است.')
            number = int(self.env['ir.sequence'].next_by_code('megasanj.waybill'))
            vals.update(name=str(number), number=number, policy_id=policy.id, queue_day=day,
                        queue_number=last.queue_number + 1, state='issued', gate_state='waiting')
            if vals.get('freight_by_contractor'):
                vals['freight_amount'] = 0
                vals['api_freight_irr'] = 0
            elif 'api_freight_irr' not in vals:
                amount = vals.get('freight_amount',0)
                rial = self.env.ref('base.IRR')
                if amount and company.currency_id != rial and not rial.rate_ids:
                    raise ValidationError('برای کرایه، نرخ تبدیل ریال به ارز شرکت را در تنظیمات ارز اودو تعیین کنید.')
                vals['api_freight_irr'] = round(company.currency_id._convert(amount,rial,company,fields.Date.today()))
        records = super().create(vals_list)
        for rec in records:
            points = self.env['megasanj.checkpoint'].search([('company_id', '=', rec.company_id.id), ('active', '=', True)])
            self.env['megasanj.gate.check'].create([{'waybill_id': rec.id, 'checkpoint_id': p.id} for p in points])
        return records

    @api.constrains('picking_id', 'purchase_line_id', 'company_id', 'origin_net_weight_kg', 'no_origin_weighbridge', 'freight_amount')
    def _check_receipt(self):
        for r in self:
            if r.picking_id.company_id != r.company_id or r.purchase_line_id.order_id != r.picking_id.purchase_id:
                raise ValidationError('رسید و ردیف خرید باید از همان شرکت و سفارش باشند.')
            if not r.category_id.ms_enabled or not r.picking_id.move_ids.filtered(lambda m: m.purchase_line_id == r.purchase_line_id):
                raise ValidationError('ردیف خرید متعلق به محموله قابل توزین این رسید نیست.')
            if r.freight_amount < 0 or (not r.no_origin_weighbridge and r.origin_net_weight_kg <= 0):
                raise ValidationError('کرایه و وزن مبدأ معتبر نیست.')

    def write(self, vals):
        system = {'ms_returned','state','gate_state','called_at','entry_at','exit_at','number','name','reservation_token','reservation_expires_at','policy_id','queue_number','queue_day','api_freight_irr','freight_by_contractor'}
        if system.intersection(vals) and self.env.context.get('_ms_internal') is not INTERNAL:
            raise UserError('وضعیت بارنامه از گردش رزرو و انتظامات تغییر می‌کند.')
        if {'picking_id','purchase_line_id','company_id'}.intersection(vals):
            raise UserError('ارتباط رسید بارنامه ثابت است؛ بارنامه تازه ایجاد کنید.')
        locked = {'picking_id','purchase_line_id','driver_name','driver_phone','trailer_plate','origin_province','origin_city',
                  'origin_net_weight_kg','no_origin_weighbridge','freight_by_contractor','freight_amount','policy_id','company_id','event_id'}
        if locked.intersection(vals) and self.filtered(lambda r: r.state in ('used', 'cancelled')):
            raise UserError('بارنامه مصرف‌شده ثابت است.')
        if 'driver_partner_id' in vals and self.env.context.get('_ms_internal') is not INTERNAL:
            raise UserError('ذی‌نفع کرایه از اطلاعات راننده تعیین می‌شود.')
        driver_changed=bool({'driver_name','driver_phone'}.intersection(vals))
        freight_changed='freight_amount' in vals and self.env.context.get('_ms_internal') is not INTERNAL
        if driver_changed or freight_changed:
            rial=self.env.ref('base.IRR')
            for rec in self:
                update=dict(vals)
                if driver_changed:
                    update['driver_partner_id']=self._ms_driver_partner(vals.get('driver_name',rec.driver_name),vals.get('driver_phone',rec.driver_phone),rec.company_id).id
                if freight_changed:
                    if vals['freight_amount'] and rec.currency_id!=rial and not rial.rate_ids:
                        raise ValidationError('نرخ تبدیل ریال به ارز شرکت لازم است.')
                    update['api_freight_irr']=round(rec.currency_id._convert(vals['freight_amount'],rial,rec.company_id,fields.Date.today()))
                super(Waybill,rec).write(update)
            return True
        return super().write(vals)

    def reserve(self):
        self.ensure_one()
        self.env.cr.execute('SELECT id FROM megasanj_waybill WHERE id=%s FOR UPDATE', (self.id,))
        self.invalidate_recordset()
        if self.state == 'reserved' and self.reservation_expires_at < fields.Datetime.now():
            self.with_context(_ms_internal=INTERNAL).write({'state': 'issued', 'reservation_token': False})
        if self.state != 'issued':
            raise UserError('بارنامه آزاد نیست.')
        self.with_context(_ms_internal=INTERNAL).write({'state': 'reserved', 'reservation_token': str(uuid.uuid4()),
                    'reservation_expires_at': fields.Datetime.now() + timedelta(minutes=30)})
        return self

    def gate_action(self, action):
        self.ensure_one()
        if not self.env.user.has_group('megasanj.group_gate'):
            raise UserError('عملیات انتظامات نیازمند دسترسی انتظامات است.')
        self.env.cr.execute('SELECT pg_advisory_xact_lock(%s, %s)', (74182, self.company_id.id))
        self.invalidate_recordset()
        target = {'call': 'ready', 'enter': 'inside', 'exit': 'exited'}.get(action)
        expected = {'call': 'waiting', 'enter': 'ready', 'exit': 'inside'}.get(action)
        if self.gate_state == target:
            return
        if not target or self.gate_state != expected or (self.state == 'cancelled' and not (action == 'exit' and self.ms_returned)):
            raise UserError('تغییر وضعیت صف مجاز نیست.')
        if action == 'call':
            called = self.search([('company_id', '=', self.company_id.id),
                                  ('gate_state', '=', 'ready'), ('state', '!=', 'cancelled')], limit=1)
            if called:
                raise UserError('خودروی پلاک %s با راننده %s (بارنامه %s) در حال فراخوان است؛ ابتدا ورود آن را ثبت کنید.'
                                % (called.trailer_plate, called.driver_name, called.name))
        if action == 'exit':
            docs = self.document_ids.filtered(lambda d: d.state != 'void')
            if not self.ms_returned and (not docs or any(d.state != 'posted' for d in docs)):
                raise UserError('توزین و گردش مرتبط باید تعیین تکلیف شده باشد.')
            if not self.ms_returned and docs.discrepancy_id.filtered(lambda d: d.state != 'closed'):
                raise UserError('صورت‌جلسه مغایرت باز است.')
            required = self.env['megasanj.checkpoint'].search([('company_id','=',self.company_id.id),('active','=',True),('required','=',True)])
            passed = self.gate_check_ids.filtered(lambda c: c.state == 'pass').checkpoint_id
            if required - passed:
                raise UserError('همه چک‌پوینت‌های الزامی باید تأیید شوند.')
        self.with_context(_ms_internal=INTERNAL).write({'gate_state': target, {'call': 'called_at', 'enter': 'entry_at', 'exit': 'exit_at'}[action]: fields.Datetime.now()})

    def action_call(self):
        for r in self: r.gate_action('call')

    def action_enter(self):
        for r in self: r.gate_action('enter')

    def action_exit(self):
        for r in self: r.gate_action('exit')


class Checkpoint(models.Model):
    _name = 'megasanj.checkpoint'
    _description = 'چک‌پوینت انتظامات'
    name = fields.Char('عنوان', required=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda s: s.env.company)
    active = fields.Boolean(default=True)
    required = fields.Boolean('تأیید برای خروج الزامی است', default=True)

    @api.model_create_multi
    def create(self,vals_list):
        records=super().create(vals_list)
        for point in records:
            open_trucks=self.env['megasanj.waybill'].search([('company_id','=',point.company_id.id),('gate_state','!=','exited'),('state','!=','cancelled')])
            self.env['megasanj.gate.check'].create([{'waybill_id':w.id,'checkpoint_id':point.id} for w in open_trucks])
        return records


class GateCheck(models.Model):
    _name = 'megasanj.gate.check'
    _description = 'ثبت بررسی انتظامات'
    waybill_id = fields.Many2one('megasanj.waybill', required=True, ondelete='restrict')
    checkpoint_id = fields.Many2one('megasanj.checkpoint', required=True, ondelete='restrict')
    company_id = fields.Many2one(related='waybill_id.company_id', store=True)
    state = fields.Selection([('todo','بررسی‌نشده'), ('pass','تأیید'), ('fail','رد')], default='todo', required=True)
    user_id = fields.Many2one('res.users', readonly=True)
    checked_at = fields.Datetime(readonly=True)
    notes = fields.Text('توضیحات')
    _checkpoint_unique = models.Constraint('unique(waybill_id,checkpoint_id)', 'چک‌پوینت تکراری است.')

    @api.model_create_multi
    def create(self,vals_list):
        for v in vals_list:
            w=self.env['megasanj.waybill'].browse(v.get('waybill_id')).exists()
            point=self.env['megasanj.checkpoint'].browse(v.get('checkpoint_id')).exists()
            if w.company_id != point.company_id or w.gate_state == 'exited':
                raise ValidationError('چک‌پوینت باید متعلق به شرکت خودرو و پیش از خروج باشد.')
            v.update(state='todo',user_id=False,checked_at=False)
        return super().create(vals_list)

    def write(self, vals):
        if {'waybill_id','checkpoint_id','user_id','checked_at'}.intersection(vals):
            raise UserError('ارتباط و مسئول ثبت بررسی قابل بازنویسی نیست.')
        if any(r.waybill_id.gate_state == 'exited' for r in self):
            raise UserError('بررسی خودروی خارج‌شده ثابت است.')
        if 'state' in vals:
            if not self.env.user.has_group('megasanj.group_gate'):
                raise UserError('دسترسی انتظامات لازم است.')
            vals = dict(vals, user_id=self.env.uid, checked_at=fields.Datetime.now())
        return super().write(vals)


class Freight(models.Model):
    _name = 'megasanj.freight'
    _description = 'رسید کرایه باسکول'
    _inherit = ['mail.thread']
    name = fields.Char('شماره', default='New', copy=False)
    document_id = fields.Many2one('megasanj.document', required=True, ondelete='restrict')
    company_id = fields.Many2one(related='document_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    partner_id = fields.Many2one('res.partner', string='ذی‌نفع کرایه')
    amount = fields.Monetary('کرایه')
    deduction = fields.Monetary('کسر مبلغ')
    payable = fields.Monetary('قابل پرداخت', compute='_compute_payable')
    state = fields.Selection([('pending','در انتظار مالی'),('approved','تأییدشده'),('void','باطل')], default='pending', tracking=True)
    bill_id = fields.Many2one('account.move', readonly=True)
    _document_unique = models.Constraint('unique(document_id)', 'رسید کرایه این توزین موجود است.')

    @api.model_create_multi
    def create(self, vals_list):
        raise UserError('ایجاد سند اختصاصی کرایه حذف شده است؛ کرایه در سفارش خرید ثبت می‌شود.')

    @api.depends('amount', 'deduction')
    def _compute_payable(self):
        for r in self: r.payable = r.amount - r.deduction

    @api.constrains('amount', 'deduction')
    def _check_amount(self):
        for r in self:
            if r.amount < 0 or not 0 <= r.deduction <= r.amount or (r.deduction and not r.document_id.discrepancy_id):
                raise ValidationError('کسر کرایه باید در محدوده کرایه و دارای صورت‌جلسه باشد.')

    def action_approve(self):
        raise UserError('گردش قدیمی کرایه غیرفعال شده است؛ از سفارش خرید و صورتحساب استاندارد اودو استفاده کنید.')

    def write(self, vals):
        if {'amount','document_id','state','bill_id','deduction','partner_id'}.intersection(vals) and self.env.context.get('_ms_internal') is not INTERNAL:
            raise UserError('مبلغ پایه و وضعیت رسید از گردش تأیید مالی می‌آید.')
        if self.filtered(lambda r:r.state == 'approved'):
            raise UserError('رسید مالی تأییدشده ثابت است؛ اصلاح از گردش حسابداری انجام شود.')
        return super().write(vals)
