{
    'name': 'مگاسنج',
    'author': 'MegaSanj',
    'summary': 'توزین محموله از رسید خرید تا مصرف واقعی سفارش تولید',
    'version': '19.0.1.9.0',
    'category': 'Inventory/Inventory',
    'license': 'LGPL-3',
    'depends': ['purchase_stock', 'stock_landed_costs', 'mrp_account', 'quality_mrp_workorder'],
    'data': ['security/security.xml', 'security/ir.model.access.csv', 'security/rules.xml',
             'data/data.xml', 'views/views.xml', 'views/workflows.xml', 'report/reports.xml', 'data/native_freight.xml'],
    'application': True,
    'installable': True,
}
