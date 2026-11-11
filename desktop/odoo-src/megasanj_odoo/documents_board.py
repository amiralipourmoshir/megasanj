"""Odoo-only admission drafts and the two-pass weighing board."""
import json,uuid
from datetime import datetime
from PySide6.QtCore import Qt,QTimer,QRectF
from PySide6.QtGui import QPainter,QColor
from PySide6.QtWidgets import (QWidget,QFrame,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,
    QScrollArea,QTableWidget,QHeaderView,QAbstractItemView,QInputDialog)
from weighbridge_app.ui import message,fa_digits,iran_jalali_datetime
from weighbridge_app.theme import set_widget_style,theme_color
from weighbridge_app.design import set_tone
from weighbridge_app.hall_plate import draw_plate_in_rect,plate_parts


class WeighingPlate(QWidget):
    def __init__(self,value,parent=None):
        super().__init__(parent);self.value=value or '';self.setMinimumHeight(44)
        self.setToolTip(self.value)

    def paintEvent(self,event):
        painter=QPainter(self)
        width=min(200,max(1,self.width()-24));height=width*220/1040
        rect=QRectF((self.width()-width)/2,(self.height()-height)/2,width,height)
        if plate_parts(self.value):draw_plate_in_rect(painter,self.value,rect)
        else:
            painter.setPen(QColor(theme_color('#F3F7FF')));painter.drawText(self.rect(),Qt.AlignCenter,fa_digits(self.value or 'پلاک ثبت نشده'))
        painter.end()


def text_cell(value,name='',bold=False):
    host=QWidget();box=QVBoxLayout(host);box.setContentsMargins(14,8,14,8)
    label=QLabel(str(value or '—'),objectName=name);label.setTextFormat(Qt.PlainText)
    label.setAlignment(Qt.AlignCenter);label.setWordWrap(True);label.setToolTip(str(value or '—'))
    set_widget_style(label,'font-size:14px;color:#F3F7FF;'+('font-weight:600;' if bold else ''))
    box.addWidget(label);return host


def fit_compact_table(table,limit=10):
    from shiboken6 import isValid
    if not isValid(table):return
    header=table.horizontalHeader()
    height=max(header.height(),header.sizeHint().height())
    height+=sum(table.rowHeight(i) for i in range(min(limit,table.rowCount())))
    height+=2*table.frameWidth()+4
    if table.horizontalScrollBar().isVisible():height+=table.horizontalScrollBar().height()
    if table.height()!=height:table.setFixedHeight(height)


def weighing_actions(item,gate,callback):
    card=QFrame(objectName='weighingCard');box=QVBoxLayout(card)
    box.setContentsMargins(12,8,12,8);box.setSpacing(6)
    button=QPushButton('ادامه فرآیند',objectName='continueWeighing');button.setMinimumHeight(40)
    button.setEnabled(not gate or gate=='inside')
    if gate and gate!='inside':
        reasons={'waiting':'ابتدا اجازه ورود و ورود خودرو را ثبت کنید.',
                 'ready':'ابتدا ورود خودرو را در نوبت‌دهی ثبت کنید.',
                 'exited':'خودرو خارج شده؛ بارنامه را بررسی کنید.'}
        reason=reasons.get(gate,'وضعیت ورود خودرو را بررسی کنید.')
        button.setToolTip(reason);button.setAccessibleDescription(reason)
        notice=QLabel(reason,objectName='weighingBlockedReason');notice.setWordWrap(True)
        notice.setAlignment(Qt.AlignCenter)
        set_widget_style(notice,'color:#FFB454;font-size:12px;');box.addWidget(notice)
    button.clicked.connect(lambda checked=False:callback(item['id']));box.addWidget(button)
    return card


def weighing_phase(row,metadata=None):
    metadata=metadata or {}
    if metadata.get('odoo_state')=='void':return 'complete'
    gross=float(row['gross_weight_kg'] or 0);tare=float(row['tare_weight_kg'] or 0)
    if gross and tare:return 'complete'
    if gross or tare:return 'second'
    return 'draft'


