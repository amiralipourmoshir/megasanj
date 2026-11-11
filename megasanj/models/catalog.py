from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError


class Category(models.Model):
    _inherit = 'product.category'
    ms_enabled = fields.Boolean('محموله قابل توزین')
    ms_code = fields.Char('کد پایدار مگاسنج', copy=False)
    ms_policy_ids = fields.One2many('megasanj.policy', 'category_id', string='سیاست‌های پذیرش')
    ms_policy_id = fields.Many2one('megasanj.policy', compute='_compute_ms_policy')
    _ms_code_unique = models.Constraint('unique(ms_code)', 'کد محموله تکراری است.')

    @api.depends('ms_policy_ids.state', 'ms_policy_ids.version')
    def _compute_ms_policy(self):
        for rec in self:
            rec.ms_policy_id = rec.ms_policy_ids.filtered(lambda p: p.state == 'published').sorted('version', reverse=True)[:1]

    @api.constrains('ms_enabled', 'ms_code')
    def _check_ms_code(self):
        import re
        for rec in self.filtered('ms_enabled'):
            if not rec.ms_code or not re.fullmatch(r'[a-z][a-z0-9_-]{0,49}', rec.ms_code):
                raise ValidationError('برای محموله، کد انگلیسی پایدار لازم است.')

    def write(self, vals):
        if 'ms_code' in vals and any(r.ms_code and r.ms_code != vals['ms_code'] for r in self):
            raise UserError('کد تثبیت‌شده محموله قابل تغییر نیست.')
        return super().write(vals)


class Policy(models.Model):
    _name = 'megasanj.policy'
    _description = 'سیاست نسخه‌دار پذیرش محموله'
    _order = 'category_id, version desc'
    name = fields.Char(compute='_compute_name', store=True)
    category_id = fields.Many2one('product.category', required=True, ondelete='restrict')
    version = fields.Integer(default=1, required=True)
    state = fields.Selection([('draft', 'پیش‌نویس'), ('published', 'منتشرشده')], default='draft', required=True)
    calculator = fields.Selection([('net', 'وزن خالص توزین'), ('scrap_v1', 'قراضه با افت و برگشتی')], default='net', required=True)
    quality_enabled = fields.Boolean('کنترل کیفیت', default=True)
    quality_required = fields.Boolean('کیفیت اجباری', default=True)
    tolerance_kg = fields.Float('تلرانس اختلاف وزن (کیلوگرم)', default=50, digits=(16, 3))
    detect_surplus = fields.Boolean('بررسی اضافه وزن')
    inventory_requires_discrepancy_closed = fields.Boolean('رفع مغایرت پیش از ثبت انبار')
    allow_direct_production = fields.Boolean('ورود مستقیم به تولید')
    _version_unique = models.Constraint('unique(category_id, version)', 'نسخه سیاست تکراری است.')
    _positive_version = models.Constraint('check(version > 0 AND tolerance_kg >= 0)', 'نسخه و تلرانس معتبر نیست.')

    @api.depends('category_id', 'version')
    def _compute_name(self):
        for r in self:
            r.name = '%s / %s' % (r.category_id.display_name, r.version)

    @api.constrains('quality_required', 'quality_enabled')
    def _check_quality(self):
        if any(r.quality_required and not r.quality_enabled for r in self):
            raise ValidationError('کیفیت اجباری نیازمند فعال بودن کنترل کیفیت است.')

    def action_publish(self):
        self.write({'state': 'published'})

    def write(self, vals):
        if self.filtered(lambda r: r.state == 'published'):
            raise UserError('سیاست منتشرشده ثابت است؛ نسخه جدید ایجاد کنید.')
        return super().write(vals)

    def unlink(self):
        if self.filtered(lambda r: r.state == 'published'):
            raise UserError('سیاست منتشرشده حذف نمی‌شود.')
        return super().unlink()

    def contract(self):
        self.ensure_one()
        return dict(self.read(['version', 'calculator', 'quality_enabled', 'quality_required', 'tolerance_kg',
                               'detect_surplus', 'inventory_requires_discrepancy_closed', 'allow_direct_production'])[0],
                    material_type_id=self.category_id.id)


class Product(models.Model):
    _inherit = 'product.template'
    ms_weighable = fields.Boolean('قلم مجاز ترکیب محموله')


class Location(models.Model):
    _inherit = 'stock.location'
    ms_category_id = fields.Many2one('product.category', string='نوع محموله مگاسنج', domain=[('ms_enabled', '=', True)])
    ms_receiving_default = fields.Boolean('پیش‌فرض دریافت این نوع در شرکت')

    @api.constrains('ms_category_id', 'ms_receiving_default', 'usage', 'company_id')
    def _check_ms_location(self):
        for rec in self.filtered('ms_category_id'):
            if rec.usage != 'internal' or not rec.company_id:
                raise ValidationError('محل مگاسنج باید داخلی و متعلق به یک شرکت باشد.')
            if rec.ms_receiving_default:
                self.env.cr.execute('SELECT pg_advisory_xact_lock(%s, %s)', (rec.company_id.id, rec.ms_category_id.id))
                if self.search_count([('id', '!=', rec.id), ('company_id', '=', rec.company_id.id),
                                      ('ms_category_id', '=', rec.ms_category_id.id), ('ms_receiving_default', '=', True)]):
                    raise ValidationError('پیش‌فرض دریافت این نوع کالا قبلاً تعیین شده است.')


class Eligibility(models.Model):
    _name = 'megasanj.eligibility'
    _description = 'صلاحیت تأمین‌کننده برای محموله'
    partner_id = fields.Many2one('res.partner', required=True, ondelete='restrict', string='شرکت تأمین‌کننده')
    category_id = fields.Many2one('product.category', required=True, ondelete='restrict', string='نوع محموله')
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda s: s.env.company)
    _pair_unique = models.Constraint('unique(partner_id, category_id, company_id)', 'صلاحیت تکراری است.')
