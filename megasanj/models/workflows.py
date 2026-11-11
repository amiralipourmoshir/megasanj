"""Native purchase/return/accounting links; no duplicate stock or payment ledger."""
import hashlib
import re
from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools.float_utils import float_compare
from .common import INTERNAL


def phone_key(value):
    value=str(value or '').translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩','01234567890123456789'))
    value=re.sub(r'\D','',value)
    if value.startswith('0098'):value='0'+value[4:]
    elif value.startswith('98') and len(value)==12:value='0'+value[2:]
    elif len(value)==10 and value.startswith('9'):value='0'+value
    return value


class Partner(models.Model):
    _inherit='res.partner'
    ms_phone_key=fields.Char(compute='_compute_ms_phone_key',store=True,index=True)

    @api.depends('phone')
    def _compute_ms_phone_key(self):
        for p in self:p.ms_phone_key=phone_key(p.phone) or False


class Waybill(models.Model):
    _inherit='megasanj.waybill'
    parent_purchase_id=fields.Many2one(related='picking_id.purchase_id',store=True,string='سفارش خرید',readonly=True)
    ms_returned=fields.Boolean('بار مرجوع شده',readonly=True,copy=False)

    @api.model
    def _ms_driver_partner(self,name,phone,company):
        key=phone_key(phone)
        if not key:raise ValidationError('شماره تماس معتبر راننده لازم است.')
        lock=int.from_bytes(hashlib.sha256(key.encode()).digest()[:4],'big',signed=True)
        self.env.cr.execute('SELECT pg_advisory_xact_lock(%s,%s)',(74183,lock))
        partners=self.env['res.partner'].sudo().with_context(active_test=False)
        partner=partners.search([('ms_phone_key','=',key),('company_id','in',[False,company.id])],order='id',limit=1)
        if partner:
            if not partner.active:partner.active=True
            return partner
        return partners.create({'name':name,'phone':key,'is_company':False,'company_id':company.id,'supplier_rank':0})

    def _ms_repair_driver_links(self):
        for wb in self:
            partner=wb._ms_driver_partner(wb.driver_name,wb.driver_phone,wb.company_id)
            wb.with_context(_ms_internal=INTERNAL).write({'driver_partner_id':partner.id})



class Purchase(models.Model):
    _inherit='purchase.order'
    ms_waybill_ids=fields.One2many('megasanj.waybill','parent_purchase_id',string='بارنامه‌های مگاسنج')
    ms_returned=fields.Boolean('بارهای مگاسنج مرجوع شده‌اند',compute='_compute_ms_returned',store=True)

    @api.depends('ms_waybill_ids.ms_returned')
    def _compute_ms_returned(self):
        for po in self:po.ms_returned=bool(po.ms_waybill_ids) and all(po.ms_waybill_ids.mapped('ms_returned'))

    def action_ms_return_all(self):
        if not self.env.user.has_group('megasanj.group_manager'):
            raise UserError('برگشت کامل بار نیازمند مدیر مگاسنج است.')
        for po in self:
            self.env.cr.execute('SELECT id FROM purchase_order WHERE id=%s FOR UPDATE',(po.id,))
            waybills=po.ms_waybill_ids
            if not waybills:raise UserError('این سفارش بارنامه مگاسنج ندارد.')
            tracked=waybills.document_ids.move_ids
            untracked=po.picking_ids.move_ids.filtered(lambda m:m.state=='done' and m.picking_id.picking_type_code=='incoming' and not m.origin_returned_move_id and m not in tracked and float_compare(sum(r.product_uom._compute_quantity(r.quantity,m.product_uom) for r in m.returned_move_ids if r.state=='done' and r.location_dest_id==m.location_id),m.quantity,precision_rounding=m.product_uom.rounding)<0)
            if untracked:
                raise UserError('این سفارش رسیدهای قطعی خارج از مگاسنج دارد؛ آن‌ها را ابتدا از گردش استاندارد انبار برگشت دهید.')
            docs=waybills.document_ids.filtered(lambda d:d.state!='void')
            for doc in docs:
                doc.void_reason='مرجوع کامل بار از سفارش خرید '+po.name
                doc.action_void()
            po.picking_ids.filtered(lambda p:p.state not in ('done','cancel')).action_cancel()
            waybills.with_context(_ms_internal=INTERNAL).write({'state':'cancelled','ms_returned':True,'reservation_token':False,'reservation_expires_at':False})
        return True

    def button_cancel(self):
        # Native cancellation alone leaves completed receipts in inventory.
        for po in self.filtered(lambda p:p.ms_waybill_ids):po.action_ms_return_all()
        return super().button_cancel()


