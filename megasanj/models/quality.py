from odoo import api, fields, models, Command
from odoo.fields import Domain
from odoo.exceptions import UserError, ValidationError
from ..domain import composition
from .common import INTERNAL


def validate_lines(record):
    try:
        composition([{'material_item_id': l.product_id.id, 'percentage': l.percentage} for l in record.ms_line_ids],
                    record.ms_loss, record.ms_rejected)
    except (ValueError, KeyError) as exc:
        raise ValidationError(str(exc)) from exc
    if not record.ms_category_id or any(l.product_id.categ_id != record.ms_category_id or
                                       not l.product_id.ms_weighable or not l.product_id.active or
                                       not l.product_id.is_storable for l in record.ms_line_ids):
        raise ValidationError('اقلام ترکیب باید فعال، قابل انبارش و از نوع محموله رسید باشند.')


class QualityPoint(models.Model):
    _inherit = 'quality.point'
    ms_category_id = fields.Many2one('product.category', string='نوع محموله', domain=[('ms_enabled', '=', True)])
    ms_line_ids = fields.One2many('megasanj.quality.line', 'point_id', string='الگوی ترکیب درصدی', copy=True)
    ms_loss = fields.Float('افت (%)', digits=(7, 4))
    ms_rejected = fields.Float('برگشتی (%)', digits=(7, 4))

    def _get_domain(self, product_ids, picking_type_id, measure_on=False, on_demand=False):
        domain=super()._get_domain(product_ids,picking_type_id,measure_on=measure_on,on_demand=on_demand)
        if self.env.context.get('_ms_after_weighing') is not INTERNAL:
            # Native receipt confirmation must not request this assessment before weighing.
            return domain & Domain('test_type','!=','megasanj_percentage')
        # A percentage point applies only to its cargo type, even if the native
        # "all products" scope was selected. Other Odoo check types are untouched.
        return domain & (Domain('test_type','!=','megasanj_percentage') | Domain('ms_category_id','in',product_ids.categ_id.ids))

    @api.model_create_multi
    def create(self,vals_list):
        for v in vals_list:
            test=self.env['quality.point.test_type'].browse(v.get('test_type_id')).exists()
            if test.technical_name == 'megasanj_percentage' and v.get('ms_category_id'):
                v.setdefault('product_category_ids',[Command.set([v['ms_category_id']])])
                v.setdefault('measure_on','operation')
        return super().create(vals_list)

    @api.constrains('test_type_id', 'ms_category_id', 'ms_line_ids', 'ms_loss', 'ms_rejected')
    def _check_ms_percentage(self):
        for rec in self.filtered(lambda r: r.test_type == 'megasanj_percentage'):
            validate_lines(rec)
            if rec.measure_on != 'operation' or any(t.code != 'incoming' for t in rec.picking_type_ids):
                raise ValidationError('کنترل درصدی محموله باید برای عملیات دریافت خرید و به ازای عملیات باشد.')

    @api.onchange('test_type_id')
    def _onchange_ms_test_type(self):
        if self.test_type == 'megasanj_percentage':
            self.measure_on = 'operation'


