"""Decimal contract rules, independent of the ORM and desktop UI."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def number(value, name='weight', minimum=Decimal('0'), maximum=Decimal('99999999.999')):
    if isinstance(value, bool):
        raise ValueError('%s: boolean is not a number' % name)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('%s: invalid number' % name) from None
    if not result.is_finite() or result < minimum or result > maximum:
        raise ValueError('%s: out of range' % name)
    return result


def kg(value):
    return number(value).quantize(Decimal('.001'), rounding=ROUND_HALF_UP)


def composition(rows, loss=0, rejected=0):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError('حداقل یک و حداکثر صد قلم لازم است.')
    ids = [int(r['material_item_id']) for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError('قلم تکراری در ترکیب مجاز نیست.')
    percentages = [number(r['percentage'], 'percentage', Decimal('.000001'), Decimal('100')) for r in rows]
    if abs(sum(percentages) - 100) > Decimal('.0001'):
        raise ValueError('مجموع درصد ترکیب باید ۱۰۰ باشد.')
    loss, rejected = number(loss, 'loss', maximum=Decimal('100')), number(rejected, 'rejected', maximum=Decimal('100'))
    if loss + rejected >= 100:
        raise ValueError('مجموع افت و برگشتی باید کمتر از ۱۰۰ باشد.')
    return percentages, loss, rejected


def calculate(gross, tare, rows, calculator='net', loss=0, rejected=0):
    percentages, loss, rejected = composition(rows, loss, rejected)
    gross, tare = kg(gross), kg(tare)
    if tare <= 0 or gross <= tare:
        raise ValueError('وزن پر باید بیشتر از وزن خالی و هر دو مثبت باشند.')
    net = gross - tare
    original = net
    if calculator == 'scrap_v1':
        original = net / (1 - rejected / 100)
    elif calculator == 'scrap_net':
        pass
    elif calculator != 'net' or loss or rejected:
        raise ValueError('روش وزن خالص از افت و برگشتی پشتیبانی نمی‌کند.')
    accepted = kg(original * (100 - rejected - loss) / 100)
    remainder = accepted
    lines = []
    for index, (row, percentage) in enumerate(zip(rows, percentages)):
        weight = remainder if index == len(rows) - 1 else kg(accepted * percentage / 100)
        remainder -= weight
        lines.append(dict(row, weight_kg=float(weight)))
    return {'net_weight_kg': float(net), 'original_load_weight_kg': float(kg(original)),
            'accepted_weight_kg': float(accepted), 'loss_weight_kg': float(kg(original * loss / 100)),
            'rejected_weight_kg': float(kg(original * rejected / 100)), 'items': lines}
