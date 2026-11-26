# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Install hook: fill the unit codes of the standard units (send AC-48,
ADR-0013; promoted from the feature's staged migration 04).

Standard units go out with their UN/ECE Rec 20 code with no configuration.
Two passes, never over a code already set:

1. by the external id of the standard unit (``units.TABLE``);
2. by name (case-insensitive) for units still without a code - covers units
   the ERP does not ship (kk, vk, v, MWh) that a company created itself.

The same fill runs on upgrade to 19.0.1.4.0
(``migrations/19.0.1.4.0/post-fill-unit-codes.py``). Down: ``rec20_code``
is dropped with the add-on on uninstall; nothing else is written.
"""

from .lib.finvoice import units

# English names of the staged table, read in pass 2 next to the shipped
# table's unit names, readable Finnish names and incoming aliases.
ENGLISH_NAMES = {
    "units": "C62", "unit": "C62",
    "dozens": "DZN", "dozen": "DZN",
    "hours": "HUR", "hour": "HUR",
    "days": "DAY", "day": "DAY",
    "months": "MON", "month": "MON",
    "weeks": "WEE", "week": "WEE",
    "years": "ANN", "year": "ANN",
    "minutes": "MIN", "minute": "MIN", "minuutti": "MIN",
    "liter": "LTR", "litre": "LTR",
}


def names_to_codes():
    """Lower-case unit name -> Rec 20 code for the by-name pass."""
    mapping = dict(ENGLISH_NAMES)
    for row in units.TABLE:
        for name in (*row.aliases, row.name.lower(), *row.unit_names):
            mapping[name] = row.code
    return mapping


def fill_unit_codes(env):
    """Give every unit without a code the shipped one (see module doc)."""
    for row in units.TABLE:
        if not row.xmlid:
            continue
        unit = env.ref(row.xmlid, raise_if_not_found=False)
        if unit and not unit.rec20_code:
            unit.rec20_code = row.code
    by_name = names_to_codes()
    Uom = env["uom.uom"].with_context(active_test=False)
    for unit in Uom.search([("rec20_code", "=", False)]):
        code = by_name.get((unit.name or "").strip().lower())
        if code:
            unit.rec20_code = code


def post_init_hook(env):
    fill_unit_codes(env)
