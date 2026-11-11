from __future__ import annotations

from typing import Any
import requests


def safe_response_error(response: requests.Response) -> str:
    """Turn an HTTP failure into an operator-safe Persian message."""
    status = response.status_code
    data: Any = None
    try:
        data = response.json()
    except (ValueError, requests.JSONDecodeError):
        pass

    server_message = data.get("message") if isinstance(data, dict) else None
    if status == 409 and server_message:
        return str(server_message)[:500]
    if status == 404 and server_message:
        return str(server_message)[:500]
    if status == 401:
        return "توکن اتصال معتبر نیست. تنظیمات مقصد را بررسی کنید."
    if status == 403:
        return "حساب اتصال مجوز ارسال سند به این مقصد را ندارد."
    if status == 422:
        if isinstance(data, dict):
            errors = data.get("errors")
            if isinstance(errors, dict):
                field_labels={"supplier_company_id":"شرکت تأمین‌کننده","vehicle.trailer_plate":"پلاک تریلی","waybill.number":"شماره بارنامه","waybill.reservation_token":"رزرو بارنامه","company.id":"شرکت تأمین‌کننده","driver.name":"نام راننده","driver.phone":"شماره موبایل راننده","origin.province":"استان مبدأ","origin.city":"شهر مبدأ","weighings":"اطلاعات توزین","composition":"ترکیب قراضه","inputs":"ترکیب ورودی فرآوری","output.scrap_type_id":"قراضه خروجی","output.weight_kg":"وزن خروجی","weighing.manual_reason":"علت ثبت دستی","quality_expert_name":"کارشناس کنترل کیفیت"}
                key,values=next(((str(key),values) for key,values in errors.items() if isinstance(values,list) and values),("",[]))
                if values:
                    label=next((label for field,label in field_labels.items() if key==field or key.startswith(field+".")),"یکی از فیلدها")
                    return f"اطلاعات «{label}» معتبر نیست. مقدار ثبت‌شده را بررسی کنید."
        return f"اطلاعات سند معتبر نیست: {server_message or 'مقادیر ثبت‌شده را بررسی کنید.'}"
    if status == 429:
        return "تعداد درخواست‌ها زیاد است؛ اپلیکیشن کمی بعد دوباره تلاش می‌کند."
    if status >= 500:
        return "سرور مقصد موقتاً پاسخ‌گو نیست؛ سند در صف باقی می‌ماند."
    return f"مقصد سند را نپذیرفت (کد {status})."


