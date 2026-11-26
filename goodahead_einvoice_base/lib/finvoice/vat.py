# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""VAT categories, reasons and their defaults (finvoice content ADR-0002 –
ADR-0005).

    category, origin = effective_category(set_category, rate, "FI", formulas)
    problems = check_tax(rate, category, code, reason)   # [] when consistent
    lines = reason_lines(reason, category)               # VatFreeText texts

The set category is the ERP's own e-invoicing category of the tax; when none
is set, a rate above zero is standard rate and a 0 % tax takes the one
category its VAT report lines name (``REPORT_CATEGORIES``), never a guess.
An empty VAT reason means the category's default wording (two lines,
Finnish then English, each one ``VatFreeText``).

Pure Python, no ERP import (receiving ADR-0004).
"""

import re

#: UNCL 5305 categories this feature knows: standard, zero rated, exempt,
#: reverse charge, intra-EU supply, export, outside scope.
CATEGORIES = ("S", "Z", "E", "AE", "K", "G", "O")
NO_VAT_CATEGORIES = ("Z", "E", "AE", "K", "G", "O")
STANDARD = "S"

#: The default wording per no-VAT category (spec §8):
#: (Finnish line, English line), each one VatFreeText of at most 70.
DEFAULT_REASONS = {
    "AE": ("Käännetty verovelvollisuus", "Reverse charge"),
    "K": ("Yhteisömyynti, AVL 72 a §", "Intra-Community supply, VAT Directive art. 138"),
    "G": ("Vienti EU:n ulkopuolelle, AVL 70 §", "Export outside the EU"),
    "E": ("Arvonlisäverosta vapautettu myynti", "VAT exempt supply"),
    "Z": ("Nollaverokannan myynti", "Zero-rated supply"),
    "O": ("Ei arvonlisäveron alaista myyntiä", "Outside the scope of VAT"),
}

#: The one standard exemption reason code of a category; E and Z have none
#: (plain exempt has only article-specific codes).
DEFAULT_EXEMPTION_CODES = {
    "AE": "VATEX-EU-AE",
    "K": "VATEX-EU-IC",
    "G": "VATEX-EU-G",
    "O": "VATEX-EU-O",
}

#: Article codes of the VAT directive that also belong to a category with a
#: standard code: article 146 is the export exemption (a localisation ships
#: its export taxes with VATEX-EU-146-1E), so it fits G as well as E.
ARTICLE_CODES = {"G": ("VATEX-EU-146",)}

#: VAT report country -> expression formula (sign stripped) -> category
#:. Every other formula, the zero-rated turnover ``fi_304``
#: included, names none.
REPORT_CATEGORIES = {
    "FI": {"fi_311": "K", "fi_312": "AE", "fi_319": "AE"},
}

#: A VAT reason: at most three lines of 70 characters (Finvoice VatFreeText).
REASON_LINES_MAX = 3
REASON_LINE_MAX = 70
#: Finvoice VatExemptionReasonCode (VatExReasonCodeType): 1-20 letters,
#: digits or hyphens. The ERP lists longer national codes (review F3).
EXEMPTION_CODE_MAX = 20
_EXEMPTION_CODE = re.compile(rf"[A-Za-z0-9\-]{{1,{EXEMPTION_CODE_MAX}}}")

ORIGIN_SET = "set"
ORIGIN_DEFAULT = "default"
ORIGIN_UNCLASSIFIED = "unclassified"


def default_category(rate, report_country, formulas):
    """``"S"`` for a rate above zero; else the single category the report
    ``formulas`` name for ``report_country``; ``None`` when none or several."""
    if (rate or 0) > 0:
        return STANDARD
    table = REPORT_CATEGORIES.get((report_country or "").upper())
    if not table:
        return None
    named = {table[f] for f in (_strip_sign(f) for f in formulas or ()) if f in table}
    if len(named) != 1:
        return None
    return named.pop()


def effective_category(set_category, rate, report_country, formulas):
    """``(category | None, origin)``: the set category wins, else the default."""
    if set_category:
        return set_category, ORIGIN_SET
    category = default_category(rate, report_country, formulas)
    if category:
        return category, ORIGIN_DEFAULT
    return None, ORIGIN_UNCLASSIFIED


def code_fits_category(code, category):
    """Whether exemption reason ``code`` belongs to ``category``.

    AE, K, G and O each take their one standard code; E takes every other
    code (the directive's article codes and the national ones); S and Z take
    none. No code always fits; a category outside the seven is not checked.
    """
    if not code:
        return True
    if category not in CATEGORIES:
        return True
    if code.startswith(ARTICLE_CODES.get(category, ())):
        return True
    if category in DEFAULT_EXEMPTION_CODES:
        return code == DEFAULT_EXEMPTION_CODES[category]
    if category == "E":
        return code not in DEFAULT_EXEMPTION_CODES.values()
    return False


def check_tax(rate, category, code, reason):
    """The AC-06 problems of a sales tax's settings; ``[]`` when consistent."""
    problems = []
    positive = (rate or 0) > 0
    if positive and category in NO_VAT_CATEGORIES:
        problems.append(
            f"A tax with a rate above zero cannot have the no-VAT category {category}: "
            "use the standard-rate category S or clear the category."
        )
    if not positive and category == STANDARD:
        problems.append(
            "A 0 % tax cannot have the standard-rate category S: choose the category "
            "that says why no VAT is charged, or clear the category."
        )
    if not code_fits_category(code, category):
        problems.append(
            f"The exemption reason code {code} does not belong to the VAT category "
            f"{category or '(none)'}."
        )
    if not exemption_code_fits_schema(code):
        problems.append(
            f"The exemption reason code {code} does not fit the e-invoice: at most "
            f"{EXEMPTION_CODE_MAX} letters, digits or hyphens."
        )
    if not reason_fits(reason):
        problems.append(
            f"The VAT reason is too long: {REASON_LINES_MAX} lines of "
            f"{REASON_LINE_MAX} characters fit."
        )
    return problems


def reason_fits(reason):
    """Whether ``reason`` fits three lines of 70 characters."""
    lines = (reason or "").splitlines()
    if len(lines) > REASON_LINES_MAX:
        return False
    return all(len(line.strip()) <= REASON_LINE_MAX for line in lines)


def exemption_code_fits_schema(code):
    """Whether ``code`` is a valid Finvoice exemption reason code; no code fits."""
    return not code or bool(_EXEMPTION_CODE.fullmatch(code))


def sendable_vat(lines, code, category):
    """``{reason_lines, exemption_code, left_out}`` a message can carry
    (review F3, AC-25): the lines of at most 70 characters, at most three of
    them, else the category's default wording when none is left; the code
    when the schema takes it. ``left_out`` names what was dropped
    (``"reason_lines"``, ``"exemption_code"``) for a readiness warning. A
    tax the AC-06 check covers never loses anything here; one it skips
    (purchase taxes, install mode) still sends a valid message."""
    left_out = []
    kept = [line for line in lines or () if len(line) <= REASON_LINE_MAX]
    if len(kept) < len(lines or ()) or len(kept) > REASON_LINES_MAX:
        left_out.append("reason_lines")
    kept = kept[:REASON_LINES_MAX]
    if not kept and lines:
        kept = reason_lines(None, category)
    if not exemption_code_fits_schema(code):
        left_out.append("exemption_code")
        code = None
    return {"reason_lines": kept, "exemption_code": code or None, "left_out": left_out}


def reason_lines(reason, category):
    """The VatFreeText texts of an entry: the set reason's non-empty lines,
    else the category's default lines; ``[]`` for S or no category."""
    if not category or category == STANDARD:
        return []
    lines = [line.strip() for line in (reason or "").splitlines() if line.strip()]
    if lines:
        return lines
    return list(DEFAULT_REASONS.get(category, ()))


def default_reason_text(category):
    """The category's default wording as one text (the reason's placeholder)."""
    return "\n".join(DEFAULT_REASONS.get(category or "", ()))


def _strip_sign(formula):
    return (formula or "").strip().lstrip("+-").strip()


# ---------------------------------------------------------------------------
# Line resolution and the VAT breakdown
# ---------------------------------------------------------------------------

NO_PERCENTAGE_TAX = "no_percentage_tax"
MIXED_CATEGORIES = "mixed_categories"
TIED_CATEGORIES = "tied_categories"
SHARED_REASON = "shared_reason"


def resolve_line(taxes):
    """``{rate, category, warning}`` of a line from its taxes
    ``[{tax_id, rate, category, percent}]``.

    The rate is the sum of the percentage taxes (as the breakdown always
    grouped). The category is the one of the tax with the highest rate;
    ``None`` on a tie of different categories at that rate or without any
    percentage tax. ``warning`` names the case readiness reports.
    """
    percentage = [tax for tax in taxes or () if tax.get("percent")]
    rate = sum((tax.get("rate") or 0.0) for tax in percentage)
    if not percentage:
        return {"rate": rate or 0.0, "category": None, "warning": NO_PERCENTAGE_TAX}
    highest = max(tax.get("rate") or 0.0 for tax in percentage)
    top = {tax.get("category") for tax in percentage if (tax.get("rate") or 0.0) == highest}
    if len(top) > 1:
        return {"rate": rate, "category": None, "warning": TIED_CATEGORIES}
    category = top.pop()
    mixed = len({tax.get("category") for tax in percentage}) > 1
    return {"rate": rate, "category": category, "warning": MIXED_CATEGORIES if mixed else None}


def group_breakdown(lines, tax_amounts, reasons):
    """The VAT breakdown: one entry per ``(rate, category)`` of the lines.

    ``lines`` are ``{untaxed, taxes}`` (``taxes`` as for ``resolve_line``);
    ``tax_amounts`` maps a percentage tax to its VAT on the invoice (the
    invoice's tax lines); ``reasons`` maps a tax to its ``{reason_lines,
    exemption_code}``. Each entry is ``{percent, category, base, amount,
    reason_lines, exemption_code, tax_ids, warning}``: the base is the sum of
    its lines; a tax's VAT is shared over the entries its lines are in by
    base, so the amounts add up to the invoice's VAT; an entry with several
    taxes of its category but different reasons takes those of the tax with
    the largest base on the invoice, with ``warning`` ``shared_reason``.
    """
    entries = {}
    tax_bases = {}  # tax -> {entry key: base}
    tax_totals = {}  # tax -> base over the whole invoice
    for line in lines:
        resolved = resolve_line(line.get("taxes"))
        key = (resolved["rate"], resolved["category"])
        entry = entries.setdefault(key, {
            "percent": resolved["rate"], "category": resolved["category"], "base": 0.0,
            "amount": 0.0, "_taxes": {},
        })
        untaxed = line.get("untaxed") or 0.0
        entry["base"] += untaxed
        for tax in line.get("taxes") or ():
            if not tax.get("percent"):
                continue
            tax_id = tax["tax_id"]
            entry["_taxes"][tax_id] = tax
            per_entry = tax_bases.setdefault(tax_id, {})
            per_entry[key] = per_entry.get(key, 0.0) + untaxed
            tax_totals[tax_id] = tax_totals.get(tax_id, 0.0) + untaxed
    for tax_id, amount in (tax_amounts or {}).items():
        _share(entries, tax_bases.get(tax_id), amount)
    result = []
    for entry in entries.values():
        taxes = entry.pop("_taxes")
        entry["base"] = round(entry["base"], 2)
        entry["amount"] = round(entry["amount"], 2)
        entry["tax_ids"] = sorted(taxes)
        entry.update(_entry_reason(entry["category"], taxes, tax_totals, reasons or {}))
        result.append(entry)
    return result


def _share(entries, bases, amount):
    """Add ``amount`` to the entries by ``bases``; the last takes the cents left."""
    if not bases:
        return
    total = sum(bases.values())
    keys = list(bases)
    given = 0.0
    for key in keys[:-1]:
        part = round(amount * bases[key] / total, 2) if total else 0.0
        entries[key]["amount"] += part
        given += part
    entries[keys[-1]]["amount"] += round(amount - given, 2)


def _entry_reason(category, taxes, tax_totals, reasons):
    empty = {"reason_lines": [], "exemption_code": None, "warning": None}
    if not category or category == STANDARD:
        return empty
    own = [tax_id for tax_id, tax in taxes.items() if tax.get("category") == category]
    if not own:
        return empty
    chosen = max(own, key=lambda tax_id: (tax_totals.get(tax_id, 0.0), -tax_id))
    reason = reasons.get(chosen) or {}
    variants = {
        (tuple((reasons.get(tax_id) or {}).get("reason_lines") or ()),
         (reasons.get(tax_id) or {}).get("exemption_code"))
        for tax_id in own
    }
    return {
        "reason_lines": list(reason.get("reason_lines") or []),
        "exemption_code": reason.get("exemption_code"),
        "warning": SHARED_REASON if len(variants) > 1 else None,
    }
