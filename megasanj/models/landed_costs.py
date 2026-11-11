"""Bridge a waybill's freight to native Purchase, Bills and Landed Costs.

No separate freight approval, payment or valuation document is created here.
Legacy financial records remain read-only for accounting history.
"""
from odoo import api, fields, models, Command
from odoo.exceptions import UserError
from .common import INTERNAL


class Waybill(models.Model):
    _inherit = 'megasanj.waybill'

    freight_line_ids = fields.One2many('purchase.order.line', 'ms_waybill_id', string='خدمت حمل در سفارش خرید', readonly=True)

    def _ms_apply_landed_freight(self):
        for wb in self:
            if wb.freight_by_contractor or wb.freight_amount <= 0 or wb.ms_returned:
                continue
            self.env.cr.execute('SELECT id FROM megasanj_waybill WHERE id=%s FOR UPDATE', (wb.id,))
            docs=wb.document_ids.filtered(lambda d:d.state=='posted')
            receipts=docs.picking_id.filtered(lambda p:p.state=='done')
            if not receipts:continue
            if docs.move_ids.filtered(lambda m:m.picking_id in receipts and m.state=='done' and m.product_id.cost_method not in ('average','fifo')):
                raise UserError('برای اعمال خودکار کرایه، روش بهای تمام‌شده اقلام رسید باید میانگین موزون یا FIFO باشد؛ دسته‌بندی کالا را در اودو بررسی کنید.')
            costs=self.env['stock.landed.cost'].sudo().with_company(wb.company_id).with_context(_ms_internal=INTERNAL)
            cost=costs.search([('ms_waybill_id','=',wb.id)],limit=1)
            if cost and cost.state=='done':continue
            wb._ms_sync_purchase_freight()
            product=wb.company_id._ms_ensure_freight_product()
            bills=wb.freight_line_ids.invoice_lines.move_id.filtered(lambda b:b.move_type=='in_invoice' and b.state!='cancel')
            bills|=self.env['megasanj.freight'].sudo().search([('document_id.waybill_id','=',wb.id)]).bill_id.filtered(lambda b:b.move_type=='in_invoice' and b.state!='cancel')
            if not cost:
                existing=costs.search([('picking_ids','in',receipts.ids),('cost_lines.product_id','=',product.id),('state','!=','cancel')])
                if existing:
                    if len(existing)!=1 or existing.picking_ids!=receipts or len(existing.cost_lines)!=1 or not wb.currency_id.is_zero(existing.amount_total-wb.freight_amount):
                        raise UserError('هزینه جانبی حمل قبلاً برای این رسید ثبت شده است؛ مبلغ و رسید آن را در حسابداری بررسی کنید.')
                    cost=existing
                    cost.ms_waybill_id=wb
                else:
                    if not costs._default_account_journal_id():
                        raise UserError('دفتر روزنامه هزینه‌های جانبی خرید تنظیم نشده است؛ تنظیمات ارزش‌گذاری انبار را تکمیل کنید.')
                    accounts=product.sudo().with_company(wb.company_id).product_tmpl_id.get_product_accounts()
                    cost=costs.create({'company_id':wb.company_id.id,'ms_waybill_id':wb.id,
                        'description':'کرایه بارنامه %s · %s'%(wb.name,wb.parent_purchase_id.name),
                        'picking_ids':[Command.set(receipts.ids)],
                        'vendor_bill_id':bills.id if len(bills)==1 else False,
                        'cost_lines':[Command.create({'product_id':product.id,'name':product.name,
                            'account_id':accounts['stock_valuation'].id,'price_unit':wb.freight_amount,'split_method':'by_quantity'})]})
            if cost.state=='cancel':
                raise UserError('هزینه جانبی این بارنامه لغو شده است؛ در حسابداری تعیین تکلیف کنید.')
            if cost.state!='done':
                cost.compute_landed_cost()
                cost.button_validate()

    def _ms_sync_purchase_freight(self):
        for wb in self:
            wb.env.cr.execute('SELECT id FROM megasanj_waybill WHERE id=%s FOR UPDATE', (wb.id,))
            lines = self.env['purchase.order.line'].sudo().search([('ms_waybill_id', '=', wb.id)])
            legacy = self.env['megasanj.freight'].sudo().search([('document_id.waybill_id', '=', wb.id), ('bill_id', '!=', False)])
            # Never recreate or rewrite a historical payable (possibly paid).
            if legacy.filtered(lambda f:f.bill_id.state == 'posted'):
                continue
            if wb.freight_by_contractor or not wb.freight_amount:
                if lines.invoice_lines:
                    raise UserError('کرایه صورتحساب دارد؛ اصلاح را از حسابداری انجام دهید.')
                if po := wb.parent_purchase_id:
                    if po.state == 'purchase':
                        # Native Odoo prohibits deleting confirmed PO lines.
                        lines.write({'product_qty':0,'price_unit':0})
                    else:
                        lines.unlink()
                continue
            po = wb.parent_purchase_id
            if po.state == 'cancel':
                continue
            if lines:
                if lines.invoice_lines:
                    continue
                lines.write({'product_qty':1,'price_unit':wb.currency_id._convert(wb.freight_amount, po.currency_id, wb.company_id, fields.Date.to_date(wb.create_date))})
                continue
            product = wb.company_id._ms_ensure_freight_product()
            # Narrow sudo: only this company's linked PO, native service, and
            # the amount already validated by the waybill workflow.
            self.env['purchase.order.line'].sudo().with_company(wb.company_id).with_context(_ms_internal=INTERNAL).create({
                'order_id': po.id, 'product_id': product.id, 'ms_waybill_id': wb.id,
                'name': '%s · بارنامه %s' % (product.name, wb.name),
                'product_qty': 1, 'product_uom_id': product.uom_id.id,
                'price_unit': wb.currency_id._convert(wb.freight_amount, po.currency_id, wb.company_id, fields.Date.to_date(wb.create_date)),
                'date_planned': fields.Datetime.now(),
            })

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._ms_sync_purchase_freight()
        return records

    def write(self, vals):
        if 'freight_amount' in vals and self.filtered(lambda w:w.freight_amount != vals['freight_amount'] and w.freight_line_ids.invoice_lines):
            raise UserError('کرایه صورتحساب دارد؛ اصلاح را از حسابداری انجام دهید.')
        result = super().write(vals)
        if {'freight_amount', 'freight_by_contractor'}.intersection(vals):
            self._ms_sync_purchase_freight()
        return result