class Document(models.Model):
    _inherit='megasanj.document'
    ms_returned=fields.Boolean('مرجوع‌شده',readonly=True,copy=False)

    def _ms_mark_returned(self):
        for doc in self.sorted('id'):
            self.env.cr.execute('SELECT id FROM megasanj_document WHERE id=%s FOR UPDATE',(doc.id,))
            doc.invalidate_recordset()
            doc._set({'state':'void','ms_returned':True,'error':False})
            wb=doc.waybill_id
            if doc.waybill_id:
                doc.waybill_id.with_context(_ms_internal=INTERNAL).write({'state':'cancelled','ms_returned':True,'reservation_token':False,'reservation_expires_at':False})
        # Financial obligations survive a physical return, including posted bills.


class Picking(models.Model):
    _inherit='stock.picking'

    def _action_done(self):
        returns=self.move_ids.filtered(lambda m:m.origin_returned_move_id.ms_document_id and m.location_dest_id==m.origin_returned_move_id.location_id)
        result=super()._action_done()
        for doc in returns.origin_returned_move_id.ms_document_id.sudo():
            originals=doc.move_ids.filtered(lambda m:m.state=='done' and not m.origin_returned_move_id and m.picking_id==doc.picking_id)
            if originals and all(float_compare(sum(r.product_uom._compute_quantity(r.quantity,m.product_uom) for r in m.returned_move_ids if r.state=='done' and r.location_dest_id==m.location_id),m.quantity,precision_rounding=m.product_uom.rounding)>=0 for m in originals):
                doc._set({'return_picking_id':self.filtered(lambda p:any(m.origin_returned_move_id.ms_document_id==doc for m in p.move_ids))[:1].id})
                doc._ms_mark_returned()
        return result


class Quality(models.Model):
    _inherit='quality.check'
    ms_load_returned=fields.Boolean(compute='_compute_ms_load_returned',string='بار مرجوع شده')

    @api.depends('ms_weighbridge_id.ms_returned','ms_document_id.ms_returned')
    def _compute_ms_load_returned(self):
        for q in self:q.ms_load_returned=bool((q.ms_document_id or q.ms_weighbridge_id).ms_returned)


class Freight(models.Model):
    _inherit='megasanj.freight'
    penalty_weight_kg=fields.Integer(related='document_id.discrepancy_id.penalty_weight_kg',string='وزن مشمول جریمه (کیلوگرم)')
    load_returned=fields.Boolean(related='document_id.ms_returned',string='بار مرجوع شده؛ کرایه نیازمند تعیین تکلیف مالی است')

    @api.depends('amount','deduction','bill_id.ms_freight_deduction')
    def _compute_payable(self):
        for r in self:r.payable=r.amount-(r.bill_id.ms_freight_deduction if r.bill_id else r.deduction)

    def action_approve(self):
        result=super().action_approve()
        if len(self)==1 and self.bill_id:
            return {'type':'ir.actions.act_window','res_model':'account.move','res_id':self.bill_id.id,'view_mode':'form','target':'current'}
        return result


