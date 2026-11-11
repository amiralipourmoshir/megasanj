"""Completion of a server-issued weight discrepancy; mirrors the admin fields."""
from concurrent.futures import ThreadPoolExecutor
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog, QVBoxLayout, QFormLayout, QLabel, QLineEdit, QTextEdit, QPushButton, QHBoxLayout
from .design import app_style, set_tone
from .theme import set_widget_style
from .services import request_weight_discrepancy

_pool = ThreadPoolExecutor(max_workers=2)
_DIGITS = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')


def validate_decision(penalty, reason, national_id):
    penalty = penalty.strip().translate(_DIGITS)
    reason, national_id = reason.strip(), national_id.strip().translate(_DIGITS)
    if not penalty.isascii() or not penalty.isdecimal() or int(penalty) > 2147483647:
        raise ValueError('جریمه آهن باید عدد صحیح و نامنفی بر حسب کیلوگرم باشد.')
    if not reason or len(reason) > 2000:
        raise ValueError('شرح تصمیم الزامی است و حداکثر ۲۰۰۰ نویسه دارد.')
    if len(national_id) > 20:
        raise ValueError('شماره ملی حداکثر ۲۰ نویسه دارد.')
    return {'penalty_weight_kg': int(penalty), 'deduction_reason': reason, 'driver_national_id': national_id or None}


class WeightDiscrepancyDialog(QDialog):
    def __init__(self, db, document, parent=None):
        super().__init__(parent)
        self.db, self.document = db, document
        self.future = None
        self.fresh = False
        self.submitting = False
        self.setWindowTitle('تکمیل صورت‌جلسه عدم تطابق وزن')
        self.setLayoutDirection(Qt.RightToLeft)
        self.resize(700, 590)
        set_widget_style(self, app_style())
        root = QVBoxLayout(self); root.setContentsMargins(24,24,24,24); root.setSpacing(16)
        root.addWidget(QLabel('تکمیل صورت‌جلسه عدم تطابق وزن', objectName='heroTitle'))
        self.summary = QLabel('دریافت اطلاعات گواهی از سرور…'); self.summary.setWordWrap(True); root.addWidget(self.summary)
        note = QLabel('همان فرآیند پنل: جریمه بر حسب کیلوگرم آهن ثبت می‌شود؛ تعیین مبلغ ریالی بر عهده واحد مالی است. ثبت نهایی به اتصال سرور نیاز دارد.'); note.setWordWrap(True); root.addWidget(note)
        form = QFormLayout(); form.setSpacing(14)
        self.national = QLineEdit(); self.national.setMaxLength(20); self.national.setLayoutDirection(Qt.LeftToRight)
        self.penalty = QLineEdit(); self.penalty.setMaxLength(10); self.penalty.setPlaceholderText('عدد صحیح؛ صفر نیز مجاز است')
        self.reason = QTextEdit(); self.reason.setAcceptRichText(False); self.reason.setMinimumHeight(150)
        form.addRow('شماره ملی راننده (اختیاری)', self.national)
        form.addRow('جریمه آهن (کیلوگرم)', self.penalty); form.addRow('علت و شرح تصمیم', self.reason); root.addLayout(form)
        self.feedback = QLabel(''); self.feedback.setWordWrap(True); set_tone(self.feedback,'warning'); root.addWidget(self.feedback)
        buttons = QHBoxLayout()
        self.save = QPushButton('ثبت تعهد و نهایی‌سازی', objectName='primaryButton'); self.save.clicked.connect(self.submit)
        self.refresh = QPushButton('دریافت وضعیت'); self.refresh.clicked.connect(self.load)
        self.cancel = QPushButton('بستن'); self.cancel.clicked.connect(self.reject)
        buttons.addWidget(self.save); buttons.addWidget(self.refresh); buttons.addStretch(); buttons.addWidget(self.cancel); root.addLayout(buttons)
        self.timer = QTimer(self); self.timer.setInterval(80); self.timer.timeout.connect(self.poll)
        self.load()

    def start(self, decision=None):
        if self.future is not None:return
        self.submitting = decision is not None
        self.save.setEnabled(False); self.refresh.setEnabled(False); self.cancel.setEnabled(False)
        for field in (self.penalty,self.national,self.reason):field.setEnabled(False)
        self.feedback.setText('در حال ثبت…' if self.submitting else 'در حال دریافت وضعیت…')
        self.future = _pool.submit(request_weight_discrepancy, self.db.session, self.document['event_id'], decision)
        self.timer.start()

    def load(self):self.start()

    def poll(self):
        if not self.future or not self.future.done():return
        future, self.future = self.future, None; self.timer.stop()
        self.cancel.setEnabled(True); self.refresh.setEnabled(True)
        try:
            data = future.result()
            self.db.save_document_weight_discrepancy(self.document['id'],data)
            self.fresh = data.get('status') not in ('closed','voided')
            self.summary.setText(f"گواهی {data['certificate_number']}\nراننده: {data.get('driver_name') or '—'} · بارنامه: {data.get('waybill_number') or '—'}\nوزن مبدأ: {data.get('origin_weight_kg') or 'نامعلوم'} · وزن مقصد: {data.get('destination_weight_kg') or 0} کیلوگرم\nکسری: {data.get('shortage_weight_kg') or 0} · اضافه وزن: {data.get('surplus_weight_kg') or 0} · حد مجاز: {data.get('tolerance_weight_kg') or 0} کیلوگرم")
            if not self.fresh:
                self.penalty.setText(str(data.get('penalty_weight_kg') or 0));self.national.setText(data.get('driver_national_id') or '');self.reason.setPlainText(data.get('deduction_reason') or '')
                self.feedback.setText('صورت‌جلسه نهایی شده و قابل تغییر نیست.' if data.get('status')=='closed' else 'صورت‌جلسه باطل شده است.')
                if self.submitting:self.accept();return
            else:
                self.feedback.setText('اطلاعات گواهی به‌روز است؛ شرح تصمیم و جریمه را تکمیل کنید.')
        except Exception as exc:
            self.feedback.setText(str(exc));self.fresh=False
        self.save.setEnabled(self.fresh)
        for field in (self.penalty,self.national,self.reason):field.setEnabled(self.fresh)

    def submit(self):
        if not self.fresh:return
        try:decision=validate_decision(self.penalty.text(),self.reason.toPlainText(),self.national.text())
        except ValueError as exc:self.feedback.setText(str(exc));return
        self.start(decision)

    def reject(self):
        if self.future is None:super().reject()

    def closeEvent(self,event):
        if self.future is not None:event.ignore()
        else:super().closeEvent(event)
