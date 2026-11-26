# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The shipped UN/ECE Recommendation 20 unit table.

One table for both directions: sending writes ``code`` and the readable
Finnish ``name`` on every invoice row, receiving reads a row's unit through
``normalise_incoming``. Standard units need no configuration; the ERP field
``uom.uom.rec20_code`` (filled from this table at install) is what the
Administrator overrides.

Pure Python, no ERP import (receiving ADR-0004).
"""

import re
from collections import namedtuple

Unit = namedtuple("Unit", "xmlid unit_names code name aliases")
Unit.__doc__ = """A shipped unit.

xmlid       external id of the standard unit, or None
unit_names  lower-case unit names that identify the unit when it has no
            external id (or the id is missing on a version)
code        UN/ECE Rec 20 code
name        readable Finnish name written next to the code
aliases     lower-case free-text codes read as this unit when receiving
"""

# data-model.md §Unit codes, shipped table. Minute and kWh also carry the
# external id the 19.0 standard units have; it is skipped where missing.
TABLE = (
    Unit(
        "uom.product_uom_unit", (), "C62", "kpl",
        ("kpl", "kp", "pcs", "pc", "st", "ea", "each", "kappale", "h87"),
    ),
    Unit("uom.product_uom_dozen", (), "DZN", "tusina", ()),
    Unit("uom.product_uom_hour", (), "HUR", "h", ("h", "hr", "hour", "tunti", "tuntia")),
    Unit("uom.product_uom_day", (), "DAY", "pv", ("pv", "vrk", "day", "päivä")),
    Unit(None, ("kk", "month"), "MON", "kk", ("kk", "kuukausi", "month")),
    Unit(None, ("vk", "week"), "WEE", "vk", ("vk", "viikko")),
    Unit(None, ("v", "year"), "ANN", "v", ("v", "vuosi")),
    Unit("uom.product_uom_minute", ("min", "minute"), "MIN", "min", ("min",)),
    Unit("uom.product_uom_kgm", (), "KGM", "kg", ("kg",)),
    Unit("uom.product_uom_gram", (), "GRM", "g", ("g",)),
    Unit("uom.product_uom_ton", (), "TNE", "t", ("tn", "tonni")),
    Unit("uom.product_uom_meter", (), "MTR", "m", ("m",)),
    Unit("uom.product_uom_km", (), "KMT", "km", ("km",)),
    Unit("uom.product_uom_cm", (), "CMT", "cm", ()),
    Unit("uom.product_uom_millimeter", (), "MMT", "mm", ()),
    Unit("uom.product_uom_square_meter", (), "MTK", "m2", ("m2", "m²")),
    Unit("uom.product_uom_cubic_meter", (), "MTQ", "m3", ("m3", "m³")),
    Unit("uom.product_uom_litre", (), "LTR", "l", ("l", "ltr", "litra")),
    Unit("uom.product_uom_kwh", ("kwh",), "KWH", "kWh", ("kwh",)),
    Unit(None, ("mwh",), "MWH", "MWh", ("mwh",)),
)

_REC20_RE = re.compile(r"^[A-Z0-9]{2,3}$")
_NAMES = {row.code: row.name for row in TABLE}
_ALIASES = {alias: row.code for row in TABLE for alias in row.aliases}
# Codes of the Rec 20 list read as a shipped unit ("H87 read as the same unit").
_SAME_UNIT = {"H87": "C62"}


def is_valid_rec20(code):
    """True for 2-3 upper-case letters or digits."""
    if not isinstance(code, str):
        return False
    return bool(_REC20_RE.match(code))


def readable_name(code):
    """Readable Finnish name of a shipped code (``C62`` -> ``kpl``), else None."""
    if not code:
        return None
    return _NAMES.get(code)


def normalise_incoming(code_un, code_free):
    """Pick the Rec 20 code of an incoming quantity element.

    A valid ``QuantityUnitCodeUN`` wins; else an alias of the free
    ``QuantityUnitCode``; else a free code that is itself a valid code;
    else None (never a guessed unit - AC-51).
    """
    un = (code_un or "").strip()
    if is_valid_rec20(un):
        return _SAME_UNIT.get(un, un)
    free = (code_free or "").strip()
    if not free:
        return None
    alias = _ALIASES.get(free.lower())
    if alias:
        return alias
    if is_valid_rec20(free):
        return free
    return None