class PurchaseLine(models.Model):
    _inherit = 'purchase.order.line'
    ms_waybill_id = fields.Many2one('megasanj.waybill', string='بارنامه حمل', copy=False, readonly=True, ondelete='restrict', check_company=True)
    _ms_waybill_freight_unique = models.Constraint('unique(ms_waybill_id)', 'خدمت حمل این بارنامه قبلاً در خرید ثبت شده است.')

    @api.model_create_multi
    def create(self, vals_list):
        if any(v.get('ms_waybill_id') for v in vals_list) and self.env.context.get('_ms_internal') is not INTERNAL:
            raise UserError('ارتباط خدمت حمل و بارنامه از گردش مگاسنج تعیین می‌شود.')
        return super().create(vals_list)

    def write(self, vals):
        if 'ms_waybill_id' in vals and self.env.context.get('_ms_internal') is not INTERNAL:
            raise UserError('ارتباط خدمت حمل و بارنامه ثابت است.')
        return super().write(vals)

    def _prepare_account_move_line(self, move=False):
        vals = super()._prepare_account_move_line(move=move)
        if self.ms_waybill_id:
            vals['is_landed_costs_line'] = True
        return vals


class Bill(models.Model):
    _inherit = 'account.move'

    @api.model_create_multi
    def create(self, vals_list):
        bills=super().create(vals_list)
        for bill in bills.filtered(lambda b:b.move_type=='in_invoice'):
            costs=self.env['stock.landed.cost'].sudo().search([('ms_waybill_id','in',bill.invoice_line_ids.purchase_line_id.ms_waybill_id.ids),('vendor_bill_id','=',False)])
            costs.write({'vendor_bill_id':bill.id})
        return bills

    def button_create_landed_costs(self):
        self.ensure_one()
        if self.move_type!='in_invoice':return super().button_create_landed_costs()
        self.env.cr.execute('SELECT id FROM account_move WHERE id=%s FOR UPDATE', (self.id,))
        self.invalidate_recordset(['landed_costs_ids'])
        lines = self.invoice_line_ids.filtered('is_landed_costs_line')
        waybills = lines.purchase_line_id.ms_waybill_id
        if not waybills:
            waybills = self.env['megasanj.freight'].sudo().search([('bill_id','=',self.id)]).document_id.waybill_id
        auto=self.env['stock.landed.cost'].search([('ms_waybill_id','in',waybills.ids)])
        if auto:
            auto.filtered(lambda c:not c.vendor_bill_id).write({'vendor_bill_id':self.id})
            return auto._get_records_action(name='هزینه‌های جانبی حمل',views=[(False,'form')] if len(auto)==1 else [(False,'list'),(False,'form')])
        if waybills and self.landed_costs_ids:
            return self.action_view_landed_costs()
        result = super().button_create_landed_costs()
        # A single load can be identified unambiguously. Mixed bills retain
        # native selection/allocation so unrelated costs are not misassigned.
        if len(waybills) == 1 and (self.ms_freight_id or all(l.purchase_line_id.ms_waybill_id == waybills for l in lines)):
            pickings = waybills.document_ids.filtered(lambda d: d.state == 'posted').picking_id.filtered(lambda p: p.state == 'done')
            if pickings:
                self.env['stock.landed.cost'].browse(result['res_id']).picking_ids = [Command.set(pickings.ids)]
        return result


