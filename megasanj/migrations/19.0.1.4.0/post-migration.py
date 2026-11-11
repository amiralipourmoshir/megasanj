from odoo import api, SUPERUSER_ID
from odoo.addons.megasanj.models.common import INTERNAL


def migrate(cr, version):
    env=api.Environment(cr,SUPERUSER_ID,{})
    env['megasanj.waybill'].search([])._ms_repair_driver_links()
    for freight in env['megasanj.freight'].search([('bill_id','!=',False)]):
        if not freight.bill_id.ms_freight_id:freight.bill_id.ms_freight_id=freight
        if freight.deduction and freight.penalty_weight_kg>0:
            from odoo.addons.megasanj.models.workflows import AccountMove
            super(AccountMove,freight.bill_id).write({'ms_freight_deduction':freight.deduction,'ms_freight_deduction_reason':'انتقال تصمیم مالی ثبت‌شده در نسخه قبلی'})
    docs=env['megasanj.document'].search([('state','=','void')])
    docs._ms_mark_returned()
    env['megasanj.freight'].search([('state','=','void'),('document_id','in',docs.ids),('bill_id','=',False)]).with_context(_ms_internal=INTERNAL).write({'state':'pending'})
    messages=env['megasanj.discrepancy'].search([]).notification_message_id
    messages.write({'author_id':env.ref('base.partner_root').id})
