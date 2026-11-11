"""Optional review fixtures. Run through `odoo-bin shell` ONLY on the isolated test DB."""
assert env.cr.dbname == 'megasanj_test_20261029', 'Review fixtures must never run on the business database'
from odoo import Command

env.ref('base.user_admin').group_ids += env.ref('megasanj.group_manager')
env=env(user=env.ref('base.user_admin').id)
category = env['product.category'].search([('ms_code','=','scrap')],limit=1)
if not category:
    category=env['product.category'].create({'name':'قراضه آهن — نمونه مگاسنج','ms_code':'scrap','ms_enabled':True})
    policy=env['megasanj.policy'].create({'category_id':category.id,'calculator':'scrap_v1','quality_required':True,
        'quality_enabled':True,'inventory_requires_discrepancy_closed':True,'allow_direct_production':True})
    policy.action_publish()
    kg=env.ref('uom.product_uom_kgm')
    kg.rounding=.001
    products=env['product.product'].create([{'name':n,'type':'consu','is_storable':True,'uom_id':kg.id,
        'categ_id':category.id,'ms_weighable':True} for n in ['قراضه سبک — نمونه','قراضه سنگین — نمونه']])
    cargo=env['product.product'].create({'name':'محموله قراضه — نمونه','type':'consu','is_storable':True,'uom_id':kg.id,'categ_id':category.id})
    partner=env['res.partner'].create({'name':'تأمین‌کننده آزمایشی مگاسنج','is_company':True,'supplier_rank':1})
    env['megasanj.eligibility'].create({'partner_id':partner.id,'category_id':category.id})
    warehouse=env['stock.warehouse'].search([('company_id','=',env.company.id)],limit=1)
    warehouse.lot_stock_id.write({'ms_category_id':category.id,'ms_receiving_default':True})
    env['megasanj.checkpoint'].create({'name':'بررسی اسناد و مجوز خروج انتظامات'})
    point=env['quality.point'].create({'title':'ترکیب درصدی محموله ورودی','test_type_id':env.ref('megasanj.test_type_percentage').id,
        'measure_on':'operation','picking_type_ids':[Command.set(warehouse.in_type_id.ids)],'ms_category_id':category.id,
        'ms_loss':1,'ms_rejected':2,'ms_line_ids':[Command.create({'product_id':p.id,'percentage':pct}) for p,pct in zip(products,[60,40])]})
    po=env['purchase.order'].create({'partner_id':partner.id,'order_line':[Command.create({'product_id':cargo.id,
        'product_qty':30000,'product_uom_id':kg.id,'price_unit':10})]})
    po.button_confirm()
    picking=po.picking_ids[:1]
    wb=env['megasanj.waybill'].create({'picking_id':picking.id,'purchase_line_id':po.order_line.id,'driver_name':'راننده آزمایشی',
        'driver_phone':'09120000000','trailer_plate':'35ط455-23','origin_province':'تهران','origin_city':'تهران',
        'origin_net_weight_kg':25000,'freight_by_contractor':True})
    wb.action_call(); wb.action_enter()
    checks=picking.check_ids.filtered(lambda q:q.test_type == 'megasanj_percentage')
    checks.action_ms_release()
    finished=env['product.product'].create({'name':'محصول تولیدی آزمایشی مگاسنج','type':'consu','is_storable':True,'uom_id':kg.id})
    bom=env['mrp.bom'].create({'product_tmpl_id':finished.product_tmpl_id.id,'product_qty':1000,'product_uom_id':kg.id,
        'bom_line_ids':[Command.create({'product_id':p.id,'product_qty':qty,'product_uom_id':kg.id}) for p,qty in zip(products,[600,400])]})
    mo=env['mrp.production'].create({'product_id':finished.id,'bom_id':bom.id,'product_qty':1000,'product_uom_id':kg.id,
        'location_src_id':warehouse.lot_stock_id.id})
    mo.action_confirm()
env.cr.commit()
print('Isolated review fixtures ready; no business database touched')