class AccountMove(models.Model):
    _inherit='account.move'
    ms_freight_id=fields.Many2one('megasanj.freight',string='سند کرایه مگاسنج',readonly=True,copy=False,ondelete='restrict',check_company=True)
    ms_penalty_weight_kg=fields.Integer(related='ms_freight_id.penalty_weight_kg',string='وزن مشمول جریمه (کیلوگرم)',readonly=True)
    ms_load_returned=fields.Boolean(related='ms_freight_id.load_returned',string='بار مرجوع شده',readonly=True)
    ms_freight_deduction=fields.Monetary('کسر مبلغ کرایه (سابقه قدیمی)',copy=False,readonly=True)
    ms_freight_deduction_reason=fields.Char('شرح کسر مبلغ (سابقه قدیمی)',copy=False,readonly=True)

class ReturnLine(models.TransientModel):
    _inherit='stock.return.picking.line'

    def _prepare_move_default_values(self,new_picking):
        vals=super()._prepare_move_default_values(new_picking)
        if self.move_id.ms_document_id:
            vals.update(location_id=self.move_id.location_dest_id.id,location_dest_id=self.move_id.location_id.id)
        return vals

    def _ms_remaining_qty(self):
        self.ensure_one()
        move=self.move_id
        returned=sum(r.product_uom._compute_quantity(r.quantity if r.state=='done' else r.product_uom_qty,move.product_uom) for r in move.returned_move_ids if r.state!='cancel' and r.location_dest_id==move.location_id)
        return max(0,move.quantity-returned)

    def _process_line(self,new_picking):
        result=super()._process_line(new_picking)
        for line in self.filtered(lambda l:l.move_id.ms_document_id):
            moves=new_picking.move_ids.filtered(lambda m:m.origin_returned_move_id==line.move_id)
            moves.with_context(_ms_internal=INTERNAL).write({'ms_document_id':line.move_id.ms_document_id.id,'to_refund':True})
        return result


class Discrepancy(models.Model):
    _inherit='megasanj.discrepancy'
    ms_load_returned=fields.Boolean(related='document_id.ms_returned',string='بار مرجوع شده',readonly=True)


class ReturnPicking(models.TransientModel):
    _inherit='stock.return.picking'

    def _create_return(self):
        picking=super()._create_return()
        moves=picking.move_ids
        if moves.filtered('ms_document_id') and len(moves.location_id)==1 and len(moves.location_dest_id)==1:
            picking.write({'location_id':moves.location_id.id,'location_dest_id':moves.location_dest_id.id})
        return picking


class AccountMoveLine(models.Model):
    _inherit='account.move.line'
    ms_freight_base_line=fields.Boolean('ردیف پایه کرایه مگاسنج',readonly=True,copy=False)


class Company(models.Model):
    _inherit='res.company'

    def _ms_ensure_freight_product(self):
        self.ensure_one()
        self.env.cr.execute('SELECT pg_advisory_xact_lock(%s,%s)',(74184,self.id))
        self.invalidate_recordset(['ms_freight_product_id'])
        product=self.ms_freight_product_id
        if not product:
            products=self.env['product.product'].sudo().with_context(active_test=False)
            product=products.search([('default_code','=','MS-FREIGHT-SERVICE'),('type','=','service'),('company_id','in',[False,self.id])],order='id',limit=1)
            if not product:
                product=products.with_company(self).create({'name':'خدمات حمل‌ونقل مگاسنج','default_code':'MS-FREIGHT-SERVICE','type':'service','sale_ok':False,'purchase_ok':True,'company_id':self.id})
            self.sudo().ms_freight_product_id=product
        if product.type!='service' or (product.company_id and product.company_id!=self):
            raise UserError('محصول کرایه شرکت باید از نوع خدمت و متعلق به همین شرکت باشد.')
        if not product.active:product.sudo().active=True
        product.sudo().write({'name':'خدمات حمل‌ونقل مگاسنج','landed_cost_ok':True,'split_method_landed_cost':'by_quantity','purchase_method':'purchase'})
        return product

    @api.model
    def _ms_setup_freight_products(self):
        for company in self.sudo().search([]):company._ms_ensure_freight_product()
