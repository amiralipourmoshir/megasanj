"""Odoo-specific selections; capture, documents, printing and durable delivery stay shared."""
import hashlib
import json
from pathlib import Path
import webbrowser
import requests
import copy
from .capture import CaptureStore, CaptureDialogMixin
from .waybill import OdooWaybillDialog
from .documents_board import OdooDocumentsMixin
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QLabel, QComboBox, QHBoxLayout, QPushButton, QInputDialog, QMessageBox
from weighbridge_app.database import Database
from weighbridge_app.ui import MainWindow, DocumentDialog, WaybillDialog, message


def fetch(db, path):
    if not db.session:
        raise ValueError('ابتدا وارد اودو شوید.')
    response = requests.get(db.session.api_url(path),headers={'Authorization':'Bearer '+db.session.token,'Accept':'application/json'},timeout=12,allow_redirects=False)
    payload = response.json()
    if response.status_code != 200 or not payload.get('success'):
        raise ValueError(str(payload.get('message') or 'دریافت اطلاعات اودو انجام نشد.'))
    return payload['data']


class OdooDatabase(CaptureStore, Database):
    erp_label='اودو'
    app_icon_path=Path(__file__).resolve().parent/'assets/icon.png'
    queue_capability='queue'
    def __init__(self, path):
        super().__init__(path)
        self.migrate_captures()
        with self.connection() as con:
            con.executescript('''
              CREATE TABLE IF NOT EXISTS odoo_waybill_context(event_id TEXT PRIMARY KEY,picking_id INTEGER NOT NULL,purchase_line_id INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS odoo_document_context(document_id INTEGER PRIMARY KEY,production_id INTEGER);
              CREATE TABLE IF NOT EXISTS odoo_server_documents(event_id TEXT PRIMARY KEY,metadata_json TEXT NOT NULL);
            ''')

    def bind_session(self,session):
        if session.user.get('edition') != 'odoo19':
            raise ValueError('این نسخه فقط به ماژول مگاسنج اودو ۱۹ متصل می‌شود.')
        super().bind_session(session)
        with self.connection() as con:
            con.execute("UPDATE endpoints SET name=replace(name,'مگاپالس','اودو')")

    def bind_receipt(self,event_id,picking_id,line_id):
        with self.connection() as con:
            row=con.execute('SELECT picking_id,purchase_line_id FROM odoo_waybill_context WHERE event_id=?',(event_id,)).fetchone()
            if row and (row['picking_id'],row['purchase_line_id']) != (picking_id,line_id):
                raise ValueError('برای تلاش مجدد همان بارنامه، رسید قبلی را حفظ کنید؛ تغییر رسید نیازمند بارنامه تازه است.')
            con.execute('INSERT OR IGNORE INTO odoo_waybill_context VALUES(?,?,?)',(event_id,picking_id,line_id))

    def waybill_payload_from_data(self,data):
        payload=super().waybill_payload_from_data(data)
        with self.connection() as con:
            row=con.execute('SELECT picking_id,purchase_line_id FROM odoo_waybill_context WHERE event_id=?',(data['event_id'],)).fetchone()
        if not row: raise ValueError('رسید و ردیف خرید این بارنامه انتخاب نشده است.')
        return dict(payload,picking_id=row['picking_id'],purchase_line_id=row['purchase_line_id'])

    def reconcile_server_cache(self):
        """Collect a complete authoritative snapshot before reconciling reused numbers."""
        if not self.session:raise ValueError('نشست اودو لازم است.')
        data={'waybills':[],'documents':[]};cursor='';seen=set();sync=None
        while True:
            params={'snapshot':1}
            if cursor:params['cursor']=cursor
            response=requests.get(self.transportation_sync_url(),params=params,headers={'Authorization':'Bearer '+self.session.token,'Accept':'application/json'},timeout=20,allow_redirects=False)
            from weighbridge_app.services import safe_response_error
            if response.status_code!=200:raise ValueError(safe_response_error(response))
            page=response.json()
            if page.get('success') is not True:raise ValueError('دریافت داده معتبر از اودو انجام نشد.')
            sync=page['sync']
            for key in data:data[key].extend((page.get('data') or {}).get(key,[]))
            if not sync.get('has_more'):break
            cursor=sync.get('cursor')
            if not cursor or cursor in seen:raise ValueError('صفحه‌بندی همگام‌سازی معتبر نیست.')
            seen.add(cursor)
        self.apply_transportation_sync({'success':True,'data':data,'sync':sync},complete_snapshot=True)

    def create_waybill(self,data,official_number=None):
        if official_number:
            with self.connection() as con:
                conflict=con.execute('SELECT event_id,server_confirmed FROM waybills WHERE number=?',(official_number,)).fetchone()
            if conflict and conflict['server_confirmed'] and conflict['event_id']!=data['event_id']:
                self.reconcile_server_cache()
        return super().create_waybill(data,official_number)

    def set_production(self,document_id,production_id):
        with self.connection() as con:
            doc=con.execute('SELECT status FROM advanced_documents WHERE id=?',(document_id,)).fetchone()
            if not doc or doc['status'] == 'finalized': raise ValueError('سند نهایی قابل تغییر نیست.')
            con.execute('INSERT INTO odoo_document_context VALUES(?,?) ON CONFLICT(document_id) DO UPDATE SET production_id=excluded.production_id',(document_id,production_id))

    def production_for(self,document_id):
        with self.connection() as con:
            row=con.execute('SELECT production_id FROM odoo_document_context WHERE document_id=?',(document_id,)).fetchone()
            return row[0] if row else None

    def _advanced_payload(self,con,doc_id,finalized_at):
        payload=super()._advanced_payload(con,doc_id,finalized_at)
        movement=payload['document']['movement_type']
        if payload.get('schema_version') != 3:
            raise ValueError('ابتدا اطلاعات مرجع نسخه ۳ اودو را دریافت کنید.')
        if movement in ('warehouse_to_production','external_to_production'):
            row=con.execute('SELECT production_id FROM odoo_document_context WHERE document_id=?',(doc_id,)).fetchone()
            if not row or not row[0]: raise ValueError('سفارش تولید اودو را انتخاب کنید.')
            payload['production_id']=int(row[0])
        return payload

    def mark_event_delivery(self, delivery_id, ok, code, error='', **kwargs):
        # Shared worker owns retries. Preserve the normal status behavior.
        return super().mark_event_delivery(delivery_id,ok,code,error,**kwargs)

    def save_event_weight_discrepancy(self,delivery_id,response):
        super().save_event_weight_discrepancy(delivery_id,response)
        if response.get('event_id'):
            with self.connection() as con:
                con.execute('INSERT INTO odoo_server_documents VALUES(?,?) ON CONFLICT(event_id) DO UPDATE SET metadata_json=excluded.metadata_json',
                            (response['event_id'],json.dumps(response,ensure_ascii=False)))

    def apply_transportation_sync(self,payload,complete_snapshot=False):
        payload=copy.deepcopy(payload)
        original_documents=list((payload.get('data') or {}).get('documents',[]))
        # A stale server snapshot must never erase a newer offline weighing/outbox.
        with self.connection() as con:
            pending={r[0] for r in con.execute("SELECT DISTINCT d.event_id FROM advanced_documents d JOIN webhook_events w ON w.document_id=d.id JOIN event_deliveries e ON e.event_id=w.id WHERE e.status!='synced'")}
            outbound_pending=set(pending)
            pending.update(r[0] for r in con.execute("SELECT event_id FROM advanced_documents d WHERE cache_source='local' AND status!='finalized' AND NOT EXISTS (SELECT 1 FROM odoo_capture_events c WHERE c.document_id=d.id)"))
        with self.connection() as con:
            for item in original_documents:
                if item.get('odoo_state') not in (None,'draft') and item.get('event_id') not in outbound_pending:
                    pending.discard(item.get('event_id'))
                if item.get('deleted_at') or item.get('odoo_state')=='void':
                    pending.discard(item.get('event_id'))
                    con.execute("UPDATE advanced_documents SET status='finalized' WHERE event_id=?",(item.get('event_id'),))
        if payload.get('data'):
            payload['data']['documents']=[d for d in payload['data'].get('documents',[]) if d.get('event_id') not in pending]
        result=super().apply_transportation_sync(payload,complete_snapshot)
        with self.connection() as con:
            for wb in (payload.get('data') or {}).get('waybills',[]):
                if wb.get('event_id') and wb.get('picking_id') and wb.get('purchase_line_id'):
                    con.execute('INSERT INTO odoo_waybill_context VALUES(?,?,?) ON CONFLICT(event_id) DO UPDATE SET picking_id=excluded.picking_id,purchase_line_id=excluded.purchase_line_id',(wb['event_id'],wb['picking_id'],wb['purchase_line_id']))
            for doc in original_documents:
                if doc.get('workflow_version',0)>=2 and doc.get('odoo_state') in ('gross','tare') and doc.get('event_id') not in pending and doc.get('capture_payload'):
                    local=con.execute('SELECT id FROM advanced_documents WHERE event_id=?',(doc['event_id'],)).fetchone()
                    if local:
                        con.execute('INSERT OR IGNORE INTO odoo_capture_events VALUES(?,?,?)',(local[0],1,json.dumps(doc['capture_payload'],ensure_ascii=False,sort_keys=True)))
                        if doc.get('production_id'):con.execute('INSERT OR IGNORE INTO odoo_document_context VALUES(?,?)',(local[0],doc['production_id']))
            for doc in (payload.get('data') or {}).get('documents',[]):
                if doc.get('event_id'):
                    con.execute('INSERT INTO odoo_server_documents VALUES(?,?) ON CONFLICT(event_id) DO UPDATE SET metadata_json=excluded.metadata_json',
                                (doc['event_id'],json.dumps(doc,ensure_ascii=False)))
        return result

    def update_draft(self,doc_id,data):
        result=super().update_draft(doc_id,data)
        with self.connection() as con:con.execute("UPDATE advanced_documents SET cache_source='local' WHERE id=?",(doc_id,))
        return result

    def odoo_status(self,event_id):
        with self.connection() as con:
            row=con.execute('SELECT metadata_json FROM odoo_server_documents WHERE event_id=?',(event_id,)).fetchone()
            return json.loads(row[0]) if row else {}


