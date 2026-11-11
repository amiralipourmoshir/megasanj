"""Searchable purchase receipt picker for the Odoo desktop edition."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QGroupBox,QFormLayout,
    QLineEdit,QLabel,QPushButton,QTableWidget,QTableWidgetItem,QHeaderView,QAbstractItemView)


def normalized(value):
    return str(value or '').translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹يك','0123456789یک')).casefold().strip()


class PurchaseReceiptPicker(QDialog):
    def __init__(self,receipts,parent=None):
        super().__init__(parent)
        self.selected=None
        self.setWindowTitle('انتخاب سفارش خرید و رسید محموله')
        self.setLayoutDirection(Qt.RightToLeft)
        self.resize(1100,640)
        layout=QVBoxLayout(self)
        title=QLabel('سفارش خرید محموله را پیدا کنید')
        title.setStyleSheet('font-size:19px;font-weight:600;padding:8px')
        layout.addWidget(title)
        layout.addWidget(QLabel('هر ردیف، یک قلم خرید در یک رسید باز است. پس از انتخاب، مشخصات خرید به بارنامه متصل می‌شود.'))
        box=QGroupBox('فیلترها');form=QFormLayout(box)
        self.search=QLineEdit();self.search.setPlaceholderText('جست‌وجو در تمام ستون‌ها…')
        self.purchase=QLineEdit();self.purchase.setPlaceholderText('شماره سفارش خرید یا رسید')
        self.supplier=QLineEdit();self.supplier.setPlaceholderText('نام یا کد تأمین‌کننده')
        self.material=QLineEdit();self.material.setPlaceholderText('نام محموله یا کالا')
        for label,edit in [('جست‌وجوی سریع',self.search),('خرید / رسید',self.purchase),('تأمین‌کننده',self.supplier),('محموله',self.material)]:
            form.addRow(label,edit);edit.textChanged.connect(self.apply_filters)
        reset=QPushButton('پاک‌کردن فیلترها');reset.clicked.connect(self.clear_filters);form.addRow(reset)
        layout.addWidget(box)
        self.table=QTableWidget(0,7)
        self.table.setHorizontalHeaderLabels(['سفارش خرید','رسید','تأمین‌کننده','محموله','سفارش‌شده','دریافت‌شده','واحد'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3,QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        for receipt in receipts:
            for line in receipt.get('lines',[]):
                row=self.table.rowCount();self.table.insertRow(row)
                company=receipt.get('company') or {}
                vendor=' · '.join(str(company.get(k) or '') for k in ('code','name')).strip(' ·')
                values=[receipt['purchase_name'],receipt['name'],vendor,line['name'],str(line.get('ordered_qty','')),str(line.get('received_qty','')),line.get('unit','')]
                for col,value in enumerate(values):
                    item=QTableWidgetItem(value)
                    if col==0:item.setData(Qt.UserRole,(receipt,line))
                    self.table.setItem(row,col,item)
        self.table.setSortingEnabled(True)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        self.table.itemDoubleClicked.connect(lambda _:self.accept_selected())
        layout.addWidget(self.table,1)
        self.summary=QLabel();layout.addWidget(self.summary)
        buttons=QHBoxLayout();buttons.addStretch()
        cancel=QPushButton('انصراف');cancel.clicked.connect(self.reject)
        self.choose=QPushButton('انتخاب خرید و ادامه ثبت بارنامه');self.choose.setEnabled(False)
        self.choose.clicked.connect(self.accept_selected)
        buttons.addWidget(cancel);buttons.addWidget(self.choose);layout.addLayout(buttons)
        self.apply_filters()

    def clear_filters(self):
        for edit in (self.search,self.purchase,self.supplier,self.material):edit.clear()

    def apply_filters(self):
        if not hasattr(self,'table'):return
        terms=[normalized(e.text()) for e in (self.search,self.purchase,self.supplier,self.material)]
        visible=0
        for row in range(self.table.rowCount()):
            values=[normalized(self.table.item(row,c).text()) for c in range(7)]
            haystacks=[' '.join(values),' '.join(values[:2]),values[2],values[3]]
            show=all(all(word in hay for word in term.split()) for term,hay in zip(terms,haystacks))
            self.table.setRowHidden(row,not show);visible+=show
        self.table.clearSelection();self.selected=None;self.choose.setEnabled(False)
        self.summary.setText(f'{visible} قلم مطابق فیلترها از {self.table.rowCount()} قلم در رسیدهای باز' if visible else 'خریدی با این فیلترها پیدا نشد؛ فیلترها را تغییر دهید.')

    def selection_changed(self):
        selected=self.table.selectionModel().selectedRows()
        self.choose.setEnabled(bool(selected) and not self.table.isRowHidden(selected[0].row()))

    def accept_selected(self):
        rows=self.table.selectionModel().selectedRows()
        if not rows or self.table.isRowHidden(rows[0].row()):return
        self.selected=self.table.item(rows[0].row(),0).data(Qt.UserRole)
        self.accept()
