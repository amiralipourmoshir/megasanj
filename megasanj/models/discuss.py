import base64
from markupsafe import Markup, escape
from odoo import api, fields, models, Command, SUPERUSER_ID
from odoo.exceptions import AccessError, UserError
from odoo.tools import file_open
from .common import INTERNAL


class Channel(models.Model):
    _inherit = 'discuss.channel'

    def _ms_channels(self):
        channel = self.env.ref('megasanj.discuss_channel', raise_if_not_found=False)
        return self.filtered(lambda c: channel and c.id == channel.id)

    def _ms_guard(self):
        if self._ms_channels() and self.env.uid != SUPERUSER_ID and self.env.context.get('_ms_internal') is not INTERNAL and not self.env.user.has_group('megasanj.group_senior'):
            raise AccessError('فقط مدیران ارشد مگاسنج می‌توانند اعضا و تنظیمات گروه را مدیریت کنند.')

    @api.model
    def _ms_setup(self):
        senior = self.env.ref('megasanj.group_senior')
        admins = self.env.ref('base.group_system').all_user_ids.filtered(lambda u: u.active and not u.share)
        senior.write({'user_ids': [Command.link(u.id) for u in admins]})
        channel = self.env.ref('megasanj.discuss_channel', raise_if_not_found=False)
        with file_open('megasanj/static/description/icon.png', 'rb') as image:
            icon = base64.b64encode(image.read())
        if not channel:
            channel = self.with_context(_ms_internal=INTERNAL).create({'name':'مگاسنج', 'channel_type':'group', 'image_128':icon,
                'channel_partner_ids':[Command.link(p.id) for p in senior.all_user_ids.partner_id]})
            self.env['ir.model.data'].create({'module':'megasanj','name':'discuss_channel','model':'discuss.channel','res_id':channel.id,'noupdate':True})
        else:
            channel.with_context(_ms_internal=INTERNAL).write({'image_128':icon})
            channel.with_context(_ms_internal=INTERNAL).add_members(partner_ids=senior.all_user_ids.partner_id.ids, post_joined_message=False)
        return channel

    def _add_members(self, **kwargs):
        self._ms_guard()
        return super()._add_members(**kwargs)

    def invite_by_email(self, emails):
        self._ms_guard()
        return super().invite_by_email(emails)

    def write(self, vals):
        if set(vals) & {'name','image_128','uuid','channel_member_ids','channel_partner_ids','group_public_id','group_ids','parent_channel_id','channel_type'}:
            self._ms_guard()
        return super().write(vals)

    def unlink(self):
        if self._ms_channels():
            raise UserError('گروه سیستمی مگاسنج حذف نمی‌شود.')
        return super().unlink()


class Member(models.Model):
    _inherit = 'discuss.channel.member'

    @api.model_create_multi
    def create(self, vals_list):
        self.env['discuss.channel'].browse([v.get('channel_id') for v in vals_list if v.get('channel_id')])._ms_guard()
        return super().create(vals_list)

    def unlink(self):
        protected = self.filtered(lambda m: m.channel_id._ms_channels() and m.partner_id != self.env.user.partner_id)
        protected.channel_id._ms_guard()
        return super().unlink()


class DiscrepancyNotification(models.Model):
    _inherit = 'megasanj.discrepancy'
    notification_message_id = fields.Many2one('mail.message', readonly=True, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._ms_notify()
        return records

    def _ms_notify(self):
        channel = self.env.ref('megasanj.discuss_channel')
        for rec in self:
            self.env.cr.execute('SELECT id FROM megasanj_discrepancy WHERE id=%s FOR UPDATE', (rec.id,))
            rec.invalidate_recordset(['notification_message_id'])
            if rec.notification_message_id:
                continue
            doc, wb = rec.document_id, rec.document_id.waybill_id
            body = Markup('<div dir="rtl"><b>صدور صورت‌جلسه مغایرت وزن: %s</b><br/>سند باسکول: %s ـ بارنامه: %s<br/>راننده: %s ـ پلاک: %s<br/>وزن مبدأ: %s کیلوگرم<br/>وزن مبنای مقایسه مقصد: %s کیلوگرم<br/>اختلاف: %s کیلوگرم ـ تلرانس: %s کیلوگرم<br/><a href="/odoo/megasanj.discrepancy/%s">مشاهده صورت‌جلسه و رسیدگی</a></div>') % tuple(escape(str(v)) for v in (rec.name,doc.name,wb.name,wb.driver_name,wb.trailer_plate,rec.origin_kg,rec.destination_kg,round(rec.destination_kg-rec.origin_kg,3),rec.tolerance_kg,rec.id))
            msg = channel.sudo().message_post(body=body, author_id=self.env.ref('base.partner_root').id, message_type='comment', subtype_xmlid='mail.mt_comment')
            super(DiscrepancyNotification, rec).write({'notification_message_id':msg.id})
