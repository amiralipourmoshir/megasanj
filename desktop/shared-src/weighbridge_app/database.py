from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit


from .materials import MaterialCache


class Database(MaterialCache):
    erp_label = 'مگاپالس'
    PORTABLE_SETTING_KEYS = (
        "admin_panel_url",
        "scrap_api_url", "scrap_api_token",
        "company_api_url", "company_api_token",
        "waybill_available_url", "waybill_reserve_url", "waybill_release_url",
        "transport_api_token", "scale_port", "scale_baudrate",
        "scale_maximum_weight_kg",
    )

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.session = None
        self.upgrade_company_identity()
        self.migrate()
        self.upgrade_waybill_weight_constraint()
        self.migrate_materials()

    def upgrade_company_identity(self):
        """One-time, atomic upgrade of old personal reference ids; never guess equal ids."""
        with self._lock, self.connection() as con:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'vendors' not in tables:
                return
            backup_path = self.path + '.pre-company.bak'
            if not Path(backup_path).exists():
                backup = sqlite3.connect(backup_path)
                try:con.backup(backup)
                finally:backup.close()
            con.execute('BEGIN IMMEDIATE')
            mapping = {}
            if 'material_vendors' in tables:
                for row in con.execute('SELECT * FROM material_vendors'):
                    mapping[str(row['id'])] = dict(row)
            for table in ('waybills', 'advanced_documents'):
                if table not in tables:continue
                columns = {r[1] for r in con.execute(f'PRAGMA table_info({table})')}
                if 'vendor_id' not in columns:continue
                con.execute(f'ALTER TABLE {table} RENAME COLUMN vendor_id TO supplier_company_id')
                con.execute(f'ALTER TABLE {table} RENAME COLUMN vendor_snapshot TO company_snapshot')
                con.execute(f'ALTER TABLE {table} ADD COLUMN legacy_supplier_snapshot TEXT')
                for row in con.execute(f'SELECT id,supplier_company_id,company_snapshot FROM {table}').fetchall():
                    old = json.loads(row['company_snapshot'] or '{}')
                    reference = mapping.get(str(row['supplier_company_id']), {})
                    company_id = reference.get('company_id') or old.get('company_id') or (old.get('company') or {}).get('id')
                    company = dict(old.get('company') or {})
                    if company_id:
                        company.update(id=company_id, code=company.get('code') or reference.get('code') or old.get('code') or '', name=company.get('name') or reference.get('company_name') or old.get('company_name') or '')
                    else:
                        company = {}
                    con.execute(f'UPDATE {table} SET supplier_company_id=?,company_snapshot=?,legacy_supplier_snapshot=? WHERE id=?', (str(company_id or '') if table=='waybills' else company_id, json.dumps(company,ensure_ascii=False), row['company_snapshot'], row['id']))
            con.execute('ALTER TABLE vendors RENAME TO legacy_supplier_members')
            if 'material_vendors' in tables:
                con.execute('ALTER TABLE material_vendors RENAME TO legacy_material_members')
            for old_key,new_key in [('vendor_api_url','company_api_url'),('vendor_api_token','company_api_token')]:
                con.execute('INSERT OR IGNORE INTO settings(key,value) SELECT ?,value FROM settings WHERE key=?', (new_key,old_key))
                con.execute('DELETE FROM settings WHERE key=?', (old_key,))
            draft_row = con.execute("SELECT value FROM settings WHERE key='waybill_draft'").fetchone()
            if draft_row and draft_row[0]:
                draft = json.loads(draft_row[0])
                member = mapping.get(str(draft.pop('vendor_id', '')), {})
                draft['supplier_company_id'] = member.get('company_id')
                con.execute("UPDATE settings SET value=? WHERE key='waybill_draft'", (json.dumps(draft,ensure_ascii=False),))
            con.execute("UPDATE settings SET value=replace(replace(value,'/reference/vendors','/reference/companies'),'/weighbridge/vendors','/weighbridge/companies') WHERE key='company_api_url'")
            con.execute("DELETE FROM settings WHERE key='material_reference_version'")

    def upgrade_waybill_weight_constraint(self):
        # Rebuild the original table without changing child-table foreign-key targets.
        with self._lock:
            con=sqlite3.connect(self.path)
            try:
                sql=con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='waybills'").fetchone()[0]
                if "CHECK(origin_net_weight_kg>0)" not in sql:return
                indexes=[row[0] for row in con.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='waybills' AND sql IS NOT NULL")]
                sequence=con.execute("SELECT seq FROM sqlite_sequence WHERE name='waybills'").fetchone()
                con.execute("PRAGMA foreign_keys=OFF")
                con.execute("BEGIN IMMEDIATE")
                new_sql=sql.replace('CREATE TABLE waybills', 'CREATE TABLE waybills_rebuilt',1).replace('CREATE TABLE "waybills"', 'CREATE TABLE waybills_rebuilt',1).replace('CHECK(origin_net_weight_kg>0)','CHECK(origin_net_weight_kg>=0)')
                con.execute(new_sql)
                con.execute("INSERT INTO waybills_rebuilt SELECT * FROM waybills")
                con.execute("DROP TABLE waybills")
                con.execute("ALTER TABLE waybills_rebuilt RENAME TO waybills")
                for index in indexes:con.execute(index)
                if sequence:con.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='waybills'",(sequence[0],))
                if con.execute("PRAGMA foreign_key_check").fetchone():raise RuntimeError("Waybill migration failed foreign-key validation")
                con.commit()
            except Exception:
                con.rollback(); raise
            finally:con.close()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=20)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA journal_mode=WAL")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def migrate(self) -> None:
        with self.connection() as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scrap_types (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS endpoints (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL, url TEXT NOT NULL, token TEXT NOT NULL DEFAULT '',
              active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id TEXT NOT NULL UNIQUE, document_number TEXT NOT NULL UNIQUE,
              registered_at TEXT NOT NULL, scrap_id TEXT, scrap_name TEXT NOT NULL,
              weight_kg REAL NOT NULL CHECK(weight_kg > 0), movement_type TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deliveries (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              document_id INTEGER NOT NULL REFERENCES documents(id),
              endpoint_id INTEGER NOT NULL REFERENCES endpoints(id),
              status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
              last_error TEXT, response_code INTEGER, last_attempt_at TEXT, delivered_at TEXT,
              UNIQUE(document_id, endpoint_id)
            );
            CREATE INDEX IF NOT EXISTS ix_deliveries_status ON deliveries(status);
            CREATE INDEX IF NOT EXISTS ix_documents_registered ON documents(registered_at DESC);
            CREATE TABLE IF NOT EXISTS companies (id TEXT PRIMARY KEY,national_id TEXT NOT NULL DEFAULT '',registration_number TEXT NOT NULL DEFAULT '',code TEXT NOT NULL DEFAULT '',name TEXT NOT NULL,legal_name TEXT NOT NULL DEFAULT '',phone TEXT NOT NULL DEFAULT '',province TEXT NOT NULL DEFAULT '',city TEXT NOT NULL DEFAULT '',address TEXT NOT NULL DEFAULT '',description TEXT NOT NULL DEFAULT '',active INTEGER NOT NULL DEFAULT 1,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS advanced_documents (id INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL UNIQUE,document_number TEXT NOT NULL UNIQUE,movement_type TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'draft',registered_at TEXT NOT NULL,finalized_at TEXT,entry_at TEXT,exit_at TEXT,supplier_company_id TEXT,company_snapshot TEXT NOT NULL DEFAULT '{}',driver_name TEXT NOT NULL DEFAULT '',driver_phone TEXT NOT NULL DEFAULT '',vehicle_plate TEXT NOT NULL DEFAULT '',trailer_plate TEXT NOT NULL DEFAULT '',vehicle_type TEXT NOT NULL DEFAULT '',origin_province TEXT NOT NULL DEFAULT '',origin_city TEXT NOT NULL DEFAULT '',destination TEXT NOT NULL DEFAULT '',waybill_number TEXT NOT NULL DEFAULT '',expert_name TEXT NOT NULL DEFAULT '',assessed_at TEXT,rejected_percentage REAL NOT NULL DEFAULT 0,rejected_weight_kg REAL NOT NULL DEFAULT 0,loss_percentage REAL NOT NULL DEFAULT 0,loss_weight_kg REAL NOT NULL DEFAULT 0,accepted_weight_kg REAL NOT NULL DEFAULT 0,notes TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS weighings (id INTEGER PRIMARY KEY AUTOINCREMENT,document_id INTEGER NOT NULL REFERENCES advanced_documents(id) ON DELETE CASCADE,sequence INTEGER NOT NULL,role TEXT NOT NULL,weight_kg REAL NOT NULL,source TEXT NOT NULL,captured_at TEXT NOT NULL,stable INTEGER NOT NULL DEFAULT 0,manual_reason TEXT,raw_payload TEXT,operator_name TEXT NOT NULL DEFAULT '',UNIQUE(document_id,sequence));
            CREATE TABLE IF NOT EXISTS document_compositions (id INTEGER PRIMARY KEY AUTOINCREMENT,document_id INTEGER NOT NULL REFERENCES advanced_documents(id) ON DELETE CASCADE,scrap_type_id TEXT NOT NULL,scrap_name TEXT NOT NULL,percentage REAL NOT NULL,weight_kg REAL NOT NULL DEFAULT 0,notes TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT,document_id INTEGER,action TEXT NOT NULL,details TEXT NOT NULL DEFAULT '{}',actor TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS desktop_event_owners (capability TEXT NOT NULL,event_id TEXT NOT NULL,user_id INTEGER NOT NULL,PRIMARY KEY(capability,event_id));
            CREATE TABLE IF NOT EXISTS webhook_events (id INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL UNIQUE,document_id INTEGER NOT NULL REFERENCES advanced_documents(id),event_type TEXT NOT NULL,occurred_at TEXT NOT NULL,payload_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS event_deliveries (id INTEGER PRIMARY KEY AUTOINCREMENT,event_id INTEGER NOT NULL REFERENCES webhook_events(id) ON DELETE CASCADE,endpoint_id INTEGER NOT NULL REFERENCES endpoints(id),status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,response_code INTEGER,last_attempt_at TEXT,delivered_at TEXT,UNIQUE(event_id,endpoint_id));
            CREATE INDEX IF NOT EXISTS ix_event_deliveries_status ON event_deliveries(status);
            CREATE TABLE IF NOT EXISTS waybill_sequences (
              id INTEGER PRIMARY KEY CHECK(id=1), last_number INTEGER NOT NULL DEFAULT 0
            );
            INSERT OR IGNORE INTO waybill_sequences(id,last_number) VALUES(1,0);
            CREATE TABLE IF NOT EXISTS waybills (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id TEXT NOT NULL UNIQUE, number INTEGER NOT NULL UNIQUE,
              supplier_company_id TEXT NOT NULL, company_snapshot TEXT NOT NULL DEFAULT '{}',
              origin_net_weight_kg INTEGER NOT NULL CHECK(origin_net_weight_kg>=0),
              driver_name TEXT NOT NULL, driver_phone TEXT NOT NULL,
              trailer_plate TEXT NOT NULL, origin_province TEXT NOT NULL,
              origin_city TEXT NOT NULL, freight_amount INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL DEFAULT 'issued', reservation_token TEXT,
              reserved_at TEXT, reservation_expires_at TEXT, used_at TEXT,
              issued_at TEXT NOT NULL, created_at TEXT NOT NULL,
              server_confirmed INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS ix_waybills_status ON waybills(status,number DESC);
            CREATE TABLE IF NOT EXISTS waybill_deliveries (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              waybill_id INTEGER NOT NULL REFERENCES waybills(id) ON DELETE CASCADE,
              endpoint_id INTEGER NOT NULL REFERENCES endpoints(id),
              status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
              last_error TEXT, response_code INTEGER, last_attempt_at TEXT, delivered_at TEXT,
              UNIQUE(waybill_id,endpoint_id)
            );
            CREATE INDEX IF NOT EXISTS ix_waybill_deliveries_status ON waybill_deliveries(status);
            CREATE TABLE IF NOT EXISTS processing_documents (
              id INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL UNIQUE,
              document_number TEXT NOT NULL UNIQUE,processed_at TEXT NOT NULL,
              output_scrap_type_id TEXT NOT NULL,output_scrap_name TEXT NOT NULL,
              output_weight_kg INTEGER NOT NULL CHECK(output_weight_kg>0),
              quality_expert_name TEXT NOT NULL,notes TEXT NOT NULL DEFAULT '',
              weight_source TEXT NOT NULL,weight_stable INTEGER NOT NULL DEFAULT 0,
              manual_reason TEXT,raw_payload TEXT,created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS processing_inputs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,processing_document_id INTEGER NOT NULL REFERENCES processing_documents(id) ON DELETE CASCADE,
              scrap_type_id TEXT NOT NULL,scrap_name TEXT NOT NULL,percentage REAL NOT NULL,
              UNIQUE(processing_document_id,scrap_type_id)
            );
            CREATE TABLE IF NOT EXISTS processing_deliveries (
              id INTEGER PRIMARY KEY AUTOINCREMENT,processing_document_id INTEGER NOT NULL REFERENCES processing_documents(id) ON DELETE CASCADE,
              endpoint_id INTEGER NOT NULL REFERENCES endpoints(id),status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
              last_error TEXT,response_code INTEGER,last_attempt_at TEXT,delivered_at TEXT,UNIQUE(processing_document_id,endpoint_id)
            );
            CREATE INDEX IF NOT EXISTS ix_processing_deliveries_status ON processing_deliveries(status);
            CREATE TABLE IF NOT EXISTS processing_draft (
              id INTEGER PRIMARY KEY CHECK(id=1),current_step INTEGER NOT NULL DEFAULT 0,
              payload_json TEXT NOT NULL DEFAULT '{}',updated_at TEXT NOT NULL
            );
            """)
            endpoint_columns={row["name"] for row in con.execute("PRAGMA table_info(endpoints)").fetchall()}
            if "kind" not in endpoint_columns:
                con.execute("ALTER TABLE endpoints ADD COLUMN kind TEXT NOT NULL DEFAULT 'weighbridge'")
            endpoint_columns={row["name"] for row in con.execute("PRAGMA table_info(endpoints)").fetchall()}
            if "site_id" not in endpoint_columns:
                con.execute("ALTER TABLE endpoints ADD COLUMN site_id INTEGER")
            if "path" not in endpoint_columns:
                con.execute("ALTER TABLE endpoints ADD COLUMN path TEXT NOT NULL DEFAULT ''")
            con.execute("""CREATE TABLE IF NOT EXISTS webhook_sites (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL, base_url TEXT NOT NULL, token TEXT NOT NULL DEFAULT '',
              active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")
            # Older installations only had the weighbridge destination.  Add
            # the transportation waybill endpoint on the same server and copy
            # its token, otherwise issued waybills never enter any sync queue.
            has_waybill_endpoint=con.execute("SELECT 1 FROM endpoints WHERE kind='waybill' LIMIT 1").fetchone()
            if not has_waybill_endpoint:
                source=con.execute("SELECT * FROM endpoints WHERE kind='weighbridge' AND active=1 ORDER BY id LIMIT 1").fetchone()
                if source:
                    parts=urlsplit(source["url"])
                    if parts.scheme in ("http","https") and parts.netloc:
                        url=urlunsplit((parts.scheme,parts.netloc,"/api/transportation/webhooks/waybills","",""))
                        now=datetime.now().astimezone().isoformat(timespec="seconds")
                        con.execute("INSERT INTO endpoints(name,url,token,active,created_at,kind) VALUES(?,?,?,?,?,?)",
                                    ("بارنامه‌های حمل",url,source["token"],1,now,"waybill"))
            has_processing_endpoint=con.execute("SELECT 1 FROM endpoints WHERE kind='processing' LIMIT 1").fetchone()
            if not has_processing_endpoint:
                source=con.execute("SELECT * FROM endpoints WHERE kind='weighbridge' AND active=1 ORDER BY id LIMIT 1").fetchone()
                if source:
                    parts=urlsplit(source["url"])
                    if parts.scheme in ("http","https") and parts.netloc:
                        url=urlunsplit((parts.scheme,parts.netloc,"/api/webhooks/weighbridge/scrap-processing","",""))
                        now=datetime.now().astimezone().isoformat(timespec="seconds")
                        con.execute("INSERT INTO endpoints(name,url,token,active,created_at,kind) VALUES(?,?,?,?,?,?)",
                                    ("اسناد فرآوری",url,source["token"],1,now,"processing"))
            # Convert legacy standalone destinations to site/ERP groups.  The
            # final URL remains on endpoints so old sync queues keep working.
            legacy=con.execute("SELECT * FROM endpoints WHERE site_id IS NULL ORDER BY id").fetchall()
            grouped: dict[tuple[str,str], int] = {}
            now=datetime.now().astimezone().isoformat(timespec="seconds")
            for endpoint in legacy:
                parts=urlsplit(endpoint["url"])
                if parts.scheme not in ("http","https") or not parts.netloc:
                    continue
                base=urlunsplit((parts.scheme,parts.netloc,"","",""))
                key=(base,endpoint["token"] or "")
                site_id=grouped.get(key)
                if site_id is None:
                    current=con.execute("SELECT id FROM webhook_sites WHERE base_url=? AND token=? ORDER BY id LIMIT 1",key).fetchone()
                    if current:
                        site_id=int(current["id"])
                    else:
                        cursor=con.execute("INSERT INTO webhook_sites(name,base_url,token,active,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                            (endpoint["name"] or parts.netloc,base,endpoint["token"] or "",int(endpoint["active"]),now,now))
                        site_id=int(cursor.lastrowid)
                    grouped[key]=site_id
                relative=parts.path or "/"
                if parts.query: relative += "?" + parts.query
                con.execute("UPDATE endpoints SET site_id=?,path=? WHERE id=?",(site_id,relative,int(endpoint["id"])))
            # Backfill old local waybills whenever a destination becomes
            # available. INSERT OR IGNORE keeps this migration idempotent.
            con.execute("""INSERT OR IGNORE INTO waybill_deliveries(waybill_id,endpoint_id)
                SELECT w.id,e.id FROM waybills w CROSS JOIN endpoints e
                WHERE e.active=1 AND e.kind='waybill'""")
            con.execute("""INSERT OR IGNORE INTO processing_deliveries(processing_document_id,endpoint_id)
                SELECT p.id,e.id FROM processing_documents p CROSS JOIN endpoints e
                WHERE e.active=1 AND e.kind='processing'""")
            waybill_columns={row["name"] for row in con.execute("PRAGMA table_info(waybills)").fetchall()}
            for column,kind in (("queue_number","INTEGER"),("queue_day","TEXT")):
                if column not in waybill_columns:con.execute(f"ALTER TABLE waybills ADD COLUMN {column} {kind}")
            if "server_confirmed" not in waybill_columns:
                con.execute("ALTER TABLE waybills ADD COLUMN server_confirmed INTEGER NOT NULL DEFAULT 0")
                # Re-submit one authoritative delivery per legacy waybill. The
                # server resolves event_id idempotently and returns its real number.
                con.execute("""UPDATE waybill_deliveries SET status='pending'
                    WHERE id IN (SELECT MIN(d.id) FROM waybill_deliveries d
                    JOIN endpoints e ON e.id=d.endpoint_id
                    JOIN waybills w ON w.id=d.waybill_id
                    WHERE e.active=1 AND e.kind='waybill' AND w.server_confirmed=0
                    GROUP BY d.waybill_id)""")
            if "cache_source" not in waybill_columns:
                con.execute("ALTER TABLE waybills ADD COLUMN cache_source TEXT NOT NULL DEFAULT 'local'")
            for flag in ("freight_by_contractor", "no_origin_weighbridge"):
                if flag not in waybill_columns:
                    con.execute(f"ALTER TABLE waybills ADD COLUMN {flag} INTEGER NOT NULL DEFAULT 0")
            advanced_columns={row["name"] for row in con.execute("PRAGMA table_info(advanced_documents)").fetchall()}
            if "quality_pending" not in advanced_columns:
                con.execute("ALTER TABLE advanced_documents ADD COLUMN quality_pending INTEGER NOT NULL DEFAULT 0")
            if "quality_document_code" not in advanced_columns:
                con.execute("ALTER TABLE advanced_documents ADD COLUMN quality_document_code TEXT NOT NULL DEFAULT ''")
            for definition in (
                "weight_discrepancy_json TEXT NOT NULL DEFAULT '{}'",
                "waybill_id INTEGER", "waybill_reservation_token TEXT",
                "waybill_snapshot TEXT NOT NULL DEFAULT '{}'",
            ):
                name=definition.split()[0]
                if name not in advanced_columns:
                    con.execute(f"ALTER TABLE advanced_documents ADD COLUMN {definition}")
            if "cache_source" not in advanced_columns:
                con.execute("ALTER TABLE advanced_documents ADD COLUMN cache_source TEXT NOT NULL DEFAULT 'local'")
            if not con.execute("SELECT 1 FROM settings WHERE key='waybill_fingerprint_v2'").fetchone():
                con.execute("""UPDATE waybill_deliveries SET status='pending'
                    WHERE status='needs_attention' AND response_code=409
                    AND instr(COALESCE(last_error,''),'شناسه رویداد')>0""")
                con.execute("INSERT INTO settings(key,value) VALUES('waybill_fingerprint_v2','1')")

    def record_submission_error(self, event_id: str, error: str, code: int | None = None) -> None:
        details=json.dumps({"event_id":event_id,"error":str(error)[:1000],"response_code":code},ensure_ascii=False)
        now=datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connection() as con:
            con.execute("INSERT INTO audit_log(action,details,created_at) VALUES(?,?,?)",("waybill.submission_failed",details,now))

    def get_setting(self, key: str, default: str = "") -> str:
        if self.session:
            paths = {
                "scrap_api_url": "transportation/reference/scrap-types",
                "company_api_url": "transportation/reference/companies",
                "waybill_available_url": "transportation/waybills/available",
                "waybill_reserve_url": "transportation/waybills/{number}/reserve",
                "waybill_release_url": "transportation/waybills/{number}/release",
                "transportation_sync_url": "transportation/desktop-sync",
            }
            if key in paths:return self.session.api_url(paths[key])
            if key in ("transport_api_token", "scrap_api_token", "company_api_token"):return self.session.token
            if key == "admin_panel_url":return self.session.base_url
        with self.connection() as con:
            row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def for_session(self, session):
        current_site=self.get_setting('desktop_database_site')
        target=self
        if current_site and current_site != session.base_url:
            suffix=hashlib.sha256(session.base_url.encode()).hexdigest()[:16]
            original=Path(self.path).parent / 'weighbridge.sqlite3'
            if original.exists():
                primary=type(self)(original)
                target=primary if primary.get_setting('desktop_database_site') == session.base_url else type(self)(original.parent / ('weighbridge-'+suffix+'.sqlite3'))
            else:target=type(self)(original.parent / ('weighbridge-'+suffix+'.sqlite3'))
        target.set_setting('desktop_database_site',session.base_url)
        target.bind_session(session)
        return target

    def bind_session(self, session) -> None:
        self.session = session
        if self.get_setting('deleted_erp_'+hashlib.sha256(session.base_url.rstrip('/').encode()).hexdigest())=='1':return
        paths = {
            "weighbridge": "transportation/webhooks/weighbridge",
            "waybill": "transportation/webhooks/waybills",
            "processing": "webhooks/weighbridge/scrap-processing",
        }
        for kind, path in paths.items():
            url = session.api_url(path)
            with self.connection() as con:
                existing = con.execute("SELECT id FROM endpoints WHERE url=? AND active=1 ORDER BY id LIMIT 1", (url,)).fetchone()
            if not existing:self.upsert_endpoint(None,"مگاپالس · " + kind,url,"",True,kind)

    def allows(self, capability: str) -> bool:
        return bool(self.session and self.session.allows(capability))

    def delivery_token(self, url: str, fallback: str = "") -> str:
        if self.session:
            if not self.session.owns_url(url):raise ValueError("مقصد ارسال با سایت حساب واردشده یکسان نیست.")
            return self.session.token
        return fallback

    def allowed_deliveries(self, capability: str, rows):
        if not self.session:return rows
        if not self.allows(capability):return []
        permitted=[]
        with self.connection() as con:
            for row in rows:
                if not self.session.owns_url(row['url']):continue
                event_id=row['event_id'] if 'event_id' in row.keys() else json.loads(row['payload_json']).get('event_id')
                owner=con.execute('SELECT user_id FROM desktop_event_owners WHERE capability=? AND event_id=?',(capability,event_id)).fetchone()
                if owner and int(owner['user_id']) != int(self.session.user['id']):continue
                permitted.append(row)
        return permitted

    def _stamp_actor(self, con, capability: str, event_id: str):
        if self.session:
            con.execute('INSERT OR IGNORE INTO desktop_event_owners(capability,event_id,user_id) VALUES(?,?,?)',
                        (capability,event_id,int(self.session.user['id'])))

    def has_session_pending(self) -> bool:
        return any(self.allowed_deliveries(capability, rows) for capability, rows in (
            ('weighbridge', self.pending_deliveries()), ('weighbridge', self.pending_event_deliveries()),
            ('waybills', self.pending_waybill_deliveries()), ('processing', self.pending_processing_deliveries()),
        ))

    def transportation_sync_url(self) -> str:
        explicit=self.get_setting("transportation_sync_url").strip()
        if explicit:return explicit
        candidates=(self.get_setting("admin_panel_url"),self.get_setting("waybill_available_url"),self.get_setting("scrap_api_url"))
        for candidate in candidates:
            parts=urlsplit(candidate.strip())
            if parts.scheme in ("http","https") and parts.netloc:
                return urlunsplit((parts.scheme,parts.netloc,"/api/transportation/desktop-sync","",""))
        return ""

    def quality_document_url(self) -> str:
        explicit=self.get_setting("quality_document_url").strip()
        if explicit:return explicit
        candidates=(self.get_setting("admin_panel_url"),self.get_setting("waybill_available_url"),self.get_setting("scrap_api_url"))
        for candidate in candidates:
            if not candidate:continue
            parts=urlsplit(candidate)
            if parts.scheme and parts.netloc:return urlunsplit((parts.scheme,parts.netloc,"/api/transportation/weighbridge-quality/{code}","",""))
        return ""

    def server_sync_state(self) -> dict[str,str]:
        return {key:self.get_setting(key) for key in ("server_sync_cursor","server_sync_last_success","server_sync_last_error")}

    def apply_transportation_sync(self, payload: dict[str,Any], complete_snapshot: bool=False) -> tuple[int,int]:
        """Atomically refresh the SQLite read cache from authoritative server rows."""
        data=payload.get("data") or {}; sync=payload.get("sync") or {}
        waybills=data.get("waybills") or []; documents=data.get("documents") or []
        now=datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock,self.connection() as con:
            if complete_snapshot and 'waybills' in sync.get('sections',['waybills','documents']):
                # Remove obsolete read-cache rows before inserting server numbers.
                # Otherwise a reset sequence collides before the normal final cleanup.
                live={str(w.get('event_id')) for w in waybills if w.get('event_id') and not w.get('deleted_at')}
                stale=con.execute('SELECT * FROM waybills').fetchall()
                for row in stale:
                    if row['event_id'] in live:continue
                    owned=con.execute("SELECT 1 FROM desktop_event_owners WHERE capability='waybills' AND event_id=?",(row['event_id'],)).fetchone()
                    pending=con.execute("SELECT 1 FROM waybill_deliveries WHERE waybill_id=? AND status!='synced'",(row['id'],)).fetchone()
                    confirmed_cache=bool(row['server_confirmed'] and row['cache_source']=='server')
                    if owned and pending and not confirmed_cache:continue
                    con.execute('CREATE TABLE IF NOT EXISTS retired_waybill_cache(event_id TEXT PRIMARY KEY,retired_at TEXT NOT NULL,payload_json TEXT NOT NULL)')
                    archived=dict(row)
                    archived['_deliveries']=[dict(d) for d in con.execute('SELECT * FROM waybill_deliveries WHERE waybill_id=?',(row['id'],))]
                    con.execute('INSERT OR REPLACE INTO retired_waybill_cache VALUES(?,?,?)',(row['event_id'],now,json.dumps(archived,ensure_ascii=False)))
                    con.execute('DELETE FROM waybill_deliveries WHERE waybill_id=?',(row['id'],))
                    con.execute('DELETE FROM waybills WHERE id=?',(row['id'],))
            for item in waybills:
                event_id=str(item.get("event_id") or "")
                if not event_id:continue
                existing=con.execute("SELECT id,cache_source FROM waybills WHERE event_id=?",(event_id,)).fetchone()
                if item.get("deleted_at"):
                    if existing:con.execute("DELETE FROM waybills WHERE id=?",(existing["id"],))
                    continue
                company=item.get("company") or {}; number=int(item.get("number") or 0)
                values=(number,str(item.get("supplier_company_id") or company.get("id") or ""),json.dumps(company,ensure_ascii=False),int(item.get("origin_net_weight_kg") or 0),str(item.get("driver_name") or ""),str(item.get("driver_phone") or ""),str(item.get("trailer_plate") or ""),str(item.get("origin_province") or ""),str(item.get("origin_city") or ""),int(item.get("freight_amount") or 0),str(item.get("status") or "issued"),item.get("reservation_token"),item.get("reserved_at"),item.get("reservation_expires_at"),item.get("used_at"),item.get("issued_at") or now,now)
                if existing:
                    con.execute("""UPDATE waybills SET number=?,supplier_company_id=?,company_snapshot=?,origin_net_weight_kg=?,driver_name=?,driver_phone=?,trailer_plate=?,origin_province=?,origin_city=?,freight_amount=?,status=?,reservation_token=?,reserved_at=?,reservation_expires_at=?,used_at=?,issued_at=?,server_confirmed=1,cache_source='server' WHERE id=?""",values[:-1]+(existing["id"],))
                else:
                    con.execute("""INSERT INTO waybills(event_id,number,supplier_company_id,company_snapshot,origin_net_weight_kg,driver_name,driver_phone,trailer_plate,origin_province,origin_city,freight_amount,status,reservation_token,reserved_at,reservation_expires_at,used_at,issued_at,created_at,server_confirmed,cache_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'server')""",(event_id,)+values)
                # The server row is authoritative.  Any former local delivery
                # queue for the same event must not be submitted again.
                con.execute("UPDATE waybills SET queue_number=?,queue_day=? WHERE event_id=?",(item.get("queue_number"),item.get("queue_day"),event_id))
                cached_waybill=con.execute("SELECT id FROM waybills WHERE event_id=?",(event_id,)).fetchone()
                if cached_waybill:
                    con.execute("DELETE FROM waybill_deliveries WHERE waybill_id=?",(int(cached_waybill["id"]),))
                con.execute("UPDATE waybills SET freight_by_contractor=?,no_origin_weighbridge=?,freight_amount=CASE WHEN ? THEN 0 ELSE freight_amount END,origin_net_weight_kg=CASE WHEN ? THEN 0 ELSE origin_net_weight_kg END WHERE event_id=?",(int(bool(item.get("freight_by_contractor"))),int(bool(item.get("no_origin_weighbridge"))),int(bool(item.get("freight_by_contractor"))),int(bool(item.get("no_origin_weighbridge"))),event_id))
                con.execute("UPDATE waybill_sequences SET last_number=MAX(last_number,?) WHERE id=1",(number,))
            for item in documents:
                event_id=str(item.get("event_id") or "")
                if not event_id:continue
                existing=con.execute("SELECT id,cache_source FROM advanced_documents WHERE event_id=?",(event_id,)).fetchone()
                if item.get("deleted_at"):
                    if existing:
                        doc_id=int(existing["id"])
                        status=con.execute("SELECT status FROM advanced_documents WHERE id=?",(doc_id,)).fetchone()
                        if status and status["status"]!="finalized":
                            continue
                        con.execute("DELETE FROM event_deliveries WHERE event_id IN (SELECT id FROM webhook_events WHERE document_id=?)",(doc_id,))
                        con.execute("DELETE FROM webhook_events WHERE document_id=?",(doc_id,))
                        con.execute("DELETE FROM audit_log WHERE document_id=?",(doc_id,))
                        con.execute("DELETE FROM advanced_documents WHERE id=?",(doc_id,))
                    continue
                if existing and item.get('status')=='first_weighed':
                    local=con.execute("SELECT status FROM advanced_documents WHERE id=?",(existing['id'],)).fetchone()
                    pending=con.execute("SELECT 1 FROM event_deliveries d JOIN webhook_events e ON e.id=d.event_id WHERE e.document_id=? AND d.status!='synced'",(existing['id'],)).fetchone()
                    if local['status']!='finalized' or pending:
                        if item.get('quality_document_code'):
                            con.execute("UPDATE advanced_documents SET quality_document_code=? WHERE id=?",(item['quality_document_code'],existing['id']))
                        continue
                company=item.get("company") or {}; driver=item.get("driver") or {}; vehicle=item.get("vehicle") or {}; origin=item.get("origin") or {}; quality=item.get("quality") or {}
                # Support older desktop-sync snapshots whose quality object omitted accepted weight.
                quality=dict(quality)
                if quality.get("accepted_weight_kg") is None:
                    quality["accepted_weight_kg"] = item.get("accepted_weight_kg")
                    if quality["accepted_weight_kg"] is None:
                        quality["accepted_weight_kg"] = sum(float(part.get("weight_kg") or 0) for part in item.get("composition") or [])
                for field in ("rejected_weight_kg", "loss_weight_kg"):
                    if quality.get(field) is None and item.get(field) is not None:
                        quality[field] = item[field]
                waybill_number=str(item.get("waybill_number") or ""); waybill_local=None
                if waybill_number:
                    found=con.execute("SELECT id FROM waybills WHERE number=?",(int(waybill_number),)).fetchone(); waybill_local=found["id"] if found else None
                row_values=(str(item.get("document_number") or event_id),str(item.get("movement_type") or "external_to_warehouse"),str(item.get("status") or "finalized"),item.get("registered_at") or now,item.get("finalized_at"),item.get("entry_at"),item.get("exit_at"),str(item.get("supplier_company_id") or company.get("id") or ""),json.dumps(company,ensure_ascii=False),str(driver.get("name") or ""),str(driver.get("phone") or ""),str(vehicle.get("plate") or ""),str(vehicle.get("trailer_plate") or ""),str(vehicle.get("type") or ""),str(origin.get("province") or ""),str(origin.get("city") or ""),str(item.get("destination") or ""),waybill_number,str(quality.get("expert_name") or ""),quality.get("assessed_at"),float(quality.get("rejected_percentage") or 0),float(quality.get("rejected_weight_kg") or 0),float(quality.get("loss_percentage") or 0),float(quality.get("loss_weight_kg") or 0),float(quality.get("accepted_weight_kg") or 0),str(item.get("notes") or ""),now,waybill_local,json.dumps(item.get("waybill_snapshot") or {},ensure_ascii=False))
                if existing:
                    doc_id=int(existing["id"])
                    con.execute("""UPDATE advanced_documents SET document_number=?,movement_type=?,status=?,registered_at=?,finalized_at=?,entry_at=?,exit_at=?,supplier_company_id=?,company_snapshot=?,driver_name=?,driver_phone=?,vehicle_plate=?,trailer_plate=?,vehicle_type=?,origin_province=?,origin_city=?,destination=?,waybill_number=?,expert_name=?,assessed_at=?,rejected_percentage=?,rejected_weight_kg=?,loss_percentage=?,loss_weight_kg=?,accepted_weight_kg=?,notes=?,updated_at=?,waybill_id=?,waybill_snapshot=?,cache_source='server' WHERE id=?""",row_values+(doc_id,))
                else:
                    cursor=con.execute("""INSERT INTO advanced_documents(event_id,document_number,movement_type,status,registered_at,finalized_at,entry_at,exit_at,supplier_company_id,company_snapshot,driver_name,driver_phone,vehicle_plate,trailer_plate,vehicle_type,origin_province,origin_city,destination,waybill_number,expert_name,assessed_at,rejected_percentage,rejected_weight_kg,loss_percentage,loss_weight_kg,accepted_weight_kg,notes,created_at,updated_at,waybill_id,waybill_snapshot,cache_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'server')""",(event_id,)+row_values[:26]+(now,)+row_values[26:])
                    doc_id=int(cursor.lastrowid)
                con.execute("UPDATE advanced_documents SET source_warehouse_id=?,destination_warehouse_id=?,warehouse_snapshot_json=? WHERE id=?",(item.get("source_warehouse_id"),item.get("destination_warehouse_id"),json.dumps(item.get("warehouse_snapshot") or [],ensure_ascii=False),doc_id))
                if "weight_discrepancy" in item:
                    discrepancy = item.get("weight_discrepancy") or {}
                    if not isinstance(discrepancy, dict):
                        discrepancy = {}
                    con.execute("UPDATE advanced_documents SET weight_discrepancy_json=? WHERE id=?", (json.dumps(discrepancy, ensure_ascii=False), doc_id))
                con.execute("UPDATE advanced_documents SET quality_pending=? WHERE id=?",(int(bool(item.get("quality_pending"))),doc_id))
                if item.get("quality_document_code"):
                    con.execute("UPDATE advanced_documents SET quality_document_code=? WHERE id=?",(item["quality_document_code"],doc_id))
                # A document received from MegaPulse is no longer an outbound
                # local event.  Remove stale queues/audits before rebuilding its
                # read-only cache representation.
                con.execute("DELETE FROM event_deliveries WHERE event_id IN (SELECT id FROM webhook_events WHERE document_id=?)",(doc_id,))
                con.execute("DELETE FROM webhook_events WHERE document_id=?",(doc_id,))
                con.execute("DELETE FROM audit_log WHERE document_id=?",(doc_id,))
                con.execute("DELETE FROM weighings WHERE document_id=?",(doc_id,)); con.execute("DELETE FROM document_compositions WHERE document_id=?",(doc_id,))
                for weighing in item.get("weighings") or []:
                    con.execute("""INSERT INTO weighings(document_id,sequence,role,weight_kg,source,captured_at,stable,manual_reason,raw_payload,operator_name) VALUES(?,?,?,?,?,?,?,?,?,?)""",(doc_id,int(weighing.get("sequence") or 0),str(weighing.get("role") or ""),float(weighing.get("weight_kg") or 0),str(weighing.get("source") or "device"),weighing.get("captured_at") or now,int(bool(weighing.get("stable",True))),weighing.get("manual_reason"),weighing.get("raw_payload"),str(weighing.get("operator_name") or "")))
                for composition in item.get("composition") or []:
                    con.execute("""INSERT INTO document_compositions(document_id,scrap_type_id,scrap_name,percentage,weight_kg,notes) VALUES(?,?,?,?,?,?)""",(doc_id,str(composition.get("scrap_type_id") or ""),str(composition.get("scrap_name") or ""),float(composition.get("percentage") or 0),float(composition.get("weight_kg") or 0),str(composition.get("notes") or "")))
            for item in waybills:
                if not item.get("deleted_at"):
                    con.execute("UPDATE waybills SET material_name=? WHERE event_id=?",(((item.get('issuance_snapshot') or {}).get('material_type') or {}).get('name') or (item.get('material_type') or {}).get('name'),item.get('event_id')))
                    con.execute("UPDATE waybills SET material_type_id=?,material_policy_json=?,material_movement=? WHERE event_id=?",(item.get("material_type_id"),json.dumps(item.get("material_policy") or {}),item.get("movement_type"),item.get("event_id")))
            for item in documents:
                if not item.get("deleted_at"):
                    con.execute("UPDATE advanced_documents SET material_name=? WHERE event_id=?",(((item.get('issuance_snapshot') or {}).get('material_type') or {}).get('name') or (item.get('material_type') or {}).get('name'),item.get('event_id')))
                    con.execute("UPDATE advanced_documents SET material_type_id=?,material_policy_json=? WHERE event_id=?",(item.get("material_type_id"),json.dumps(item.get("material_policy") or {}),item.get("event_id")))
                    if item.get("material_composition"):
                        cached=con.execute("SELECT id FROM advanced_documents WHERE event_id=?",(item.get("event_id"),)).fetchone()
                        if cached:
                            con.execute("DELETE FROM document_compositions WHERE document_id=?",(cached[0],))
                            for part in item["material_composition"]:
                                con.execute("INSERT INTO document_compositions(document_id,scrap_type_id,scrap_name,percentage,weight_kg) VALUES(?,?,?,?,?)",(cached[0],str(part["material_item_id"]),part["material_name"],part["percentage"],part["weight_kg"]))
            if complete_snapshot:
                valid_waybill_events={str(item.get("event_id") or "") for item in waybills if item.get("event_id") and not item.get("deleted_at")}
                valid_document_events={str(item.get("event_id") or "") for item in documents if item.get("event_id") and not item.get("deleted_at")}
                # A full snapshot replaces the cache, regardless of whether an
                # old row was once marked local/server.  The sole exception is
                # an unfinished local draft, which has not yet reached the ERP.
                stale_documents=con.execute("SELECT id,event_id,status FROM advanced_documents").fetchall() if 'documents' in sync.get('sections',['documents','waybills']) else []
                for stale in stale_documents:
                    if stale["event_id"] in valid_document_events:continue
                    # All unfinished wizard states are drafts in practice
                    # (draft/first_weighed/second_weighed/quality_assessed).
                    if stale["status"]!="finalized":continue
                    owned=con.execute("SELECT 1 FROM desktop_event_owners WHERE capability='weighbridge' AND event_id=?",(stale['event_id'],)).fetchone()
                    pending=con.execute("SELECT 1 FROM event_deliveries x JOIN webhook_events w ON w.id=x.event_id WHERE w.document_id=? AND x.status!='synced'",(stale['id'],)).fetchone()
                    if owned and pending:continue
                    doc_id=int(stale["id"])
                    con.execute("DELETE FROM event_deliveries WHERE event_id IN (SELECT id FROM webhook_events WHERE document_id=?)",(doc_id,))
                    con.execute("DELETE FROM webhook_events WHERE document_id=?",(doc_id,))
                    con.execute("DELETE FROM audit_log WHERE document_id=?",(doc_id,))
                    con.execute("DELETE FROM advanced_documents WHERE id=?",(doc_id,))
                stale_waybills=con.execute("SELECT id,event_id FROM waybills").fetchall()
                for stale in stale_waybills:
                    if stale["event_id"] not in valid_waybill_events:
                        owned=con.execute("SELECT 1 FROM desktop_event_owners WHERE capability='waybills' AND event_id=?",(stale['event_id'],)).fetchone()
                        pending=con.execute("SELECT 1 FROM waybill_deliveries WHERE waybill_id=? AND status!='synced'",(stale['id'],)).fetchone()
                        if owned and pending:continue
                        con.execute("DELETE FROM waybills WHERE id=?",(int(stale["id"]),))
                # The legacy document table has no draft state and must never
                # remain a second source of truth after the server snapshot.
                con.execute("DELETE FROM deliveries")
                con.execute("DELETE FROM documents")
                con.execute("INSERT INTO settings(key,value) VALUES('server_snapshot_reconciled','1') ON CONFLICT(key) DO UPDATE SET value='1'")
                con.execute("INSERT INTO settings(key,value) VALUES('server_cache_policy_version','2') ON CONFLICT(key) DO UPDATE SET value='2'")
                con.execute("INSERT INTO settings(key,value) VALUES('server_quality_contract_version','1') ON CONFLICT(key) DO UPDATE SET value='1'")
            if sync.get("cursor"):
                con.execute("INSERT INTO settings(key,value) VALUES('server_sync_cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(sync["cursor"]),))
            if sync.get("snapshot_version"):
                con.execute("INSERT INTO settings(key,value) VALUES('server_snapshot_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(sync["snapshot_version"]),))
            con.execute("INSERT INTO settings(key,value) VALUES('server_sync_last_success',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(now,))
            con.execute("DELETE FROM settings WHERE key='server_sync_last_error'")
        return len(waybills),len(documents)

    def record_server_sync_error(self, error: str) -> None:
        self.set_setting("server_sync_last_error",str(error)[:1000])

    def set_setting(self, key: str, value: str) -> None:
        if self.session and key in ('transport_api_token','scrap_api_token','company_api_token'):return
        with self.connection() as con:
            con.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def delete_settings(self, keys: tuple[str, ...] | list[str]) -> None:
        if not keys:
            return
        placeholders = ",".join("?" for _ in keys)
        with self.connection() as con:
            con.execute(f"DELETE FROM settings WHERE key IN ({placeholders})", tuple(keys))

    def configuration_bundle(self) -> dict[str, Any]:
        """Return only portable application configuration, never operational documents."""
        with self.connection() as con:
            rows = con.execute(
                f"SELECT key,value FROM settings WHERE key IN ({','.join('?' for _ in self.PORTABLE_SETTING_KEYS)})",
                self.PORTABLE_SETTING_KEYS,
            ).fetchall()
            sites = con.execute("SELECT id,name,base_url,token,active FROM webhook_sites ORDER BY id").fetchall()
            endpoints = con.execute("SELECT site_id,name,url,token,active,kind,path FROM endpoints ORDER BY id").fetchall()
        return {
            "format": "megasanj-settings",
            "version": 2,
            "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "settings": {row["key"]: row["value"] for row in rows},
            "webhook_sites": [dict(row) for row in sites],
            "endpoints": [dict(row) for row in endpoints],
        }

    def apply_configuration_bundle(self, bundle: dict[str, Any]) -> None:
        """Validate and atomically apply an exported MegaSanj settings file."""
        if not isinstance(bundle, dict) or bundle.get("format") != "megasanj-settings":
            raise ValueError("این فایل، خروجی معتبر تنظیمات مگاسنج نیست.")
        if bundle.get("version") not in (1,2):
            raise ValueError("نسخه فایل تنظیمات توسط این برنامه پشتیبانی نمی‌شود.")
        settings = bundle.get("settings")
        endpoints = bundle.get("endpoints")
        sites = bundle.get("webhook_sites", [])
        if not isinstance(settings, dict) or not isinstance(endpoints, list) or not isinstance(sites,list):
            raise ValueError("ساختار فایل تنظیمات ناقص یا آسیب‌دیده است.")
        clean_settings = {
            str(key): str(value)
            for key, value in settings.items()
            if key in self.PORTABLE_SETTING_KEYS and value is not None
        }
        clean_endpoints: list[dict[str, Any]] = []
        for endpoint in endpoints:
            if not isinstance(endpoint, dict):
                raise ValueError("یکی از مقصدهای وب‌هوک ساختار معتبری ندارد.")
            url = str(endpoint.get("url") or "").strip()
            name = str(endpoint.get("name") or "").strip()
            kind = str(endpoint.get("kind") or "weighbridge")
            if not name or not url.startswith(("http://", "https://")) or kind not in ("weighbridge", "waybill", "processing"):
                raise ValueError("اطلاعات یکی از مقصدهای وب‌هوک معتبر نیست.")
            clean_endpoints.append({
                "name": name,
                "url": url,
                "token": str(endpoint.get("token") or ""),
                "active": int(bool(endpoint.get("active", True))),
                "kind": kind,
                "site_id": int(endpoint.get("site_id") or 0),
                "path": str(endpoint.get("path") or ""),
            })

        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock, self.connection() as con:
            placeholders = ",".join("?" for _ in self.PORTABLE_SETTING_KEYS)
            con.execute(f"DELETE FROM settings WHERE key IN ({placeholders})", self.PORTABLE_SETTING_KEYS)
            con.executemany(
                "INSERT INTO settings(key,value) VALUES(?,?)",
                clean_settings.items(),
            )
            # Old UI-only scale values are deliberately retired by the TETA10-A profile.
            con.execute("DELETE FROM settings WHERE key IN ('scale_stability_samples','scale_tolerance_kg','scale_pattern')")
            con.execute("UPDATE endpoints SET active=0")
            con.execute("UPDATE webhook_sites SET active=0")
            site_map: dict[int,int] = {}
            for site in sites:
                name=str(site.get("name") or "").strip(); base=str(site.get("base_url") or "").strip().rstrip("/")
                if not name or not base.startswith(("http://","https://")):
                    raise ValueError("اطلاعات یکی از سایت‌های وب‌هوک معتبر نیست.")
                cursor=con.execute("INSERT INTO webhook_sites(name,base_url,token,active,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (name,base,str(site.get("token") or ""),int(bool(site.get("active",True))),now,now))
                site_map[int(site.get("id") or 0)]=int(cursor.lastrowid)
            for endpoint in clean_endpoints:
                mapped_site_id=site_map.get(endpoint["site_id"])
                if not mapped_site_id:
                    parts=urlsplit(endpoint["url"]); base=urlunsplit((parts.scheme,parts.netloc,"","",""))
                    current_site=con.execute("SELECT id FROM webhook_sites WHERE base_url=? AND token=? ORDER BY id DESC LIMIT 1",(base,endpoint["token"])).fetchone()
                    if current_site:mapped_site_id=int(current_site["id"])
                    else:
                        cursor=con.execute("INSERT INTO webhook_sites(name,base_url,token,active,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                            (endpoint["name"],base,endpoint["token"],endpoint["active"],now,now))
                        mapped_site_id=int(cursor.lastrowid)
                relative=endpoint["path"]
                if not relative:
                    parts=urlsplit(endpoint["url"]); relative=parts.path or "/"
                    if parts.query:relative += "?"+parts.query
                current = con.execute(
                    "SELECT id FROM endpoints WHERE url=? AND kind=? ORDER BY id LIMIT 1",
                    (endpoint["url"], endpoint["kind"]),
                ).fetchone()
                if current:
                    endpoint_id = int(current["id"])
                    con.execute(
                        "UPDATE endpoints SET name=?,token=?,active=?,site_id=?,path=? WHERE id=?",
                        (endpoint["name"], endpoint["token"], endpoint["active"],mapped_site_id,relative,endpoint_id),
                    )
                else:
                    cursor = con.execute(
                        "INSERT INTO endpoints(name,url,token,active,created_at,kind,site_id,path) VALUES(?,?,?,?,?,?,?,?)",
                        (endpoint["name"], endpoint["url"], endpoint["token"], endpoint["active"], now, endpoint["kind"],mapped_site_id,relative),
                    )
                    endpoint_id = int(cursor.lastrowid)
            # Existing offline records also gain the newly imported active destinations.
            con.execute("""INSERT OR IGNORE INTO waybill_deliveries(waybill_id,endpoint_id)
                SELECT w.id,e.id FROM waybills w CROSS JOIN endpoints e
                WHERE e.active=1 AND e.kind='waybill' AND w.cache_source!='server'""")
            con.execute("""INSERT OR IGNORE INTO event_deliveries(event_id,endpoint_id)
                SELECT w.id,e.id FROM webhook_events w CROSS JOIN endpoints e
                WHERE e.active=1 AND e.kind='weighbridge'""")
            con.execute("""INSERT OR IGNORE INTO processing_deliveries(processing_document_id,endpoint_id)
                SELECT p.id,e.id FROM processing_documents p CROSS JOIN endpoints e
                WHERE e.active=1 AND e.kind='processing'""")

    def save_scraps(self, scraps: list[dict[str, Any]]) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connection() as con:
            received_ids = [str(item["id"]) for item in scraps]
            if received_ids:
                placeholders = ",".join("?" for _ in received_ids)
                con.execute(f"DELETE FROM scrap_types WHERE id NOT IN ({placeholders})", received_ids)
            else:
                con.execute("DELETE FROM scrap_types")
            for item in scraps:
                con.execute("INSERT INTO scrap_types(id,name,updated_at) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at", (str(item["id"]), item["name"], now))

    def save_companies(self, companies: list[dict[str, Any]]) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        fields = ('code','name','legal_name','phone','province','city','address','description','national_id','registration_number')
        with self._lock, self.connection() as con:
            con.execute('UPDATE companies SET active=0')
            for company in companies:
                values = [str(company.get(field) or '') for field in fields]
                con.execute("INSERT INTO companies(id,code,name,legal_name,phone,province,city,address,description,national_id,registration_number,active,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET code=excluded.code,name=excluded.name,legal_name=excluded.legal_name,phone=excluded.phone,province=excluded.province,city=excluded.city,address=excluded.address,description=excluded.description,national_id=excluded.national_id,registration_number=excluded.registration_number,active=excluded.active,updated_at=excluded.updated_at", (str(company['id']), *values, int(company.get('is_active',company.get('active',True))), now))

    def companies(self, active_only: bool = True) -> list[sqlite3.Row]:
        with self.connection() as con:
            return con.execute("SELECT * FROM companies" + (" WHERE active=1" if active_only else "") + " ORDER BY name").fetchall()

    def primary_waybill_endpoint(self) -> sqlite3.Row | None:
        if self.session:
            with self.connection() as con:
                return con.execute("SELECT * FROM endpoints WHERE active=1 AND kind='waybill' AND url=? ORDER BY id LIMIT 1",(self.session.api_url("transportation/webhooks/waybills"),)).fetchone()
        with self.connection() as con:
            return con.execute("SELECT * FROM endpoints WHERE active=1 AND kind='waybill' ORDER BY id LIMIT 1").fetchone()

    @staticmethod
    def waybill_payload_from_data(data: dict[str, Any]) -> dict[str, Any]:
        company=data.get("company") or {}
        return ({"material_type_id":data["material_type_id"]} if data.get("material_type_id") else {}) | {"schema_version":1,"event_id":data["event_id"],"supplier_company_id":int(company.get("id") or data.get("supplier_company_id") or 0),
            "origin_net_weight_kg":0 if data.get("no_origin_weighbridge") else int(data["origin_net_weight_kg"]),"driver_name":data["driver_name"],
            "driver_phone":data["driver_phone"],"trailer_plate":data["trailer_plate"],
            "origin_province":data["origin_province"],"origin_city":data["origin_city"],
            "freight_amount":0 if data.get("freight_by_contractor") else int(data.get("freight_amount") or 0),"freight_by_contractor":bool(data.get("freight_by_contractor")),"no_origin_weighbridge":bool(data.get("no_origin_weighbridge")),"issued_at":data.get("issued_at")}

    def create_waybill(self, data: dict[str, Any], official_number: int | None = None) -> int:
        """Persist a waybill; an official number is authoritative only when returned by the server."""
        data=dict(data)
        if data.get("freight_by_contractor"):data["freight_amount"]=0
        if data.get("no_origin_weighbridge"):data["origin_net_weight_kg"]=0
        elif int(data["origin_net_weight_kg"])<=0:raise ValueError("وزن مبدأ باید مثبت باشد.")
        now=data.get("issued_at") or datetime.now().astimezone().isoformat(timespec="seconds")
        company=data.get("company") or {}
        with self._lock,self.connection() as con:
            existing=con.execute("SELECT id,number,server_confirmed FROM waybills WHERE event_id=?",(data["event_id"],)).fetchone()
            if existing:
                if official_number is not None and existing["server_confirmed"] and int(existing["number"]) != int(official_number):
                    raise ValueError("سرور برای یک رویداد ثبت‌شده شماره متفاوتی برگرداند.")
                return int(existing["id"])
            if official_number is None:
                con.execute("UPDATE waybill_sequences SET last_number=last_number+1 WHERE id=1")
                number=int(con.execute("SELECT last_number FROM waybill_sequences WHERE id=1").fetchone()[0])
            else:
                number=int(official_number)
                if number <= 0:raise ValueError("شماره قطعی برگشتی از سرور معتبر نیست.")
                conflict=con.execute("SELECT id,server_confirmed FROM waybills WHERE number=?",(number,)).fetchone()
                if conflict:
                    if conflict["server_confirmed"]:
                        raise ValueError("شماره قطعی سرور قبلاً برای بارنامه دیگری ثبت شده است.")
                    con.execute("UPDATE waybill_sequences SET last_number=last_number+1 WHERE id=1")
                    replacement=int(con.execute("SELECT last_number FROM waybill_sequences WHERE id=1").fetchone()[0])
                    while con.execute("SELECT 1 FROM waybills WHERE number=?",(replacement,)).fetchone():
                        replacement += 1
                    con.execute("UPDATE waybills SET number=? WHERE id=?",(replacement,conflict["id"]))
                con.execute("UPDATE waybill_sequences SET last_number=MAX(last_number,?) WHERE id=1",(number,))
            cur=con.execute("""INSERT INTO waybills(event_id,number,supplier_company_id,company_snapshot,origin_net_weight_kg,driver_name,driver_phone,trailer_plate,origin_province,origin_city,freight_amount,issued_at,created_at,server_confirmed)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(data["event_id"],number,str(company["id"]),json.dumps(company,ensure_ascii=False),int(data["origin_net_weight_kg"]),data["driver_name"],data["driver_phone"],data["trailer_plate"],data["origin_province"],data["origin_city"],int(data.get("freight_amount") or 0),now,now,int(official_number is not None)))
            waybill_id=int(cur.lastrowid)
            con.execute("UPDATE waybills SET freight_by_contractor=?,no_origin_weighbridge=? WHERE id=?",(int(bool(data.get("freight_by_contractor"))),int(bool(data.get("no_origin_weighbridge"))),waybill_id))
            con.execute("UPDATE waybills SET queue_number=?,queue_day=? WHERE id=?",(data.get("queue_number"),data.get("queue_day"),waybill_id))
            con.execute("UPDATE waybills SET material_type_id=?,material_policy_json=?,material_movement=? WHERE event_id=?",(data.get("material_type_id"),json.dumps(data.get("material_policy") or {}),data.get("material_movement"),data["event_id"]))
            con.execute("UPDATE waybills SET material_name=? WHERE event_id=?",(data.get("material_name"),data["event_id"]))
            self._stamp_actor(con,'waybills',data['event_id'])
            con.execute("INSERT OR IGNORE INTO waybill_deliveries(waybill_id,endpoint_id) SELECT ?,id FROM endpoints WHERE active=1 AND kind='waybill'",(waybill_id,))
            return waybill_id

    def confirm_waybill_number(self, waybill_id: int, official_number: int) -> None:
        """Atomically replace a provisional local number with the server-issued number."""
        official_number=int(official_number)
        if official_number <= 0:raise ValueError("سرور شماره معتبر بارنامه را برنگرداند.")
        with self._lock,self.connection() as con:
            current=con.execute("SELECT id,event_id,number,server_confirmed FROM waybills WHERE id=?",(waybill_id,)).fetchone()
            if not current:raise ValueError("بارنامه محلی پیدا نشد.")
            if current["server_confirmed"]:
                if int(current["number"]) != official_number:
                    raise ValueError("یک مقصد ثانویه شماره‌ای متفاوت با شماره قطعی بارنامه برگرداند.")
                return
            conflict=con.execute("SELECT id,event_id,server_confirmed FROM waybills WHERE number=? AND id<>?",(official_number,waybill_id)).fetchone()
            if conflict:
                if conflict["server_confirmed"]:
                    raise ValueError("شماره قطعی سرور با یک بارنامه تأییدشده دیگر تداخل دارد.")
                con.execute("UPDATE waybill_sequences SET last_number=last_number+1 WHERE id=1")
                replacement=int(con.execute("SELECT last_number FROM waybill_sequences WHERE id=1").fetchone()[0])
                while con.execute("SELECT 1 FROM waybills WHERE number=?",(replacement,)).fetchone():
                    replacement += 1
                con.execute("UPDATE waybills SET number=? WHERE id=?",(replacement,conflict["id"]))
                con.execute("UPDATE waybill_sequences SET last_number=MAX(last_number,?) WHERE id=1",(replacement,))
            con.execute("UPDATE waybills SET number=?,server_confirmed=1 WHERE id=?",(official_number,waybill_id))
            con.execute("UPDATE waybill_sequences SET last_number=MAX(last_number,?) WHERE id=1",(official_number,))

    def mark_waybill_endpoint_synced(self, waybill_id: int, endpoint_id: int, response_code: int = 201) -> None:
        now=datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connection() as con:
            con.execute("""UPDATE waybill_deliveries SET status='synced',attempts=attempts+1,
                last_error=NULL,response_code=?,last_attempt_at=?,delivered_at=?
                WHERE waybill_id=? AND endpoint_id=?""",(response_code,now,now,waybill_id,endpoint_id))

    def waybills(self, search: str = "", available_only: bool = False) -> list[sqlite3.Row]:
        clauses=[]; args=[]
        if available_only: clauses.append("w.status='issued' AND w.server_confirmed=1")
        if search:
            clauses.append("(CAST(w.number AS TEXT) LIKE ? OR w.driver_name LIKE ? OR w.driver_phone LIKE ? OR w.trailer_plate LIKE ? OR w.company_snapshot LIKE ?)")
            args.extend([f"%{search}%"]*5)
        where=" WHERE "+" AND ".join(clauses) if clauses else ""
        sql=f"""SELECT w.*,
          COALESCE((SELECT COUNT(*) FROM waybill_deliveries d WHERE d.waybill_id=w.id),0) total_count,
          COALESCE((SELECT SUM(d.status='synced') FROM waybill_deliveries d WHERE d.waybill_id=w.id),0) ok_count
          FROM waybills w{where} ORDER BY w.number DESC"""
        with self.connection() as con:return con.execute(sql,args).fetchall()

    def waybill(self, waybill_id: int) -> sqlite3.Row | None:
        with self.connection() as con:return con.execute("SELECT * FROM waybills WHERE id=?",(waybill_id,)).fetchone()

    def waybill_by_number(self, number: int) -> sqlite3.Row | None:
        with self.connection() as con:return con.execute("SELECT * FROM waybills WHERE number=? AND server_confirmed=1",(number,)).fetchone()

    def available_waybills_by_number_prefix(self, prefix: str, limit: int = 12) -> list[sqlite3.Row]:
        digits="".join(character for character in str(prefix) if character.isdigit())
        if not digits:return []
        with self.connection() as con:
            return con.execute("SELECT * FROM waybills WHERE status='issued' AND server_confirmed=1 AND CAST(number AS TEXT) LIKE ? ORDER BY number DESC LIMIT ?",(digits+"%",limit)).fetchall()

    def delete_waybill(self, waybill_id: int) -> None:
        with self._lock,self.connection() as con:
            con.execute("DELETE FROM waybill_deliveries WHERE waybill_id=?",(waybill_id,))
            if con.execute("DELETE FROM waybills WHERE id=?",(waybill_id,)).rowcount != 1:raise ValueError("بارنامه پیدا نشد.")

    def waybill_payload(self, row: sqlite3.Row | dict[str,Any]) -> dict[str,Any]:
        company=json.loads(row["company_snapshot"] or "{}")
        return self.waybill_payload_from_data(dict(row) | {"company": company})

    def pending_waybill_deliveries(self, limit: int=100) -> list[sqlite3.Row]:
        with self.connection() as con:return con.execute("""SELECT d.id delivery_id,d.attempts,e.url,e.token,w.id waybill_local_id,w.* FROM waybill_deliveries d JOIN endpoints e ON e.id=d.endpoint_id JOIN waybills w ON w.id=d.waybill_id WHERE d.status IN ('pending','failed') AND e.active=1 ORDER BY d.id LIMIT ?""",(limit,)).fetchall()

    def mark_waybill_delivery(self, delivery_id:int, ok:bool, code:int|None, error:str="", needs_attention:bool=False)->None:
        now=datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connection() as con:con.execute("UPDATE waybill_deliveries SET status=?,attempts=attempts+1,last_error=?,response_code=?,last_attempt_at=?,delivered_at=? WHERE id=?",("synced" if ok else ("needs_attention" if needs_attention else "failed"),error[:1000] or None,code,now,now if ok else None,delivery_id))

    def mark_waybill_used(self, number:int, used_at:str|None=None)->None:
        used_at=used_at or datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connection() as con:con.execute("UPDATE waybills SET status='used',used_at=? WHERE number=?",(used_at,number))

    def release_local_waybill(self, number:int)->None:
        """Make a locally cached waybill selectable again after its document is removed."""
        with self.connection() as con:
            con.execute("""UPDATE waybills SET status='issued',reservation_token=NULL,
              reserved_at=NULL,reservation_expires_at=NULL,used_at=NULL WHERE number=?""",(number,))

    def create_draft(self, event_id: str, number: str, movement: str, data: dict[str, Any]) -> int:
        now=data.get("registered_at") or datetime.now().astimezone().isoformat(timespec="seconds"); company=data.get("company") or {}
        with self._lock,self.connection() as con:
            cur=con.execute("""INSERT INTO advanced_documents(event_id,document_number,movement_type,status,registered_at,entry_at,supplier_company_id,company_snapshot,driver_name,driver_phone,vehicle_plate,trailer_plate,vehicle_type,origin_province,origin_city,destination,waybill_number,notes,created_at,updated_at,waybill_id,waybill_reservation_token,waybill_snapshot) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (event_id,number,movement,"draft",now,data.get("entry_at",now),company.get("id"),json.dumps(company,ensure_ascii=False),data.get("driver_name",""),data.get("driver_phone",""),data.get("vehicle_plate",""),data.get("trailer_plate",""),data.get("vehicle_type",""),data.get("origin_province",""),data.get("origin_city",""),data.get("destination",""),data.get("waybill_number",""),data.get("notes",""),now,now,data.get("waybill_id"),data.get("waybill_reservation_token"),json.dumps(data.get("waybill") or {},ensure_ascii=False)))
            doc_id=int(cur.lastrowid); self._stamp_actor(con,'weighbridge',event_id); con.execute("INSERT INTO audit_log(document_id,action,details,actor,created_at) VALUES(?,?,?,?,?)",(doc_id,"document.created",json.dumps(data,ensure_ascii=False),self.session.user['name'] if self.session else data.get("actor",""),now)); return doc_id

    def advanced_document(self, doc_id: int) -> sqlite3.Row | None:
        with self.connection() as con:return con.execute("SELECT * FROM advanced_documents WHERE id=?",(doc_id,)).fetchone()

    def update_draft(self, doc_id: int, data: dict[str, Any]) -> None:
        now=datetime.now().astimezone().isoformat(timespec="seconds"); company=data.get("company") or {}
        with self._lock,self.connection() as con:
            con.execute("""UPDATE advanced_documents SET movement_type=?,supplier_company_id=?,company_snapshot=?,driver_name=?,driver_phone=?,vehicle_plate=?,trailer_plate=?,origin_province=?,origin_city=?,destination=?,waybill_number=?,notes=?,updated_at=?,waybill_id=?,waybill_reservation_token=?,waybill_snapshot=?,quality_document_code=? WHERE id=? AND status!='finalized'""",
              (data["movement_type"],company.get("id"),json.dumps(company,ensure_ascii=False),data.get("driver_name",""),data.get("driver_phone",""),data.get("vehicle_plate",""),data.get("trailer_plate",""),data.get("origin_province",""),data.get("origin_city",""),data.get("destination",""),data.get("waybill_number",""),data.get("notes",""),now,data.get("waybill_id"),data.get("waybill_reservation_token"),json.dumps(data.get("waybill") or {},ensure_ascii=False),data.get("quality_document_code",""),doc_id))

    def advanced_documents(self, include_finalized: bool=True) -> list[sqlite3.Row]:
        where = "" if include_finalized else " WHERE d.status!='finalized'"
        sql = f"""SELECT d.*,
          COALESCE((SELECT weight_kg FROM weighings WHERE document_id=d.id AND role='gross'),0) gross_weight_kg,
          COALESCE((SELECT weight_kg FROM weighings WHERE document_id=d.id AND role='tare'),0) tare_weight_kg,
          COALESCE((SELECT COUNT(*) FROM event_deliveries x JOIN webhook_events w ON w.id=x.event_id WHERE w.document_id=d.id),0) total_count,
          COALESCE((SELECT SUM(CASE WHEN x.status='synced' THEN 1 ELSE 0 END) FROM event_deliveries x JOIN webhook_events w ON w.id=x.event_id WHERE w.document_id=d.id),0) ok_count,
          CASE
            WHEN d.status!='finalized' THEN 'draft'
            WHEN d.cache_source='server' THEN 'synced'
            WHEN (SELECT COUNT(*) FROM event_deliveries x JOIN webhook_events w ON w.id=x.event_id WHERE w.document_id=d.id)=0 THEN 'no_endpoint'
            WHEN (SELECT SUM(CASE WHEN x.status='synced' THEN 1 ELSE 0 END) FROM event_deliveries x JOIN webhook_events w ON w.id=x.event_id WHERE w.document_id=d.id)=(SELECT COUNT(*) FROM event_deliveries x JOIN webhook_events w ON w.id=x.event_id WHERE w.document_id=d.id) THEN 'synced'
            WHEN (SELECT SUM(CASE WHEN x.status='needs_attention' THEN 1 ELSE 0 END) FROM event_deliveries x JOIN webhook_events w ON w.id=x.event_id WHERE w.document_id=d.id)>0 THEN 'needs_attention'
            WHEN (SELECT SUM(CASE WHEN x.status='synced' THEN 1 ELSE 0 END) FROM event_deliveries x JOIN webhook_events w ON w.id=x.event_id WHERE w.document_id=d.id)>0 THEN 'partial'
            WHEN (SELECT SUM(CASE WHEN x.status='failed' THEN 1 ELSE 0 END) FROM event_deliveries x JOIN webhook_events w ON w.id=x.event_id WHERE w.document_id=d.id)>0 THEN 'failed'
            ELSE 'pending' END sync_status
          FROM advanced_documents d{where} ORDER BY d.registered_at DESC"""
        with self.connection() as con:
            return con.execute(sql).fetchall()

    def delete_advanced_document(self, doc_id: int) -> None:
        """Permanently remove any advanced document from this local database only."""
        with self._lock, self.connection() as con:
            con.execute("DELETE FROM event_deliveries WHERE event_id IN (SELECT id FROM webhook_events WHERE document_id=?)", (doc_id,))
            con.execute("DELETE FROM webhook_events WHERE document_id=?", (doc_id,))
            con.execute("DELETE FROM document_compositions WHERE document_id=?", (doc_id,))
            con.execute("DELETE FROM weighings WHERE document_id=?", (doc_id,))
            con.execute("DELETE FROM audit_log WHERE document_id=?", (doc_id,))
            deleted = con.execute("DELETE FROM advanced_documents WHERE id=?", (doc_id,)).rowcount
            if deleted != 1:
                raise ValueError("سند در دیتابیس محلی پیدا نشد.")

    def first_weighing_queued(self, doc_id):
        with self.connection() as con:
            return bool(con.execute("SELECT 1 FROM webhook_events WHERE document_id=? AND event_type='weighbridge.document.first_weighed'",(doc_id,)).fetchone())

    def queue_first_weighing(self, doc_id):
        with self._lock,self.connection() as con:
            doc=con.execute("SELECT * FROM advanced_documents WHERE id=?",(doc_id,)).fetchone()
            first=con.execute("SELECT * FROM weighings WHERE document_id=? AND sequence=1",(doc_id,)).fetchone()
            if not doc or not first or doc['status']=='finalized':return False
            if not doc['material_type_id']:raise ValueError('ابتدا اطلاعات مرجع کالا را همگام کنید.')
            previous=con.execute("SELECT id FROM webhook_events WHERE document_id=? AND event_type='weighbridge.document.first_weighed'",(doc_id,)).fetchone()
            if previous:return True
            policy=json.loads(doc['material_policy_json'] or '{}')
            payload={'schema_version':3,'phase':'first','event_id':doc['event_id'],'material_type_id':doc['material_type_id'],
                'document':{'document_number':doc['document_number'],'movement_type':doc['movement_type'],'status':'first_weighed','registered_at':doc['registered_at'],'entry_at':doc['entry_at']},
                'vehicle':{'trailer_plate':doc['trailer_plate'],'type':doc['vehicle_type']},
                'driver':{'name':doc['driver_name'],'phone':doc['driver_phone']},
                'origin':{'province':doc['origin_province'],'city':doc['origin_city']},
                'composition':[], 'weighings':[{'sequence':1,'kind':'gross','weight_kg':first['weight_kg'],'source':first['source'],'captured_at':first['captured_at'],'manual_reason':first['manual_reason'],'raw_payload':first['raw_payload']}]}
            if policy.get('id'):payload['material_policy_id']=int(policy['id'])
            if doc['waybill_number']:payload['waybill']={'number':int(doc['waybill_number']),'reservation_token':doc['waybill_reservation_token']}
            for key in ('source_warehouse_id','destination_warehouse_id'):
                if doc[key]:payload[key]=doc[key]
            cur=con.execute("INSERT INTO webhook_events(event_id,document_id,event_type,occurred_at,payload_json) VALUES(?,?,?,?,?)",(doc['event_id']+':first',doc_id,'weighbridge.document.first_weighed',first['captured_at'],json.dumps(payload,ensure_ascii=False)))
            con.execute("INSERT OR IGNORE INTO event_deliveries(event_id,endpoint_id) SELECT ?,id FROM endpoints WHERE active=1 AND kind='weighbridge'",(cur.lastrowid,))
            return True

    def add_weighing(self,doc_id:int,sequence:int,role:str,weight_kg:float,source:str,captured_at:str,stable:bool,manual_reason:str="",raw_payload:str="",actor:str="") -> None:
        if source=="manual" and not manual_reason.strip():raise ValueError("برای ثبت دستی وزن، درج علت الزامی است.")
        with self._lock,self.connection() as con:
            doc=con.execute("SELECT status FROM advanced_documents WHERE id=?",(doc_id,)).fetchone()
            if not doc or doc["status"]=="finalized":raise ValueError("این سند قابل توزین نیست.")
            if sequence==1:
                staged=con.execute("SELECT payload_json FROM webhook_events WHERE document_id=? AND event_type='weighbridge.document.first_weighed'",(doc_id,)).fetchone()
                if staged and abs(float(json.loads(staged['payload_json'])['weighings'][0]['weight_kg'])-float(weight_kg))>.0001:
                    raise ValueError('توزین اول برای ثبت در سایت ارسال شده و قابل بازنویسی نیست.')
            con.execute("""INSERT INTO weighings(document_id,sequence,role,weight_kg,source,captured_at,stable,manual_reason,raw_payload,operator_name) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(document_id,sequence) DO UPDATE SET role=excluded.role,weight_kg=excluded.weight_kg,source=excluded.source,captured_at=excluded.captured_at,stable=excluded.stable,manual_reason=excluded.manual_reason,raw_payload=excluded.raw_payload,operator_name=excluded.operator_name""",(doc_id,sequence,role,weight_kg,source,captured_at,int(stable),manual_reason,raw_payload,actor))
            con.execute("UPDATE advanced_documents SET status=?,updated_at=? WHERE id=?",("first_weighed" if sequence==1 else "second_weighed",captured_at,doc_id))
            con.execute("INSERT INTO audit_log(document_id,action,details,actor,created_at) VALUES(?,?,?,?,?)",(doc_id,"weighing.manual" if source=="manual" else "weighing.scale",json.dumps({"sequence":sequence,"weight_kg":weight_kg,"reason":manual_reason,"raw":raw_payload},ensure_ascii=False),actor,captured_at))

    def clear_quality_document(self, doc_id: int) -> None:
        with self._lock, self.connection() as con:
            doc = con.execute("SELECT status FROM advanced_documents WHERE id=?", (doc_id,)).fetchone()
            if not doc or doc['status'] == 'finalized':
                raise ValueError("سند نهایی‌شده قابل تغییر نیست.")
            con.execute("DELETE FROM document_compositions WHERE document_id=?", (doc_id,))
            con.execute("UPDATE advanced_documents SET quality_document_code='',expert_name='',assessed_at=NULL,loss_percentage=0,rejected_percentage=0,loss_weight_kg=0,rejected_weight_kg=0 WHERE id=?", (doc_id,))

    def save_composition(self,doc_id:int,items:list[dict[str,Any]],expert_name:str,rejected_percentage:float=0,assessed_at:str|None=None,loss_percentage:float=0)->None:
        assessed_at=assessed_at or datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock,self.connection() as con:
            con.execute("DELETE FROM document_compositions WHERE document_id=?",(doc_id,)); con.executemany("INSERT INTO document_compositions(document_id,scrap_type_id,scrap_name,percentage,weight_kg,notes) VALUES(?,?,?,?,?,?)",[(doc_id,str(x["scrap_type_id"]),x["scrap_name"],float(x["percentage"]),float(x.get("weight_kg",0)),x.get("notes","")) for x in items]); con.execute("UPDATE advanced_documents SET status='quality_assessed',expert_name=?,assessed_at=?,rejected_percentage=?,loss_percentage=?,updated_at=? WHERE id=?",(expert_name,assessed_at,rejected_percentage,loss_percentage,assessed_at,doc_id))

    def document_parts(self,doc_id:int)->tuple[list[sqlite3.Row],list[sqlite3.Row]]:
        with self.connection() as con:return (con.execute("SELECT * FROM weighings WHERE document_id=? ORDER BY sequence",(doc_id,)).fetchall(),con.execute("SELECT * FROM document_compositions WHERE document_id=? ORDER BY id",(doc_id,)).fetchall())

    def finalize_document(self,doc_id:int,calculation:Any,finalized_at:str|None=None)->str:
        finalized_at=finalized_at or datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock,self.connection() as con:
            doc=con.execute("SELECT * FROM advanced_documents WHERE id=?",(doc_id,)).fetchone(); ws=con.execute("SELECT * FROM weighings WHERE document_id=? ORDER BY sequence",(doc_id,)).fetchall()
            if not doc or len(ws)!=2:raise ValueError("برای صدور سند، هر دو توزین باید ثبت شده باشند.")
            con.execute("UPDATE advanced_documents SET status='finalized',finalized_at=?,exit_at=?,rejected_weight_kg=?,loss_percentage=?,loss_weight_kg=?,accepted_weight_kg=?,updated_at=? WHERE id=?",(finalized_at,ws[1]["captured_at"],float(calculation.rejected_weight_kg),float(calculation.loss_percentage),float(calculation.loss_weight_kg),float(calculation.accepted_weight_kg),finalized_at,doc_id))
            pending=not calculation.items and doc["movement_type"] not in ("warehouse_to_production","warehouse_transfer")
            if pending:
                con.execute("DELETE FROM document_compositions WHERE document_id=?",(doc_id,))
                con.execute("UPDATE advanced_documents SET quality_pending=1,quality_document_code='',expert_name='',assessed_at=NULL,loss_percentage=0,rejected_percentage=0 WHERE id=?",(doc_id,))
            for x in calculation.items:con.execute("UPDATE document_compositions SET weight_kg=? WHERE document_id=? AND scrap_type_id=?",(float(x["weight_kg"]),doc_id,x["scrap_type_id"]))
            payload=self._advanced_payload(con,doc_id,finalized_at); cur=con.execute("INSERT INTO webhook_events(event_id,document_id,event_type,occurred_at,payload_json) VALUES(?,?,?,?,?)",(doc["event_id"],doc_id,"weighbridge.document.finalized",finalized_at,json.dumps(payload,ensure_ascii=False))); con.execute("INSERT OR IGNORE INTO event_deliveries(event_id,endpoint_id) SELECT ?,id FROM endpoints WHERE active=1 AND kind='weighbridge'",(cur.lastrowid,)); return doc["event_id"]

    def _advanced_payload(self,con:sqlite3.Connection,doc_id:int,finalized_at:str)->dict[str,Any]:
        return self.material_payload(con,doc_id,self._legacy_advanced_payload(con,doc_id,finalized_at))

    def _legacy_advanced_payload(self,con:sqlite3.Connection,doc_id:int,finalized_at:str)->dict[str,Any]:
        d=con.execute("SELECT * FROM advanced_documents WHERE id=?",(doc_id,)).fetchone(); ws=con.execute("SELECT * FROM weighings WHERE document_id=? ORDER BY sequence",(doc_id,)).fetchall(); ps=con.execute("SELECT * FROM document_compositions WHERE document_id=? ORDER BY id",(doc_id,)).fetchall(); company=json.loads(d["company_snapshot"] or "{}") or None
        composition=[{"scrap_type_id":x["scrap_type_id"],"scrap_name":x["scrap_name"],"percentage":x["percentage"],"weight_kg":x["weight_kg"]} for x in ps]
        waybill=json.loads(d["waybill_snapshot"] or "{}") or None
        return {"schema_version":2,"quality_pending":bool(d["quality_pending"]),"event_id":d["event_id"],"event_type":"weighbridge.document.finalized","quality_document_code":d["quality_document_code"] or None,"document":{"document_number":d["document_number"],"movement_type":d["movement_type"],"status":"finalized","registered_at":d["registered_at"],"finalized_at":finalized_at,"entry_at":d["entry_at"],"exit_at":ws[1]["captured_at"]},"waybill":{"number":int(d["waybill_number"]),"reservation_token":d["waybill_reservation_token"]} if d["waybill_number"] else None,"waybill_snapshot":waybill,"company":company,"driver":{"name":d["driver_name"],"phone":d["driver_phone"]},"vehicle":{"trailer_plate":d["trailer_plate"],"type":d["vehicle_type"]},"origin":{"province":d["origin_province"],"city":d["origin_city"]},"destination":d["destination"],"weighings":[{"sequence":x["sequence"],"kind":"gross" if x["sequence"] == 1 else "tare","weight_kg":x["weight_kg"],"source":"manual" if x["source"] == "manual" else "device","captured_at":x["captured_at"],"manual_reason":x["manual_reason"],"raw_payload":x["raw_payload"]} for x in ws],"composition":composition,"items":[{"id":x["scrap_type_id"],"name":x["scrap_name"],"weight":x["weight_kg"],"weight_kg":x["weight_kg"]} for x in ps],"quality":{"rejected_percentage":d["rejected_percentage"],"rejected_weight_kg":d["rejected_weight_kg"],"loss_percentage":d["loss_percentage"],"loss_weight_kg":d["loss_weight_kg"],"expert_name":d["expert_name"],"assessed_at":d["assessed_at"]}}

    def pending_event_deliveries(self,limit:int=50)->list[sqlite3.Row]:
        with self.connection() as con:return con.execute("SELECT x.id delivery_id,x.attempts,e.url,e.token,w.payload_json FROM event_deliveries x JOIN webhook_events w ON w.id=x.event_id JOIN endpoints e ON e.id=x.endpoint_id WHERE x.status IN ('pending','failed') AND e.active=1 ORDER BY x.id LIMIT ?",(limit,)).fetchall()

    def save_document_weight_discrepancy(self, doc_id: int, certificate: dict) -> None:
        with self.connection() as con:
            con.execute("UPDATE advanced_documents SET weight_discrepancy_json=? WHERE id=?", (json.dumps(certificate, ensure_ascii=False), doc_id))

    def save_event_weight_discrepancy(self, delivery_id: int, response: dict) -> None:
        """Keep the server certificate on the document, independently of delivery state."""
        certificate = response.get("weight_discrepancy")
        if not isinstance(certificate, dict) or not certificate.get("certificate_number"):
            return
        certificate = dict(certificate)
        certificate["comparison"] = response.get("weight_comparison") or certificate.get("comparison") or {}
        with self.connection() as con:
            con.execute("""UPDATE advanced_documents SET weight_discrepancy_json=? WHERE id IN (
                SELECT w.document_id FROM webhook_events w JOIN event_deliveries d ON d.event_id=w.id WHERE d.id=?)""",
                (json.dumps(certificate, ensure_ascii=False), delivery_id))

    def mark_event_delivery(self,delivery_id:int,ok:bool,code:int|None,error:str="",needs_attention:bool=False)->None:
        now=datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connection() as con:con.execute("UPDATE event_deliveries SET status=?,attempts=attempts+1,last_error=?,response_code=?,last_attempt_at=?,delivered_at=? WHERE id=?",("synced" if ok else ("needs_attention" if needs_attention else "failed"),error[:1000] or None,code,now,now if ok else None,delivery_id))

    def advanced_delivery_details(self, doc_id: int) -> list[sqlite3.Row]:
        with self.connection() as con:
            return con.execute("""SELECT x.*,e.name,e.url FROM event_deliveries x
              JOIN webhook_events w ON w.id=x.event_id JOIN endpoints e ON e.id=x.endpoint_id
              WHERE w.document_id=? ORDER BY x.id""", (doc_id,)).fetchall()

    def retry_advanced_document(self, doc_id: int) -> None:
        with self.connection() as con:
            con.execute("""UPDATE event_deliveries SET status='pending',last_error=NULL
              WHERE event_id IN (SELECT id FROM webhook_events WHERE document_id=?) AND status!='synced'""", (doc_id,))

    def can_delete_failed_advanced_document(self, doc_id: int) -> bool:
        with self.connection() as con:
            row=con.execute("""SELECT SUM(CASE WHEN x.status='synced' THEN 1 ELSE 0 END) synced_count,
              SUM(CASE WHEN x.status IN ('failed','needs_attention') THEN 1 ELSE 0 END) failed_count
              FROM event_deliveries x JOIN webhook_events w ON w.id=x.event_id WHERE w.document_id=?""",(doc_id,)).fetchone()
            return bool(row and int(row["synced_count"] or 0)==0 and int(row["failed_count"] or 0)>0)

    def delete_failed_advanced_document(self, doc_id: int) -> None:
        with self._lock,self.connection() as con:
            row=con.execute("""SELECT SUM(CASE WHEN x.status='synced' THEN 1 ELSE 0 END) synced_count,
              SUM(CASE WHEN x.status IN ('failed','needs_attention') THEN 1 ELSE 0 END) failed_count
              FROM event_deliveries x JOIN webhook_events w ON w.id=x.event_id WHERE w.document_id=?""",(doc_id,)).fetchone()
            if not row or int(row["synced_count"] or 0)>0 or int(row["failed_count"] or 0)==0:
                raise ValueError("فقط سندی که به هیچ مقصدی ارسال موفق نشده قابل حذف است.")
            con.execute("DELETE FROM event_deliveries WHERE event_id IN (SELECT id FROM webhook_events WHERE document_id=?)",(doc_id,))
            con.execute("DELETE FROM webhook_events WHERE document_id=?",(doc_id,))
            con.execute("DELETE FROM audit_log WHERE document_id=?",(doc_id,))
            deleted=con.execute("DELETE FROM advanced_documents WHERE id=?",(doc_id,)).rowcount
            if deleted!=1:raise ValueError("سند پیدا نشد.")

    def scraps(self) -> list[sqlite3.Row]:
        with self.connection() as con:
            return con.execute("SELECT * FROM scrap_types ORDER BY name").fetchall()

    def create_processing_document(self, data: dict[str, Any]) -> int:
        now=data.get("processed_at") or datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock,self.connection() as con:
            cur=con.execute("""INSERT INTO processing_documents(event_id,document_number,processed_at,output_scrap_type_id,output_scrap_name,output_weight_kg,quality_expert_name,notes,weight_source,weight_stable,manual_reason,raw_payload,created_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",(data["event_id"],data["document_number"],now,str(data["output_scrap_type_id"]),data["output_scrap_name"],int(round(float(data["output_weight_kg"]))),data["quality_expert_name"],data.get("notes","") or "",data["weight_source"],int(bool(data.get("weight_stable"))),data.get("manual_reason"),data.get("raw_payload"),now))
            doc_id=int(cur.lastrowid)
            source_id=data.get("source_warehouse_id");destination_id=data.get("destination_warehouse_id")
            snapshot=[w for w in self.warehouses() if w["id"] in (source_id,destination_id)]
            con.execute("UPDATE processing_documents SET source_warehouse_id=?,destination_warehouse_id=?,warehouse_snapshot_json=? WHERE id=?",(source_id,destination_id,json.dumps(snapshot,ensure_ascii=False),doc_id))
            self._stamp_actor(con,'processing',data['event_id'])
            con.executemany("INSERT INTO processing_inputs(processing_document_id,scrap_type_id,scrap_name,percentage) VALUES(?,?,?,?)",[(doc_id,str(x["scrap_type_id"]),x["scrap_name"],float(x["percentage"])) for x in data["inputs"]])
            con.execute("INSERT OR IGNORE INTO processing_deliveries(processing_document_id,endpoint_id) SELECT ?,id FROM endpoints WHERE active=1 AND kind='processing'",(doc_id,))
            return doc_id

    def processing_draft(self) -> dict[str, Any] | None:
        with self.connection() as con:
            row=con.execute("SELECT current_step,payload_json,updated_at FROM processing_draft WHERE id=1").fetchone()
        if not row:return None
        try:payload=json.loads(row["payload_json"] or "{}")
        except (TypeError,json.JSONDecodeError):payload={}
        return {"current_step":int(row["current_step"] or 0),"payload":payload,"updated_at":row["updated_at"]}

    def save_processing_draft(self, current_step:int, payload:dict[str,Any]) -> None:
        now=datetime.now().astimezone().isoformat(timespec="seconds")
        encoded=json.dumps(payload,ensure_ascii=False,separators=(",",":"))
        with self._lock,self.connection() as con:
            con.execute("""INSERT INTO processing_draft(id,current_step,payload_json,updated_at) VALUES(1,?,?,?)
              ON CONFLICT(id) DO UPDATE SET current_step=excluded.current_step,payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
              (max(0,min(1,int(current_step))),encoded,now))

    def delete_processing_draft(self) -> None:
        with self._lock,self.connection() as con:con.execute("DELETE FROM processing_draft WHERE id=1")

    def processing_documents(self) -> list[sqlite3.Row]:
        with self.connection() as con:return con.execute("""SELECT p.*,
          COALESCE((SELECT COUNT(*) FROM processing_deliveries d WHERE d.processing_document_id=p.id),0) total_count,
          COALESCE((SELECT SUM(d.status='synced') FROM processing_deliveries d WHERE d.processing_document_id=p.id),0) ok_count
          FROM processing_documents p ORDER BY p.processed_at DESC""").fetchall()

    def processing_document(self, doc_id:int) -> sqlite3.Row|None:
        with self.connection() as con:return con.execute("SELECT * FROM processing_documents WHERE id=?",(doc_id,)).fetchone()

    def processing_inputs(self, doc_id:int) -> list[sqlite3.Row]:
        with self.connection() as con:return con.execute("SELECT * FROM processing_inputs WHERE processing_document_id=? ORDER BY id",(doc_id,)).fetchall()

    def processing_payload(self,row:sqlite3.Row|dict[str,Any]) -> dict[str,Any]:
        warehouse_fields={}
        if "source_warehouse_id" in row.keys() and row["source_warehouse_id"]:
            warehouse_fields={"source_warehouse_id":row["source_warehouse_id"],"destination_warehouse_id":row["destination_warehouse_id"]}
        return {**warehouse_fields,"schema_version":1,"event_id":row["event_id"],"processed_at":row["processed_at"],
          "inputs":[{"scrap_type_id":int(x["scrap_type_id"]),"percentage":float(x["percentage"])} for x in self.processing_inputs(int(row["id"]))],
          "output":{"scrap_type_id":int(row["output_scrap_type_id"]),"weight_kg":int(row["output_weight_kg"])},
          "weighing":{"source":row["weight_source"],"stable":bool(row["weight_stable"]),"manual_reason":row["manual_reason"],"raw_payload":row["raw_payload"]},
          "quality_expert_name":row["quality_expert_name"],"notes":row["notes"]}

    def pending_processing_deliveries(self,limit:int=100)->list[sqlite3.Row]:
        with self.connection() as con:return con.execute("""SELECT d.id delivery_id,d.attempts,e.url,e.token,p.* FROM processing_deliveries d JOIN endpoints e ON e.id=d.endpoint_id JOIN processing_documents p ON p.id=d.processing_document_id WHERE d.status IN ('pending','failed') AND e.active=1 ORDER BY d.id LIMIT ?""",(limit,)).fetchall()

    def mark_processing_delivery(self,delivery_id:int,ok:bool,code:int|None,error:str="",official_number:str="")->None:
        now=datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connection() as con:
            con.execute("UPDATE processing_deliveries SET status=?,attempts=attempts+1,last_error=?,response_code=?,last_attempt_at=?,delivered_at=? WHERE id=?",("synced" if ok else "failed",error[:1000] or None,code,now,now if ok else None,delivery_id))
            if ok and official_number:con.execute("UPDATE processing_documents SET document_number=? WHERE id=(SELECT processing_document_id FROM processing_deliveries WHERE id=?)",(official_number,delivery_id))

    def endpoints(self, active_only: bool = False) -> list[sqlite3.Row]:
        query = "SELECT * FROM endpoints" + (" WHERE active=1" if active_only else "") + " ORDER BY id"
        with self.connection() as con:
            return con.execute(query).fetchall()

    @staticmethod
    def endpoint_url(base_url: str, path: str) -> str:
        return base_url.strip().rstrip("/") + "/" + path.strip().lstrip("/")

    def webhook_sites(self, active_only: bool = False) -> list[sqlite3.Row]:
        where=" WHERE s.active=1" if active_only else ""
        with self.connection() as con:
            return con.execute(f"""SELECT s.*,
              COUNT(e.id) endpoint_count,
              SUM(CASE WHEN e.active=1 THEN 1 ELSE 0 END) active_endpoint_count
              FROM webhook_sites s LEFT JOIN endpoints e ON e.site_id=s.id
              {where} GROUP BY s.id ORDER BY s.id""").fetchall()

    def webhook_site(self, site_id: int) -> sqlite3.Row | None:
        with self.connection() as con:
            return con.execute("SELECT * FROM webhook_sites WHERE id=?",(site_id,)).fetchone()

    def site_endpoints(self, site_id: int, active_only: bool = False) -> list[sqlite3.Row]:
        sql="SELECT * FROM endpoints WHERE site_id=?" + (" AND active=1" if active_only else "") + " ORDER BY id"
        with self.connection() as con:return con.execute(sql,(site_id,)).fetchall()

    def upsert_webhook_site(self, site_id: int | None, name: str, base_url: str, token: str,
                            active: bool, endpoints: list[dict[str,Any]]) -> int:
        name=name.strip(); base_url=base_url.strip().rstrip("/")
        parts=urlsplit(base_url)
        if not name or parts.scheme not in ("http","https") or not parts.netloc:
            raise ValueError("نام سیستم و آدرس پایه معتبر الزامی است.")
        kinds={"weighbridge","waybill","processing"}; seen:set[str]=set()
        cleaned=[]
        for endpoint in endpoints:
            kind=str(endpoint.get("kind") or "")
            path=str(endpoint.get("path") or "").strip()
            if kind not in kinds or kind in seen or not path:
                raise ValueError("نوع و مسیر هر Endpoint باید معتبر و غیرتکراری باشد.")
            seen.add(kind); cleaned.append((kind,path,bool(endpoint.get("active",True))))
        if not cleaned:raise ValueError("حداقل یک Endpoint برای سایت وارد کنید.")
        now=datetime.now().astimezone().isoformat(timespec="seconds")
        labels={"weighbridge":"اسناد باسکول","waybill":"بارنامه‌ها","processing":"اسناد فرآوری"}
        with self._lock,self.connection() as con:
            con.execute('DELETE FROM settings WHERE key=?',('deleted_erp_'+hashlib.sha256(base_url.encode()).hexdigest(),))
            if site_id:
                con.execute("UPDATE webhook_sites SET name=?,base_url=?,token=?,active=?,updated_at=? WHERE id=?",(name,base_url,token,int(active),now,site_id))
            else:
                cursor=con.execute("INSERT INTO webhook_sites(name,base_url,token,active,created_at,updated_at) VALUES(?,?,?,?,?,?)",(name,base_url,token,int(active),now,now))
                site_id=int(cursor.lastrowid)
            configured=set()
            for kind,path,endpoint_active in cleaned:
                configured.add(kind); final_url=self.endpoint_url(base_url,path); effective=int(active and endpoint_active)
                row=con.execute("SELECT id FROM endpoints WHERE site_id=? AND kind=? ORDER BY id LIMIT 1",(site_id,kind)).fetchone()
                if row:
                    endpoint_id=int(row["id"]); con.execute("UPDATE endpoints SET name=?,url=?,token=?,active=?,path=? WHERE id=?",(labels[kind],final_url,token,effective,path,endpoint_id))
                else:
                    cursor=con.execute("INSERT INTO endpoints(name,url,token,active,created_at,kind,site_id,path) VALUES(?,?,?,?,?,?,?,?)",(labels[kind],final_url,token,effective,now,kind,site_id,path))
                    endpoint_id=int(cursor.lastrowid)
                if effective and kind=="waybill":con.execute("INSERT OR IGNORE INTO waybill_deliveries(waybill_id,endpoint_id) SELECT id,? FROM waybills",(endpoint_id,))
                elif effective and kind=="processing":con.execute("INSERT OR IGNORE INTO processing_deliveries(processing_document_id,endpoint_id) SELECT id,? FROM processing_documents",(endpoint_id,))
                elif effective and kind=="weighbridge":con.execute("INSERT OR IGNORE INTO event_deliveries(event_id,endpoint_id) SELECT id,? FROM webhook_events",(endpoint_id,))
            for kind in kinds-configured:con.execute("UPDATE endpoints SET active=0 WHERE site_id=? AND kind=?",(site_id,kind))
            return int(site_id)

    def disable_webhook_site(self, site_id:int)->None:
        with self.connection() as con:
            con.execute("UPDATE webhook_sites SET active=0 WHERE id=?",(site_id,)); con.execute("UPDATE endpoints SET active=0 WHERE site_id=?",(site_id,))

    def delete_webhook_site(self,site_id:int)->None:
        with self._lock,self.connection() as con:
            site=con.execute('SELECT base_url FROM webhook_sites WHERE id=?',(site_id,)).fetchone()
            if not site:raise ValueError('سایت یا ERP پیدا نشد.')
            for table in ('deliveries','event_deliveries','waybill_deliveries','processing_deliveries'):
                con.execute(f'DELETE FROM {table} WHERE endpoint_id IN (SELECT id FROM endpoints WHERE site_id=?)',(site_id,))
            con.execute('DELETE FROM endpoints WHERE site_id=?',(site_id,))
            con.execute('DELETE FROM webhook_sites WHERE id=?',(site_id,))
            key='deleted_erp_'+hashlib.sha256(site['base_url'].rstrip('/').encode()).hexdigest()
            con.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',(key,'1'))

    def upsert_endpoint(self, endpoint_id: int | None, name: str, url: str, token: str, active: bool, kind: str = "weighbridge") -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connection() as con:
            parts=urlsplit(url); base=urlunsplit((parts.scheme,parts.netloc,"","","")); path=parts.path or "/"
            if parts.query:path += "?"+parts.query
            site=con.execute("SELECT id FROM webhook_sites WHERE base_url=? AND token=? ORDER BY id LIMIT 1",(base,token)).fetchone()
            if site:site_id=int(site["id"])
            else:
                cursor=con.execute("INSERT INTO webhook_sites(name,base_url,token,active,created_at,updated_at) VALUES(?,?,?,?,?,?)",(name,base,token,int(active),now,now)); site_id=int(cursor.lastrowid)
            if endpoint_id:
                con.execute("UPDATE endpoints SET name=?,url=?,token=?,active=?,kind=?,site_id=?,path=? WHERE id=?", (name, url, token, int(active), kind,site_id,path,endpoint_id))
            else:
                con.execute("INSERT INTO endpoints(name,url,token,active,created_at,kind,site_id,path) VALUES(?,?,?,?,?,?,?,?)", (name, url, token, int(active), now, kind,site_id,path))
            if kind == "waybill" and active:
                target_id=endpoint_id or int(con.execute("SELECT last_insert_rowid()").fetchone()[0])
                con.execute("INSERT OR IGNORE INTO waybill_deliveries(waybill_id,endpoint_id) SELECT id,? FROM waybills WHERE cache_source!='server'",(target_id,))
            if kind == "processing" and active:
                target_id=endpoint_id or int(con.execute("SELECT last_insert_rowid()").fetchone()[0])
                con.execute("INSERT OR IGNORE INTO processing_deliveries(processing_document_id,endpoint_id) SELECT id,? FROM processing_documents",(target_id,))

    def delete_endpoint(self, endpoint_id: int) -> None:
        with self.connection() as con:
            con.execute("UPDATE endpoints SET active=0 WHERE id=?", (endpoint_id,))

    def create_document(self, event_id: str, number: str, registered_at: str, scrap_id: str, scrap_name: str, weight: float, movement: str) -> int:
        with self._lock, self.connection() as con:
            cur = con.execute("INSERT INTO documents(event_id,document_number,registered_at,scrap_id,scrap_name,weight_kg,movement_type,created_at) VALUES(?,?,?,?,?,?,?,?)", (event_id, number, registered_at, scrap_id, scrap_name, weight, movement, registered_at))
            doc_id = int(cur.lastrowid)
            con.execute("INSERT OR IGNORE INTO deliveries(document_id,endpoint_id) SELECT ?,id FROM endpoints WHERE active=1", (doc_id,))
            return doc_id

    def documents(self, search: str = "", movement: str = "", status: str = "") -> list[sqlite3.Row]:
        clauses, args = [], []
        if search:
            clauses.append("(d.document_number LIKE ? OR d.scrap_name LIKE ?)")
            args.extend([f"%{search}%", f"%{search}%"])
        if movement:
            clauses.append("d.movement_type=?"); args.append(movement)
        if status:
            clauses.append("COALESCE(s.sync_status,'no_endpoint')=?"); args.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = f"""SELECT d.*, COALESCE(s.sync_status,'no_endpoint') sync_status,
          COALESCE(s.ok_count,0) ok_count, COALESCE(s.total_count,0) total_count
          FROM documents d LEFT JOIN (
            SELECT document_id, COUNT(*) total_count,
              SUM(CASE WHEN status='synced' THEN 1 ELSE 0 END) ok_count,
              CASE WHEN SUM(CASE WHEN status='synced' THEN 1 ELSE 0 END)=COUNT(*) THEN 'synced'
                   WHEN SUM(CASE WHEN status='needs_attention' THEN 1 ELSE 0 END)>0 THEN 'needs_attention'
                   WHEN SUM(CASE WHEN status='synced' THEN 1 ELSE 0 END)>0 THEN 'partial'
                   WHEN SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END)>0 THEN 'failed'
                   ELSE 'pending' END sync_status
            FROM deliveries GROUP BY document_id) s ON s.document_id=d.id
          {where} ORDER BY d.registered_at DESC"""
        with self.connection() as con:
            return con.execute(sql, args).fetchall()

    def document(self, doc_id: int) -> sqlite3.Row | None:
        with self.connection() as con:
            return con.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()

    def delivery_details(self, doc_id: int) -> list[sqlite3.Row]:
        with self.connection() as con:
            return con.execute("SELECT x.*,e.name,e.url FROM deliveries x JOIN endpoints e ON e.id=x.endpoint_id WHERE x.document_id=?", (doc_id,)).fetchall()

    def pending_deliveries(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.connection() as con:
            return con.execute("""SELECT x.id delivery_id,x.attempts,e.url,e.token,d.* FROM deliveries x
              JOIN endpoints e ON e.id=x.endpoint_id JOIN documents d ON d.id=x.document_id
              WHERE x.status IN ('pending','failed') AND e.active=1 ORDER BY x.id LIMIT ?""", (limit,)).fetchall()

    def mark_delivery(self, delivery_id: int, ok: bool, code: int | None, error: str = "", needs_attention: bool = False) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connection() as con:
            con.execute("""UPDATE deliveries SET status=?,attempts=attempts+1,last_error=?,response_code=?,last_attempt_at=?,delivered_at=? WHERE id=?""",
                        ("synced" if ok else ("needs_attention" if needs_attention else "failed"), error[:1000] or None, code, now, now if ok else None, delivery_id))

    def retry_document(self, doc_id: int) -> None:
        with self.connection() as con:
            con.execute("UPDATE deliveries SET status='pending',last_error=NULL WHERE document_id=? AND status!='synced'", (doc_id,))

    def can_delete_failed_document(self, doc_id: int) -> bool:
        with self.connection() as con:
            row = con.execute("""SELECT
                SUM(CASE WHEN status='synced' THEN 1 ELSE 0 END) synced_count,
                SUM(CASE WHEN status IN ('failed','needs_attention') THEN 1 ELSE 0 END) failed_count
                FROM deliveries WHERE document_id=?""", (doc_id,)).fetchone()
            return bool(row and int(row["synced_count"] or 0) == 0 and int(row["failed_count"] or 0) > 0)

    def delete_failed_document(self, doc_id: int) -> None:
        with self._lock, self.connection() as con:
            row = con.execute("""SELECT
                SUM(CASE WHEN status='synced' THEN 1 ELSE 0 END) synced_count,
                SUM(CASE WHEN status IN ('failed','needs_attention') THEN 1 ELSE 0 END) failed_count
                FROM deliveries WHERE document_id=?""", (doc_id,)).fetchone()
            if not row or int(row["synced_count"] or 0) > 0 or int(row["failed_count"] or 0) == 0:
                raise ValueError('فقط سندی که به هیچ مقصدی ارسال موفق نشده قابل حذف است.')
            con.execute("DELETE FROM deliveries WHERE document_id=?", (doc_id,))
            deleted = con.execute("DELETE FROM documents WHERE id=?", (doc_id,)).rowcount
            if deleted != 1:
                raise ValueError('سند پیدا نشد.')

    def delete_document(self, doc_id: int) -> None:
        """Permanently remove any legacy document from this local database only."""
        with self._lock, self.connection() as con:
            con.execute("DELETE FROM deliveries WHERE document_id=?", (doc_id,))
            deleted = con.execute("DELETE FROM documents WHERE id=?", (doc_id,)).rowcount
            if deleted != 1:
                raise ValueError("سند در دیتابیس محلی پیدا نشد.")

    @staticmethod
    def payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return {
            "event_id": row["event_id"], "document_number": row["document_number"],
            "registered_at": row["registered_at"], "sent_at": row["registered_at"],
            "movement_type": row["movement_type"],
            "items": [{"name": row["scrap_name"], "weight": float(row["weight_kg"]), "weight_kg": float(row["weight_kg"])}],
        }
