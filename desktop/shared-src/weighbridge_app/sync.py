from PySide6.QtCore import QObject, Signal, Slot

from .database import Database
from .services import send_webhook, fetch_transportation_sync, fetch_scraps, fetch_companies
import json
from datetime import datetime


class SyncWorker(QObject):
    finished = Signal(int, int)
    progress = Signal(str)
    attention = Signal(str, str)
    weight_discrepancy = Signal(str, dict)

    def __init__(self, db: Database):
        super().__init__()
        self.db = db

    @Slot()
    def run(self):
        ok_count = fail_count = 0
        warned_documents = set()
        for row in self.db.allowed_deliveries('processing', self.db.pending_processing_deliveries(200)):
            self.progress.emit(f"ارسال سند فرآوری {row['document_number']}…")
            ok, code, error, response = send_webhook(row["url"],self.db.delivery_token(row["url"],row["token"]),self.db.processing_payload(row))
            official=""
            if ok and isinstance(response,dict):official=str((response.get("data") or {}).get("document_number") or "")
            self.db.mark_processing_delivery(row["delivery_id"],ok,code,error,official)
            if not ok and row["attempts"]==0:self.attention.emit(row["document_number"],error)
            ok_count += int(ok); fail_count += int(not ok)
        for row in self.db.allowed_deliveries('waybills', self.db.pending_waybill_deliveries(200)):
            number = str(row["number"])
            self.progress.emit(f"ارسال بارنامه {number}…")
            ok, code, error, response = send_webhook(row["url"], self.db.delivery_token(row["url"],row["token"]), self.db.waybill_payload(row))
            terminal = bool(code and 400 <= code < 500 and code not in (408, 429))
            if ok and not row["server_confirmed"]:
                try:
                    response_data = response.get("data", response) if isinstance(response, dict) else {}
                    official_number = int(response_data.get("number") or 0)
                    self.db.confirm_waybill_number(row["waybill_local_id"], official_number)
                    with self.db.connection() as con:
                        con.execute("UPDATE waybills SET queue_number=?,queue_day=? WHERE id=?",(response_data.get("queue_number"),response_data.get("queue_day"),row["waybill_local_id"]))
                except (TypeError, ValueError) as exc:
                    ok = False
                    terminal = True
                    error = str(exc) or "سرور شماره قطعی بارنامه را برنگرداند."
            self.db.mark_waybill_delivery(row["delivery_id"], ok, code, error, needs_attention=terminal)
            if not ok and (terminal or row["attempts"] == 0): self.attention.emit(f"بارنامه {number}", error)
            ok_count += int(ok); fail_count += int(not ok)
        for row in self.db.allowed_deliveries('weighbridge', self.db.pending_event_deliveries(200)):
            payload = json.loads(row["payload_json"])
            number = payload.get("document", {}).get("document_number", "سند")
            self.progress.emit(f"ارسال {number}…")
            ok, code, error, response = send_webhook(row["url"], self.db.delivery_token(row["url"],row["token"]), payload)
            terminal = bool(code and 400 <= code < 500 and code not in (408, 429))
            self.db.mark_event_delivery(row["delivery_id"], ok, code, error, needs_attention=terminal)
            if not ok and (terminal or row["attempts"] == 0): self.attention.emit(number, error)
            if ok and isinstance(response, dict):
                self.db.save_event_weight_discrepancy(row["delivery_id"], response)
            discrepancy = response.get("weight_discrepancy") if ok and isinstance(response, dict) else None
            if isinstance(discrepancy, dict) and number not in warned_documents:
                warned_documents.add(number)
                discrepancy["comparison"] = response.get("weight_comparison") or {}
                self.weight_discrepancy.emit(number, discrepancy)
            ok_count += int(ok); fail_count += int(not ok)
        for row in self.db.allowed_deliveries('weighbridge', self.db.pending_deliveries(200)):
            self.progress.emit(f"ارسال {row['document_number']}…")
            ok, code, error, _response = send_webhook(row["url"], self.db.delivery_token(row["url"],row["token"]), self.db.payload(row))
            terminal = bool(code and 400 <= code < 500 and code not in (408, 429))
            self.db.mark_delivery(row["delivery_id"], ok, code, error, needs_attention=terminal)
            if not ok and (terminal or row["attempts"] == 0):
                self.attention.emit(row["document_number"], error)
            ok_count += int(ok); fail_count += int(not ok)
        self.finished.emit(ok_count, fail_count)


