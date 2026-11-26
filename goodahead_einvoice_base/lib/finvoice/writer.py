# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Write one Finvoice 3.0 message from a plain values dict (sending ADR-0002).

    xml_bytes = write(values)                 # receiver="einvoice"
    problems = validate_schema(xml_bytes)     # must be [] before it leaves

``values`` is the dict of contracts/public-api.md §6.2 (``message``,
``receiver``, ``seller``, ``buyer``, ``invoice``, ``lines``, ``taxes``,
``epi``), built by the ERP from the invoice. Every element is written in the
order of the ``Finvoice3.0.xsd`` sequences; an optional value that is empty is
left out.

Rules worth knowing:

- Amounts are Finvoice decimals: comma separator, two decimals (unit prices up
  to five), each with ``AmountCurrencyIdentifier``.
- ``totals.total`` is the VAT-included total *before* cash rounding;
  ``totals.rounding`` (when not zero) is written as
  ``InvoiceTotalRoundoffAmount``, which the reader of the message adds to it.
- A line's unit goes out as ``@QuantityUnitCodeUN`` (its UN/ECE Rec 20 code,
  left out when the line has none - AC-49) and ``@QuantityUnitCode`` (the
  readable name, cut to the schema's 14 characters). ``UnitPriceBaseQuantity``
  is written only when the price is per several units.
- Descriptive text (names, addresses, descriptions) is cut to the schema's
  length; identifiers and numbers never are - a wrong one must fail the
  schema check rather than leave with different data.
- Every message names its image: ``InvoiceUrlNameText`` ``APIX_PDFFILE`` and
  ``InvoiceUrlText`` ``file://invoice.pdf``, the PDF in the same package.

- A credit note (``invoice.type`` ``credit_note``) is ``INV02``
  ``HYVITYSLASKU``. Its values carry the amounts as the ERP document states
  them (positive); the writer writes totals, VAT breakdown, row quantities and
  row amounts and the instructed amount negative (unit prices stay positive).
  ``OriginalInvoiceNumber`` is written only when ``invoice.original_number``
  is set.
- ``receiver="print"`` sends the message to APIX's print service:
  ``PRINT_RECEIVER`` replaces the customer's e-invoice address, and the
  customer's postal address becomes required.
- Content of finvoice content T05, every key optional: ``delivery`` (the
  delivery party, after the buyer; ``street2`` its second street line),
  ``invoice.type_code_un``,
  ``seller_reference`` / ``order_number`` / ``buyer_reference`` (identifiers,
  never cut: the values builder leaves an over-long one out),
  ``totals.rows``, ``taxes[].reason_lines`` / ``exemption_code`` (written
  only for an entry with a ``code``), ``lines[].kind == "text"`` (a row of
  ``RowFreeText`` only), ``lines[].gtin``, ``lines[].discount`` (``price_unit``
  is then the gross price; a credit note's discount amount is negated with
  its row amounts) and ``epi.payment_means_code``.
- A value the schema requires that is empty raises
  ``FinvoiceError(MISSING_FIELD, field="<section>.<key>")`` before anything
  is built; a value present but malformed is left to ``validate_schema``,
  whose problems ``explain`` names in plain words.

Pure Python, no ERP import (receiving ADR-0004).
"""

import datetime
from decimal import ROUND_HALF_UP, Decimal

from lxml import etree

from . import gtin, rows, units
from .errors import MISSING_FIELD, FinvoiceError

VERSION = "3.0"
PDF_URL_NAME = "APIX_PDFFILE"
PDF_URL = "file://invoice.pdf"

# invoice.type -> (InvoiceTypeCode, InvoiceTypeText)
INVOICE_TYPES = {
    "invoice": ("INV01", "LASKU"),
    "credit_note": ("INV02", "HYVITYSLASKU"),
}
# invoice.type -> InvoiceTypeCodeUN (UNTDID 1001: commercial invoice, credit note)
INVOICE_TYPES_UN = {"invoice": "380", "credit_note": "381"}
# EpiPaymentMeansCode (UNTDID 4461): SEPA credit transfer in euros, else a
# plain credit transfer.
PAYMENT_MEANS_SEPA = "58"
PAYMENT_MEANS_CREDIT_TRANSFER = "30"
RECEIVERS = ("einvoice", "print")
# (ToIdentifier, ToIntermediator) of APIX's print service. *Observed* from the
# community stack (api-sync-report OQ-1): kept in this one constant so a
# confirmed value changes one line.
PRINT_RECEIVER = ("TULOSTUS", "APIX")

# Values the schema requires, checked in this order (section, key).
REQUIRED = (
    ("message", "identifier"),
    ("message", "timestamp"),
    ("message", "sender_edicode"),
    ("seller", "name"),
    ("buyer", "name"),
    ("invoice", "number"),
    ("invoice", "date"),
    ("epi", "date"),
    ("epi", "iban"),
    ("epi", "due_date"),
)
REQUIRED_BY_RECEIVER = {
    "einvoice": (("receiver", "edicode"), ("receiver", "operator_identifier")),
    "print": (("buyer", "street"), ("buyer", "zip"), ("buyer", "city")),
}

_CENT = Decimal("0.01")
_PRICE_STEP = Decimal("0.00001")
_PERCENT_STEP = Decimal("0.001")
_UNIT_NAME_MAX = 14
_FREE_TEXT_MAX = 512


def write(values, *, receiver="einvoice"):
    """Return one Finvoice 3.0 message (UTF-8 bytes) for ``values``."""
    if receiver not in RECEIVERS:
        raise ValueError(f"Unknown Finvoice receiver {receiver!r}.")
    invoice = values["invoice"]
    if invoice.get("type", "invoice") not in INVOICE_TYPES:
        raise ValueError(f"Unknown invoice type {invoice.get('type')!r}.")
    _check_required(values, receiver)
    currency = invoice.get("currency") or "EUR"
    sign = -1 if invoice.get("type") == "credit_note" else 1
    to = values.get("receiver") or {}
    if receiver == "print":
        to = dict(zip(("edicode", "operator_identifier"), PRINT_RECEIVER))

    root = etree.Element("Finvoice", Version=VERSION)
    _message_details(root, values["message"], to)
    _seller(root, values["seller"])
    _buyer(root, values["buyer"])
    _delivery(root, values.get("delivery"))
    _invoice_details(root, invoice, values.get("taxes") or [], currency, sign)
    for line in values.get("lines") or []:
        _row(root, line, currency, sign)
    _epi(root, values["epi"], currency, sign)
    _sub(root, "InvoiceUrlNameText", PDF_URL_NAME)
    _sub(root, "InvoiceUrlText", PDF_URL)
    return etree.tostring(root, encoding="UTF-8", xml_declaration=True, pretty_print=True)


def payment_means_code(currency):
    """``EpiPaymentMeansCode`` for an invoice in ``currency``."""
    if (currency or "").upper() == "EUR":
        return PAYMENT_MEANS_SEPA
    return PAYMENT_MEANS_CREDIT_TRANSFER


def _check_required(values, receiver):
    """Raise ``finvoice.missing_field`` for the first required value that is empty."""
    for section, key in REQUIRED + REQUIRED_BY_RECEIVER[receiver]:
        value = (values.get(section) or {}).get(key)
        if value is None or str(value).strip() == "":
            raise FinvoiceError(MISSING_FIELD, field=f"{section}.{key}")


# ---------------------------------------------------------------------------
# Sections, in schema order
# ---------------------------------------------------------------------------


def _message_details(root, message, receiver):
    frame = _sub(root, "MessageTransmissionDetails")
    sender = _sub(frame, "MessageSenderDetails")
    _sub(sender, "FromIdentifier", message.get("sender_edicode"))
    _sub(sender, "FromIntermediator", message.get("sender_intermediator") or "APIX")
    to = _sub(frame, "MessageReceiverDetails")
    _sub(to, "ToIdentifier", receiver.get("edicode"))
    _sub(to, "ToIntermediator", receiver.get("operator_identifier"))
    details = _sub(frame, "MessageDetails")
    _sub(details, "MessageIdentifier", message.get("identifier"))
    _sub(details, "MessageTimeStamp", format_timestamp(message.get("timestamp")))


def _seller(root, seller):
    party = _sub(root, "SellerPartyDetails")
    _opt(party, "SellerPartyIdentifier", seller.get("business_id"))
    _sub(party, "SellerOrganisationName", _cut(seller.get("name"), 70))
    _opt(party, "SellerOrganisationTaxCode", seller.get("vat"))
    _address(party, "Seller", seller)
    iban = _compact(seller.get("iban"))
    bic = _compact(seller.get("bic"))
    if not (iban and bic):
        return
    information = _sub(root, "SellerInformationDetails")
    account = _sub(information, "SellerAccountDetails")
    _sub(account, "SellerAccountID", iban, IdentificationSchemeName="IBAN")
    _sub(account, "SellerBic", bic, IdentificationSchemeName="BIC")


def _buyer(root, buyer):
    party = _sub(root, "BuyerPartyDetails")
    _opt(party, "BuyerPartyIdentifier", buyer.get("business_id"))
    _sub(party, "BuyerOrganisationName", _cut(buyer.get("name"), 70))
    _opt(party, "BuyerOrganisationTaxCode", buyer.get("vat"))
    _address(party, "Buyer", buyer)
    _opt(root, "BuyerContactPersonName", _cut(buyer.get("contact_name"), 70))
    if buyer.get("email"):
        communication = _sub(root, "BuyerCommunicationDetails")
        _sub(communication, "BuyerEmailaddressIdentifier", buyer["email"])


def _delivery(root, delivery):
    """``DeliveryPartyDetails``: the name cut to 35 and the postal address;
    an optional ``street2`` is the second ``DeliveryStreetName`` (the schema
    takes up to three). The values builder passes it only when it adds
    something."""
    if not delivery:
        return
    party = _sub(root, "DeliveryPartyDetails")
    _sub(party, "DeliveryOrganisationName", _cut(delivery.get("name"), 35))
    address = _sub(party, "DeliveryPostalAddressDetails")
    _sub(address, "DeliveryStreetName", _cut(delivery.get("street"), 35))
    _opt(address, "DeliveryStreetName", _cut(delivery.get("street2"), 35))
    _sub(address, "DeliveryTownName", _cut(delivery.get("city"), 35))
    _sub(address, "DeliveryPostCodeIdentifier", delivery.get("zip"))
    _opt(address, "CountryCode", (delivery.get("country_code") or "").upper())


def _address(party, prefix, partner):
    """``<prefix>PostalAddressDetails``: written only when street, town and
    post code are all known (the schema requires the three together)."""
    street, city, postcode = partner.get("street"), partner.get("city"), partner.get("zip")
    if not (street and city and postcode):
        return
    address = _sub(party, f"{prefix}PostalAddressDetails")
    _sub(address, f"{prefix}StreetName", _cut(street, 35))
    _sub(address, f"{prefix}TownName", _cut(city, 35))
    _sub(address, f"{prefix}PostCodeIdentifier", postcode)
    _opt(address, "CountryCode", (partner.get("country_code") or "").upper())


def _invoice_details(root, invoice, taxes, currency, sign):
    details = _sub(root, "InvoiceDetails")
    invoice_type = invoice.get("type", "invoice")
    type_code, type_text = INVOICE_TYPES[invoice_type]
    _sub(details, "InvoiceTypeCode", type_code)
    _opt(details, "InvoiceTypeCodeUN", invoice.get("type_code_un"))
    _sub(details, "InvoiceTypeText", type_text)
    _sub(details, "OriginCode", "Original")
    _sub(details, "InvoiceNumber", invoice.get("number"))
    _date(details, "InvoiceDate", invoice.get("date"))
    if invoice_type == "credit_note":
        _opt(details, "OriginalInvoiceNumber", invoice.get("original_number"))
    # Identifiers: written as given, never cut.
    _opt(details, "SellerReferenceIdentifier", invoice.get("seller_reference"))
    _opt(details, "OrderIdentifier", invoice.get("order_number"))
    _opt(details, "BuyerReferenceIdentifier", invoice.get("buyer_reference"))
    totals = invoice.get("totals") or {}
    if totals.get("rows") is not None:
        _amount(details, "RowsTotalVatExcludedAmount", _signed(totals["rows"], sign), currency)
    _amount(details, "InvoiceTotalVatExcludedAmount", _signed(totals.get("untaxed"), sign),
            currency)
    _amount(details, "InvoiceTotalVatAmount", _signed(totals.get("tax"), sign), currency)
    _amount(details, "InvoiceTotalVatIncludedAmount", _signed(totals.get("total"), sign),
            currency)
    if _decimal(totals.get("rounding")):
        _amount(details, "InvoiceTotalRoundoffAmount", _signed(totals["rounding"], sign),
                currency)
    for tax in taxes:
        _vat_specification(details, tax, currency, sign)
    for chunk in _chunks(invoice.get("note"), _FREE_TEXT_MAX):
        _sub(details, "InvoiceFreeText", chunk)
    if invoice.get("due_date"):
        terms = _sub(details, "PaymentTermsDetails")
        _date(terms, "InvoiceDueDate", invoice["due_date"])


def _vat_specification(details, tax, currency, sign):
    """One VAT breakdown entry; an entry without a category states no
    reason and no exemption code."""
    spec = _sub(details, "VatSpecificationDetails")
    _amount(spec, "VatBaseAmount", _signed(tax.get("base"), sign), currency)
    _sub(spec, "VatRatePercent", format_percent(tax.get("percent")))
    code = tax.get("code")
    _opt(spec, "VatCode", code)
    _amount(spec, "VatRateAmount", _signed(tax.get("amount"), sign), currency)
    if not code:
        return
    for text in tax.get("reason_lines") or []:
        _sub(spec, "VatFreeText", text)
    _opt(spec, "VatExemptionReasonCode", tax.get("exemption_code"))


def _row(root, line, currency, sign):
    if line.get("kind") == "text":
        _text_row(root, line)
        return
    row = _sub(root, "InvoiceRow")
    _opt(row, "ArticleIdentifier", line.get("code"))
    _opt(row, "ArticleName", _cut(line.get("name"), 100))
    if gtin.is_valid_gtin(line.get("gtin")):
        _sub(row, "EanCode", line["gtin"], SchemeID=gtin.GTIN_SCHEME)
    unit = _unit_attributes(line)
    _sub(row, "InvoicedQuantity", format_quantity(_signed(line.get("quantity"), sign)), **unit)
    _sub(row, "UnitPriceAmount", format_unit_price(line.get("price_unit")),
         AmountCurrencyIdentifier=currency)
    base_quantity = _decimal(line.get("base_quantity"))
    if base_quantity is not None and base_quantity != 1:
        _sub(row, "UnitPriceBaseQuantity", format_quantity(base_quantity), **unit)
    for chunk in _chunks(line.get("description"), _FREE_TEXT_MAX):
        _sub(row, "RowFreeText", chunk)
    discount = line.get("discount")
    if discount:
        _sub(row, "RowDiscountPercent", format_percent(discount.get("percent")))
        _amount(row, "RowDiscountAmount", _signed(discount.get("amount"), sign), currency)
    _sub(row, "RowVatRatePercent", format_percent(line.get("tax_percent")))
    _opt(row, "RowVatCode", line.get("tax_code"))
    _amount(row, "RowVatAmount", _signed(line.get("tax"), sign), currency)
    _amount(row, "RowVatExcludedAmount", _signed(line.get("untaxed"), sign), currency)
    _amount(row, "RowAmount", _signed(line.get("total"), sign), currency)


def _text_row(root, line):
    """A note or heading: a row of ``RowFreeText`` only - no quantity, price,
    amount or VAT."""
    texts = line.get("texts") or rows.text_row_texts(line.get("description"))
    if not texts:
        return
    row = _sub(root, "InvoiceRow")
    for text in texts:
        _sub(row, "RowFreeText", text)


def _unit_attributes(line):
    """``QuantityUnitCodeUN`` only for a valid Rec 20 code (AC-49: a unit
    without one never blocks), ``QuantityUnitCode`` the readable name."""
    attributes = {}
    code = line.get("rec20_code")
    if units.is_valid_rec20(code):
        attributes["QuantityUnitCodeUN"] = code
    name = line.get("unit_name") or units.readable_name(code)
    if name:
        attributes["QuantityUnitCode"] = name[:_UNIT_NAME_MAX].strip()
    return attributes


def _epi(root, epi, currency, sign):
    details = _sub(root, "EpiDetails")
    identification = _sub(details, "EpiIdentificationDetails")
    _date(identification, "EpiDate", epi.get("date"))
    _sub(identification, "EpiReference", _compact(epi.get("reference")) or "")
    party = _sub(details, "EpiPartyDetails")
    bfi = _sub(party, "EpiBfiPartyDetails")
    bic = _compact(epi.get("bic"))
    if bic:
        _sub(bfi, "EpiBfiIdentifier", bic, IdentificationSchemeName="BIC")
    beneficiary = _sub(party, "EpiBeneficiaryPartyDetails")
    _opt(beneficiary, "EpiNameAddressDetails", _cut(epi.get("beneficiary_name"), 35))
    _sub(beneficiary, "EpiAccountID", _compact(epi.get("iban")), IdentificationSchemeName="IBAN")
    instruction = _sub(details, "EpiPaymentInstructionDetails")
    reference = _compact(epi.get("remittance_reference"))
    if reference:
        scheme = "ISO" if reference.upper().startswith("RF") else "SPY"
        _sub(instruction, "EpiRemittanceInfoIdentifier", reference,
             IdentificationSchemeName=scheme)
    _sub(instruction, "EpiInstructedAmount", format_amount(_signed(epi.get("amount"), sign)),
         AmountCurrencyIdentifier=currency)
    _sub(instruction, "EpiCharge", "SLEV", ChargeOption="SLEV")
    _date(instruction, "EpiDateOptionDate", epi.get("due_date"))
    _opt(instruction, "EpiPaymentMeansCode", epi.get("payment_means_code"))


# ---------------------------------------------------------------------------
# Value formatting (Finvoice decimals and dates)
# ---------------------------------------------------------------------------


def format_amount(value):
    """``10.2`` -> ``"10,20"``: two decimals, comma separator."""
    return _comma(_decimal(value or 0).quantize(_CENT, rounding=ROUND_HALF_UP))


def format_unit_price(value):
    """Two to five decimals: ``12.5`` -> ``"12,50"``, ``0.12345`` -> ``"0,12345"``."""
    number = _decimal(value or 0).quantize(_PRICE_STEP, rounding=ROUND_HALF_UP)
    text = format(number, "f").rstrip("0")
    whole, _sep, decimals = text.partition(".")
    return f"{whole},{decimals.ljust(2, '0')}"


def format_quantity(value):
    """``500.0`` -> ``"500"``, ``1.5`` -> ``"1,5"`` (no exponent, no trailing zeros)."""
    return _comma(_plain(_decimal(value or 0)))


def format_percent(value):
    """``25.5`` -> ``"25,5"``, ``24.0`` -> ``"24"`` (at most three decimals)."""
    number = _decimal(value or 0).quantize(_PERCENT_STEP, rounding=ROUND_HALF_UP)
    return _comma(_plain(number))


def format_date(value):
    """A date, datetime or ``YYYY-MM-DD`` string -> ``CCYYMMDD``."""
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.strftime("%Y%m%d")
    return str(value)[:10].replace("-", "")


def format_timestamp(value):
    """A datetime or ISO string -> ``xs:dateTime`` to the second."""
    if isinstance(value, datetime.datetime):
        return value.isoformat(timespec="seconds")
    return str(value or "")


def _decimal(value):
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def _signed(value, sign):
    """``value`` as a Decimal, negated for a credit note (``sign`` -1)."""
    number = _decimal(value)
    if not number:  # None, or zero (never "-0,00")
        return number
    return number * sign


def _plain(number):
    if number == number.to_integral_value():
        return format(number.quantize(Decimal(1)), "f")
    return format(number.normalize(), "f")


def _comma(number):
    text = number if isinstance(number, str) else format(number, "f")
    return text.replace(".", ",")


# ---------------------------------------------------------------------------
# Element helpers
# ---------------------------------------------------------------------------


def _sub(parent, tag, value=None, **attributes):
    element = etree.SubElement(parent, tag, **attributes)
    if value is not None:
        element.text = str(value)
    return element


def _opt(parent, tag, value, **attributes):
    """An optional element: left out when the value is empty."""
    if value is None or value == "":
        return None
    return _sub(parent, tag, value, **attributes)


def _amount(parent, tag, value, currency):
    return _sub(parent, tag, format_amount(value), AmountCurrencyIdentifier=currency)


def _date(parent, tag, value):
    return _sub(parent, tag, format_date(value), Format="CCYYMMDD")


def _cut(value, length):
    if not value:
        return value
    return str(value).strip()[:length].strip()


def _compact(value):
    """Identifiers of NMTOKEN type (IBAN, BIC, references) carry no spaces."""
    if not value:
        return value
    return "".join(str(value).split())


def _chunks(value, length):
    text = (value or "").strip()
    return [text[i:i + length] for i in range(0, len(text), length)]
