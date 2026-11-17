# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Generate Peppol BIS Billing 3.0 UBL Invoice/CreditNote XML from extracted data.

The UBL is never sent anywhere: it is only the input to Odoo's own
`account_edi_ubl_cii` decoder, which builds the bill. It is not validated
against Peppol schematron, and vendors from any country must import, so fields
such as `EndpointID` are omitted rather than guessed.
"""

import logging

from lxml import etree

# Core's own EAS table and endpoint rules, so emitted endpoints always pass
# core's partner constraint.
from odoo.addons.account_edi_ubl_cii.models.account_edi_common import EAS_MAPPING
from odoo.addons.account_edi_ubl_cii.models.res_partner import (
    PEPPOL_ENDPOINT_INVALID_CHARS_RE_BY_EAS,
    PEPPOL_ENDPOINT_INVALIDCHARS_RE,
    sanitize_peppol_endpoint,
)

from .bank_numbers import payee_account_number, payee_institution_id

_logger = logging.getLogger(__name__)

# Last-resort currency when neither the extraction nor the caller (normally the
# company's own currency) supplies one.
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
    """Make a credit note's amounts positive magnitudes.

    The `CreditNote` type already carries the sign; negative amounts inside it
    would produce a refund that increases the amount owed. When the payable
    total is negative the whole document is negated (not `abs()` per field), so
    relative signs between lines are preserved.
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
            # Leave bools for `_amount_of` to reject; `-True` would be -1.
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                container[key] = -value

    # Copy, so a re-import does not rewrite the stored extraction.
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
        default_currency: ISO code used when the extraction states none,
            normally the importing company's currency.

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

    # Core matches `res.currency.name` exactly and silently falls back to the
    # company currency on a miss, so normalise the model's value.
    currency = str(data.get("currency") or "").strip().upper()
    if not currency:
        fallback = str(default_currency or "").strip().upper() or DEFAULT_CURRENCY
        # Reachable only from re-imports of a stored extraction, which are not
        # re-validated.
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

    # PaymentMeans: IBAN or US account number, with BIC or ABA routing number.
    acc_number = payee_account_number(seller)
    institution_id = payee_institution_id(seller)
    if acc_number or data.get("payment_reference"):
        pm = etree.SubElement(root, f"{{{NS_CAC}}}PaymentMeans")
        # `or`, not a `.get` default: the field is nullable in the schema.
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

    # The note is not repeated in cac:PaymentTerms/cbc:Note: Odoo imports both
    # into the narration.

    # TaxTotal
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
        # `etree.set()` rejects None.
        unit_code = line.get("unit_code") or "C62"
        qty_el = etree.SubElement(line_el, f"{{{NS_CBC}}}{qty_tag}")
        qty_el.text = _fmt_decimal(qty, f"{at}: quantity")
        qty_el.set("unitCode", unit_code)

        _add_amount(line_el, f"{{{NS_CBC}}}LineExtensionAmount",
                    _amount_of(line, "line_total", f"{at}: line_total"),
                    currency, f"{at}: line_total")

        # Line-level TaxTotal. A null line tax (document states tax only in
        # total) omits the element rather than writing 0.00; Odoo's decoder
        # takes line tax from ClassifiedTaxCategory anyway. An absent key fails.
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

        # Emit the net unit price (line_total / qty) so PriceAmount x quantity
        # equals LineExtensionAmount. With a gross price and a line discount,
        # Odoo adds an untaxed "Rounding" line and taxes the gross amount.
        # Because of this, a misread quantity does not show in any total; see
        # `claude_client._validate_line_magnitude`.
        price = etree.SubElement(line_el, f"{{{NS_CAC}}}Price")
        line_total_f = _amount_of(line, "line_total", f"{at}: line_total")
        if qty:
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


def _add_text(parent, tag, text):
    el = etree.SubElement(parent, tag)
    el.text = str(text) if text is not None else ""
    return el


def _add_amount(parent, tag, amount, currency, label="an amount"):
    el = etree.SubElement(parent, tag)
    el.text = _fmt_decimal(amount, label)
    el.set("currencyID", currency)
    return el


# Sentinel: `None` is a refused value, so it cannot mean "no default".
_NO_DEFAULT = object()


def _amount_of(container, key, label, default=_NO_DEFAULT):
    """A number out of the extraction, or a named `UBLGenerationError`.

    Absent and null amounts fail rather than becoming 0.00. This backs up
    `claude_client._validate_extraction` for re-imports of a hand-edited
    extraction, which are not re-validated. ``default`` applies only to an
    absent key (for non-money fields such as quantity or tax rate).
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
    # `float(True)` is 1.0.
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
    # A missing amount is an error, never "0.00"; defaults belong in `_amount_of`.
    if value is None:
        raise UBLGenerationError(
            f"{label} is null; a missing amount must not be written as 0.00."
        )
    return f"{float(value):.2f}"


# EndpointID is emitted only where core's `EAS_MAPPING` says the country's
# Peppol endpoint is the VAT number and the value passes core's own rules.
# Every other case (unmapped countries, non-VAT schemes such as the Finnish OVT
# code, invalid values) emits no EndpointID: that only skips one of core's
# partner-matching steps and can never fail an import.


def peppol_endpoint_is_valid(eas, endpoint):
    """Core's character and length rules for an endpoint, applied here.

    Mirrors the tail of `res.partner._build_error_peppol_endpoint`, so a value
    core would reject is omitted instead of failing the import.
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

    `(None, None)` whenever core maps no VAT-based scheme for the country
    (e.g. US, China, Finland). Never raises.
    """
    # Values come from the model's JSON and may be ints.
    vat = str(vat or "").strip().upper()
    if not vat:
        return None, None

    # A VAT prefix outranks the address country; if they disagree, emit nothing.
    prefix = vat[:2] if vat[:2].isalpha() else None
    # Reject bools and non-alphabetic values: `str(True)[:2]` is "TR" (Turkey).
    country = str(country_code or "").strip().upper()[:2]
    if isinstance(country_code, bool) or not country.isalpha():
        country = None
    if prefix and country and prefix != country:
        return None, None
    country = prefix or country
    if not country:
        return None, None

    for eas in vat_peppol_eas_codes(country):
        # Sanitised as core does, since core matches on the stored pair.
        endpoint = sanitize_peppol_endpoint(vat, eas)
        if peppol_endpoint_is_valid(eas, endpoint):
            return eas, endpoint
    return None, None


def _contact_text(value):
    """A contact detail as text, or "" (never a stringified bool)."""
    if isinstance(value, bool):
        return ""
    return str(value or "").strip()


def _add_party(root, role_tag, party_data):
    role_el = etree.SubElement(root, f"{{{NS_CAC}}}{role_tag}")
    party = etree.SubElement(role_el, f"{{{NS_CAC}}}Party")

    # Core matches partners on the EndpointID/schemeID pair and rejects invalid
    # pairs, so a wrong scheme could match the wrong vendor or fail the import.
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

    # Core decodes Telephone and ElectronicMail for partner matching. UBL has no
    # website element core reads, so the website travels on the context instead.
    phone = _contact_text(party_data.get("phone"))
    email = _contact_text(party_data.get("email"))
    if phone or email:
        contact = etree.SubElement(party, f"{{{NS_CAC}}}Contact")
        if phone:
            _add_text(contact, f"{{{NS_CBC}}}Telephone", phone)
        if email:
            _add_text(contact, f"{{{NS_CBC}}}ElectronicMail", email)
