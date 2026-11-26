# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Generate Peppol BIS Billing 3.0 UBL Invoice/CreditNote XML from extracted data.

🔴 WHY A EUROPEAN E-INVOICING FORMAT FOR A US BUSINESS. The UBL here is never
sent to anybody. It exists only as the input to Odoo's own
`account_edi_ubl_cii` decoder, which is what builds the bill — so the format is
an internal interchange, chosen because Odoo already knows how to read it, not
because the US deployment participates in Peppol. Nothing validates it against Peppol
schematron, and nothing should: a US vendor bill would fail those rules and
would be wrong to pass them.

That distinction decides several things below — most visibly `EndpointID`, which
is omitted rather than filled with a plausible-looking code. See `_add_party`.

🔴 AND IT IS NOT A US MODULE EITHER. The US deployment receives bills from the United
States, from Finland and from China, and the list is open. Nothing here may
depend on a vendor's country being one somebody thought about: a country with no
honest Peppol scheme must IMPORT, not fail.
"""

import logging

from lxml import etree

# 🔴 THESE THREE IMPORTS ARE WHY THIS FILE IS AN ADAPTER AND NOT IN `lib/`
# (ADR-0004: `lib/` imports no ERP; moved here in 19.0.1.8.0). They are the
# EAS table and the endpoint rules that core's own partner constraint is
# enforced with. The alternative — a table of other countries' identifier
# schemes maintained by hand in this repo — is what put a Belgian scheme on a
# Finnish VAT number and failed the first real Finnish bill. Importing is
# safe: the module hard-depends on `account_edi_ubl_cii`, so by the time
# anything imports this file that addon is loaded.
from odoo.addons.account_edi_ubl_cii.models.account_edi_common import EAS_MAPPING
from odoo.addons.account_edi_ubl_cii.models.res_partner import (
    PEPPOL_ENDPOINT_INVALID_CHARS_RE_BY_EAS,
    PEPPOL_ENDPOINT_INVALIDCHARS_RE,
    sanitize_peppol_endpoint,
)

from ..lib.bank_numbers import payee_account_number, payee_institution_id

_logger = logging.getLogger(__name__)

# 🔴 THE LAST RESORT, AND IT IS NOT WHERE THE ANSWER NORMALLY COMES FROM.
#
# This was `DEFAULT_CURRENCY = "USD"`, a literal, and the comment beside it
# argued that USD was right because "this module was written for a European
# business; the US deployment is a US one". Both halves of that are true and the
# conclusion was still wrong: the module is now ONE module on both
# deployments, so a constant here is a constant that is wrong on one of them
# whichever value it holds. On a Finnish installation it denominated an
# unmarked re-import in dollars.
#
# So the fallback is the COMPANY's own currency, passed in by
# `account_move._ai_generate_ubl_from_extracted`, and this literal is what is
# left when there is no company at all — the lib-only callers and
# `generate_peppol_invoice_xml(data)` with one argument, which several tests
# and the F31 byte-identity diff use.
#
# ⚠️ IT IS ALREADY THE SECOND LINE OF DEFENCE, NOT THE FIRST. Since 19.0.2.3.0
# `_validate_extraction` rejects an extraction with no currency outright, so
# nothing reaching here came straight from Claude. What does reach it is the
# re-import path, which replays a stored (possibly hand-edited) extraction
# without re-validating it.
DEFAULT_CURRENCY = "USD"

NS_INVOICE = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
NS_CREDIT_NOTE = "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
NS_CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
NS_CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"

CUSTOMIZATION_ID = (
    "urn:cen.eu:en16931:2017#compliant#"
    "urn:fdc:peppol.eu:2017:poacc:billing:3.0"
)
PROFILE_ID = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"


class UBLGenerationError(Exception):
    """Raised when UBL XML generation fails due to invalid data."""


# Monetary and quantity fields whose sign follows the document's own convention.
# `price_unit` is NOT here: a unit price stays positive on a credit note, and
# negating it would make the line inconsistent with its own total.
_SIGNED_LINE_FIELDS = ("quantity", "line_total", "tax_amount")
_SIGNED_SUBTOTAL_FIELDS = ("taxable_amount", "tax_amount")
_SIGNED_TOTAL_FIELDS = (
    "line_extension",
    "tax_exclusive",
    "tax_inclusive",
    "payable",
)


def _normalise_credit_note_signs(data):
    """Make a credit note's amounts POSITIVE magnitudes.

    🔴 THE SIGN IS CARRIED BY THE DOCUMENT TYPE, ONCE. A UBL `CreditNote` with
    `CreditNoteTypeCode` 381 already means "this reverses what you were
    charged". Negative amounts INSIDE it are a second negation, and Odoo's
    decoder builds an `in_refund` from the result — so a refund that INCREASES
    what we owe the vendor. That is a money bug with no visible symptom at
    import time; it looks like an ordinary credit note until somebody reconciles.

    Vendors genuinely print credit memos both ways — "Credit Memo" with positive
    figures, or an invoice-shaped document with negatives — so the extraction
    legitimately arrives in either convention and the prompt alone cannot be
    relied on to settle it. This normalises instead of trusting.

    ⚠️ IT FLIPS BY THE DOCUMENT'S OWN TOTAL, NOT WITH `abs()` PER FIELD. A blunt
    `abs()` would destroy the relative signs inside a credit note that also
    re-charges something, turning a correction line into another credit. Here
    the whole document is multiplied by -1 only when its payable total is
    negative, which fixes the convention while leaving every internal
    relationship intact. A document already stated as positives is untouched.
    """
    totals = data.get("totals") or {}
    payable = totals.get("payable")
    if payable is None:
        payable = totals.get("tax_inclusive")
    try:
        if payable is None or float(payable) >= 0:
            return data
    except (TypeError, ValueError):
        return data

    def flip(container, keys):
        for key in keys:
            value = container.get(key)
            # A `bool` is left as it is, so `_amount_of` refuses it by name:
            # negated, `True` would become the int -1 and pass as an amount.
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                container[key] = -value

    # Copied rather than mutated: `generate_peppol_invoice_xml` is called on the
    # stored extraction during a re-import, and silently rewriting the JSON a
    # human is editing would be its own surprise.
    data = dict(data)
    data["lines"] = [dict(line) for line in data.get("lines") or []]
    data["tax_subtotals"] = [dict(sub) for sub in data.get("tax_subtotals") or []]
    data["totals"] = dict(totals)

    for line in data["lines"]:
        flip(line, _SIGNED_LINE_FIELDS)
    for subtotal in data["tax_subtotals"]:
        flip(subtotal, _SIGNED_SUBTOTAL_FIELDS)
    flip(data["totals"], _SIGNED_TOTAL_FIELDS)

    _logger.info(
        "AI invoice import: credit note %s was extracted with negative amounts; "
        "normalised to positive magnitudes, since the CreditNote type code "
        "already carries the sign.",
        data.get("invoice_number"),
    )
    return data


def generate_peppol_invoice_xml(data, default_currency=None):
    """Generate Peppol BIS Billing 3.0 UBL Invoice or CreditNote XML.

    Args:
        data: dict from Claude extraction (the "data" key of extract_invoice result)
        default_currency: the ISO name to use when the extraction states none —
            normally the importing company's own currency. See
            `DEFAULT_CURRENCY` for why this stopped being a constant.

    Returns:
        bytes: UTF-8 encoded XML document

    Raises:
        UBLGenerationError: on missing required fields or invalid data
    """
    doc_type = data.get("type", "Invoice")
    is_credit_note = doc_type == "CreditNote"

    if is_credit_note:
        data = _normalise_credit_note_signs(data)

    if is_credit_note:
        root_ns = NS_CREDIT_NOTE
        root_tag = "CreditNote"
        line_tag = "CreditNoteLine"
        qty_tag = "CreditedQuantity"
    else:
        root_ns = NS_INVOICE
        root_tag = "Invoice"
        line_tag = "InvoiceLine"
        qty_tag = "InvoicedQuantity"

    nsmap = {
        None: root_ns,
        "cac": NS_CAC,
        "cbc": NS_CBC,
    }

    root = etree.Element(f"{{{root_ns}}}{root_tag}", nsmap=nsmap)

    _add_text(root, f"{{{NS_CBC}}}CustomizationID", CUSTOMIZATION_ID)
    _add_text(root, f"{{{NS_CBC}}}ProfileID", PROFILE_ID)
    _add_text(root, f"{{{NS_CBC}}}ID", data.get("invoice_number", ""))
    _add_text(root, f"{{{NS_CBC}}}IssueDate", data.get("issue_date", ""))

    if data.get("due_date"):
        _add_text(root, f"{{{NS_CBC}}}DueDate", data["due_date"])

    invoice_type_code = "381" if is_credit_note else "380"
    _add_text(root, f"{{{NS_CBC}}}{root_tag}TypeCode", invoice_type_code)

    if data.get("note"):
        _add_text(root, f"{{{NS_CBC}}}Note", data["note"])

    # `str()` and `.upper()` are not decoration. The value comes out of Claude's
    # JSON, so its type is whatever the model emitted, and core matches on
    # `res.currency.name` EXACTLY — a lower-case "usd" or a stray space matches
    # nothing, and core then silently substitutes the company currency.
    currency = str(data.get("currency") or "").strip().upper()
    if not currency:
        fallback = str(default_currency or "").strip().upper() or DEFAULT_CURRENCY
        # 🔴 This fallback is now the SECOND line, not the first: since
        # 19.0.2.3.0 `_validate_extraction` rejects an extraction with no
        # currency outright. It is still reachable from the re-import path,
        # which replays a stored (possibly hand-edited) extraction without
        # re-validating it — so it stays, and it says so in the log rather than
        # quietly denominating a Finnish or Chinese bill in dollars.
        _logger.warning(
            "AI invoice import: %s named no currency; falling back to %s (%s). "
            "If this document is not in %s the bill will carry the wrong value.",
            data.get("invoice_number"),
            fallback,
            "the importing company's own currency"
            if default_currency
            else "no company was given, so the module's last-resort literal",
            fallback,
        )
        currency = fallback
    _add_text(root, f"{{{NS_CBC}}}DocumentCurrencyCode", currency)

    # AccountingSupplierParty
    seller = data.get("seller", {})
    _add_party(root, "AccountingSupplierParty", seller)

    # AccountingCustomerParty
    buyer = data.get("buyer", {})
    _add_party(root, "AccountingCustomerParty", buyer)

    # PaymentMeans
    # The payee account, whichever vocabulary it arrived in: an IBAN abroad, a
    # routing + account number in the US. `PayeeFinancialAccount/ID` carries the
    # account and `FinancialInstitutionBranch/ID` the institution, which is the
    # BIC or the ABA routing number — the same slot serves both.
    acc_number = payee_account_number(seller)
    institution_id = payee_institution_id(seller)
    if acc_number or data.get("payment_reference"):
        pm = etree.SubElement(root, f"{{{NS_CAC}}}PaymentMeans")
        # 🔴 `.get(key, default)` DOES NOT DEFAULT A NULL, and `payment_means_code` is
        # documented as nullable in the schema — so a perfectly compliant extraction
        # ("the document does not say how it is to be paid") emitted
        # `<cbc:PaymentMeansCode>None</cbc:PaymentMeansCode>`. `dict.get`'s second
        # argument applies when the key is ABSENT, never when it is present and null,
        # and a model that is told it may answer null answers null. `or` is the spelling
        # that covers both. Measured, not read: `tests/test_ubl_generator.py`.
        means_code = data.get("payment_means_code") or "30"
        _add_text(pm, f"{{{NS_CBC}}}PaymentMeansCode", str(means_code))
        if data.get("payment_reference"):
            _add_text(pm, f"{{{NS_CBC}}}PaymentID", data["payment_reference"])
        if acc_number:
            pfa = etree.SubElement(pm, f"{{{NS_CAC}}}PayeeFinancialAccount")
            _add_text(pfa, f"{{{NS_CBC}}}ID", acc_number)
            if institution_id:
                fib = etree.SubElement(pfa, f"{{{NS_CAC}}}FinancialInstitutionBranch")
                _add_text(fib, f"{{{NS_CBC}}}ID", institution_id)

    # NOTE: the invoice note is emitted once, as the document-level cbc:Note
    # above. It must NOT also be repeated in cac:PaymentTerms/cbc:Note — Odoo
    # imports both into the bill narration, so the note showed up twice.

    # TaxTotal
    # `or` rather than a `.get` default, for the third time in this file: a null
    # `tax_subtotals` would otherwise reach `enumerate(None)`.
    tax_subtotals = data.get("tax_subtotals") or []
    totals = data.get("totals") or {}
    tax_inclusive = _amount_of(totals, "tax_inclusive", "totals.tax_inclusive")
    tax_exclusive = _amount_of(totals, "tax_exclusive", "totals.tax_exclusive")
    total_tax = tax_inclusive - tax_exclusive

    tt = etree.SubElement(root, f"{{{NS_CAC}}}TaxTotal")
    _add_amount(tt, f"{{{NS_CBC}}}TaxAmount", total_tax, currency, "the document tax")

    for index, sub in enumerate(tax_subtotals, start=1):
        at = f"tax subtotal {index}"
        ts = etree.SubElement(tt, f"{{{NS_CAC}}}TaxSubtotal")
        _add_amount(ts, f"{{{NS_CBC}}}TaxableAmount",
                    _amount_of(sub, "taxable_amount", f"{at}: taxable_amount"),
                    currency, f"{at}: taxable_amount")
        _add_amount(ts, f"{{{NS_CBC}}}TaxAmount",
                    _amount_of(sub, "tax_amount", f"{at}: tax_amount"),
                    currency, f"{at}: tax_amount")
        tc = etree.SubElement(ts, f"{{{NS_CAC}}}TaxCategory")
        # `or "S"`, not `.get(key, "S")` — see the PaymentMeansCode comment above. A
        # null here reached `_add_text`, which writes an EMPTY element, and an empty
        # tax category id is not something Odoo's decoder can do anything useful with.
        _add_text(tc, f"{{{NS_CBC}}}ID", sub.get("tax_category") or "S")
        # A tax RATE beside a stated tax AMOUNT: absent is defaulted, null is not.
        _add_text(tc, f"{{{NS_CBC}}}Percent",
                  _fmt_decimal(_amount_of(sub, "tax_percent", f"{at}: tax_percent",
                                          default=0)))
        ts_scheme = etree.SubElement(tc, f"{{{NS_CAC}}}TaxScheme")
        _add_text(ts_scheme, f"{{{NS_CBC}}}ID", "VAT")

    # LegalMonetaryTotal
    lmt = etree.SubElement(root, f"{{{NS_CAC}}}LegalMonetaryTotal")
    _add_amount(lmt, f"{{{NS_CBC}}}LineExtensionAmount",
                _amount_of(totals, "line_extension", "totals.line_extension"),
                currency, "totals.line_extension")
    _add_amount(lmt, f"{{{NS_CBC}}}TaxExclusiveAmount", tax_exclusive, currency,
                "totals.tax_exclusive")
    _add_amount(lmt, f"{{{NS_CBC}}}TaxInclusiveAmount", tax_inclusive, currency,
                "totals.tax_inclusive")
    _add_amount(lmt, f"{{{NS_CBC}}}PayableAmount",
                _amount_of(totals, "payable", "totals.payable"),
                currency, "totals.payable")

    # Invoice/CreditNote Lines
    lines = data.get("lines", [])
    if not lines:
        raise UBLGenerationError("No invoice lines to generate")

    for idx, line in enumerate(lines, start=1):
        at = f"line {idx}"
        line_el = etree.SubElement(root, f"{{{NS_CAC}}}{line_tag}")
        _add_text(line_el, f"{{{NS_CBC}}}ID", str(idx))

        qty = _amount_of(line, "quantity", f"{at}: quantity", default=1)
        # ⚠️ `or "C62"`: a null here was NOT silent like the others — `etree.set()`
        # rejects `None` with a TypeError and took the whole import down with a message
        # about argument types. Loud, but about the wrong thing.
        # send T17: a structured source's code nobody could read goes
        # through as written, so the import notes it instead of a silent C62.
        unit_code = line.get("unit_code") or line.get("unit_code_unmapped") or "C62"
        qty_el = etree.SubElement(line_el, f"{{{NS_CBC}}}{qty_tag}")
        qty_el.text = _fmt_decimal(qty, f"{at}: quantity")
        qty_el.set("unitCode", unit_code)

        _add_amount(line_el, f"{{{NS_CBC}}}LineExtensionAmount",
                    _amount_of(line, "line_total", f"{at}: line_total"),
                    currency, f"{at}: line_total")
        _add_row_discount(line_el, line, qty, currency, at)

        # Line-level TaxTotal.
        #
        # 🔴 A NULL LINE TAX OMITS THE ELEMENT; IT NEVER WRITES 0.00
        # (19.0.3.29.0). The document printed one tax total and none per line,
        # and `claude_client._validate_arithmetic` lets that through on purpose.
        # Odoo 19's decoder does not read this element at all: a line's tax
        # comes from `Item/ClassifiedTaxCategory` (its rate) and the document's
        # tax from `TaxTotal/TaxSubtotal` (`account_edi_ubl.py`,
        # `_import_ubl_invoice_line_add_taxes_values` and
        # `_import_ubl_invoice_add_tax_total_values`). So leaving it out loses
        # nothing, while a written zero would be a figure the vendor never
        # stated. An ABSENT key is still a failure, as before.
        if "tax_amount" in line and line["tax_amount"] is None:
            line_tax = None
        else:
            line_tax = _amount_of(line, "tax_amount", f"{at}: tax_amount")
        if line_tax is not None:
            line_tt = etree.SubElement(line_el, f"{{{NS_CAC}}}TaxTotal")
            _add_amount(line_tt, f"{{{NS_CBC}}}TaxAmount", line_tax,
                        currency, f"{at}: tax_amount")

        # Item — combine name + description into Name because Odoo's
        # UBL import reads Description first and ignores Name.
        item = etree.SubElement(line_el, f"{{{NS_CAC}}}Item")

        item_name = line.get("name", "")
        description = line.get("description")
        if description and description != item_name:
            item_name = f"{item_name} - {description}" if item_name else description

        _add_text(item, f"{{{NS_CBC}}}Name", item_name)

        product_code = line.get("product_code")
        if product_code:
            sii = etree.SubElement(item, f"{{{NS_CAC}}}SellersItemIdentification")
            _add_text(sii, f"{{{NS_CBC}}}ID", product_code)

        # ClassifiedTaxCategory on Item
        ctc = etree.SubElement(item, f"{{{NS_CAC}}}ClassifiedTaxCategory")
        _add_text(ctc, f"{{{NS_CBC}}}ID", line.get("tax_category") or "S")
        _add_text(ctc, f"{{{NS_CBC}}}Percent",
                  _fmt_decimal(_amount_of(line, "tax_percent", f"{at}: tax_percent",
                                          default=0)))
        ctc_scheme = etree.SubElement(ctc, f"{{{NS_CAC}}}TaxScheme")
        _add_text(ctc_scheme, f"{{{NS_CBC}}}ID", "VAT")

        # Price. Emit the NET unit price so PriceAmount x quantity ==
        # LineExtensionAmount. line_total already reflects any per-line discount
        # (many wholesale vendors print a list price and a discount on every line). If we emitted
        # the gross price_unit instead, Odoo's UBL import builds the line from the
        # gross price, adds a bogus untaxed "Rounding" line to reconcile it to the
        # declared line total, and taxes the gross amount — wrong VAT and total.
        #
        # 🔴 AND IT IS ALSO WHY A MISREAD QUANTITY IS INVISIBLE TO EVERY TOTAL. Because
        # the unit price emitted here is DERIVED from the line total, a quantity that is
        # wrong by a factor of a thousand produces a unit price that is wrong by a
        # thousand the other way, and every figure that reaches the document sums is
        # unchanged. `claude_client._validate_line_magnitude` is the only check that can
        # see it.
        price = etree.SubElement(line_el, f"{{{NS_CAC}}}Price")
        line_total_f = _amount_of(line, "line_total", f"{at}: line_total")
        if line.get("price_is_gross"):
            # finvoice content AC-22: a discounted row whose gross price and
            # percentage make its stated amount (`stated_document_for_the_bill`)
            # goes with the gross price; the decoder infers the discount from
            # it and the line amount.
            unit_price = _amount_of(line, "price_unit", f"{at}: price_unit")
        elif qty:
            unit_price = line_total_f / qty
        else:
            unit_price = _amount_of(line, "price_unit", f"{at}: price_unit", default=0)
        _add_amount(price, f"{{{NS_CBC}}}PriceAmount", unit_price, currency,
                    f"{at}: price")

    xml_bytes = etree.tostring(
        root,
        xml_declaration=True,
        encoding="UTF-8",
        pretty_print=True,
    )
    return xml_bytes


def _add_row_discount(line_el, line, qty, currency, at):
    """finvoice content AC-22: a row kept with its gross price carries its
    discount as a line allowance (gross row less the stated amount), which
    the ERP's decoder turns into the line's discount percentage."""
    if not line.get("price_is_gross"):
        return
    gross = abs((qty or 0) * _amount_of(line, "price_unit", f"{at}: price_unit"))
    stated = abs(_amount_of(line, "line_total", f"{at}: line_total"))
    allowance = etree.SubElement(line_el, f"{{{NS_CAC}}}AllowanceCharge")
    _add_text(allowance, f"{{{NS_CBC}}}ChargeIndicator", "false")
    _add_amount(allowance, f"{{{NS_CBC}}}Amount", round(gross - stated, 2), currency,
                f"{at}: discount")


def _add_text(parent, tag, text):
    el = etree.SubElement(parent, tag)
    el.text = str(text) if text is not None else ""
    return el


def _add_amount(parent, tag, amount, currency, label="an amount"):
    el = etree.SubElement(parent, tag)
    el.text = _fmt_decimal(amount, label)
    el.set("currencyID", currency)
    return el


# A sentinel, because `None` is the value this module refuses and cannot also be the
# "no default was given" marker.
_NO_DEFAULT = object()


def _amount_of(container, key, label, default=_NO_DEFAULT):
    """A number out of the extraction, or a named `UBLGenerationError`.

    🔴 ABSENT AND NULL ARE THE SAME FAILURE HERE, and both used to be "0.00".
    `container.get(key, 0)` covered the absent case and `_fmt_decimal(None)` covered the
    null one, so an amount Claude could not read became a zero on the bill with nothing
    anywhere to see it. That is the one shape of missing value the whole
    null-rather-than-guess design does not survive, because its justification is that a
    missing value is visible.

    ⚠️ THIS IS THE BACKSTOP, NOT THE GATE. `claude_client._validate_extraction` catches
    these on the extraction path, where the message can be better and the record goes to
    `failed` with the JSON attached. What it cannot cover is the RE-IMPORT path:
    `action_reimport_from_json` replays a stored, hand-edited extraction straight into
    this module without re-validating it, so a human who deletes a figure while fixing
    another one has, until now, been given a silent zero. Hence the check lives in both
    places.

    `default` is for the handful of fields where a documented fallback is honest because
    the value is not money — a unit of measure, a tax RATE beside a stated tax AMOUNT.
    It applies only when the key is absent; an explicit null is still a failure.
    """
    if key not in container:
        if default is not _NO_DEFAULT:
            return float(default)
        raise UBLGenerationError(
            f"{label} is missing from the extraction. An amount the bill is built from "
            f"must not be written as 0.00 — fill it in on the import record and "
            f"re-import."
        )
    value = container[key]
    if value is None:
        raise UBLGenerationError(
            f"{label} is null in the extraction. An amount that could not be read must "
            f"not be written to the bill as 0.00 — fill it in on the import record and "
            f"re-import."
        )
    # `bool` is an `int` in Python, so `float(True)` is 1.0. The validator already
    # refuses it on the extraction path; this is the re-import path, which is not
    # re-validated (CodeRabbit, #388).
    if isinstance(value, bool):
        raise UBLGenerationError(
            f"{label} is {value!r} in the extraction, which is not an amount — fill in "
            f"the figure on the import record and re-import."
        )
    try:
        return float(value)
    except (TypeError, ValueError):
        raise UBLGenerationError(
            f"{label} is {value!r}, which is not a number."
        ) from None


def _fmt_decimal(value, label="an amount"):
    # 🔴 NO MORE `return "0.00"` FOR `None`. See `_amount_of`: that default turned every
    # unreadable figure into a zero that passed validation, passed generation, built a
    # bill and was invisible. Callers that genuinely have a default pass it to
    # `_amount_of`; everything else is a defect and says so here.
    if value is None:
        raise UBLGenerationError(
            f"{label} is null; a missing amount must not be written as 0.00."
        )
    return f"{float(value):.2f}"


# 🔴 THE EAS TABLE IS CORE'S OWN. THERE IS NO HAND-WRITTEN ONE BESIDE IT, AND
# THERE MUST NOT BE.
#
# What stood here was a two-entry dict written by hand, and it said Finland was
# `9925`. `9925` is BELGIAN VAT — core validates it with `[^beBE0-9]`, because a
# Belgian VAT number looks like `BE0123456789`. So the first real Finnish bill,
# `FI00000211`, was rejected at partner creation with "The Peppol endpoint is
# not valid", and the whole import failed. Nobody in this repo is in a position
# to keep a table of forty countries' identifier schemes right, so the question
# is put to core instead.
#
# `EAS_MAPPING[country]` is core's own `{EAS: field}` table, and an entry whose
# field is `'vat'` is core stating that THIS country's Peppol endpoint IS the
# VAT number. That is exactly the claim an `EndpointID/@schemeID` pair makes, so
# those entries — and only those — can be emitted honestly. The order is core's
# order, and the first candidate that validates wins, which is what core's own
# `_compute_peppol_eas` does.
#
# Everything else gets NO `EndpointID` at all:
#
#   * a country core maps to something that is not the VAT — Finland is
#     `{'0216': None}` (the OVT e-invoicing address, which is not derivable from
#     a VAT number and which core itself declines to derive), the Netherlands is
#     KvK/OIN, Hungary an l10n field. We hold a VAT; we do not hold that.
#   * a country core does not map at all — the United States, China, and every
#     country nobody has thought about yet. This is the default, and it is the
#     one that matters: an unanticipated country must import.
#   * a value that fails core's own character/length rules.
#
# Omitting costs one of core's six partner-matching steps; VAT, bank account,
# email, phone and name all still run. It can never fail an import, and it
# removes the way to match the WRONG vendor with confidence.
#
# ⚠️ `0208`, `0009`, `0007` and `EM` carry extra format rules in core's
# `_build_error_peppol_endpoint` beyond the character check below. None of them
# is a `'vat'` entry, so none of them is reachable from here — and
# `test_peppol_endpoint.py` asserts that against core rather than trusting this
# comment.


def peppol_endpoint_is_valid(eas, endpoint):
    """Core's character and length rules for an endpoint, applied here.

    Mirrors the tail of `res.partner._build_error_peppol_endpoint`, which is the
    `@api.constrains` that rejected the Finnish bill. Checking before emitting
    is what turns "the import failed" into "the import has no EndpointID".
    """
    if not endpoint:
        return False
    invalid_chars_re = PEPPOL_ENDPOINT_INVALID_CHARS_RE_BY_EAS.get(
        eas, PEPPOL_ENDPOINT_INVALIDCHARS_RE
    )
    return not invalid_chars_re.search(endpoint) and 1 <= len(endpoint) <= 50


def vat_peppol_eas_codes(country_code):
    """The EAS codes core itself resolves to a partner's VAT, in core's order."""
    return [
        eas
        for eas, field in EAS_MAPPING.get(country_code, {}).items()
        if field == "vat"
    ]


def peppol_endpoint_for_party(country_code, vat):
    """`(eas, endpoint)` for a party, or `(None, None)` — the normal answer.

    `(None, None)` for a US vendor (an EIN is not a Peppol participant
    identifier), for a Chinese one, for a Finnish one (Finland's Peppol address
    is an OVT code, not its VAT number), and for every country core does not map
    to a VAT scheme. Never raises, and never guesses.
    """
    # 🔴 `str(...)` is not decoration. Both of these come straight out of
    # Claude's JSON, where the type is whatever the model emitted — a VAT number
    # printed as bare digits can arrive as an int, and so can a country field.
    # Until this function existed, both values only ever reached `_add_text`,
    # which does its own `str()`; consuming them as strings here is what put a
    # plain `.strip()` on an untrusted type, and an `AttributeError` here fails
    # the whole import, which is the exact outcome this module exists to avoid.
    vat = str(vat or "").strip().upper()
    if not vat:
        return None, None

    # Which country's VAT is this? A prefix on the number is the number's own
    # answer and outranks the address; if the two disagree we know one of them
    # is wrong and we do not know which, so we say nothing.
    prefix = vat[:2] if vat[:2].isalpha() else None
    # 🔴 A COUNTRY CODE IS TWO LETTERS, AND `str()` CAN INVENT LETTERS.
    # `str(True)` is `"True"`, whose first two characters are `"TR"` — which
    # core maps to EAS `9952`, TURKEY. Before this function stringified its
    # input that case was an `AttributeError`: loud, and in its own way
    # correct. Stringifying it without this guard would turn it into a
    # confidently wrong scheme on a vendor nobody checked — the exact failure
    # `_add_party`'s comment is about, arriving through the fix for a different
    # one.
    #
    # Rejecting a non-alphabetic value also settles an inconsistency that was
    # here before: `0` was read as "no country" while `49` was read as "a
    # country that contradicts the VAT prefix", from the same non-information.
    # Neither is a country, so neither says anything now.
    country = str(country_code or "").strip().upper()[:2]
    if isinstance(country_code, bool) or not country.isalpha():
        country = None
    if prefix and country and prefix != country:
        return None, None
    country = prefix or country
    if not country:
        return None, None

    for eas in vat_peppol_eas_codes(country):
        # Sanitised the way core sanitises its own partners' endpoints, so the
        # value we emit is the value core would have stored — which is the whole
        # point of emitting it, since core matches on the pair.
        endpoint = sanitize_peppol_endpoint(vat, eas)
        if peppol_endpoint_is_valid(eas, endpoint):
            return eas, endpoint
    return None, None


def _contact_text(value):
    """A contact detail as text, or "" — never a stringified bool.

    See `_add_party`'s Contact block for why the bool case is named explicitly
    rather than left to `str(... or "")`.
    """
    if isinstance(value, bool):
        return ""
    return str(value or "").strip()


def _add_party(root, role_tag, party_data):
    role_el = etree.SubElement(root, f"{{{NS_CAC}}}{role_tag}")
    party = etree.SubElement(role_el, f"{{{NS_CAC}}}Party")

    # 🔴 EndpointID IS OMITTED UNLESS THE SCHEME IS BOTH CORRECT AND VALID, AND
    # NOTHING IS PUT IN ITS PLACE.
    #
    # `EndpointID/@schemeID` is a Peppol EAS code: it names the scheme under
    # which a participant can be ADDRESSED on the Peppol network. It is not a
    # label — core's `_import_retrieve_customer_from_eas_endpoint` matches
    # partners on exactly this pair, and core's `@api.constrains` REJECTS a pair
    # whose value does not fit the scheme. So a wrong code has two ways to hurt:
    # it can match the wrong vendor with confidence, or it can fail the import
    # outright. Both have now happened in this module — a US EIN went out as a
    # German VAT number, and a Finnish VAT number went out as a Belgian one and
    # failed the first real Finnish bill.
    #
    # `peppol_endpoint_for_party` answers from core's own table and core's own
    # rules, and answers `(None, None)` for every country where no scheme
    # honestly applies. See the block above it for why that is safe.
    vat = str(party_data.get("vat") or "").strip().upper()
    eas, endpoint_value = peppol_endpoint_for_party(party_data.get("country_code"), vat)
    if eas and endpoint_value:
        endpoint = etree.SubElement(party, f"{{{NS_CBC}}}EndpointID")
        endpoint.text = endpoint_value
        endpoint.set("schemeID", eas)

    # PostalAddress
    addr = etree.SubElement(party, f"{{{NS_CAC}}}PostalAddress")
    if party_data.get("street"):
        _add_text(addr, f"{{{NS_CBC}}}StreetName", party_data["street"])
    if party_data.get("city"):
        _add_text(addr, f"{{{NS_CBC}}}CityName", party_data["city"])
    if party_data.get("postal_code"):
        _add_text(addr, f"{{{NS_CBC}}}PostalZone", party_data["postal_code"])
    # The state. Mandatory in a US address and meaningless in most European
    # ones, which is why the original had no slot for it. `CountrySubentity` is
    # UBL's field for exactly this.
    if party_data.get("state_code"):
        _add_text(addr, f"{{{NS_CBC}}}CountrySubentity", party_data["state_code"])
    country = etree.SubElement(addr, f"{{{NS_CAC}}}Country")
    _add_text(country, f"{{{NS_CBC}}}IdentificationCode", party_data.get("country_code", ""))

    # PartyTaxScheme
    if vat:
        pts = etree.SubElement(party, f"{{{NS_CAC}}}PartyTaxScheme")
        _add_text(pts, f"{{{NS_CBC}}}CompanyID", vat)
        pts_scheme = etree.SubElement(pts, f"{{{NS_CAC}}}TaxScheme")
        _add_text(pts_scheme, f"{{{NS_CBC}}}ID", "VAT")

    # PartyLegalEntity
    ple = etree.SubElement(party, f"{{{NS_CAC}}}PartyLegalEntity")
    _add_text(ple, f"{{{NS_CBC}}}RegistrationName", party_data.get("name", ""))

    # cac:Contact — the party's own phone and email.
    #
    # 🔴 CORE READS BOTH, AND IT HAD NEVER HAD ANYTHING TO READ. At the deployed
    # revision `account_edi_ubl_cii/models/account_edi_ubl.py:2654` maps
    # `.//{*}Telephone` to `customer_values['phone']` and `:2659` maps
    # `.//{*}ElectronicMail` to `customer_values['email']`, and core's search
    # plan has had an email step and a phone step all along — but the extraction
    # schema carried no `phone` or `email` for the seller at all, so those two
    # slots arrived empty on every document this module ever generated. That is
    # half of why six of the seven bills in the sample set minted a duplicate vendor.
    #
    # ⚠️ THERE IS NO SLOT FOR A WEBSITE and none is invented. UBL's
    # `cac:Contact` has no URL element core decodes, so the extracted website
    # travels on the context instead (`ai_seller_website`, set by
    # `account.move._ai_decode_ubl_into_move`) — the same relay the business id
    # already uses, and for the same reason: an element core does not read is an
    # element that can only be decoded as something else.
    #
    # `_contact_text` rather than `str(... or "")`, because `str(True)` is
    # `"True"`. That is not a hypothetical: this module has already shipped a
    # `str()` that turned a bool into the country code `TR` and therefore into a
    # Peppol scheme meaning TURKEY (see `peppol_endpoint_for_party` above). A
    # bool is not a phone number and must not become text here either.
    phone = _contact_text(party_data.get("phone"))
    email = _contact_text(party_data.get("email"))
    if phone or email:
        contact = etree.SubElement(party, f"{{{NS_CAC}}}Contact")
        if phone:
            _add_text(contact, f"{{{NS_CBC}}}Telephone", phone)
        if email:
            _add_text(contact, f"{{{NS_CBC}}}ElectronicMail", email)
