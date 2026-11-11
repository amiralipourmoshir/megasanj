"""Durable incremental weighing; shared serial capture and retry worker stay intact."""
import copy
import json
from datetime import datetime
from PySide6.QtWidgets import QLabel
from weighbridge_app.ui import message


class CaptureStore:
    def add_weighing(self, doc_id, sequence, role, weight_kg, source, captured_at, stable, manual_reason='', raw_payload='', actor=''):
        with self.connection() as con:
            rows=con.execute('SELECT payload_json FROM odoo_capture_events WHERE document_id=?',(doc_id,)).fetchall()
        for row in rows:
            for saved in json.loads(row[0])['weighings']:
                if saved['sequence']==sequence and any(saved.get(key)!=value for key,value in
                    [('weight_kg',weight_kg),('source',source),('captured_at',captured_at),('manual_reason',manual_reason),('raw_payload',raw_payload)]):
                    raise ValueError('وزن ارسال‌شده ثابت است؛ اصلاح از مسیر ابطال سند در اودو انجام شود.')
        return super().add_weighing(doc_id,sequence,role,weight_kg,source,captured_at,stable,manual_reason,raw_payload,actor)

    def migrate_captures(self):
        with self.connection() as con:
            con.execute('CREATE TABLE IF NOT EXISTS odoo_capture_events(document_id INTEGER NOT NULL,phase INTEGER NOT NULL,payload_json TEXT NOT NULL,PRIMARY KEY(document_id,phase))')

    def capture_payload(self, doc_id):
        with self.connection() as con:
            doc=con.execute('SELECT * FROM advanced_documents WHERE id=?',(doc_id,)).fetchone()
            ws=con.execute('SELECT * FROM weighings WHERE document_id=? ORDER BY sequence',(doc_id,)).fetchall()
        direct=doc['movement_type']=='external_to_production'
        production=self.production_for(doc_id) if direct else None
        if not ws or ws[0]['role'] not in ('gross','tare') or not doc['waybill_number'] or (not direct and not doc['destination_warehouse_id']):
            return None
        if direct and not production:raise ValueError('پیش از ثبت وزن، سفارش تولید اودو را انتخاب کنید.')
        policy=json.loads(doc['material_policy_json'] or '{}')
        return {'schema_version':3,'event_type':'weighbridge.capture','event_id':doc['event_id'],
            'material_type_id':doc['material_type_id'],'material_policy_id':policy.get('id'),
            **({'production_id':production} if direct else {'destination_warehouse_id':doc['destination_warehouse_id']}),
            'document':{'document_number':doc['document_number'],'movement_type':doc['movement_type'],'registered_at':doc['registered_at'],'notes':doc['notes'] or ''},
            'waybill':{'number':int(doc['waybill_number']),'reservation_token':doc['waybill_reservation_token']},
            'vehicle':{'trailer_plate':doc['trailer_plate']},'driver':{'name':doc['driver_name'],'phone':doc['driver_phone']},
            'origin':{'province':doc['origin_province'],'city':doc['origin_city']},
            'weighings':[{'sequence':w['sequence'],'kind':w['role'],'weight_kg':w['weight_kg'],'source':'manual' if w['source']=='manual' else 'device',
                'captured_at':w['captured_at'],'manual_reason':w['manual_reason'],'raw_payload':w['raw_payload']} for w in ws]}

    def queue_capture(self, doc_id):
        payload=self.capture_payload(doc_id)
        if not payload:return False
        phase=len(payload['weighings'])
        with self.connection() as con:
            base=con.execute('SELECT payload_json FROM odoo_capture_events WHERE document_id=? AND phase=1',(doc_id,)).fetchone()
        if base:
            frozen=json.loads(base[0]); frozen['weighings']=payload['weighings']; payload=frozen
        measured={w['kind']:w['weight_kg'] for w in payload['weighings']}
        if phase==2 and (set(measured)!={'gross','tare'} or measured['gross']<=measured['tare']):
            raise ValueError('وزن پر باید بیشتر از وزن خالی باشد.')
        text=json.dumps(payload,ensure_ascii=False,sort_keys=True)
        with self._lock,self.connection() as con:
            previous=con.execute('SELECT payload_json FROM odoo_capture_events WHERE document_id=? AND phase=?',(doc_id,phase)).fetchone()
            if previous:
                if previous[0] != text:raise ValueError('توزین ارسال‌شده ثابت است؛ برای اصلاح باید سند را در اودو ابطال کنید.')
                return False
            if phase==2:
                first=con.execute('SELECT payload_json FROM odoo_capture_events WHERE document_id=? AND phase=1',(doc_id,)).fetchone()
                if first and json.loads(first[0])['weighings'][0] != payload['weighings'][0]:
                    raise ValueError('وزن خالی ثبت‌شده قابل تغییر نیست.')
            stamp=datetime.now().astimezone().isoformat(timespec='seconds')
            cur=con.execute('INSERT INTO webhook_events(event_id,document_id,event_type,occurred_at,payload_json) VALUES(?,?,?,?,?)',
                (payload['event_id']+':capture:'+str(phase),doc_id,'weighbridge.capture',stamp,text))
            con.execute("INSERT INTO event_deliveries(event_id,endpoint_id) SELECT ?,id FROM endpoints WHERE active=1 AND kind='weighbridge'",(cur.lastrowid,))
            self._stamp_actor(con,'weighbridge',payload['event_id'])
            self._stamp_actor(con,'weighbridge',payload['event_id']+':capture:'+str(phase))
            con.execute('INSERT INTO odoo_capture_events VALUES(?,?,?)',(doc_id,phase,text))
            con.execute("UPDATE advanced_documents SET status=? WHERE id=?",('first_weighed' if phase==1 else 'finalized',doc_id))
        return True


