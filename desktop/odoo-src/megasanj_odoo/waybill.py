import json
from weighbridge_app.ui import WaybillDialog


class OdooWaybillDialog(WaybillDialog):
    """Freight responsibility is supplied by the purchase receipt, never the operator."""
    def __init__(self, db, receipt, parent=None):
        saved=json.loads(db.get_setting('waybill_draft') or '{}')
        if saved.get('event_id'):
            with db.connection() as con:
                confirmed=con.execute('SELECT 1 FROM waybills WHERE event_id=? AND server_confirmed=1',(saved['event_id'],)).fetchone()
            if confirmed:db.set_setting('waybill_draft','')
        super().__init__(db,parent)
        contractor=bool(receipt['freight_by_contractor'])
        self.freight_by_contractor.setChecked(contractor)
        self.freight_by_contractor.setEnabled(False)
        self.freight_by_contractor.hide()
        self.freight_field.setVisible(not contractor)
        if contractor:self.freight.setValue(0)