class OdooDocumentDialog(CaptureDialogMixin, DocumentDialog):
    def __init__(self,db,parent=None,existing=None,document_id=None,**kwargs):
        self._staged=False
        ident=document_id or (existing['id'] if existing else None)
        weights,_=db.document_parts(ident) if ident else ([],[])
        with db.connection() as con:
            captured=bool(ident and con.execute('SELECT 1 FROM odoo_capture_events WHERE document_id=?',(ident,)).fetchone())
        meta=db.odoo_status(existing['event_id']) if existing else {}
        known=captured or meta.get('workflow_version',0)>=2
        self._staged_candidate=not weights or known
        self._staged_first_kind=weights[0]['role'] if weights and known else 'gross'
        self._first_trip=not weights
        self._split_ready=False
        super().__init__(db,parent,existing,document_id,**kwargs)
        self._split_ready=True
        if existing and existing['destination_warehouse_id']:
            warehouse=next((w for w in db.warehouses() if int(w['id'])==int(existing['destination_warehouse_id'])),None)
            if warehouse:
                self.destination_factory.blockSignals(True)
                self.destination_factory.setCurrentIndex(self.destination_factory.findData(warehouse['factory_id']))
                self.destination_factory.blockSignals(False)
                self._resolved_destination=warehouse
                self.destination_warehouse_label.setText(warehouse['name'])
        if self._staged_candidate and self.movement.currentData().startswith('external_'):
            self.current_step=0 if self._first_trip else 2
            if existing and existing['waybill_number']:
                self.waybill_debounce.stop()
                self.waybill_popup.hide()
                self.waybill_search_box.setEnabled(False)
                self.waybill_results.hide()
                self.movement.setEnabled(False)
                self.material_type.setEnabled(False)
        self.configure_staged()
        self.movement.currentIndexChanged.connect(self.configure_staged)
        self.goto_step(self.current_step)
        self.setWindowTitle('مگاسنج اودو · توزین مرتبط با رسید و سفارش تولید')
        row=QHBoxLayout()
        row.addWidget(QLabel('سفارش تولید (برای مصرف یا خرید مستقیم به تولید)'))
        self.odoo_production=QComboBox()
        self.odoo_production.addItem('انتخاب سفارش تولید…',None)
        self.odoo_refresh=QPushButton('دریافت سفارش‌های تولید')
        row.addWidget(self.odoo_production,1)
        row.addWidget(self.odoo_refresh)
        self.layout().insertLayout(1,row)
        self._production_widgets=[row.itemAt(i).widget() for i in range(row.count()) if row.itemAt(i).widget()]
        self.configure_staged()
        self.odoo_refresh.clicked.connect(self.refresh_productions)
        current=db.production_for(self.document_id) if self.document_id else None
        if current: self.odoo_production.addItem('MO #'+str(current),current); self.odoo_production.setCurrentIndex(1)
        self.odoo_production.currentIndexChanged.connect(self.save_production)

    def refresh_productions(self):
        try:
            selected=self.odoo_production.currentData()
            rows=fetch(self.db,'megasanj/productions')
            self.odoo_production.blockSignals(True)
            self.odoo_production.clear(); self.odoo_production.addItem('انتخاب سفارش تولید…',None)
            for row in rows:
                self.odoo_production.addItem(row['name']+' · '+row['product'],row['id'])
            index=self.odoo_production.findData(selected)
            if index >= 0:self.odoo_production.setCurrentIndex(index)
            self.odoo_production.blockSignals(False)
        except Exception as exc: message(self,'دریافت سفارش تولید',str(exc),True)

    def save_production(self):
        if self.document_id:
            self.db.set_production(self.document_id,self.odoo_production.currentData())