def auth_headers(token: str) -> dict[str, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token.strip():
        headers["Authorization"] = f"Bearer {token.strip()}"
    return headers


def fetch_scraps(url: str, token: str = "", timeout: int = 12) -> list[dict[str, str]]:
    response = requests.get(url, headers=auth_headers(token), timeout=timeout)
    response.raise_for_status()
    raw: Any = response.json()
    if isinstance(raw, dict):
        raw = raw.get("data", raw.get("items", raw.get("scraps", [])))
        if isinstance(raw, dict):
            raw = raw.get("data", [])
    if not isinstance(raw, list):
        raise ValueError("ساختار پاسخ API قابل شناسایی نیست")
    result = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            result.append({"id": item, "name": item})
        elif isinstance(item, dict):
            # A custom/legacy endpoint may return all scrap types. Never expose
            # production-only scraps in the weighbridge composition picker.
            if item.get("is_warehouse_storable") is False:
                continue
            name = item.get("name") or item.get("title") or item.get("label")
            if name:
                result.append({"id": str(item.get("id", item.get("uuid", name))), "name": str(name)})
    return result


def fetch_companies(url: str, token: str = "", timeout: int = 12) -> list[dict[str, Any]]:
    response = requests.get(url, headers=auth_headers(token), timeout=timeout)
    response.raise_for_status()
    raw: Any = response.json()
    if isinstance(raw, dict):
        raw = raw.get("data", raw.get("items", raw.get("companies", [])))
        if isinstance(raw, dict): raw = raw.get("data", [])
    if not isinstance(raw, list): raise ValueError("ساختار پاسخ API شرکت‌ها قابل شناسایی نیست")
    result = []
    for item in raw:
        if not isinstance(item, dict): continue
        name = item.get("name") or item.get("title")
        if name:
            result.append({k: item.get(k, "") for k in ("code","legal_name","phone","province","city","address","description","national_id","registration_number")} |
                          {"id": str(item.get("id", item.get("uuid", name))), "name": str(name), "active": item.get("is_active", item.get("active", True))})
    return result


def fetch_available_waybills(url: str, token: str = "", search: str = "", timeout: int = 12, material_type_id=None) -> list[dict[str, Any]]:
    response=requests.get(url,params={"search":search,**({"material_type_id":material_type_id} if material_type_id else {})},headers=auth_headers(token),timeout=timeout)
    if not response.ok:raise RuntimeError(safe_response_error(response))
    raw=response.json()
    if isinstance(raw,dict):raw=raw.get("data",raw.get("items",[]))
    result=[]
    for item in raw if isinstance(raw,list) else []:
        if not isinstance(item,dict):continue
        row=dict(item); driver=row.pop("driver",{}) or {}; origin=row.pop("origin",{}) or {}
        row["driver_name"]=row.get("driver_name") or driver.get("name",""); row["driver_phone"]=row.get("driver_phone") or driver.get("phone","")
        row["origin_province"]=row.get("origin_province") or origin.get("province",""); row["origin_city"]=row.get("origin_city") or origin.get("city","")
        result.append(row)
    return result


def fetch_transportation_sync(url: str, token: str = "", cursor: str = "", known_version: str = "", timeout: int = 20) -> dict[str, Any]:
    """Fetch one incremental, authoritative page from MegaPulse."""
    response = requests.get(
        url,
        params={key:value for key,value in {
            "cursor":cursor, "snapshot":"1", "known_version":known_version,
        }.items() if value},
        headers=auth_headers(token),
        timeout=timeout,
    )
    if not response.ok:
        raise RuntimeError(safe_response_error(response))
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise RuntimeError("پاسخ همگام‌سازی سامانه ساختار معتبری ندارد.")
    return payload


def normalize_quality_code(code: str) -> str:
    return code.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")).strip().upper()


def search_quality_documents(url: str, search: str, material_kind: str, token: str = "", timeout: int = 5) -> list[dict[str, Any]]:
    endpoint = url.removesuffix('/{code}').rstrip('/')
    response = requests.get(endpoint, params={'search': normalize_quality_code(search), 'material_kind': material_kind}, headers=auth_headers(token), timeout=timeout)
    if not response.ok:
        raise RuntimeError(safe_response_error(response))
    payload = response.json()
    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise RuntimeError("پاسخ جست‌وجوی اسناد کیفیت معتبر نیست.")
    return data


def fetch_quality_document(url: str, code: str, token: str = "", timeout: int = 12) -> dict[str, Any]:
    endpoint = url.replace("{code}", normalize_quality_code(code))
    response = requests.get(endpoint, headers=auth_headers(token), timeout=timeout)
    if not response.ok:
        raise RuntimeError(safe_response_error(response))
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise RuntimeError("پاسخ سند کنترل کیفیت ساختار معتبری ندارد.")
    return data


def reserve_waybill(url_template: str, number: int, token: str = "", timeout: int = 12) -> dict[str, Any]:
    url=url_template.replace("{number}",str(number))
    response=requests.post(url,json={},headers=auth_headers(token),timeout=timeout)
    if not response.ok:raise RuntimeError(safe_response_error(response))
    data=response.json()
    return dict(data.get("data",data)) if isinstance(data,dict) else {}


def release_waybill(url_template: str, number: int, reservation_token: str, token: str = "", timeout: int = 12) -> None:
    url=url_template.replace("{number}",str(number))
    response=requests.post(url,json={"reservation_token":reservation_token},headers=auth_headers(token),timeout=timeout)
    if not response.ok:raise RuntimeError(safe_response_error(response))


def send_webhook(url: str, token: str, payload: dict, timeout: int = 15) -> tuple[bool, int | None, str, dict]:
    try:
        r = requests.post(url, json=payload, headers=auth_headers(token), timeout=timeout)
        try:
            response_data = r.json() if r.content else {}
            if not isinstance(response_data, dict): response_data = {}
        except ValueError:
            response_data = {}
        if 200 <= r.status_code < 300:
            import os
            if os.environ.get('MEGASANJ_EDITION') == 'odoo' and response_data.get('success') is not True:
                return False, r.status_code, 'پاسخ اودو ثبت معتبر را تأیید نکرد؛ رویداد در صف حفظ شد.', response_data
            return True, r.status_code, "", response_data
        return False, r.status_code, safe_response_error(r), response_data
    except requests.RequestException as exc:
        return False, None, "اتصال به سرور برقرار نشد. سند ایمن در صف باقی می‌ماند.", {}


def test_endpoint(url: str, token: str) -> tuple[bool, str]:
    try:
        r = requests.options(url, headers=auth_headers(token), timeout=8)
        if r.status_code == 405:
            return True, "مسیر در دسترس است"
        if r.status_code == 404:
            return False, "مسیر API روی سرور پیدا نشد"
        r.raise_for_status()
        return True, "اتصال موفق بود"
    except requests.Timeout:
        return False, "مهلت پاسخ‌گویی API تمام شد"
    except requests.ConnectionError:
        return False, "ارتباط با سرور برقرار نشد"
    except requests.RequestException as exc:
        response=getattr(exc,"response",None)
        return False, safe_response_error(response) if response is not None else "اتصال به API برقرار نشد"

def request_weight_discrepancy(session, event_id, decision=None):
    """Read or finalize a certificate using the signed-in desktop account."""
    from urllib.parse import quote
    if not session or not session.allows('weighbridge'):
        raise ValueError('نشست یا دسترسی باسکول معتبر نیست؛ دوباره وارد شوید.')
    path = 'transportation/weighbridge/' + quote(str(event_id), safe='') + '/weight-discrepancy'
    headers = {'Accept': 'application/json', 'Authorization': 'Bearer ' + session.token}
    try:
        if decision is None:
            response = requests.get(session.api_url(path), headers=headers, timeout=12, allow_redirects=False)
        else:
            response = requests.post(session.api_url(path + '/finalize'), json=decision, headers=headers, timeout=12, allow_redirects=False)
        if response.status_code >= 500:
            raise ValueError(f'خطای داخلی سرویس صورت‌جلسه در سرور (HTTP {response.status_code})؛ ارتباط برقرار است، اما پردازش درخواست ناموفق بود. اطلاعات فرم حفظ شده است.')
        if response.status_code != 200:
            raise ValueError(safe_response_error(response))
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError('سرور پاسخ JSON معتبر برنگرداند؛ مسیر سرویس صورت‌جلسه باید بررسی شود.') from exc
        data = payload.get('data') if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not data.get('certificate_number'):
            raise ValueError('پاسخ صورت‌جلسه معتبر نیست.')
        return data
    except (requests.RequestException, requests.JSONDecodeError) as exc:
        raise ValueError('ارتباط با سرور برقرار نشد؛ اطلاعات فرم حفظ شده است. وضعیت را دوباره بررسی کنید.') from exc