class Migration(models.Model):
    _inherit = 'res.company'

    @api.model
    def _ms_migrate_native_freight(self):
        """Idempotent conversion; posted/draft historical bills stay intact."""
        self._ms_setup_freight_products()
        waybills = self.env['megasanj.waybill'].sudo().search([])
        waybills._ms_sync_purchase_freight()
        for freight in self.env['megasanj.freight'].sudo().search([('bill_id','!=',False)]):
            bill=freight.bill_id
            lines=bill.invoice_line_ids.filtered(lambda l:l.ms_freight_base_line or l.product_id==freight.company_id.ms_freight_product_id)
            if len(lines)!=1:
                raise UserError('ردیف کرایه صورتحساب قدیمی %s مشخص نیست؛ مهاجرت متوقف شد.' % bill.display_name)
            if bill.state=='draft':
                po=freight.document_id.waybill_id.parent_purchase_id
                bill.partner_id=po.partner_id
                lines.purchase_line_id=freight.document_id.waybill_id.freight_line_ids
            lines.is_landed_costs_line=True
        legacy = self.env['megasanj.freight'].sudo().search([('state', '=', 'pending'), ('bill_id', '=', False)])
        legacy.with_context(_ms_internal=INTERNAL).write({'state': 'void'})
        # Remove the artificial vendor classification, even where an old
        # MegaSanj bill exists: the posted ledger remains untouched. Preserve
        # actual vendors with other purchases/bills or supplier eligibility.
        legacy_bills=self.env['megasanj.freight'].sudo().search([]).bill_id.ids
        for partner in waybills.driver_partner_id.filtered(lambda p: p.supplier_rank > 0):
            if not self.env['purchase.order'].sudo().search_count([('partner_id', '=', partner.id)]) and not self.env['account.move'].sudo().search_count([('partner_id', '=', partner.id), ('id','not in',legacy_bills), ('move_type', 'in', ['in_invoice', 'in_refund'])]) and not self.env['megasanj.eligibility'].sudo().search_count([('partner_id', '=', partner.id)]):
                partner.supplier_rank = 0
        waybills._ms_apply_landed_freight()


class Document(models.Model):
    _inherit='megasanj.document'

    def action_post(self):
        result=super().action_post()
        self.filtered(lambda d:d.state=='posted').waybill_id._ms_apply_landed_freight()
        return result


class LandedCost(models.Model):
    _inherit='stock.landed.cost'
    ms_waybill_id=fields.Many2one('megasanj.waybill',string='بارنامه مگاسنج',copy=False,readonly=True,ondelete='restrict',check_company=True)
    _ms_waybill_unique=models.Constraint('unique(ms_waybill_id)','هزینه جانبی این بارنامه قبلاً ثبت شده است.')

    @api.model_create_multi
    def create(self,vals_list):
        if any(v.get('ms_waybill_id') for v in vals_list) and self.env.context.get('_ms_internal') is not INTERNAL:
            raise UserError('ارتباط هزینه جانبی با بارنامه از گردش مگاسنج ثبت می‌شود.')
        return super().create(vals_list)

    def write(self,vals):
        if 'ms_waybill_id' in vals and self.env.context.get('_ms_internal') is not INTERNAL:
            raise UserError('ارتباط هزینه جانبی و بارنامه ثابت است.')
        return super().write(vals)
