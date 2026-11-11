def main():
    import os
    os.environ['MEGASANJ_EDITION'] = 'odoo'
    from weighbridge_app.main import main as shared_main
    return shared_main()