class OdooMainWindow(OdooDocumentsMixin, MainWindow):
    document_dialog_class=OdooDocumentDialog

    def sync_thread_closed(self):
        super().sync_thread_closed()
        # A weighing can be queued after the current worker took its snapshot.
        # Drain it after that worker closes, including recoverable failures.
        if not self._logging_out and not getattr(self,'_closing',False) and self.db.has_session_pending():
            QTimer.singleShot(15000,self.start_sync)

    @staticmethod
    def _file_data_uri(path):
        if path.name == 'megapulse-official-stamp.png':path=Path(__file__).resolve().parent/'assets/odoo-origin-stamp.png'
        if path.name == 'megasanj-icon.png':path=Path(__file__).resolve().parent/'assets/icon.png'
        return MainWindow._file_data_uri(path)

    @staticmethod
    def _origin_stamp_html():
        uri=MainWindow._file_data_uri(Path(__file__).resolve().parent/'assets/odoo-origin-stamp.png')
        return "<p align='center'><img width='88' height='64' alt='مهر سامانه اودو' src='"+uri+"'><br><span style='font-size:7pt;color:#52677f'>صادرشده توسط مگاسنج · سامانه اودو</span></p>"

    def waybill_html(self,row):
        return super().waybill_html(row).replace("class='stamp' width='54' height='54'", "class='stamp' width='74' height='54'").replace('width:54px; height:54px;', 'width:74px; height:54px;')

    def document_html(self,row):
        return super().document_html(row).replace('</body>',self._origin_stamp_html()+'</body>')

    def processing_html(self,row):
        return super().processing_html(row).replace('</body>',self._origin_stamp_html()+'</body>')

    def _document_print_pages(self,row,advanced,output):
        pages=super()._document_print_pages(row,advanced,output)
        if pages is None:return None
        # Minutes use a separate Qt layout; keep the seal inside its sign block.
        return [page.replace('مهر و امضا:<br>', 'مهر و امضا:<br>'+self._origin_stamp_html())
                if 'data-minutes-page' in page else page for page in pages]

    def _weighbridge_verification_url(self,row):
        event_id=row['event_id'] if 'event_id' in row.keys() else ''
        data=self.db.odoo_status(event_id)
        ident=data.get('odoo_id') or data.get('id')
        return self.db.session.base_url+'/web#model=megasanj.document&view_type=form&id='+str(ident) if ident else ''

    def __init__(self,db):
        super().__init__(db)
        self.queue_page.entries_changed.connect(self._sync_admitted_documents)
        self.setWindowTitle('مگاسنج · نسخه مخصوص اودو ۱۹')
        icon=Path(__file__).resolve().parent/'assets/icon.png'
        self.setWindowIcon(QIcon(str(icon)))
        brand=self.findChild(QLabel,'brandIcon')
        if brand:brand.setPixmap(QPixmap(str(icon)).scaled(52,52,Qt.KeepAspectRatio,Qt.SmoothTransformation))

    def complete_draft(self,doc_id,dlg):
        if not getattr(dlg,'_staged',False):return super().complete_draft(doc_id,dlg)
        if getattr(dlg,'_first_capture_saved',False):
            self.refresh_documents();self.start_sync();return
        from datetime import datetime
        with self.db.connection() as con:
            con.execute("UPDATE advanced_documents SET status='finalized',finalized_at=? WHERE id=?",(datetime.now().astimezone().isoformat(timespec='seconds'),doc_id))
        self.refresh_documents();self.start_sync()
        message(self,'توزین تکمیل شد','دو وزن برای اودو ارسال می‌شوند؛ کنترل کیفیت و ثبت موجودی در اودو ادامه دارد. وضعیت همگام‌سازی را بررسی کنید.')

    @staticmethod
    def waybill_freight_visible(row):
        return not bool(row['freight_by_contractor'])

    @staticmethod
    def waybill_freight_label(row):
        return MainWindow.waybill_freight_label(row) if OdooMainWindow.waybill_freight_visible(row) else '—'

    def new_waybill(self):
        if not self.db.allows('waybills'):return
        try:
            # Receipt selection must use the current catalog, not the login-time cache.
            from weighbridge_app.materials import fetch_materials
            reference=fetch_materials(self.db.material_reference_url(),self.db.session.token)
            self.db.save_material_reference(reference)
            self.db.refresh_reference_aliases()
            self.db.reconcile_server_cache()
            receipts=fetch(self.db,'megasanj/receipts')
            options=[(receipt,line) for receipt in receipts for line in receipt['lines']]
            if not options:raise ValueError('رسید خرید باز با محموله قابل توزین در اودو وجود ندارد.')
            from .purchase_picker import PurchaseReceiptPicker
            picker=PurchaseReceiptPicker(receipts,self)
            if not picker.exec() or not picker.selected:return
            receipt,line=picker.selected
            dlg=OdooWaybillDialog(self.db,receipt,self)
            self.db.bind_receipt(dlg.event_id,receipt['id'],line['id'])
            found=False
            for i in range(dlg.material_type.count()):
                material=dlg.material_type.itemData(i)
                if material and int(material['id']) == int(line['material_type_id']):
                    dlg.material_type.setCurrentIndex(i); found=True; break
            if not found:raise ValueError('برای نوع محموله این رسید، در اودو سیاست پذیرش را منتشر کنید؛ سپس بارنامه جدید را دوباره باز کنید.')
            dlg.material_type.setEnabled(False)
            found=False
            for i in range(dlg.company.count()):
                company=dlg.company.itemData(i)
                if company and int(company['id']) == int(receipt['supplier_company_id']):
                    dlg.company.setCurrentIndex(i);found=True;break
            if not found:raise ValueError('صلاحیت تأمین‌کننده این رسید در اودو تعریف نشده است.')
            dlg.company.setEnabled(False)
            dlg.setWindowTitle('بارنامه اودو · '+receipt['name'])
            if dlg.exec():
                self.refresh_waybills();self.start_sync()
                message(self,'بارنامه ثبت شد','نوبت ایجاد شد. انتظامات پس از فراخوان، ورود خودرو را ثبت می‌کند.')
        except Exception as exc: message(self,'بارنامه اودو',str(exc),True)

    def extra_document_sections(self,document,document_id):
        event_id=document['event_id'] if 'event_id' in document.keys() else ''
        status=self.db.odoo_status(event_id)
        if not status:return []
        state={'gross':'وزن پر ثبت‌شده','tare':'وزن خالی ثبت‌شده','quality':'در انتظار کنترل کیفیت','ready':'نیازمند عملیات اودو','pending':'در انتظار رفع مغایرت','posted':'ثبت‌شده','void':'باطل'}.get(status.get('odoo_state'),'در انتظار عملیات اودو' if status.get('inventory_pending') else 'ثبت‌شده')
        effect={'consumption_recorded':'مصرف واقعی ثبت شد؛ کسر نهایی با تکمیل MO','receipt':'رسید خرید','transfer':'انتقال داخلی','direct':'ورود و تخصیص مستقیم به MO'}.get(status.get('effect'),'—')
        return [('گردش اودو',(('وضعیت عملیات',state),('اثر توزین',effect),('سفارش تولید',str(status.get('production_id') or '—')),
                              ('اقدام لازم',status.get('action_required') or '—')))]

    def advanced_document_html(self,doc_id):
        import html
        output=super().advanced_document_html(doc_id)
        output=output.replace("class='stamp' width='40' height='40'", "class='stamp' width='64' height='46'").replace('width:40px;height:40px;', 'width:64px;height:46px;')
        doc=self.db.advanced_document(doc_id)
        if doc:
            status=self.db.odoo_status(doc['event_id'])
            if status.get('inventory_pending'):
                banner='<p dir="rtl"><strong>ثبت انبار/مصرف در انتظار عملیات اودو است.</strong> '+html.escape(status.get('action_required') or '')+'</p>'
                output=output.replace('</body>',banner+'</body>')
        return output