class ReferenceDataWorker(QObject):
    finished = Signal(str)
    updated = Signal(str,bool,str)

    def __init__(self, db):
        super().__init__(); self.db=db

    @Slot()
    def run(self):
        errors=[]
        from .materials import fetch_materials
        url = self.db.material_reference_url()
        if url:
            try:
                result = fetch_materials(url,self.db.get_setting('transport_api_token'),self.db.get_setting('material_reference_version'))
                self.db.save_material_reference(result)
                if self.db.get_setting('material_reference_version'):
                    self.db.refresh_reference_aliases()
                    self.updated.emit('materials',True,'اطلاعات کالاها و حوزه‌های تأمین تازه شد')
                    self.finished.emit(''); return
                self.updated.emit('materials',True,'اطلاعات کالاها و حوزه‌های تأمین تازه شد')
            except Exception:
                errors.append('materials')
                self.updated.emit('materials',False,'دریافت کالاها انجام نشد؛ اطلاعات محلی حفظ شد.')
        for kind,url_key,token_key,fetch,save in (
            ('scraps','scrap_api_url','scrap_api_token',fetch_scraps,self.db.save_scraps),
            ('companies','company_api_url','company_api_token',fetch_companies,self.db.save_companies)):
            try:
                rows=fetch(self.db.get_setting(url_key),self.db.get_setting(token_key)); save(rows)
                self.updated.emit(kind,True,f'{len(rows)} مورد دریافت شد')
            except Exception:
                text='دریافت انجام نشد؛ آخرین اطلاعات محلی حفظ شد.'
                errors.append(kind); self.updated.emit(kind,False,text)
        self.finished.emit(','.join(errors))


class ServerCacheSyncWorker(QObject):
    finished = Signal(int, int, str)

    def __init__(self, db: Database):
        super().__init__()
        self.db = db

    @Slot()
    def run(self):
        waybill_count = document_count = 0
        try:
            url=self.db.transportation_sync_url()
            if not url:
                raise RuntimeError("آدرس سامانه مگاپالس در تنظیمات مشخص نشده است.")
            cursor=""
            # An old version token alone does not prove that this installation
            # has already replaced its legacy/local cache.  Force one complete
            # snapshot after upgrading to the authoritative-cache strategy.
            known_version=(self.db.get_setting("server_snapshot_version")
                           if (self.db.get_setting("server_snapshot_reconciled")=="1"
                               and self.db.get_setting("server_cache_policy_version")=="2"
                               and self.db.get_setting("server_quality_contract_version")=="1") else "")
            all_waybills=[]; all_documents=[]; final_sync={}
            for _page in range(100):
                response=fetch_transportation_sync(url,self.db.get_setting("transport_api_token"),cursor,known_version if not cursor else "")
                sync=response.get("sync") or {}
                if sync.get("not_modified"):
                    now=datetime.now().astimezone().isoformat(timespec="seconds")
                    self.db.set_setting("server_sync_last_success",now); self.db.delete_settings(["server_sync_last_error"])
                    self.finished.emit(0,0,""); return
                page_data=response.get("data") or {}
                all_waybills.extend(page_data.get("waybills") or [])
                all_documents.extend(page_data.get("documents") or [])
                final_sync=sync; cursor=str(sync.get("cursor") or cursor)
                if not sync.get("has_more"):break
            else:
                raise RuntimeError("حجم تغییرات بیش از حد مجاز یک نوبت همگام‌سازی بود.")
            combined={"data":{"waybills":all_waybills,"documents":all_documents},"sync":final_sync}
            waybill_count,document_count=self.db.apply_transportation_sync(combined,complete_snapshot=True)
            self.finished.emit(waybill_count,document_count,"")
        except Exception as exc:
            self.db.record_server_sync_error(str(exc))
            self.finished.emit(waybill_count,document_count,str(exc))