class CaptureDialogMixin:
    def material_changed(self):
        super().material_changed()
        # Selecting the receipt refreshes policy widgets in the shared form.
        # Keep assessment exclusively in Odoo for this edition's inbound flow.
        if hasattr(self,'quality_form') and getattr(self,'_staged_candidate',False) and self.movement.currentData() in ('external_to_warehouse','external_to_production'):
            self.configure_staged()

    def configure_staged(self):
        if not hasattr(self,'_staged_candidate'):return
        self._staged=self._staged_candidate and self.movement.currentData() in ('external_to_warehouse','external_to_production')
        first_kind=getattr(self,'_staged_first_kind','gross')
        first_label='وزن پر' if first_kind=='gross' else 'وزن خالی'
        second_label='وزن خالی' if first_kind=='gross' else 'وزن پر'
        replacements={
            'ثبت وزن پر تریلی':'ثبت '+first_label+' خودرو',
            'در این مرحله فقط وزن پر از باسکول دریافت یا در حالت اضطراری دستی ثبت می‌شود.':'ابتدا '+first_label+' ثبت و روی همین سند به اودو ارسال می‌شود؛ ادامه توزین را بعداً می‌توانید انجام دهید.',
            'وزن خالی و کنترل کیفیت':'ثبت '+second_label+' و ارسال برای کنترل کیفیت اودو',
            'وزن خالی را ثبت کنید و سپس ترکیب قراضه‌های پذیرفته‌شده را وارد کنید.':second_label+' را ثبت کنید؛ ترکیب محموله، افت و برگشتی پس از تکمیل توزین در اودو ارزیابی می‌شوند.',
        }
        for page in (self.pages.widget(1),self.pages.widget(2)):
            for label in page.findChildren(QLabel):
                original=label.property('ms_original_text')
                if original is None:
                    original=label.text();label.setProperty('ms_original_text',original)
                if original in replacements:label.setText(replacements[original] if self._staged else original)
                if not self._staged and (original=='اقلام بار' or original.startswith('مجموع درصد اقلام')):label.show()
        for widget in getattr(self,'_production_widgets',[]):widget.setVisible(not self._staged or self.movement.currentData()=='external_to_production')
        if not self._staged:
            self.step_labels[1].setText('۲  وزن پر تریلی');self.step_labels[2].setText('۳  وزن خالی و کنترل کیفیت')
            self.first_weighing_form.labelForField(self.first_weight).setText('وزن پر')
            self.second_weighing_form.labelForField(self.second_weight).setText('وزن خالی')
            self.manual_first.setText('ثبت وزن دستی پر');self.manual_second.setText('ثبت وزن دستی خالی')
            self.finish_button.setText('محاسبه و صدور سند')
            for widget in (self.quality_code,self.quality_lookup,self.quality_status,self.composition,self.add_material_button,self.remove_material_button,self.loss,self.rejected,self.expert):widget.show()
            for field in (self.loss,self.rejected,self.expert):self.quality_form.labelForField(field).show()
            return
        self.step_labels[1].setText('۲  '+first_label)
        self.step_labels[2].setText('۳  '+second_label+'؛ QC در اودو')
        for form,spin,label in ((self.first_weighing_form,self.first_weight,first_label),(self.second_weighing_form,self.second_weight,second_label)):
            form.labelForField(spin).setText(label)
        self.manual_first.setText('ثبت '+first_label+' دستی');self.manual_second.setText('ثبت '+second_label+' دستی')
        self.finish_button.setText('تکمیل توزین و ارسال برای QC اودو')
        for widget in (self.quality_code,self.quality_lookup,self.quality_results,self.quality_status,self.composition,self.add_material_button,self.remove_material_button,self.loss,self.rejected,self.expert):
            widget.hide()
        for field in (self.loss,self.rejected,self.expert):self.quality_form.labelForField(field).hide()
        for label in self.pages.widget(2).findChildren(QLabel):
            if label.text()=='اقلام بار' or label.text().startswith('مجموع درصد اقلام'):label.hide()
            elif label.text()=='وزن خالی و کنترل کیفیت':label.setText('ثبت وزن پر؛ ارزیابی کیفیت پس از تکمیل توزین در اودو انجام می‌شود')
        weights,_=self.db.document_parts(self.document_id) if self.document_id else ([],[])
        if weights:
            self._staged_first_frozen=True
            for widget in getattr(self,'_production_widgets',[]):widget.setEnabled(False)
            self.pages.widget(0).setEnabled(False)
            self.first_weight.setReadOnly(True);self.capture_first.setEnabled(False);self.manual_first.setEnabled(False)

    def refresh_scale(self):
        super().refresh_scale()
        if getattr(self,'_staged',False) and getattr(self,'_staged_first_frozen',False):
            self.first_weight.setReadOnly(True)
            for button in (self.capture_first,self.manual_first,self.lock_first):button.setEnabled(False)

    def toggle_weight_lock(self, sequence):
        if not getattr(self,'_staged',False):return super().toggle_weight_lock(sequence)
        if sequence in self.locked_readings:
            del self.locked_readings[sequence];return
        if not self.scale_connected or not self.latest or not self.latest.stable:
            return message(self,'قفل وزن','اتصال باسکول و وزن پایدار لازم است.',True)
        self.locked_readings[sequence]=self.latest
        kind=getattr(self,'_staged_first_kind','gross')
        gross=(sequence==1)==(kind=='gross')
        message(self,'قفل وزن پر' if gross else 'قفل وزن خالی','وزن پایدار برای ثبت همین توزین قفل شد.')

    def goto_step(self, step):
        if getattr(self,'_staged',False) and getattr(self,'_split_ready',False):
            step=min(step,1) if self._first_trip else 2
        super().goto_step(step)
        if getattr(self,'_staged',False):
            first='وزن پر' if getattr(self,'_staged_first_kind','gross')=='gross' else 'وزن خالی'
            second='وزن خالی' if first=='وزن پر' else 'وزن پر'
            self.progress_label.setText(('مرحله ۱ از ۲ · اطلاعات اولیه','مرحله ۲ از ۲ · ثبت '+first+' در اودو','توزین دوم · ثبت '+second+' و ارجاع به کنترل کیفیت')[self.current_step])
            if getattr(self,'_split_ready',False):
                self.step_labels[0].setVisible(self._first_trip);self.step_labels[1].setVisible(self._first_trip);self.step_labels[2].setVisible(not self._first_trip)
                self.back_button.setVisible(self._first_trip and self.current_step==1)
                self.next_button.setVisible(self._first_trip and self.current_step==0)
                self.finish_button.setVisible(not self._first_trip or self.current_step==1)
                if self._first_trip:self.finish_button.setText('ثبت وزن پر و ارسال به اودو')

    def next_step(self):
        if getattr(self,'_staged',False) and self.current_step==1 and self.first_weight.value()<=0:
            return message(self,'توزین اول','ابتدا توزین اول خودرو را ثبت کنید.',True)
        return super().next_step()

    def persist_draft(self):
        hidden=[]
        if getattr(self,'_staged',False):
            for seq,spin in ((1,self.first_weight),(2,self.second_weight)):
                if seq not in self.captures and spin.value()>0:
                    hidden.append((spin,spin.value()));spin.setValue(0)
        try:super().persist_draft()
        finally:
            for spin,value in hidden:spin.setValue(value)
        if getattr(self,'_staged',False) and self.document_id:
            # The base form writes in display order; map the roles explicitly.
            with self.db.connection() as con:
                first=getattr(self,"_staged_first_kind","gross")
                second="tare" if first=="gross" else "gross"
                con.execute("UPDATE weighings SET role=CASE sequence WHEN 1 THEN ? ELSE ? END WHERE document_id=?",(first,second,self.document_id))
            if self.db.queue_capture(self.document_id):
                parent=self.parent()
                if parent and hasattr(parent,'start_sync'):parent.start_sync()
            self.configure_staged()

    def capture(self, sequence):
        if not getattr(self,'_staged',False):return super().capture(sequence)
        if not self.scale_connected:return message(self,'اتصال باسکول',self.scale_message or 'باسکول متصل نیست.',True)
        reading=self.locked_readings.get(sequence) or self.latest
        if not reading or not reading.stable:return message(self,'وزن ناپایدار','وزن پایدار لازم است.',True)
        if sequence==2 and not self._valid_second_weight(float(reading.weight_kg)):return
        spin=self.first_weight if sequence==1 else self.second_weight
        spin.setValue(round(float(reading.weight_kg)))
        self.captures[sequence]={'weight_kg':spin.value(),'source':'device','captured_at':datetime.now().astimezone().isoformat(timespec='seconds'),'stable':True,'manual_reason':'','raw_payload':reading.raw_payload}
        try:self.persist_draft()
        except Exception as exc:return message(self,'ثبت توزین',str(exc),True)
        self._first_capture_saved=sequence==1
        self.finalizing=True;self.accept()

    def manual(self, sequence):
        if not getattr(self,'_staged',False):return super().manual(sequence)
        if self.scale_connected:return message(self,'ثبت دستی','باسکول متصل است؛ از ثبت وزن پایدار استفاده کنید.',True)
        spin=self.first_weight if sequence==1 else self.second_weight
        if spin.value()<=0:return message(self,'ثبت وزن','وزن مثبت وارد کنید.',True)
        if sequence==2 and not self._valid_second_weight(spin.value()):return
        reason=self.scale_message or 'اتصال معتبر باسکول برقرار نیست.'
        (self.first_manual if sequence==1 else self.second_manual).setText(reason)
        self.captures[sequence]={'weight_kg':spin.value(),'source':'manual','captured_at':datetime.now().astimezone().isoformat(timespec='seconds'),'stable':False,'manual_reason':reason,'raw_payload':''}
        try:self.persist_draft()
        except Exception as exc:return message(self,'ثبت توزین',str(exc),True)
        self._first_capture_saved=sequence==1
        self.finalizing=True;self.accept()

    def _valid_second_weight(self,value):
        first=self.first_weight.value()
        gross,tare=(first,value) if getattr(self,'_staged_first_kind','gross')=='gross' else (value,first)
        if first<=0 or tare<=0 or gross<=tare:
            message(self,'توزین دوم','وزن پر باید بیشتر از وزن خالی باشد.',True)
            return False
        return True

    def finish(self):
        if not getattr(self,'_staged',False):return super().finish()
        if not self._base_complete():self.goto_step(0);return
        sequence=1 if getattr(self,'_first_trip',False) else 2
        return self.capture(sequence) if self.scale_connected else self.manual(sequence)

    validate=finish
