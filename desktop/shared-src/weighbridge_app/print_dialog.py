"""Themed print setup with a real PDF preview and direct native printing."""
from pathlib import Path
from tempfile import TemporaryDirectory
import re

from PySide6.QtCore import Qt, QTimer, QMarginsF, QSettings, QBuffer, QIODevice
from PySide6.QtGui import QPageSize, QPageLayout, QPdfWriter, QPalette, QColor
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtPrintSupport import QPrinter, QPrinterInfo, QPrintDialog
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFrame, QLabel,
    QPushButton, QComboBox, QLineEdit, QMessageBox, QFileDialog)
from .design import app_style
from .theme import set_widget_style, set_navigation_icon, theme_color, theme_events


def selected_pages(value, count):
    """Validate 1-based ranges, deduplicate, and preserve document order."""
    value=value.translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩،','01234567890123456789,')).strip()
    if not value:return list(range(count))
    pages=set()
    for item in value.split(','):
        match=re.fullmatch(r'\s*(\d+)\s*(?:-\s*(\d+)\s*)?',item)
        if not match:raise ValueError('شماره صفحات را مانند ۱،۳-۵ وارد کنید.')
        first=int(match[1]); last=int(match[2] or match[1])
        if not 1<=first<=last<=count:raise ValueError(f'صفحات باید بین ۱ و {count} باشند و بازه معکوس نباشد.')
        pages.update(range(first-1,last))
    return sorted(pages)


def _matching_driver_page_size(info, requested):
    """Return the driver's own page-size object when it exposes an equivalent.

    Several Windows PCL drivers reject a freshly constructed QPageSize even
    though they support the same physical paper. Passing the object returned by
    the driver preserves its Windows paper code and tray mapping.
    """
    try:supported=info.supportedPageSizes()
    except Exception:supported=[]
    for size in supported:
        if size.id()==requested.id() or size.isEquivalentTo(requested):return size
    return requested


def _layout_matches(printer, page_size, orientation):
    layout=printer.pageLayout()
    return layout.pageSize().isEquivalentTo(page_size) and layout.orientation()==orientation


def configure_native_page(printer, info, page_size_id, orientation):
    """Configure physical paper without coupling it to optional page margins."""
    requested=QPageSize(page_size_id)
    page_size=_matching_driver_page_size(info,requested)
    size_set=printer.setPageSize(page_size)
    orientation_set=printer.setPageOrientation(orientation)
    if not _layout_matches(printer,page_size,orientation):
        detail=[]
        if not size_set:detail.append('اندازه کاغذ')
        if not orientation_set:detail.append('جهت صفحه')
        label=' و '.join(detail) or 'تنظیمات صفحه'
        raise ValueError(f'{label} توسط درایور پرینتر اعمال نشد. تنظیمات یا Tray پرینتر را بررسی کنید.')

    # Margins are a layout preference, not a paper-capability requirement.
    # Legacy HP/PCL drivers commonly return false for a combined QPageLayout
    # containing 6 mm margins. Keep at least 6 mm where the hardware permits it
    # and otherwise retain the driver's real printable area.
    current=printer.pageLayout(); minimum=current.minimumMargins()
    margins=QMarginsF(
        max(6.0,minimum.left()),max(6.0,minimum.top()),
        max(6.0,minimum.right()),max(6.0,minimum.bottom()),
    )
    printer.setPageMargins(margins,QPageLayout.Millimeter)
    printer.setFullPage(False)
    return printer.pageLayout()


