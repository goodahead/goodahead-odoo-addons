# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Readiness rules: is an invoice ready to send, and what is missing.


Pure Python, no ERP import and no I/O (receiving ADR-0004, AC-20): the ERP
model builds plain dicts from stored data (invoice contact first, commercial
customer as fallback per field) and calls ``evaluate``.

    result = evaluate("einvoice", recipient, sender,
                      sending_enabled=True, lines=lines, due_date=date)
    result["ready"]   # False when any item blocks
    result["items"]   # [{key, severity, party, fix_at, message}]

Messages are English; the ERP layer translates them by ``key``.
"""

CHANNELS = ("einvoice", "letter", "email")

BLOCK = "block"
WARN = "warn"

_POSTAL_FIELDS = ("street", "zip", "city", "country_code")


def _item(key, severity, party, fix_at, message):
    return {
        "key": key,
        "severity": severity,
        "party": party,
        "fix_at": fix_at,
        "message": message,
    }


def _has(values, field):
    value = values.get(field)
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _missing(values, fields):
    return [field for field in fields if not _has(values, field)]


def _recipient_items(channel, recipient):
    items = []
    if channel == "einvoice":
        if not _has(recipient, "edicode"):
            items.append(_item(
                "recipient_edicode", BLOCK, "recipient", "fix_it",
                "The customer's e-invoice address is missing.",
            ))
        if not _has(recipient, "operator_identifier"):
            items.append(_item(
                "recipient_operator", BLOCK, "recipient", "fix_it",
                "The customer's e-invoice operator is missing.",
            ))
        elif not recipient.get("operator_listed"):
            items.append(_item(
                "recipient_operator", BLOCK, "recipient", "fix_it",
                f"The customer's e-invoice operator {recipient['operator_identifier']} "
                "is not on the operator list.",
            ))
    if channel in ("einvoice", "letter") and not _has(recipient, "name"):
        items.append(_item(
            "recipient_name", BLOCK, "recipient", "contact",
            "The customer's name is missing.",
        ))
    missing_postal = _missing(recipient, _POSTAL_FIELDS)
    if channel in ("einvoice", "letter") and missing_postal:
        items.append(_item(
            "recipient_postal_address",
            BLOCK if channel == "letter" else WARN,
            "recipient", "contact",
            "The customer's postal address is incomplete (missing: "
            f"{', '.join(missing_postal)}).",
        ))
    if channel == "einvoice" and not (_has(recipient, "business_id") or _has(recipient, "vat")):
        items.append(_item(
            "recipient_business_id", WARN, "recipient", "contact",
            "The customer has no Business ID or VAT code: APIX accepts the e-invoice but "
            "notes \"HUOM: Ostajan Y-tunnus/ALV-tunniste puuttuu\". Add the customer's "
            "Business ID or VAT number on the customer, or find it with Look up e-invoice "
            "address.",
        ))
    if channel == "email" and not _has(recipient, "email"):
        items.append(_item(
            "recipient_email", BLOCK, "recipient", "contact",
            "The customer's email address is missing.",
        ))
    return items


def _sender_items(channel, sender):
    if channel == "email":
        return []
    items = []
    if not _has(sender, "name"):
        items.append(_item(
            "sender_name", BLOCK, "sender", "company",
            "Your company's name is missing.",
        ))
    if channel != "einvoice":
        return items
    if not (_has(sender, "business_id") or _has(sender, "edicode")):
        items.append(_item(
            "sender_business_id", BLOCK, "sender", "company",
            "Your company's Business ID is missing.",
        ))
    missing_bank = _missing(sender, ("iban", "bic"))
    if missing_bank:
        items.append(_item(
            "sender_bank_account", BLOCK, "sender", "company",
            "The IBAN and BIC of your company's payment account are needed "
            f"(missing: {', '.join(f.upper() for f in missing_bank)}).",
        ))
    return items


def _invoice_items(channel, due_date, lines):
    items = []
    if channel == "einvoice" and not due_date:
        items.append(_item(
            "due_date", BLOCK, "sender", "contact",
            "The invoice has no due date.",
        ))
    if channel == "email":
        return items
    without_code = []
    for line in lines or ():
        name = (line.get("unit_name") or "").strip()
        if _has(line, "rec20_code") or not name or name in without_code:
            continue
        without_code.append(name)
    if without_code:
        items.append(_item(
            "units_without_code", WARN, "line", "unit_codes",
            "These units go out without a unit code: "
            f"{', '.join(without_code)}.",
        ))
    return items


def evaluate(
    channel,
    recipient,
    sender,
    *,
    sending_enabled,
    lines,
    due_date=None,
    invoice_level=True,
    vat_lines=None,
    vat_entries=None,
    references=None,
    delivery=None,
    buyer_vat_present=True,
    tax_names=None,
):
    """Evaluate readiness for one channel (``einvoice``, ``letter``, ``email``).

    ``invoice_level=False`` evaluates a contact on its own (no invoice): the
    due date and unit items are skipped.

    Finvoice content (contract §8, every item a warning, ``ready`` never
    changed): ``vat_lines`` ``[{line, name, rate, category, warning, taxes:
    [{tax_id, name, category, percent}]}]``, ``vat_entries`` (``vat.
    group_breakdown`` entries), ``references`` (``{order_number_problem,
    seller_reference_problem, seller_reference_withheld}``), ``delivery``
    (``{state, missing}``), ``buyer_vat_present``; ``tax_names`` names taxes
    of an entry that no line lists.
    """
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel {channel!r}")
    if channel != "email" and not sending_enabled:
        # AC-28: while the company's sending is off nothing else is shown.
        item = _item(
            "sending_off", BLOCK, "company", None,
            "Sending through APIX is not switched on for this company.",
        )
        return {"ready": False, "items": [item]}
    items = _recipient_items(channel, recipient)
    items += _sender_items(channel, sender)
    if invoice_level:
        items += _invoice_items(channel, due_date, lines)
    if invoice_level and channel != "email":
        items += _content_items(channel, vat_lines, vat_entries, references, delivery,
                                buyer_vat_present, tax_names)
    items = _one_buyer_id_warning(items)
    ready =not any(item["severity"] == BLOCK for item in items)
    return {"ready": ready, "items": items}


def _one_buyer_id_warning(items):
    """One warning for a buyer without an identifier: when the invoice needs
    the buyer's VAT number (``buyer_vat_missing``), that VAT number also
    clears ``recipient_business_id``, so only the stricter one is shown."""
    keys = {item["key"] for item in items}
    if "buyer_vat_missing" not in keys:
        return items
    return [item for item in items if item["key"] != "recipient_business_id"]


# ---------------------------------------------------------------------------
# Finvoice content (finvoice content T14; contract §8): every item a warning
# ---------------------------------------------------------------------------

#: VAT categories that require the buyer's VAT number (Finnish invoicing rules).
BUYER_VAT_CATEGORIES = ("AE", "K")
REFERENCE_MAX = 70
#: What a delivery address lacks, in the words the Accountant sees (review F8).
DELIVERY_LABELS = {"name": "name", "street": "street", "zip": "post code", "city": "town"}


def _content_items(channel, vat_lines, vat_entries, references, delivery, buyer_vat_present,
                   tax_names):
    """The finvoice content warnings. References go for both APIX channels
    (the letter is the same message); VAT and delivery for e-invoices."""
    items = _reference_items(references or {})
    if channel != "einvoice":
        return items
    names = dict(tax_names or {})
    for line in vat_lines or ():
        for tax in line.get("taxes") or ():
            names.setdefault(tax.get("tax_id"), tax.get("name"))
    items = _vat_items(vat_lines or (), vat_entries or (), names) + items
    if not buyer_vat_present and _needs_buyer_vat(vat_lines or (), vat_entries or ()):
        items.append(_item(
            "buyer_vat_missing", WARN, "recipient", "contact",
            "The Finnish invoicing rules require the customer's VAT number on a "
            "reverse-charge or intra-EU invoice: add it on the invoice contact or "
            "the customer.",
        ))
    delivery = delivery or {}
    if delivery.get("state") == "incomplete":
        missing = [DELIVERY_LABELS.get(key, key) for key in delivery.get("missing") or ()]
        items.append(_item(
            "delivery_address_incomplete", WARN, "recipient", "contact",
            f"The delivery address is left out of the e-invoice: it lacks {', '.join(missing)}.",
        ))
    return items


def _needs_buyer_vat(vat_lines, vat_entries):
    """A reverse-charge or intra-EU entry, or such a tax on any line - a
    line that also carries a higher-rate tax resolves to that tax's
    category and makes no such entry (review F5)."""
    if any(entry.get("category") in BUYER_VAT_CATEGORIES for entry in vat_entries):
        return True
    return any(tax.get("category") in BUYER_VAT_CATEGORIES
               for line in vat_lines for tax in line.get("taxes") or ())


def _vat_items(vat_lines, vat_entries, names):
    items = []
    unclassified = []
    for line in vat_lines:
        for tax in line.get("taxes") or ():
            name = tax.get("name")
            if tax.get("category") or not tax.get("percent", True) or name in unclassified:
                continue
            unclassified.append(name)
    if unclassified:
        items.append(_item(
            "vat_unclassified_tax", WARN, "line", "taxes",
            f"Taxes without a VAT category: {', '.join(unclassified)}. The e-invoice states "
            "none for them; an Administrator sets the category.",
        ))
    texts = {
        "no_percentage_tax": ("vat_line_no_percentage_tax", "invoice",
                              ("{lines} {verb} no percentage tax and {go} without a VAT "
                              "category.")),
        "mixed_categories": ("vat_line_mixed_categories", "taxes",
                             ("{lines} {verb} taxes of different VAT categories and {go} with the "
                             "category of the highest rate.")),
        "tied_categories": ("vat_line_tied_categories", "taxes",
                            ("{lines} {verb} taxes of different VAT categories at the same rate "
                            "and {go} without a VAT category.")),
    }
    for warning, (key, fix_at, template) in texts.items():
        lines = [line.get("line") or line.get("name") for line in vat_lines
                 if line.get("warning") == warning]
        if not lines:
            continue
        several = len(lines) > 1
        items.append(_item(key, WARN, "line", fix_at, template.format(
            lines=", ".join(str(line) for line in lines),
            verb="have" if several else "has", go="go" if several else "goes")))
    items += _left_out_items(vat_lines)
    for entry in vat_entries:
        if entry.get("warning") != "shared_reason":
            continue
        taxes = ", ".join(str(names.get(tax_id, tax_id)) for tax_id in entry.get("tax_ids") or ())
        items.append(_item(
            "vat_shared_reason", WARN, "line", "taxes",
            f"Taxes {taxes} share a VAT breakdown entry but have different VAT reasons: "
            "the reason of the one with the largest base is sent.",
        ))
    return items


LEFT_OUT_LABELS = {
    "exemption_code": "the exemption reason code (the e-invoice takes 20 letters, digits or "
                      "hyphens)",
    "reason_lines": "VAT reason lines beyond 3 lines of 70 characters",
}


def _left_out_items(vat_lines):
    """review F3: one warning per tax whose settings the e-invoice cannot
    hold (a tax the consistency check skips); the message still goes."""
    seen = {}
    for line in vat_lines:
        for tax in line.get("taxes") or ():
            if tax.get("left_out") and tax.get("name") not in seen:
                seen[tax.get("name")] = tax["left_out"]
    items = []
    for name, left_out in seen.items():
        what = " and ".join(LEFT_OUT_LABELS.get(key, key) for key in left_out)
        items.append(_item(
            "vat_settings_left_out", WARN, "line", "taxes",
            f"Tax {name}: {what} left out of the e-invoice. An Administrator corrects the "
            "tax's VAT settings.",
        ))
    return items


def _reference_items(references):
    items = []
    if references.get("order_number_problem") == "too_long":
        items.append(_item(
            "order_number_too_long", WARN, "recipient", "invoice",
            "The customer reference is left out of the e-invoice: an order number fits "
            f"{REFERENCE_MAX} characters.",
        ))
    if references.get("seller_reference_problem") == "too_long":
        items.append(_item(
            "seller_reference_too_long", WARN, "sender", "invoice",
            "The source document is left out of the e-invoice: our order number fits "
            f"{REFERENCE_MAX} characters.",
        ))
    if references.get("seller_reference_withheld"):
        items.append(_item(
            "seller_reference_withheld", WARN, "sender", "invoice",
            "Our order number would read as a payment reference and is left out: add a "
            "payment reference to the invoice to send it.",
        ))
    return items
