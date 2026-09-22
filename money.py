"""Exact XMR parsing at the database and blockchain precision boundary."""
from decimal import Decimal, InvalidOperation

ATOMIC = Decimal(10) ** 12
QUANTUM = Decimal('0.000000000001')
MAX_AMOUNT = Decimal('99999999.999999999999')


def parse_amount(value, minimum=QUANTUM):
    if not isinstance(value, (str, Decimal)) or len(str(value)) > 64:
        raise ValueError('Invalid amount.')
    try:
        amount = Decimal(value)
        if not amount.is_finite() or not minimum <= amount <= MAX_AMOUNT:
            raise ValueError('Amount is outside the permitted range.')
        if amount != amount.quantize(QUANTUM):
            raise ValueError('Amounts may have at most twelve decimal places.')
    except InvalidOperation as exc:
        raise ValueError('Invalid amount.') from exc
    return amount


def atomic_units(amount):
    return int(parse_amount(amount) * ATOMIC)