class QualityCheck(models.Model):
    _inherit = 'quality.check'
    ms_weighbridge_id = fields.Many2one('megasanj.document', string='سند توزین در انتظار QC', check_company=True, copy=False, ondelete='restrict')
    ms_driver_name = fields.Char(related='ms_weighbridge_id.driver_name', string='راننده')
    ms_driver_phone = fields.Char(related='ms_weighbridge_id.driver_phone', string='تلفن راننده')
    ms_trailer_plate = fields.Char(related='ms_weighbridge_id.trailer_plate', string='پلاک')
    ms_waybill_id = fields.Many2one(related='ms_weighbridge_id.waybill_id', string='بارنامه')
    ms_net_kg = fields.Float(related='ms_weighbridge_id.net_kg', string='خالص باسکول (کیلوگرم)')
    ms_code = fields.Char('کد کیفیت باسکول', readonly=True, copy=False, index=True)
    ms_category_id = fields.Many2one('product.category', string='نوع محموله', domain=[('ms_enabled', '=', True)])
    ms_line_ids = fields.One2many('megasanj.quality.line', 'check_id', string='ترکیب محموله', copy=True)
    ms_loss = fields.Float('افت (%)', digits=(7, 4))
    ms_rejected = fields.Float('برگشتی (%)', digits=(7, 4))
    ms_state = fields.Selection([('draft', 'در حال ارزیابی'), ('available', 'آماده توزین'),
                                 ('attached', 'مصرف‌شده'), ('cancelled', 'لغوشده')], string='وضعیت ارزیابی', default='draft', copy=False)
    ms_document_id = fields.Many2one('megasanj.document', string='سند باسکول', readonly=True, copy=False, ondelete='restrict')
    _ms_code_unique = models.Constraint('unique(ms_code)', 'کد کیفیت تکراری است.')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            point = self.env['quality.point'].browse(vals.get('point_id')).exists()
            test = self.env['quality.point.test_type'].browse(vals.get('test_type_id')).exists() or point.test_type_id
            if test.technical_name == 'megasanj_percentage':
                vals['ms_code'] = self.env['ir.sequence'].next_by_code('megasanj.quality')
                if point:
                    vals.setdefault('ms_category_id', point.ms_category_id.id)
                    vals.setdefault('ms_loss', point.ms_loss)
                    vals.setdefault('ms_rejected', point.ms_rejected)
                    vals.setdefault('ms_line_ids', [Command.create({'product_id': l.product_id.id, 'percentage': l.percentage}) for l in point.ms_line_ids])
        return super().create(vals_list)

    @api.onchange('picking_id')
    def _onchange_ms_receipt(self):
        if self.test_type == 'megasanj_percentage' and self.picking_id:
            categories = self.picking_id.move_ids.product_id.categ_id.filtered('ms_enabled')
            self.ms_category_id = categories if len(categories) == 1 else False

    def _ms_validate(self):
        self.ensure_one()
        if self.ms_weighbridge_id and self.ms_weighbridge_id.state != 'quality':
            raise ValidationError('این سند توزین در انتظار کنترل کیفیت نیست.')
        validate_lines(self)
        if not self.picking_id or self.picking_id.picking_type_code != 'incoming' or not self.picking_id.purchase_id:
            raise ValidationError('کیفیت درصدی باسکول باید به رسید خرید ورودی مرتبط باشد.')
        categories = self.picking_id.move_ids.filtered(lambda m: m.state != 'cancel').product_id.categ_id
        if self.ms_category_id not in categories:
            raise ValidationError('نوع کیفیت با رسید خرید برابر نیست.')
        if self.company_id != self.picking_id.company_id:
            raise ValidationError('شرکت کیفیت با رسید برابر نیست.')

    def do_pass(self):
        for rec in self.filtered(lambda r: r.test_type == 'megasanj_percentage'):
            rec._ms_validate()
            if rec.ms_state in ('attached', 'cancelled'):
                raise UserError('این سند کیفیت قابل استفاده مجدد نیست.')
        result = super().do_pass()
        self.filtered(lambda r: r.test_type == 'megasanj_percentage').write({'ms_state': 'available'})
        for rec in self.filtered(lambda q:q.ms_weighbridge_id and q.ms_weighbridge_id.state=='quality'):
            rec.ms_weighbridge_id.sudo()._complete_after_quality(rec)
        return result

    def action_ms_release(self):
        return self.do_pass()

    def action_ms_cancel(self):
        if self.filtered('ms_document_id'):
            raise UserError('سند کیفیت مصرف‌شده قابل لغو مستقل نیست.')
        self.write({'ms_state': 'cancelled'})

    def action_ms_reopen(self):
        if any(r.ms_state != 'available' for r in self):
            raise UserError('فقط کیفیت آزاد و مصرف‌نشده قابل بازگشت به ارزیابی است.')
        self.write({'ms_state':'draft','quality_state':'none'})

    def write(self, vals):
        if {'ms_document_id','ms_code'}.intersection(vals) or vals.get('ms_state') == 'attached':
            if self.env.context.get('_ms_internal') is not INTERNAL:
                raise UserError('اتصال کیفیت فقط از پذیرش توزین ثبت می‌شود.')
        protected = {'ms_category_id', 'ms_line_ids', 'ms_loss', 'ms_rejected', 'picking_id', 'test_type_id', 'point_id', 'quality_state', 'ms_state', 'ms_weighbridge_id'}
        if protected.intersection(vals) and self.filtered(lambda r: r.ms_state == 'attached'):
            raise UserError('کیفیت متصل به توزین قابل ویرایش نیست.')
        # Editing a released assessment invalidates its readiness until reviewed again.
        if {'ms_line_ids', 'ms_loss', 'ms_rejected', 'ms_category_id', 'picking_id'}.intersection(vals):
            if self.filtered(lambda r: r.ms_state == 'available'):
                vals = dict(vals, ms_state='draft', quality_state='none')
        return super().write(vals)

    def unlink(self):
        if self.filtered(lambda r: r.ms_state in ('available', 'attached')):
            raise UserError('کیفیت آزادشده یا مصرف‌شده را حذف نکنید؛ گردش لغو دارد.')
        return super().unlink()

    @api.depends('picking_id', 'ms_category_id', 'test_type_id')
    def _compute_allowed_product_ids(self):
        super()._compute_allowed_product_ids()
        for rec in self.filtered(lambda r: r.test_type == 'megasanj_percentage'):
            rec.allowed_product_ids = self.env['product.product'].search([('categ_id', '=', rec.ms_category_id.id), ('ms_weighable', '=', True)])