class PrintSetupDialog(QDialog):
    def __init__(self,parent,documents,title,render):
        super().__init__(parent)
        self.documents=list(documents); self.render=render; self.title=title
        self.temp=TemporaryDirectory(prefix='megasanj-print-'); self.preview_key=None
        self.advanced_printer=None
        self.setWindowTitle(title); self.setLayoutDirection(Qt.RightToLeft)
        self.resize(1120,790); self.setMinimumSize(820,590)
        set_widget_style(self,app_style()+"""
        QFrame#printSettings { background:#0D1B30; border:1px solid #304763; border-radius:14px; }
        QLabel#printCaption { color:#9CAFC8; font-size:12px; }
        QLabel#printTitle { color:#F3F7FF; font-size:22px; font-weight:700; }
        QLabel#printError { color:#FF647C; padding:8px; }
        """)
        root=QVBoxLayout(self); root.setContentsMargins(20,18,20,18); root.setSpacing(14)
        root.addWidget(QLabel(title,objectName='printTitle'))
        root.addWidget(QLabel('پیش‌نمایش زنده سند · تنظیمات چاپ و کاغذ',objectName='printCaption'))
        body=QHBoxLayout(); body.setSpacing(16)
        settings=QFrame(objectName='printSettings'); settings.setFixedWidth(280)
        form=QVBoxLayout(settings); form.setContentsMargins(18,18,18,18); form.setSpacing(10)
        self.printers=QComboBox(); self.printers.setMinimumHeight(40)
        available=QPrinterInfo.availablePrinters()
        for info in available:self.printers.addItem(info.printerName(),info.printerName())
        stored=QSettings('MegaFoolad','MegaSanj').value('printing/printer','')
        preferred=self.printers.findData(stored or QPrinterInfo.defaultPrinterName())
        if preferred>=0:self.printers.setCurrentIndex(preferred)
        if not available:self.printers.addItem('پرینتری نصب نشده است',None)
        self.paper=QComboBox()
        for label,size in (('A5',QPageSize.A5),('A4',QPageSize.A4),('A3',QPageSize.A3),('Letter',QPageSize.Letter)):
            self.paper.addItem(label,size)
        self.orientation=QComboBox(); self.orientation.addItem('عمودی · Portrait',QPageLayout.Portrait); self.orientation.addItem('افقی · Landscape',QPageLayout.Landscape)
        self.pages=QLineEdit(); self.pages.setPlaceholderText('همه صفحات؛ یا ۱،۳-۵')
        self.pages.setLayoutDirection(Qt.LeftToRight)
        self.advanced_button=QPushButton()
        self.advanced_button.setAccessibleName('تنظیمات بیشتر چاپگر')
        self.advanced_button.setCursor(Qt.PointingHandCursor)
        self.advanced_button.setMinimumHeight(64)
        set_widget_style(self.advanced_button,'''QPushButton { background:#132842; border:1px solid #304763; border-radius:10px; padding:0; min-height:64px; }
            QPushButton:hover { background:#173052; border-color:#62B8FF; }
            QPushButton:pressed { background:#1C3555; }
            QPushButton:disabled { border-color:#263B56; }
            QLabel { background:transparent; border:0; padding:0; }
            QLabel#advancedTitle { color:#62B8FF; font-size:12px; font-weight:600; }
            QLabel#advancedHint { color:#9CAFC8; font-size:10px; }
            QLabel#advancedArrow { color:#9CAFC8; font-size:20px; }
            QPushButton:disabled QLabel { color:#71839C; }''')
        advanced_row=QHBoxLayout(self.advanced_button); advanced_row.setContentsMargins(12,10,12,10); advanced_row.setSpacing(10)
        advanced_icon=QLabel(); advanced_icon.setFixedSize(23,23)
        set_navigation_icon(advanced_icon,Path(__file__).resolve().parent/'assets'/'icons'/'nav-settings.svg',23)
        advanced_text=QVBoxLayout(); advanced_text.setSpacing(2)
        advanced_text.addWidget(QLabel('تنظیمات بیشتر چاپگر',objectName='advancedTitle'))
        advanced_text.addWidget(QLabel('باز کردن تنظیمات ویندوز',objectName='advancedHint'))
        advanced_row.addWidget(advanced_icon); advanced_row.addLayout(advanced_text,1)
        advanced_row.addWidget(QLabel('‹',objectName='advancedArrow'))
        for label in self.advanced_button.findChildren(QLabel):label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.advanced_button.clicked.connect(self.open_advanced)
        self.advanced_button.setEnabled(bool(self.printers.currentData()))
        for caption,widget in (('پرینتر',self.printers),('اندازه کاغذ',self.paper),('جهت صفحه',self.orientation),('صفحات موردنظر',self.pages)):
            form.addWidget(QLabel(caption,objectName='printCaption')); form.addWidget(widget)
        form.addSpacing(6)
        form.addWidget(self.advanced_button)
        hint=QLabel('هر سند در یک صفحه، با حفظ نسبت تصاویر و متناسب با فضای قابل چاپ قرار می‌گیرد.'); hint.setWordWrap(True); hint.setObjectName('notice'); form.addWidget(hint)
        self.summary=QLabel(); self.summary.setWordWrap(True); self.summary.setObjectName('printCaption'); form.addWidget(self.summary)
        form.addStretch(); body.addWidget(settings)
        self.pdf=QPdfDocument(self)
        self.pdf_buffer=QBuffer(self)
        self.view=QPdfView(); self.view.setDocument(self.pdf); self.view.setPageMode(QPdfView.PageMode.MultiPage); self.view.setZoomMode(QPdfView.ZoomMode.FitInView)
        self.view.setLayoutDirection(Qt.LeftToRight); body.addWidget(self.view,1); root.addLayout(body,1)
        self.update_preview_palette(); theme_events().changed.connect(self.update_preview_palette)
        self.error=QLabel(objectName='printError'); self.error.setWordWrap(True); self.error.hide(); root.addWidget(self.error)
        footer=QHBoxLayout(); self.print_button=QPushButton('چاپ با این تنظیمات',objectName='primary'); self.print_button.clicked.connect(self.submit)
        footer.addWidget(self.print_button); footer.addStretch(); close=QPushButton('انصراف'); close.clicked.connect(self.reject); footer.addWidget(close); root.addLayout(footer)
        self.debounce=QTimer(self); self.debounce.setSingleShot(True); self.debounce.setInterval(200); self.debounce.timeout.connect(self.rebuild)
        for control in (self.printers,self.paper,self.orientation):control.currentIndexChanged.connect(self.schedule)
        self.pages.textChanged.connect(self.schedule)
        self.print_button.setEnabled(False); self.debounce.start(0)

    def schedule(self,*args):
        self.advanced_button.setEnabled(bool(self.printers.currentData()))
        self.print_button.setEnabled(False); self.debounce.start()

    def open_advanced(self):
        if not self.printers.currentData():return
        try:
            try:printer=self.printer()
            except ValueError:printer=self.native_device()
            dialog=QPrintDialog(printer,self); dialog.setWindowTitle('تنظیمات بیشتر چاپگر')
            dialog.setMinMax(1,max(1,len(self.documents)))
            dialog.setOption(QPrintDialog.PrintSelection,False)
            indices=selected_pages(self.pages.text(),len(self.documents))
            original_pages=self.pages.text()
            disjoint=bool(indices) and indices!=list(range(indices[0],indices[-1]+1))
            if indices and indices==list(range(indices[0],indices[-1]+1)) and len(indices)!=len(self.documents):
                printer.setPrintRange(QPrinter.PageRange); printer.setFromTo(indices[0]+1,indices[-1]+1)
            else:printer.setPrintRange(QPrinter.AllPages)
            if dialog.exec()!=QDialog.DialogCode.Accepted:return
            self.advanced_printer=printer
            name=printer.printerName(); index=self.printers.findData(name)
            if index<0:self.printers.addItem(name,name); index=self.printers.count()-1
            self.printers.setCurrentIndex(index)
            layout=printer.pageLayout(); size=layout.pageSize(); index=self.paper.findData(size.id())
            if index<0:self.paper.addItem('سفارشی',size); index=self.paper.count()-1
            self.paper.setCurrentIndex(index)
            self.orientation.setCurrentIndex(self.orientation.findData(layout.orientation()))
            self.pages.setText(f'{printer.fromPage()}-{printer.toPage()}' if printer.printRange()==QPrinter.PageRange else original_pages if disjoint else '')
            self.preview_key=None; self.schedule()
        except Exception as exc:QMessageBox.warning(self,'تنظیمات چاپگر',str(exc))

    def update_preview_palette(self,*args):
        palette=self.view.palette()
        for role in (QPalette.Dark,QPalette.Window,QPalette.Base):palette.setColor(role,QColor(theme_color('#102039')))
        self.view.setPalette(palette); self.view.viewport().setPalette(palette)

    def pdf_destination(self):
        return str(self.printers.currentData() or '').casefold() in ('microsoft print to pdf','adobe pdf')

    def native_device(self):
        name=self.printers.currentData()
        if not name or QPrinterInfo.printerInfo(name).isNull():raise ValueError('پرینتر انتخاب‌شده در دسترس نیست.')
        printer=self.advanced_printer if self.advanced_printer and self.advanced_printer.printerName()==name else QPrinter(QPrinterInfo.printerInfo(name),QPrinter.HighResolution)
        if not printer.isValid():raise ValueError('درایور پرینتر انتخاب‌شده آماده دریافت چاپ نیست.')
        return printer

    def printer(self,pdf_path=None):
        if pdf_path:
            # QPdfWriter never initializes a Windows printer driver just to
            # preview a PDF (some remote/virtual default drivers fail at init).
            printer=QPdfWriter(str(pdf_path)); printer.setResolution(144); printer.setTitle(self.title)
        else:
            printer=self.native_device()
            printer.setDocName(self.title)
        if isinstance(printer,QPrinter):
            info=QPrinterInfo.printerInfo(printer.printerName())
            configure_native_page(printer,info,self.paper.currentData(),self.orientation.currentData())
        else:
            layout=QPageLayout(QPageSize(self.paper.currentData()),self.orientation.currentData(),QMarginsF(6,6,6,6),QPageLayout.Millimeter)
            if not printer.setPageLayout(layout):raise ValueError('ساخت صفحه پیش‌نمایش با تنظیمات انتخاب‌شده انجام نشد.')
        return printer

    def rebuild(self):
        self.debounce.stop(); self.print_button.setEnabled(False)
        try:
            indices=selected_pages(self.pages.text(),len(self.documents))
            if not indices:raise ValueError('سندی برای چاپ وجود ندارد.')
            key=(tuple(indices),self.paper.currentIndex(),self.orientation.currentIndex(),self.printers.currentData())
            if key!=self.preview_key:
                self.preview_key=None
                self.pdf.close(); self.pdf_buffer.close(); path=Path(self.temp.name)/'preview.pdf'
                printer=self.printer(path)
                self.render(printer,[self.documents[i] for i in indices])
                del printer
                self.pdf_buffer.setData(path.read_bytes()); self.pdf_buffer.open(QIODevice.ReadOnly)
                self.pdf.load(self.pdf_buffer)
                if self.pdf.status()!=QPdfDocument.Status.Ready:raise RuntimeError('ساخت پیش‌نمایش PDF انجام نشد.')
                self.preview_key=key
            self.summary.setText(f'{len(indices)} صفحه انتخاب شده از {len(self.documents)} صفحه')
            self.print_button.setText('ذخیره PDF با این تنظیمات' if self.pdf_destination() else 'چاپ با این تنظیمات')
            self.error.hide(); self.print_button.setEnabled(bool(self.printers.currentData()))
            return indices
        except Exception as exc:
            self.error.setText(str(exc)); self.error.show(); return None

    def submit(self):
        indices=self.rebuild()
        if indices is None or not self.printers.currentData():return
        self.print_button.setEnabled(False)
        try:
            if self.pdf_destination():
                # Keep known PDF destinations inside the app instead of opening
                # the Windows driver's Save Print Output dialog.
                path,_=QFileDialog.getSaveFileName(self,'ذخیره فایل چاپ','document.pdf','PDF (*.pdf)',options=QFileDialog.DontUseNativeDialog)
                if not path:self.print_button.setEnabled(True); return
                if not path.lower().endswith('.pdf'):path+='.pdf'
                printer=self.printer(path)
            else:printer=self.printer()
            if isinstance(printer,QPrinter):printer.setPrintRange(QPrinter.AllPages)  # Already filtered by the app.
            self.render(printer,[self.documents[i] for i in indices])
            if isinstance(printer,QPrinter) and printer.printerState()==QPrinter.Error:raise RuntimeError('ارسال کار چاپ به پرینتر ناموفق بود.')
            QSettings('MegaFoolad','MegaSanj').setValue('printing/printer',self.printers.currentData())
            self.accept()
        except Exception as exc:
            QMessageBox.warning(self,'چاپ انجام نشد',str(exc)); self.print_button.setEnabled(True)

    def dispose(self):
        self.debounce.stop(); self.view.setDocument(None); self.pdf.close(); self.pdf_buffer.close(); self.temp.cleanup()


def print_documents(parent,documents,title,render):
    dialog=PrintSetupDialog(parent,documents,title,render)
    try:dialog.exec()
    finally:dialog.dispose(); dialog.deleteLater()
