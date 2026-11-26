# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Row arithmetic shared by sending and the import (finvoice content ADR-0008).

    discount = discount_row(quantity, gross, base_quantity, percent, untaxed)
    choice = import_discount(quantity, price, base_quantity, percent,
                             discount_amount, amount, digits=2)
    texts = text_row_texts(note)

The row amount always wins. Sending states the gross price (before discount,
VAT excluded), the line's own percentage and a discount amount that makes
``quantity × gross ÷ base quantity − discount`` equal the line's own amount to
the cent; a surcharge or a gross price that cannot be stated goes at net price
(``None``). The import keeps a discounted row's stated amount: the stated
percentage when it reproduces it at the ERP's discount precision, else one
worked out from the amounts, else the net price (``None``).

Amounts are ``Decimal`` rounded half up. Pure Python, no ERP import.
"""

from decimal import ROUND_HALF_UP, Decimal

#: Finvoice RowFreeText length.
TEXT_MAX = 512

_CENT = Decimal("0.01")
_PRICE_STEP = Decimal("0.00001")
_HUNDRED = Decimal(100)


def discount_row(quantity, gross_price, base_quantity, percent, untaxed):
    """``{gross_price, percent, amount}`` of a discounted row, or ``None``
    (net price, as today).

    ``None`` when ``percent <= 0`` (no discount or a surcharge), when the
    quantity is zero, when ``gross_price`` is not statable in five decimals,
    or when the stated gross row would fall below the line's amount.
    """
    percent = _decimal(percent)
    quantity = _decimal(quantity)
    gross = _decimal(gross_price)
    if not percent or percent <= 0 or not quantity or gross is None or gross <= 0:
        return None
    if gross.quantize(_PRICE_STEP, rounding=ROUND_HALF_UP) != gross:
        return None
    untaxed = _cents(_decimal(untaxed) or Decimal(0))
    amount = _gross_amount(quantity, gross, base_quantity) - untaxed
    if amount < 0:
        return None
    return {"gross_price": gross, "percent": percent, "amount": amount}


def reproduces(quantity, price, base_quantity, percent, amount, *, digits):
    """Whether ``price`` less ``percent`` (rounded to ``digits``) makes
    ``amount`` to the cent - the import's check at the ERP's precision."""
    quantity, price, amount = _decimal(quantity), _decimal(price), _decimal(amount)
    if quantity is None or price is None or amount is None:
        return False
    rate = _round(_decimal(percent) or Decimal(0), digits)
    gross = quantity * price / _base(base_quantity)
    return _cents(gross * (1 - rate / _HUNDRED)) == _cents(amount)


def import_discount(quantity, price, base_quantity, percent, discount_amount, amount, *,
                    digits):
    """``{price, percent}`` that make the stated row ``amount``, or ``None``.

    The stated ``percent`` when it reproduces the amount, else the percentage
    worked out from the amounts (rounded to ``digits``) when that does; a row
    with only a discount amount has its amount derived from it.
    """
    quantity, price = _decimal(quantity), _decimal(price)
    if not quantity or price is None:
        return None
    gross = quantity * price / _base(base_quantity)
    amount = _decimal(amount)
    if amount is None:
        stated_discount = _decimal(discount_amount)
        if stated_discount is None:
            return None
        amount = _cents(gross) - stated_discount
    candidates = []
    if _decimal(percent) is not None:
        candidates.append(_round(_decimal(percent), digits))
    if gross:
        candidates.append(_round((1 - amount / gross) * _HUNDRED, digits))
    for candidate in candidates:
        if candidate <= 0 or candidate > _HUNDRED:
            continue
        if reproduces(quantity, price, base_quantity, candidate, amount, digits=digits):
            return {"price": price, "percent": candidate}
    return None


def text_row_texts(text):
    """The note as RowFreeText texts of at most ``TEXT_MAX`` characters,
    split at word boundaries; a word longer than a text is cut, never lost."""
    texts = []
    current = ""
    for word in (text or "").split():
        while len(word) > TEXT_MAX:
            if current:
                texts.append(current)
                current = ""
            texts.append(word[:TEXT_MAX])
            word = word[TEXT_MAX:]
        if not current:
            current = word
            continue
        if len(current) + 1 + len(word) <= TEXT_MAX:
            current = f"{current} {word}"
            continue
        texts.append(current)
        current = word
    if current:
        texts.append(current)
    return texts


def _gross_amount(quantity, gross, base_quantity):
    return _cents(quantity * gross / _base(base_quantity))


def _base(base_quantity):
    value = _decimal(base_quantity)
    return value if value else Decimal(1)


def _decimal(value):
    if value is None or value is False or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def _cents(value):
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _round(value, digits):
    return value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
