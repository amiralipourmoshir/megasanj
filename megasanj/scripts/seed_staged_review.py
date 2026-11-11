"""Isolated database only. Prepare one real receipt for the HTTP/desktop round trip."""
assert env.cr.dbname == 'megasanj_test_20261029'
import json
from pathlib import Path
from odoo import Command
env=env(user=env.ref('base.user_admin').id)
category=env['product.category'].search([('ms_code','=','scrap')],limit=1)
assert category and category.ms_policy_id
link=env['megasanj.eligibility'].search([('category_id','=',category.id),('company_id','=',env.company.id),('active','=',True)],limit=1)
cargo=env['product.product'].search([('categ_id','=',category.id),('is_storable','=',True),('ms_weighable','=',False)],limit=1)
assert link and cargo
po=env['purchase.order'].search([('origin','=','MS-GROSS-REVIEW-20261030-R1')],limit=1)
if not po:
    po=env['purchase.order'].create({'partner_id':link.partner_id.id,'origin':'MS-GROSS-REVIEW-20261030-R1',
        'order_line':[Command.create({'product_id':cargo.id,'product_qty':30000,'product_uom_id':env.ref('uom.product_uom_kgm').id,'price_unit':0})]})
    po.button_confirm()
    wb=env['megasanj.waybill'].create({'picking_id':po.picking_ids.id,'purchase_line_id':po.order_line.id,
        'driver_name':'راننده آزمایش گردش مرحله‌ای','driver_phone':'09120000000','trailer_plate':'35ط455-23',
        'origin_province':'تهران','origin_city':'تهران','origin_net_weight_kg':30000,'freight_by_contractor':True})
    wb.action_call();wb.action_enter()
else:
    wb=env['megasanj.waybill'].search([('picking_id','in',po.picking_ids.ids)],limit=1)
location=env['stock.location'].search([('ms_category_id','=',category.id),('ms_receiving_default','=',True),('company_id','=',env.company.id)],limit=1)
fixture={'waybill':env['megasanj.api']._waybill_contract(wb),'destination_id':location.id,'quality_action_id':env.ref('megasanj.action_quality').id,'document_action_id':env.ref('megasanj.action_documents').id}
Path('/tmp/megasanj-staged-fixture.json').write_text(json.dumps(fixture,ensure_ascii=False))
env['ir.config_parameter'].sudo().set_param('report.url','http://127.0.0.1:18089')
env['ir.config_parameter'].sudo().set_param('web.base.url','http://192.168.1.8:18089')
env['ir.config_parameter'].sudo().set_param('web.base.url.freeze','True')
env.cr.commit()
print('STAGED_REVIEW_READY',po.name,po.picking_ids.name,wb.name)