class QualityLine(models.Model):
    _name = 'megasanj.quality.line'
    _description = 'ردیف ترکیب درصدی کیفیت'
    point_id = fields.Many2one('quality.point', ondelete='cascade')
    check_id = fields.Many2one('quality.check', ondelete='cascade')
    product_id = fields.Many2one('product.product', required=True, ondelete='restrict', string='قلم')
    percentage = fields.Float('درصد', digits=(7, 4), required=True)
    weight_kg = fields.Float('وزن پذیرفته‌شده (kg)', digits=(16, 3), readonly=True)
    company_id = fields.Many2one('res.company', compute='_compute_company', store=True)
    _percentage_range = models.Constraint('check(percentage > 0 AND percentage <= 100)', 'درصد باید بیشتر از صفر و حداکثر صد باشد.')
    _parent = models.Constraint('check((point_id IS NULL) <> (check_id IS NULL))', 'ردیف باید متعلق به نقطه یا سند کیفیت باشد.')
    _check_product = models.Constraint('unique(check_id, product_id)', 'قلم کیفیت تکراری است.')
    _point_product = models.Constraint('unique(point_id, product_id)', 'قلم الگو تکراری است.')

    @api.depends('point_id.company_id', 'check_id.company_id')
    def _compute_company(self):
        for r in self:
            r.company_id = r.point_id.company_id or r.check_id.company_id

    @api.model_create_multi
    def create(self, vals_list):
        for v in vals_list:
            check = self.env['quality.check'].browse(v.get('check_id')).exists()
            if check.ms_state in ('available', 'attached', 'cancelled'):
                raise UserError('ابتدا ارزیابی کیفیت را به حالت قابل ویرایش برگردانید.')
        return super().create(vals_list)

    def write(self, vals):
        if 'weight_kg' in vals and self.env.context.get('_ms_internal') is not INTERNAL:
            raise UserError('وزن کیفیت از محاسبه توزین می‌آید.')
        if self.check_id.filtered(lambda r: r.ms_state in ('available', 'attached', 'cancelled')) and set(vals) - {'weight_kg'}:
            raise UserError('ردیف کیفیت آزادشده قابل تغییر نیست.')
        return super().write(vals)

    def unlink(self):
        if self.check_id.filtered(lambda r: r.ms_state in ('available', 'attached', 'cancelled')):
            raise UserError('ردیف کیفیت آزادشده قابل حذف نیست.')
        return super().unlink()
