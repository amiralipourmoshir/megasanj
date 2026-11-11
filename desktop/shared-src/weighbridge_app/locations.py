from __future__ import annotations

import json
from importlib.resources import files


def load_iran_locations() -> tuple[list[dict], dict[int, list[dict]]]:
    """Load the bundled province/city dataset without requiring network access."""
    data_dir = files("weighbridge_app").joinpath("assets", "data")
    provinces = json.loads(data_dir.joinpath("ostan.json").read_text(encoding="utf-8"))
    cities = json.loads(data_dir.joinpath("shahr.json").read_text(encoding="utf-8"))
    provinces = sorted(provinces, key=lambda row: row["name"])
    cities_by_province: dict[int, list[dict]] = {int(row["id"]): [] for row in provinces}
    for city in cities:
        cities_by_province.setdefault(int(city["ostan"]), []).append(city)
    for rows in cities_by_province.values():
        rows.sort(key=lambda row: row["name"])
    return provinces, cities_by_province
