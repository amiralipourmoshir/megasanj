"""Typed material reference cache and v3 serialization, separate from delivery status."""
import json
import uuid
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

import requests

from .services import auth_headers, safe_response_error


def fetch_materials(url, token, known_version=""):
    response = requests.get(url, params={"known_version": known_version}, headers=auth_headers(token), timeout=15)
    if not response.ok:
        raise RuntimeError(safe_response_error(response))
    result = response.json()
    if result.get("schema_version") != 3:
        raise ValueError("نسخه اطلاعات مرجع کالا پشتیبانی نمی‌شود.")
    return result


class MaterialCache:
    def reopen_rejected_document(self, doc_id):
        """Only explicit validation rejections can be edited; lost replies remain retries."""
        with self._lock, self.connection() as con:
            doc = con.execute("SELECT * FROM advanced_documents WHERE id=?",(doc_id,)).fetchone()
            deliveries = con.execute("SELECT d.* FROM event_deliveries d JOIN webhook_events e ON e.id=d.event_id WHERE e.document_id=?",(doc_id,)).fetchall()
            if not doc or not deliveries or any(d['status']!='needs_attention' or d['response_code'] not in (409,422) for d in deliveries):
                raise ValueError('فقط سندی که همه مقصدها صریحاً رد کرده‌اند قابل اصلاح است؛ برای پاسخ نامشخص، ارسال مجدد را بزنید.')
            new_id = str(uuid.uuid4())
            con.execute("DELETE FROM event_deliveries WHERE event_id IN (SELECT id FROM webhook_events WHERE document_id=?)",(doc_id,))
            con.execute("DELETE FROM webhook_events WHERE document_id=?",(doc_id,))
            con.execute("UPDATE advanced_documents SET status='draft',event_id=?,finalized_at=NULL WHERE id=?",(new_id,doc_id))
            con.execute("UPDATE desktop_event_owners SET event_id=? WHERE capability='weighbridge' AND event_id=?",(new_id,doc['event_id']))
            con.execute("INSERT INTO audit_log(document_id,action,details,actor,created_at) VALUES(?,?,?,?,?)",(doc_id,'document.reopened',json.dumps({'previous_event_id':doc['event_id'],'event_id':new_id}),self.session.user['name'] if self.session else '',datetime.now().astimezone().isoformat()))

    def migrate_materials(self):
        with self.connection() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS material_types(id INTEGER PRIMARY KEY,code TEXT NOT NULL UNIQUE,name TEXT NOT NULL,is_active INTEGER NOT NULL,policy_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS material_items(id INTEGER PRIMARY KEY,material_type_id INTEGER NOT NULL REFERENCES material_types(id),code TEXT NOT NULL UNIQUE,name TEXT NOT NULL,is_active INTEGER NOT NULL,is_warehouse_storable INTEGER NOT NULL,scrap_type_id TEXT);
                CREATE TABLE IF NOT EXISTS material_companies(id INTEGER PRIMARY KEY,name TEXT NOT NULL,code TEXT,details_json TEXT NOT NULL,is_active INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS supplier_material_types(id INTEGER PRIMARY KEY,supplier_company_id INTEGER NOT NULL,material_type_id INTEGER NOT NULL REFERENCES material_types(id),is_active INTEGER NOT NULL,UNIQUE(supplier_company_id,material_type_id));
            """)
            con.execute("CREATE TABLE IF NOT EXISTS warehouse_reference(id INTEGER PRIMARY KEY, details_json TEXT NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS warehouse_balances(warehouse_id INTEGER NOT NULL,material_item_id INTEGER NOT NULL,quantity_g INTEGER NOT NULL,PRIMARY KEY(warehouse_id,material_item_id))")
            con.execute("CREATE TABLE IF NOT EXISTS warehouse_defaults(factory_id INTEGER NOT NULL,material_type_id INTEGER NOT NULL,warehouse_id INTEGER NOT NULL,PRIMARY KEY(factory_id,material_type_id))")
            columns = {row[1] for row in con.execute("PRAGMA table_info(advanced_documents)")}
            for name, declaration in (("source_warehouse_id", "INTEGER"), ("destination_warehouse_id", "INTEGER"), ("warehouse_snapshot_json", "TEXT")):
                if name not in columns:
                    con.execute(f"ALTER TABLE advanced_documents ADD COLUMN {name} {declaration}")
            columns = {row[1] for row in con.execute("PRAGMA table_info(processing_documents)")}
            for name, declaration in (("source_warehouse_id", "INTEGER"), ("destination_warehouse_id", "INTEGER"), ("warehouse_snapshot_json", "TEXT")):
                if name not in columns:
                    con.execute(f"ALTER TABLE processing_documents ADD COLUMN {name} {declaration}")
            for table in ("advanced_documents", "waybills"):
                columns = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
                for name, declaration in (("material_type_id", "INTEGER"), ("material_policy_json", "TEXT"), ("material_movement", "TEXT"), ("material_name", "TEXT")):
                    if name not in columns:
                        con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    def material_reference_url(self):
        central = self.get_setting("admin_panel_url").rstrip('/')
        if central:
            return central + '/api/transportation/reference/materials'
        source = self.get_setting("scrap_api_url")
        if not source:
            return ""
        parts = urlsplit(source)
        prefix=parts.path.split('/api/',1)[0]
        return urlunsplit((parts.scheme, parts.netloc, prefix+"/api/transportation/reference/materials", "", ""))

    def save_material_reference(self, response):
        if response.get("not_modified"):
            return
        data = response["data"]
        # Never clear a valid cache on malformed or incomplete network data.
        for key in ("material_types", "material_items", "supplier_material_types", "companies"):
            if not isinstance(data.get(key), list):
                raise ValueError("اطلاعات مرجع کامل نیست.")
        with self._lock, self.connection() as con:
            if "warehouses" in data:
                if not isinstance(data["warehouses"], list) or not isinstance(data.get("warehouse_receiving_defaults"), list):
                    raise ValueError("اطلاعات مرجع انبارها کامل نیست.")
                con.execute("DELETE FROM warehouse_reference")
                con.execute("DELETE FROM warehouse_defaults")
                for warehouse in data["warehouses"]:
                    con.execute("INSERT INTO warehouse_reference VALUES(?,?)", (warehouse["id"], json.dumps(warehouse,ensure_ascii=False)))
                if "warehouse_balances" in data:
                    if not isinstance(data["warehouse_balances"],list):raise ValueError("موجودی مرجع انبار معتبر نیست.")
                    con.execute("DELETE FROM warehouse_balances")
                    for balance in data["warehouse_balances"]:
                        con.execute("INSERT INTO warehouse_balances VALUES(?,?,?)",(balance["warehouse_id"],balance["material_item_id"],balance["quantity_g"]))
                for default in data["warehouse_receiving_defaults"]:
                    con.execute("INSERT INTO warehouse_defaults VALUES(?,?,?)",(default["factory_id"],default["material_type_id"],default["warehouse_id"]))
            con.execute("DELETE FROM supplier_material_types")
            con.execute("DELETE FROM material_items")
            con.execute("DELETE FROM material_companies")
            con.execute("DELETE FROM material_types")
            for row in data["material_types"]:
                con.execute("INSERT INTO material_types VALUES(?,?,?,?,?)", (row["id"],row["code"],row["name"],int(row["is_active"]),json.dumps(row["policy"],ensure_ascii=False)))
            for row in data["material_items"]:
                con.execute("INSERT INTO material_items VALUES(?,?,?,?,?,?,?)", (row["id"],row["material_type_id"],row["code"],row["name"],int(row["is_active"]),int(row["is_warehouse_storable"]),str(row["scrap_type_id"]) if row.get("scrap_type_id") else None))
            for row in data["companies"]:
                if row.get("id"):
                    con.execute("INSERT INTO material_companies VALUES(?,?,?,?,?)",(row["id"],row["name"],row.get("code"),json.dumps(row,ensure_ascii=False),int(row["is_active"])))
            companies_by_id = {str(row['id']): row for row in data['companies']}
            companies_by_code = {str(row.get('code')): row for row in data['companies'] if row.get('code')}
            for table in ('waybills', 'advanced_documents'):
                columns = {r[1] for r in con.execute(f'PRAGMA table_info({table})')}
                if 'legacy_supplier_snapshot' not in columns:continue
                for doc in con.execute(f'SELECT id,supplier_company_id,company_snapshot,legacy_supplier_snapshot FROM {table}').fetchall():
                    historical = json.loads(doc['legacy_supplier_snapshot'] or '{}')
                    company = companies_by_id.get(str(doc['supplier_company_id'])) if doc['supplier_company_id'] else companies_by_code.get(str(historical.get('code') or ''))
                    if not company:continue
                    current = json.loads(doc['company_snapshot'] or '{}')
                    # Restore missing company data, never replace a captured historical name.
                    snapshot = dict(company, **current) if current.get('name') else company
                    con.execute(f'UPDATE {table} SET supplier_company_id=?,company_snapshot=? WHERE id=?', (company['id'],json.dumps(snapshot,ensure_ascii=False),doc['id']))
            for row in data["supplier_material_types"]:
                con.execute("INSERT INTO supplier_material_types VALUES(?,?,?,?)",(row["id"],row["supplier_company_id"],row["material_type_id"],int(row["is_active"])))
            con.execute("INSERT INTO settings VALUES('material_reference_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(response["snapshot_version"],))
            con.execute("INSERT INTO settings VALUES('material_reference_received_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(datetime.now().astimezone().isoformat(),))

    def material_types(self):
        with self.connection() as con:
            return [dict(row) for row in con.execute("SELECT * FROM material_types WHERE is_active=1 ORDER BY name")]

    def waybill_companies(self):
        with self.connection() as con:
            rows = con.execute("SELECT * FROM material_companies WHERE is_active=1 ORDER BY name").fetchall()
            if self.get_setting('material_reference_version'):
                return [json.loads(row["details_json"]) for row in rows]
        return [dict(row) for row in self.companies()]

    def refresh_reference_aliases(self):
        """Feed existing processing forms from the unified catalog, without extra HTTP calls."""
        with self.connection() as con:
            scraps=[dict(row) for row in con.execute("SELECT i.scrap_type_id AS id,i.name FROM material_items i JOIN material_types t ON t.id=i.material_type_id WHERE t.code='scrap' AND t.is_active=1 AND i.is_active=1 AND i.is_warehouse_storable=1 AND i.scrap_type_id IS NOT NULL")]
        self.save_scraps(scraps)
        self.save_companies([dict(row,active=True) for row in self.waybill_companies()])

    def material_items(self, type_id):
        with self.connection() as con:
            return [dict(row) for row in con.execute("SELECT * FROM material_items WHERE material_type_id=? AND is_active=1 AND is_warehouse_storable=1 ORDER BY name",(type_id,))]

    def material_companies(self, type_id):
        with self.connection() as con:
            return [json.loads(row["details_json"]) for row in con.execute("SELECT v.* FROM material_companies v JOIN supplier_material_types r ON r.supplier_company_id=v.id JOIN material_types t ON t.id=r.material_type_id WHERE r.material_type_id=? AND r.is_active=1 AND v.is_active=1 AND t.is_active=1 ORDER BY v.name",(type_id,))]

    def set_document_material(self, doc_id, type_id, policy, movement):
        with self.connection() as con:
            row = con.execute("SELECT status,material_type_id FROM advanced_documents WHERE id=?",(doc_id,)).fetchone()
            if not row or row["status"] == "finalized":
                raise ValueError("نوع کالای سند نهایی قابل تغییر نیست.")
            if row["material_type_id"] and row["material_type_id"] != type_id:
                con.execute("DELETE FROM document_compositions WHERE document_id=?",(doc_id,))
            material=con.execute("SELECT name FROM material_types WHERE id=?",(type_id,)).fetchone()
            con.execute("UPDATE advanced_documents SET material_type_id=?,material_policy_json=?,material_movement=?,material_name=COALESCE(material_name,?) WHERE id=?",(type_id,json.dumps(policy),movement,material[0] if material else '',doc_id))

    def material_payload(self, con, doc_id, payload):
        doc = con.execute("SELECT * FROM advanced_documents WHERE id=?",(doc_id,)).fetchone()
        if not doc["material_type_id"]:
            return payload  # An existing v2 outbox is never rewritten.
        payload = dict(payload)
        payload["schema_version"] = 3
        payload["material_type_id"] = doc["material_type_id"]
        policy = json.loads(doc["material_policy_json"] or "{}")
        if policy.get('id'):
            payload['material_policy_id'] = int(policy['id'])
        rows = con.execute("SELECT * FROM document_compositions WHERE document_id=? ORDER BY id",(doc_id,)).fetchall()
        composition = []
        for row in rows:
            if policy.get("calculator") == "scrap_v1":
                item = con.execute("SELECT id FROM material_items WHERE material_type_id=? AND scrap_type_id=?",(doc["material_type_id"],row["scrap_type_id"])).fetchone()
                if not item:
                    raise ValueError("قلم قراضه در اطلاعات مرجع جدید موجود نیست؛ همگام‌سازی کنید.")
                item_id = item[0]
            else:
                item_id = int(row["scrap_type_id"])
            composition.append({"material_item_id":item_id,"percentage":float(row["percentage"])})
        payload["composition"] = composition
        if doc["source_warehouse_id"]:
            payload["source_warehouse_id"] = doc["source_warehouse_id"]
        if doc["destination_warehouse_id"]:
            payload["destination_warehouse_id"] = doc["destination_warehouse_id"]
        for field in ("company", "quality", "items", "waybill_snapshot", "destination", "event_type"):
            payload.pop(field, None)
        if not policy.get("quality_enabled") or doc["movement_type"] in ("warehouse_to_production", "warehouse_transfer"):
            payload["quality_document_code"] = None
        return payload

    def warehouses(self, type_id=None):
        with self.connection() as con:
            rows = [json.loads(row[0]) for row in con.execute("SELECT details_json FROM warehouse_reference ORDER BY id")]
        return [w for w in rows if w.get("is_active") and (w.get("factory") or {}).get("is_active") and (w.get("factory") or {}).get("is_internal_factory") and not (w.get("factory") or {}).get("deleted_at") and (type_id is None or int(w["material_type_id"]) == int(type_id))]

    def resolve_warehouse(self, factory_id, type_id):
        candidates = [w for w in self.warehouses(type_id) if int(w["factory_id"]) == int(factory_id)]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise ValueError("انبار فعال سازگار برای این کارخانه و نوع کالا تعریف نشده است.")
        with self.connection() as con:
            default = con.execute("SELECT warehouse_id FROM warehouse_defaults WHERE factory_id=? AND material_type_id=?",(factory_id,type_id)).fetchone()
        if default:
            for warehouse in candidates:
                if int(warehouse["id"]) == int(default[0]):
                    return warehouse
        raise ValueError("چند انبار وجود دارد؛ پیش‌فرض دریافت مشخص نشده است.")

    def set_document_warehouses(self, doc_id, source_id, destination_id):
        with self._lock, self.connection() as con:
            doc=con.execute("SELECT status,material_type_id,movement_type FROM advanced_documents WHERE id=?",(doc_id,)).fetchone()
            if not doc or doc["status"] == "finalized":
                raise ValueError("مقصد سند نهایی قابل تغییر نیست.")
            selected=[]
            for warehouse_id in (source_id,destination_id):
                if warehouse_id:
                    warehouse=next((w for w in self.warehouses(doc["material_type_id"]) if int(w["id"])==int(warehouse_id)),None)
                    if not warehouse:
                        raise ValueError("انبار انتخابی غیرفعال یا با کالا ناسازگار است.")
                    selected.append(warehouse)
            if source_id and destination_id and int(source_id)==int(destination_id):
                raise ValueError("انبار مبدأ و مقصد یکسان است.")
            con.execute("UPDATE advanced_documents SET source_warehouse_id=?,destination_warehouse_id=?,warehouse_snapshot_json=? WHERE id=?",(source_id,destination_id,json.dumps(selected,ensure_ascii=False),doc_id))

    def warehouse_item_ids(self, warehouse_id):
        with self.connection() as con:
            return {int(row[0]) for row in con.execute("SELECT material_item_id FROM warehouse_balances WHERE warehouse_id=? AND quantity_g>0",(warehouse_id,))}
