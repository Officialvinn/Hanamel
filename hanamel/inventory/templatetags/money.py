"""Currency formatting for the yard's shillings.

Change CURRENCY below if you'd rather show "KES" or "KSH" — it's used
everywhere, so one edit changes the whole app.
"""
from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()

CURRENCY = "KSh"
BLANK = "—"


def _to_decimal(value):
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


@register.filter
def amount(value, places=2):
    """1234567.5 -> '1,234,567.50'. Grouped, no currency symbol."""
    d = _to_decimal(value)
    if d is None:
        return BLANK
    try:
        places = int(places)
    except (TypeError, ValueError):
        places = 2
    return f"{d:,.{places}f}"


@register.filter
def money(value, places=2):
    """1234567.5 -> 'KSh 1,234,567.50'."""
    d = _to_decimal(value)
    if d is None:
        return BLANK
    return f"{CURRENCY} {amount(d, places)}"