class OdooDocumentsMixin:
    def build_documents(self):
        page=super().build_documents()
        index=self.status_filter.findData('first_weighed')
        if index>=0:self.status_filter.setItemText(index,'در انتظار توزین دوم')
        for label in page.findChildren(QLabel):
            if label.text()=='همگام‌شده با سرور':label.setText('توزین‌های تکمیل‌شده')
        for button in page.findChildren(QPushButton):
            if button.text()=='ثبت سند جدید':
                button.setText('ثبت گردش داخلی');button.setToolTip('پیش‌نویس بارنامه‌ها پس از اجازه ورود خودرو خودکار ساخته می‌شود.')
        for label in self.document_empty.findChildren(QLabel):
            if label.objectName()=='emptyHint':label.setText('پس از ورود خودرو، پیش‌نویس آن در بخش بالا قرار می‌گیرد.')
        self.table.doubleClicked.disconnect()
        self.table.doubleClicked.connect(lambda _:self.details())
        self.table.setHorizontalHeaderLabels(['راننده','وضعیت','تأمین‌کننده','پلاک','وزن خالص','انتخاب','عملیات','ورود','خروج'])
        header=self.table.horizontalHeader()
        for position,column in enumerate((5,3,0,2,1,4,7,8,6)):
            header.moveSection(header.visualIndex(column),position)
        for column,width in ((0,100),(2,110),(3,170),(4,90),(7,85),(8,85)):
            self.table.setColumnWidth(column,width)
        self.table.configure_fill((0,2,3,4,7,8))
        self.table.verticalHeader().setDefaultSectionSize(76)
        self.table.horizontalHeader().geometriesChanged.connect(lambda:QTimer.singleShot(0,lambda:fit_compact_table(self.table)))
        completed_layout=self.table.parentWidget().layout()
        completed_layout.setContentsMargins(16,16,16,16);completed_layout.setSpacing(12)
        panels=QWidget();row=QVBoxLayout(panels);row.setContentsMargins(0,0,0,0);row.setSpacing(12)
        self.weighing_boards={}
        for kind,title in [('draft','اسناد پیش‌نویس'),('second','در انتظار توزین دوم')]:
            card=QFrame(objectName='card');box=QVBoxLayout(card);box.setContentsMargins(14,14,14,14);box.setSpacing(12)
            heading=QHBoxLayout();title_label=QLabel(title);set_widget_style(title_label,'font-size:16px;font-weight:600;color:#F3F7FF;');heading.addWidget(title_label,1)
            count=QLabel('۰',objectName='queueSectionCount');count.setFixedSize(30,30);count.setAlignment(Qt.AlignCenter);set_widget_style(count,'background:#193455;color:#6AB6FF;border-radius:12px;font-weight:600;');heading.addWidget(count);box.addLayout(heading)
            table=QTableWidget(0,4)
            table.setHorizontalHeaderLabels(['پلاک','راننده','تأمین‌کننده','عملیات'])
            table.setEditTriggers(QAbstractItemView.NoEditTriggers);table.setSelectionMode(QAbstractItemView.NoSelection)
            table.verticalHeader().hide();table.verticalHeader().setDefaultSectionSize(76)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            table.horizontalHeader().setSectionResizeMode(3,QHeaderView.Fixed);table.setColumnWidth(3,240)
            table.setShowGrid(False);table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            table.horizontalHeader().geometriesChanged.connect(lambda t=table:QTimer.singleShot(0,lambda:fit_compact_table(t,5)))
            table.setAlternatingRowColors(True);table.setWordWrap(True)
            empty=QLabel('سندی در این بخش نیست.',objectName='mutedText');empty.setAlignment(Qt.AlignCenter);empty.setMinimumHeight(54)
            box.addWidget(table);box.addWidget(empty);row.addWidget(card)
            self.weighing_boards[kind]=(table,count,empty)
        page.layout().insertWidget(3,panels)
        self.table.parentWidget().layout().insertWidget(0,QLabel('اسناد ثبت‌شده',objectName='queueSectionTitle'))
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);scroll.setWidget(page)
        return scroll

    def refresh_documents(self):
        if not hasattr(self,'weighing_boards'):return
        rows=self.db.advanced_documents() if self.db.allows('weighbridge') else []
        search=self.search.text().strip().translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹','0123456789')).casefold();movement=self.movement_filter.currentData();status=self.status_filter.currentData()
        boards={'draft':[],'second':[],'complete':[]}
        for row in rows:
            metadata=self.db.odoo_status(row['event_id']);phase=weighing_phase(row,metadata)
            text=' '.join(str(row[k] or '') for k in ('document_number','waybill_number','driver_name','driver_phone','trailer_plate','company_snapshot'))
            if search and search not in text.translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹','0123456789')).casefold():continue
            if movement and row['movement_type']!=movement:continue
            if status and status not in (row['status'],row['sync_status'],metadata.get('odoo_state'),'first_weighed' if phase=='second' else phase):continue
            if self.entry_time_enabled.isChecked():
                from weighbridge_app.ui import entry_in_today_range
                from datetime import timezone,timedelta
                start=self.entry_time_from.time();end=self.entry_time_to.time()
                if start>end or not entry_in_today_range(row['registered_at'],row['entry_at'],datetime.now(timezone(timedelta(hours=3,minutes=30))).date(),start.hour()*60+start.minute(),end.hour()*60+end.minute()):continue
            boards[phase].append(row)
        for kind in ('draft','second'):
            table,count,empty=self.weighing_boards[kind];items=boards[kind]
            count.setText(fa_digits(len(items)));table.setRowCount(len(items));table.setVisible(bool(items));empty.setVisible(not items)
            for index,item in enumerate(items):
                company=(json.loads(item['company_snapshot'] or '{}') or {}).get('name','—')
                gate=(json.loads(item['waybill_snapshot'] or '{}') or {}).get('gate_state')
                table.setCellWidget(index,0,WeighingPlate(item['trailer_plate']))
                table.setCellWidget(index,1,text_cell(item['driver_name'],'weighingDriver',True))
                table.setCellWidget(index,2,text_cell(company,'weighingSupplier'))
                widget=weighing_actions(item,gate,self.continue_weighing)
                table.setCellWidget(index,3,widget)
                table.setRowHeight(index,112 if gate and gate!='inside' else 76)
            fit_compact_table(table,5)
        completed=[(r,True) for r in boards['complete']]
        if self.db.get_setting('server_snapshot_reconciled')!='1' and self.db.allows('weighbridge'):
            completed.extend((r,False) for r in self.db.documents())
        completed.sort(key=lambda item:str(item[0]['registered_at'] or ''),reverse=True)
        self.filtered_document_rows=completed
        pages=max(1,(len(completed)+self.document_page_size-1)//self.document_page_size)
        self.document_page=min(max(0,self.document_page),pages-1)
        visible=completed[self.document_page*self.document_page_size:(self.document_page+1)*self.document_page_size]
        self.render_document_cards(visible)
        fit_compact_table(self.table)
        total=sum(map(len,boards.values()));pending=len(boards['draft'])+len(boards['second'])
        self.documents_stat_total.setText(fa_digits(total));self.documents_stat_pending.setText(fa_digits(pending));self.documents_stat_synced.setText(fa_digits(len(boards['complete'])))
        self.document_page_label.setText('اسناد ثبت‌شده · صفحه '+fa_digits(self.document_page+1)+' از '+fa_digits(pages))
        self.document_prev.setEnabled(self.document_page>0);self.document_next.setEnabled(self.document_page<pages-1)
        if hasattr(self,'sidebar_count'):
            self.sidebar_count.setText(fa_digits(total)+'\nاسناد');self.sidebar_pending.setText(fa_digits(pending)+'\nدر انتظار');self.sidebar_synced.setText(fa_digits(len(boards['complete']))+'\nتکمیل‌شده')

    def render_document_cards(self,visible):
        super().render_document_cards(visible)
        names={'quality':('در انتظار کنترل کیفیت','info'),'pending':('در انتظار رفع مغایرت','warning'),
               'ready':('در انتظار ثبت انبار','info'),'posted':('ثبت‌شده','success'),'void':('برگشت‌شده','neutral')}
        for index,(item,advanced) in enumerate(visible):
            self.table.setRowHeight(index,76)
            if not advanced:continue
            plate=WeighingPlate(item['trailer_plate']);plate.setAccessibleName('پلاک '+str(item['trailer_plate'] or 'ثبت نشده'))
            company=(json.loads(item['company_snapshot'] or '{}') or {}).get('name') or 'داخلی'
            self.table.setCellWidget(index,3,plate)
            self.table.setCellWidget(index,0,text_cell(item['driver_name'] or 'راننده ثبت نشده','completedDriver',True))
            self.table.setCellWidget(index,2,text_cell(company,'completedSupplier',True))
            state=self.db.odoo_status(item['event_id']).get('odoo_state')
            old=self.table.cellWidget(index,1).findChild(QLabel)
            title,tone=names.get(state,(old.text(),old.property('tone')))
            badge=QLabel(title,objectName='statusBadge');badge.setAlignment(Qt.AlignCenter)
            badge.setWordWrap(False);badge.setTextFormat(Qt.PlainText);badge.setToolTip(old.toolTip())
            set_tone(badge,tone)
            style='font-size:13px;font-weight:600;padding:6px 12px;border-radius:7px;'
            if tone=='neutral':style+='background:#203249;color:#9CAFC8;'
            set_widget_style(badge,style);badge.ensurePolished()
            badge.setFixedSize(badge.fontMetrics().horizontalAdvance(title)+32,max(36,badge.fontMetrics().height()+14))
            holder=QWidget();layout=QHBoxLayout(holder);layout.setContentsMargins(12,8,12,8)
            layout.setAlignment(Qt.AlignCenter);layout.addWidget(badge)
            self.table.setCellWidget(index,1,holder)
            for column,key in ((7,'entry_at'),(8,'exit_at')):
                value=item[key] if key in item.keys() else None
                full=iran_jalali_datetime(value) if value else 'ثبت نشده'
                host=text_cell(full.rsplit(' · ',1)[-1] if value else '—');host.setToolTip(full)
                self.table.setCellWidget(index,column,host)
        self.table.resizeColumnToContents(1)
        self.table.fill_available_width()

    def continue_weighing(self,ident):
        if not self.db.allows('weighbridge'):return
        row=self.db.advanced_document(ident)
        if not row:return
        gate=(json.loads(row['waybill_snapshot'] or '{}') or {}).get('gate_state')
        if gate and gate!='inside':return message(self,'ورود خودرو','ابتدا ورود خودرو را در بخش نوبت‌دهی ثبت کنید.',True)
        if not self.db.material_types():
            from weighbridge_app.materials import fetch_materials
            try:self.db.save_material_reference(fetch_materials(self.db.material_reference_url(),self.db.session.token));self.db.refresh_reference_aliases()
            except Exception as exc:return message(self,'اطلاعات محموله',str(exc),True)
        dialog=self.document_dialog_class(self.db,self,row,ident)
        if dialog.exec():self.complete_draft(ident,dialog)
        self.refresh_documents()

    def details(self,doc_id=None):
        if doc_id and doc_id<0:
            row=next((r for r in self.db.advanced_documents() if r['id']==-doc_id),None)
            if row and weighing_phase(row,self.db.odoo_status(row['event_id']))!='complete':
                return self.continue_weighing(-doc_id)
        return super().details(doc_id)

    def new_document(self):
        if not self.db.allows('weighbridge'):return
        labels={'مصرف سفارش تولید':'warehouse_to_production','انتقال داخلی انبار':'warehouse_transfer'}
        label,ok=QInputDialog.getItem(self,'ثبت گردش داخلی','نوع گردش داخلی:',list(labels),0,False)
        if not ok:return
        stamp=datetime.now().astimezone();event=str(uuid.uuid4())
        ident=self.db.create_draft(event,'WB-'+stamp.strftime('%Y%m%d')+'-'+uuid.uuid4().hex[:8].upper(),labels[label],{'registered_at':stamp.isoformat(timespec='seconds')})
        dialog=self.document_dialog_class(self.db,self,self.db.advanced_document(ident),ident,created_for_dialog=True)
        dialog.movement.setEnabled(False)
        if dialog.exec():self.complete_draft(ident,dialog)
        self.refresh_documents()

    def _sync_admitted_documents(self,*_):
        if getattr(self,'_closing',False) or getattr(self,'_logging_out',False):return
        if self.server_sync_thread and self.server_sync_thread.isRunning():
            QTimer.singleShot(300,self._sync_admitted_documents);return
        self.start_server_sync()
