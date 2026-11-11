from odoo import fields, models
from odoo.exceptions import UserError
from .common import INTERNAL


class Purchase(models.Model):
    _inherit = 'purchase.order'
    ms_freight_by_contractor = fields.Boolean('کرایه حمل با پیمانکار است', tracking=True,
        help='برای بارنامه‌های این خرید، تعیین‌کننده پرداخت کرایه است. در حالت پیمانکاری، اپ هیچ مبلغ کرایه‌ای دریافت نمی‌کند.')

    def write(self, vals):
        affected=self.filtered(lambda p: 'ms_freight_by_contractor' in vals and p.ms_freight_by_contractor != bool(vals['ms_freight_by_contractor']))
        waybills=self.env['megasanj.waybill'].sudo().search([('parent_purchase_id','in',affected.ids),('state','!=','cancelled')]) if affected else self.env['megasanj.waybill']
        if waybills.filtered(lambda w:w.state=='used'):
            raise UserError('این خرید سند توزین دارد؛ مسئولیت کرایه اسناد ثبت‌شده قابل تغییر نیست.')
        result=super().write(vals)
        for wb in waybills:
            update={'freight_by_contractor':wb.parent_purchase_id.ms_freight_by_contractor}
            if update['freight_by_contractor']:update.update(freight_amount=0,api_freight_irr=0)
            wb.with_context(_ms_internal=INTERNAL).write(update)
        return result
