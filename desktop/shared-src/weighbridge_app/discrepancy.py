"""Server-issued weight certificates stored with a weighing document."""
import json


def weight_discrepancy(row):
    if "weight_discrepancy_json" not in row.keys():
        return {}
    try:
        value = json.loads(row["weight_discrepancy_json"] or "{}")
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) and value.get("certificate_number") and value.get("status") != "voided" else {}
