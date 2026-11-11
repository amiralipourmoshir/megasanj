from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable


KG = Decimal("0.001")
PERCENT = Decimal("0.001")


class DomainError(ValueError):
    """A validation error safe to show to an operator."""


@dataclass(frozen=True)
class CompositionInput:
    scrap_type_id: str
    scrap_name: str
    percentage: Decimal


@dataclass(frozen=True)
class LoadCalculation:
    gross_kg: Decimal
    second_weight_kg: Decimal
    unloaded_kg: Decimal
    initial_load_kg: Decimal
    tare_kg: Decimal
    rejected_percentage: Decimal
    rejected_weight_kg: Decimal
    loss_percentage: Decimal
    loss_weight_kg: Decimal
    accepted_weight_kg: Decimal
    items: tuple[dict, ...]


def _d(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise DomainError("مقدار عددی واردشده معتبر نیست.") from exc


def calculate_load(
    movement_type: str,
    first_weight_kg: object,
    second_weight_kg: object,
    compositions: Iterable[CompositionInput],
    rejected_percentage: object = 0,
    loss_percentage: object = 0,
) -> LoadCalculation:
    """Calculate a finalized ticket without losing decimal precision.

    For external loads the second weighing may include rejected material still on
    the truck. Percentages describe the *initial* load. For warehouse transfers
    loss/rejection are forbidden and composition percentages total exactly 100.
    """
    gross, second = _d(first_weight_kg), _d(second_weight_kg)
    rejected = _d(rejected_percentage)
    loss_pct = _d(loss_percentage)
    parts = tuple(compositions)
    if gross <= 0 or second <= 0 or gross <= second:
        raise DomainError("وزن اول باید بیشتر از وزن دوم و هر دو بیشتر از صفر باشند.")
    if not parts:
        raise DomainError("حداقل یک نوع قراضه به ترکیب بار اضافه کنید.")
    composition_pct = sum((_d(x.percentage) for x in parts), Decimal(0))
    if any(_d(x.percentage) <= 0 for x in parts):
        raise DomainError("درصد هر قراضه باید بیشتر از صفر باشد.")
    if composition_pct != 100:
        raise DomainError("مجموع درصد ترکیب قراضه‌ها باید دقیقاً ۱۰۰٪ باشد.")
    if rejected < 0 or loss_pct < 0 or rejected + loss_pct >= 100:
        raise DomainError("مجموع درصد افت و برگشتی باید کمتر از ۱۰۰٪ باشد.")
    if movement_type in ("warehouse_to_production", "warehouse_transfer"):
        if rejected != 0 or loss_pct != 0:
            raise DomainError("در انتقال انبار به تولید، افت و برگشتی نداریم و مجموع ترکیب باید ۱۰۰٪ باشد.")
        initial = unloaded = gross - second
        tare = second
        rejected_weight = Decimal(0)
    else:
        if rejected >= 100:
            raise DomainError("درصد برگشتی باید کمتر از ۱۰۰ باشد.")
        unloaded = gross - second
        initial = unloaded / (Decimal(1) - rejected / Decimal(100))
        rejected_weight = initial * rejected / Decimal(100)
        tare = second - rejected_weight
        if tare < 0:
            raise DomainError("درصد برگشتی با وزن‌های ثبت‌شده سازگار نیست.")
    accepted_weight = initial * (Decimal(100) - rejected - loss_pct) / Decimal(100)
    rows = tuple({
        "scrap_type_id": x.scrap_type_id,
        "scrap_name": x.scrap_name,
        "percentage": _d(x.percentage).quantize(PERCENT, ROUND_HALF_UP),
        "weight_kg": (accepted_weight * _d(x.percentage) / Decimal(100)).quantize(KG, ROUND_HALF_UP),
    } for x in parts)
    loss_weight = initial * loss_pct / Decimal(100)
    return LoadCalculation(
        gross.quantize(KG), second.quantize(KG), unloaded.quantize(KG),
        initial.quantize(KG, ROUND_HALF_UP), tare.quantize(KG, ROUND_HALF_UP),
        rejected.quantize(PERCENT), rejected_weight.quantize(KG, ROUND_HALF_UP),
        loss_pct.quantize(PERCENT), loss_weight.quantize(KG, ROUND_HALF_UP),
        accepted_weight.quantize(KG, ROUND_HALF_UP), rows,
    )


def calculate_weighed_load(first_weight_kg, second_weight_kg):
    """Finalize physical weighing without inventing a quality assessment or items."""
    gross, tare = _d(first_weight_kg), _d(second_weight_kg)
    if gross <= tare or tare <= 0:
        raise DomainError("وزن پر باید بیشتر از وزن خالی و هر دو مثبت باشند.")
    gross, tare = gross.quantize(KG), tare.quantize(KG)
    net = gross - tare
    zero = Decimal(0)
    return LoadCalculation(gross, tare, net, net, tare, zero, zero, zero, zero, net, ())
