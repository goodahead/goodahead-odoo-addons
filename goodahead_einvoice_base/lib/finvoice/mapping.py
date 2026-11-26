# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Finvoice message -> the shared invoice data dict.

Row by row the contract's mapping table (§2.2), with the implementation
guidelines v2.6 ("guideline §n") and the data list (Finvoice_def_3_0.xls,
"data list") deciding where the table is silent or disagrees. Rules seen in
real senders' material but not written in either are marked *observed*.

Amounts are read as Decimal and only turned into floats at the very end; signs
are settled exactly once, at the end, for credit notes.
"""

from decimal import ROUND_HALF_UP, Decimal

from . import values
from .errors import MISSING_FIELD, NOT_AN_INVOICE, UNREADABLE, FinvoiceError
from .parser import children, find, find_all, text, texts

CENT = Decimal("0.01")
_PRICE_STEP = Decimal("0.000001")

# guideline §10.5: INV02 = "Credit Note or Cancellation Invoice"; REC02 and
# RES02 = "Refund receipt".
CREDIT_TYPE_CODES = frozenset({"INV02", "REC02", "RES02"})

# guideline §10.5: message types that are not invoices at all — a request for
# quotation, quotation, order, order confirmation, delivery note, test invoice,
# price list, information message, delivery error notification — and INV06
# PRO FORMA INVOICE. None of them may become a bill.
_NOT_INVOICE_PREFIXES = ("REQ", "QUO", "ORD", "ORC", "DEV", "TES", "PRI", "INF", "DEN")
_NOT_INVOICE_CODES = frozenset({"INV06"})

# Data list, InvoiceRow: the quantity elements, in the order the contract
# table gives (Invoiced, Delivered, Ordered), with ConfirmedQuantity last —
# guideline §14: the total "is calculated based on either the delivered,
# ordered, or confirmed quantity".
_QUANTITY_ELEMENTS = (
    "InvoicedQuantity",
    "DeliveredQuantity",
    "OrderedQuantity",
    "ConfirmedQuantity",
)

# Data list, InvoiceRow: the money elements. A row with none of them and no
# quantity is a free-text row (contract §2.2; guideline §10.1).
_ROW_AMOUNT_ELEMENTS = (
    "UnitPriceAmount",
    "UnitPriceNetAmount",
    "UnitPriceVatIncludedAmount",
    "RowVatAmount",
    "RowVatExcludedAmount",
    "RowAmount",
)

DEFAULT_CURRENCY = "EUR"


def to_invoice_data(message):
    """Map one ``FinvoiceMessage`` to invoice data.

    Raises ``FinvoiceError`` with ``finvoice.missing_field`` (``field`` names
    the invoice-data key), ``finvoice.not_an_invoice`` or
    ``finvoice.unreadable`` (a value that is present but not readable).

    🔴 A MISSING VALUE DOES NOT STOP THE MAPPING. Everything the
    message holds is mapped, the missing values are left empty, and the
    ``finvoice.missing_field`` error carries that partial invoice data
    (``error.data``) — so the import record keeps editable data, and an
    Accountant fills in the one gap and re-imports instead of facing a
    record with nothing to correct. ``field`` names the first gap; the
    message names every one.
    """
    root = message.element
    missing = []

    def _missing(field, reason):
        missing.append((field, reason))

    details = find(root, "InvoiceDetails")
    if details is None:
        _missing(
            "invoice_number",
            "The Finvoice message has no InvoiceDetails, so no invoice number, date or totals.",
        )

    type_code = (text(details, "InvoiceTypeCode") or "").upper()
    _refuse_non_invoice(type_code, text(details, "InvoiceTypeText"))

    invoice_number = text(details, "InvoiceNumber")
    if not invoice_number and details is not None:
        _missing(
            "invoice_number",
            "The Finvoice message has no invoice number (InvoiceDetails/InvoiceNumber).",
        )
    # XSD: InvoiceDate is a CCYYMMDD date and mandatory.
    issue_date = values.parse_date(text(details, "InvoiceDate"), "issue_date")
    if not issue_date and details is not None:
        _missing(
            "issue_date",
            "The Finvoice message has no invoice date (InvoiceDetails/InvoiceDate).",
        )

    currency = _currency(details)
    warnings = []

    seller = _seller(root)
    if not seller["name"]:
        _missing(
            "seller.name",
            "The Finvoice message has no seller name (SellerPartyDetails/SellerOrganisationName).",
        )
    buyer = _buyer(root)

    # --- VAT breakdown first: its rates are the fallback for rows without one.
    subtotals = _vat_specifications(details, currency, warnings)
    doc_rates = sorted({s["tax_percent"] for s in subtotals if s["tax_percent"] is not None})
    header_rate = None if doc_rates else _header_rate(details)
    if header_rate is not None:
        doc_rates = [header_rate]

    # --- Rows.
    lines, leading_text, sub_lines = _rows(root, doc_rates, warnings, _missing)
    if header_rate is not None and any(
        find(row, "RowVatRatePercent") is None and find(row, "RowVatAmount") is None
        for row in children(root, "InvoiceRow")
    ):
        warnings.append(
            f"No VAT rate on the rows and no VAT breakdown: {header_rate:g} % "
            f"was taken from the header's VAT and VAT-exclusive totals."
        )

    # --- Sublines: booked only when the stated total includes them.
    excl_total = _amount(details, "InvoiceTotalVatExcludedAmount", "totals.tax_exclusive")
    target = excl_total
    if target is None:
        stated_incl = _amount(details, "InvoiceTotalVatIncludedAmount", "totals.tax_inclusive")
        stated_vat = _amount(details, "InvoiceTotalVatAmount", "totals.tax")
        if stated_incl is not None and stated_vat is not None:
            target = stated_incl - stated_vat
    lines.extend(_decide_sublines(lines, sub_lines, target, warnings))

    # --- Document-level allowances and charges (guideline §14).
    lines.extend(_document_allowances(details, lines, excl_total, doc_rates, warnings))

    # --- Rounding (data list, InvoiceTotalRoundoffAmount: "The amount which
    # will be added to the InvoiceTotalVatIncludedAmount", BT-114). Contract
    # §2.2: added as a rounding line so the lines still sum to the totals.
    roundoff = _amount(details, "InvoiceTotalRoundoffAmount", "totals.rounding")
    if roundoff:
        lines.append(_rounding_line(roundoff))

    if not lines:
        _missing(
            "lines",
            "The Finvoice message has no invoice row with an amount or a quantity.",
        )

    # guideline §14: VatBaseAmount is "the total of RowVatExcludedAmount
    # elements by VAT class" — fills a breakdown entry stated without a base,
    # and builds the whole breakdown when the message has none.
    subtotals = _complete_subtotals(subtotals, lines)

    # --- Totals.
    incl = _amount(details, "InvoiceTotalVatIncludedAmount", "totals.tax_inclusive")
    if incl is None and details is not None:
        _missing(
            "totals.tax_inclusive",
            "The Finvoice message has no invoice total (InvoiceTotalVatIncludedAmount).",
        )
    vat_total = _amount(details, "InvoiceTotalVatAmount", "totals.tax")
    if excl_total is None and incl is not None:
        # guideline §14: InvoiceTotalVatIncludedAmount = InvoiceTotalVatExcludedAmount
        # + InvoiceTotalVatAmount, and InvoiceTotalVatAmount is the sum of the
        # VatRateAmounts.
        if vat_total is None:
            vat_total = sum((s["tax_amount"] for s in subtotals), Decimal(0))
        excl_total = incl - vat_total
    rounding = roundoff or Decimal(0)
    tax_inclusive = incl + rounding if incl is not None else None
    tax_exclusive = excl_total + rounding if excl_total is not None else None
    # 🔴 THE LINE TOTAL IS THE DOCUMENT'S, NOT THE SUM OF THE MAPPED ROWS
    #. guideline §14: InvoiceTotalVatExcludedAmount = the rows'
    # total - DiscountsTotalVatExcludedAmount + ChargesTotalVatExcludedAmount,
    # and the rounding is added on top of it (data list,
    # InvoiceTotalRoundoffAmount). The mapped lines are exactly those parts:
    # the rows, the document allowances and charges as lines
    # (`_document_allowances`, which leaves them out when the rows already
    # make the stated total) and the rounding line. So the stated total plus
    # the rounding is what the lines must add up to — and the core validator
    # compares the two. Summing the mapped rows here instead made that check
    # compare the rows with themselves: a message whose rows add up to 350
    # under a stated 360 became a bill.
    line_extension = tax_exclusive

    paid = _amount(details, "InvoicePaidAmount", "totals.paid")
    type_is_credit = type_code in CREDIT_TYPE_CODES
    # contract §2.2: a negative total is a credit note whatever the type code
    # (guideline §10.5 allows negative INV01 totals and names no other way).
    rows_total = _line_sum(lines)
    if tax_inclusive is not None:
        negative = tax_inclusive < 0 or (tax_inclusive == 0 and rows_total < 0)
    else:
        negative = (tax_exclusive if tax_exclusive is not None else rows_total) < 0
    is_credit = type_is_credit or negative

    payable = _payable(root, currency, tax_inclusive, paid, is_credit, warnings)

    # --- References and dates.
    payment_reference = text(
        root, "EpiDetails/EpiPaymentInstructionDetails/EpiRemittanceInfoIdentifier"
    )
    seller_reference = text(details, "SellerReferenceIdentifier")
    if not payment_reference and values.payment_reference_is_valid(seller_reference):
        # *observed*: senders put the reference number in
        # SellerReferenceIdentifier. The data list defines that element as the
        # seller's own order reference (BT-14), so it is taken only when it
        # passes the banking reference check (guideline §13).
        payment_reference = seller_reference

    note_parts = leading_text + texts(details, "InvoiceFreeText")
    data = {
        "type": "CreditNote" if is_credit else "Invoice",
        "invoice_number": invoice_number,
        "issue_date": issue_date,
        "due_date": _due_date(root, details),
        "currency": currency,
        "seller": seller,
        "buyer": buyer,
        "payment_reference": payment_reference,
        # finvoice content AC-21: the buyer's order number (BT-13) and the
        # buyer reference (BT-10) the vendor sent; None when absent.
        "order_reference": text(details, "OrderIdentifier"),
        "buyer_reference": text(details, "BuyerReferenceIdentifier"),
        "note": "\n".join(note_parts) or None,
        "lines": lines,
        "tax_subtotals": subtotals,
        "totals": {
            "line_extension": line_extension,
            "tax_exclusive": tax_exclusive,
            "tax_inclusive": tax_inclusive,
            "payable": payable,
        },
        "source_meta": {
            "format": "finvoice",
            "version": message.version,
            "invoice_type_code": type_code or None,
            "original_invoice_number": text(details, "OriginalInvoiceNumber"),
            "rounding": roundoff,
            "paid_amount": paid,
            "seller_reference": seller_reference,
            "sign_normalised": False,
            # contracts/public-api.md §1.4: every amount is stated by the
            # message, none read off a page — the core then neither applies
            # the misreading check to a line nor refuses a message whose rows
            # do not make its totals; the bill is built and shows the gap.
            "amounts_stated": True,
            "warnings": warnings,
        },
    }

    # contract §2.2 / spec AC-24: a credit note stated with negative amounts
    # is multiplied by -1 once, here, so every amount is a positive magnitude
    # and type=CreditNote carries the sign. A credit note already stated in
    # positives (INV02 with positive totals, observed) is left alone.
    if is_credit and negative:
        _negate_document(data)
        data["source_meta"]["sign_normalised"] = True

    data = _floats(data)
    if missing:
        raise FinvoiceError(
            MISSING_FIELD,
            " ".join(reason for _field, reason in missing),
            field=missing[0][0],
            data=data,
        )
    return data


def _line_sum(lines):
    """The lines' total, a line whose amount is missing left out."""
    return sum(
        (line["line_total"] for line in lines if line["line_total"] is not None),
        Decimal(0),
    )


# ---------------------------------------------------------------------------
# Header parts
# ---------------------------------------------------------------------------


def _refuse_non_invoice(type_code, type_text):
    if not type_code:
        # XSD makes InvoiceTypeCode mandatory; a message without one is read as
        # an ordinary invoice (observed in 2.x material).
        return
    if type_code in _NOT_INVOICE_CODES or type_code.startswith(_NOT_INVOICE_PREFIXES):
        shown = f"{type_code} ({type_text})" if type_text else type_code
        raise FinvoiceError(
            NOT_AN_INVOICE,
            f"The Finvoice message is a {shown}, not an invoice, so no bill is created.",
            field="type",
        )


def _currency(details):
    # contract §2.2: @AmountCurrencyIdentifier of InvoiceTotalVatIncludedAmount,
    # EUR when absent (the attribute is required in 3.0, optional in 2.x).
    node = find(details, "InvoiceTotalVatIncludedAmount")
    code = (node.get("AmountCurrencyIdentifier") or "").strip().upper() if node is not None else ""
    if not code:
        return DEFAULT_CURRENCY
    if len(code) != 3 or not code.isalpha():
        raise FinvoiceError(
            UNREADABLE,
            f"The Finvoice currency {code!r} is not a 3-letter ISO 4217 code.",
            field="currency",
        )
    return code


def _due_date(root, details):
    # contract §2.2 / guideline §10.9: PaymentTermsDetails/InvoiceDueDate first
    # (the element repeats; the first one that has a date wins), else the EPI
    # due date (data list: EpiDateOptionDate "Only valid value in Finland is
    # Duedate").
    for terms in children(details, "PaymentTermsDetails"):
        due = values.parse_date(text(terms, "InvoiceDueDate"), "due_date")
        if due:
            return due
    return values.parse_date(
        text(root, "EpiDetails/EpiPaymentInstructionDetails/EpiDateOptionDate"), "due_date"
    )


def _seller(root):
    party = find(root, "SellerPartyDetails")
    # XSD: SellerOrganisationName is 1..n; the parts are one name. A missing
    # name is left empty and reported by the caller.
    name = " ".join(texts(party, "SellerOrganisationName")) or None
    tax_code = text(party, "SellerOrganisationTaxCode")
    business_id = text(party, "SellerPartyIdentifier")
    vat = tax_code
    if tax_code and values.looks_like_business_id(tax_code):
        # *observed*: the Business ID written in the VAT field. It is a
        # Business ID, so it goes there, and the VAT slot is left for the rule
        # below.
        vat = None
        if not business_id:
            business_id = tax_code

    address = find(party, "SellerPostalAddressDetails")
    country = (text(address, "CountryCode") or "").upper() or None
    if (
        not vat
        and business_id
        and country in (None, "FI")
        and values.business_id_checksum_ok(business_id)
    ):
        # *observed*, and the data list's own definition of
        # SellerOrganisationTaxCode: "The Business Identity Code with FI as a
        # prefix". Only for a valid Finnish Business ID and a seller that is not
        # stated to be outside Finland.
        vat = values.vat_from_business_id(business_id)

    iban, bic = _bank_account(root)
    return {
        "name": name,
        "vat": vat,
        "business_id": business_id,
        # XSD: SellerStreetName is 1..3 lines of one address.
        "street": ", ".join(texts(address, "SellerStreetName")) or None,
        "city": text(address, "SellerTownName"),
        "postal_code": text(address, "SellerPostCodeIdentifier"),
        "country_code": country,
        # contract §2.2: SellerPhoneNumberIdentifier (contact), else the
        # company phone printed in the footer (data list, SellerPhoneNumber).
        "phone": text(root, "SellerCommunicationDetails/SellerPhoneNumberIdentifier")
        or text(root, "SellerInformationDetails/SellerPhoneNumber"),
        # contract §2.2: the common address, else the contact's (data list).
        "email": text(root, "SellerInformationDetails/SellerCommonEmailaddressIdentifier")
        or text(root, "SellerCommunicationDetails/SellerEmailaddressIdentifier"),
        "website": text(root, "SellerInformationDetails/SellerWebaddressIdentifier"),
        "iban": iban,
        "bic": bic,
    }


def _bank_account(root):
    """contract §2.2: EpiAccountID, else SellerAccountID — the first IBAN that
    passes the mod-97 checksum. The BIC is the one stated with that account
    (EpiBfiIdentifier with the EPI account, SellerBic with a SellerAccountID;
    guideline §13: "With IBAN account number you should have banks BIC code in
    the EpiBfiIdentifier element as well"); with no valid IBAN the BIC is
    EpiBfiIdentifier, else the first SellerBic."""
    epi_bic = text(root, "EpiDetails/EpiPartyDetails/EpiBfiPartyDetails/EpiBfiIdentifier")
    candidates = [
        (
            text(root, "EpiDetails/EpiPartyDetails/EpiBeneficiaryPartyDetails/EpiAccountID"),
            epi_bic,
        )
    ]
    for account in find_all(root, "SellerInformationDetails/SellerAccountDetails"):
        candidates.append((text(account, "SellerAccountID"), text(account, "SellerBic")))
    for account, bic in candidates:
        normalised = values.normalise_account(account)
        if values.iban_is_valid(normalised):
            return normalised, bic or epi_bic
    fallback_bic = epi_bic or next((bic for _account, bic in candidates[1:] if bic), None)
    return None, fallback_bic


def _buyer(root):
    party = find(root, "BuyerPartyDetails")
    if party is None:
        return None
    address = find(party, "BuyerPostalAddressDetails")
    return {
        "name": " ".join(texts(party, "BuyerOrganisationName")) or None,
        "vat": text(party, "BuyerOrganisationTaxCode"),
        "business_id": text(party, "BuyerPartyIdentifier"),
        "street": ", ".join(texts(address, "BuyerStreetName")) or None,
        "city": text(address, "BuyerTownName"),
        "postal_code": text(address, "BuyerPostCodeIdentifier"),
        "country_code": (text(address, "CountryCode") or "").upper() or None,
    }


def _payable(root, currency, tax_inclusive, paid, is_credit, warnings):
    epi_node = find(root, "EpiDetails/EpiPaymentInstructionDetails/EpiInstructedAmount")
    epi = values.parse_decimal(epi_node.text, "totals.payable") if epi_node is not None else None
    # EN 16931 BT-115 as the guideline §14 example states it: amount to pay =
    # total with VAT (+ rounding, already in tax_inclusive) - previously paid.
    computed = tax_inclusive - (paid or Decimal(0)) if tax_inclusive is not None else None
    if epi is None:
        return computed
    epi_currency = (epi_node.get("AmountCurrencyIdentifier") or "").strip().upper()
    if epi_currency and epi_currency != currency:
        # guideline §13: the EPI attribute "must be EUR" on consumer invoices,
        # so a foreign-currency invoice can carry its instructed amount in
        # another currency; it is not this document's amount to pay.
        warnings.append(
            f"EpiInstructedAmount is in {epi_currency}, the invoice in {currency}; "
            f"the amount to pay is taken from the invoice total."
        )
        return computed
    if is_credit and epi == 0 and tax_inclusive:
        # *observed*: credit notes state 0,00 as the instructed amount because
        # a payment order cannot be negative; the credit is the total.
        return computed
    return epi


# ---------------------------------------------------------------------------
# VAT breakdown
# ---------------------------------------------------------------------------


def tax_category(code, rate):
    """UNCL 5305 VAT code (+ rate) -> S / Z / E / AE.

    guideline §10.4 / data list: VatCode and RowVatCode use UN/CEFACT code
    list 5305. The four the invoice data knows pass through; K (intra-Community
    supply: the buyer accounts for the VAT) is the buyer's reverse charge, AE;
    G (export outside the EU) is zero-rated, Z; O (outside the scope of VAT) is
    E. Any other code: S when a rate is charged, else Z. No code: S when a
    rate is charged, Z at 0 %, None when there is no rate.
    """
    code = (code or "").strip().upper()
    if code in ("S", "Z", "E", "AE"):
        return code
    if code == "K":
        return "AE"
    if code == "G":
        return "Z"
    if code == "O":
        return "E"
    if rate is None:
        return "S" if code else None
    return "S" if rate > 0 else "Z"


def _vat_specifications(details, currency, warnings):
    entries = []
    for spec in children(details, "VatSpecificationDetails"):
        base_node = find(spec, "VatBaseAmount")
        tax_node = find(spec, "VatRateAmount")
        base = values.parse_decimal(base_node.text if base_node is not None else None,
                                    "tax_subtotals.taxable_amount")
        tax = values.parse_decimal(tax_node.text if tax_node is not None else None,
                                   "tax_subtotals.tax_amount")
        rate = values.parse_decimal(text(spec, "VatRatePercent"), "tax_subtotals.tax_percent")
        code = text(spec, "VatCode")
        if base is None and tax is None and rate is None and code is None:
            continue
        spec_currency = None
        for node in (base_node, tax_node):
            if node is not None and node.get("AmountCurrencyIdentifier"):
                spec_currency = node.get("AmountCurrencyIdentifier").strip().upper()
                break
        entries.append({"base": base, "tax": tax, "rate": rate, "code": code,
                        "currency": spec_currency})

    # guideline §14: "VatSpecificationDetails can be repeated according to tax
    # rates and currencies" — and §19.1: "The amount of tax ... must be in the
    # currency of the country of sale." The breakdown in the document's own
    # currency is the one that adds up to its totals; the others are dropped.
    own = [e for e in entries if e["currency"] in (None, currency)]
    if own and len(own) < len(entries):
        dropped = sorted({e["currency"] for e in entries if e["currency"] not in (None, currency)})
        warnings.append(
            f"VAT breakdown also stated in {', '.join(dropped)}; only the {currency} one is used."
        )
        entries = own

    subtotals = []
    for entry in entries:
        rate, base, tax, code = entry["rate"], entry["base"], entry["tax"], entry["code"]
        if rate is None:
            if (code or "").upper() in ("Z", "E", "AE", "K", "G", "O") or tax == 0:
                rate = Decimal(0)
            elif base and tax is not None:
                # *observed*: a breakdown entry without its rate; the rate is
                # what its own amounts state.
                rate = (tax / base * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        if tax is None and base is not None and rate is not None:
            # guideline §14, secondary rule: RowVatExcludedAmount * rate.
            tax = (base * rate / 100).quantize(CENT, rounding=ROUND_HALF_UP)
        subtotals.append({
            "tax_percent": rate,
            "taxable_amount": base,
            "tax_amount": tax if tax is not None else Decimal(0),
            "tax_category": tax_category(code, rate),
        })
    return subtotals


def _complete_subtotals(subtotals, lines):
    def lines_base(rate, category):
        return _line_sum([
            line for line in lines
            if line["tax_percent"] == rate and line["tax_category"] == category
            and not line.get("_rounding")
        ])

    if subtotals:
        for sub in subtotals:
            if sub["taxable_amount"] is None:
                sub["taxable_amount"] = lines_base(sub["tax_percent"], sub["tax_category"])
        return subtotals

    # No VatSpecificationDetails at all: guideline §14 builds the breakdown
    # from the rows — VatBaseAmount = sum of RowVatExcludedAmount per VAT
    # class, VatRateAmount = sum of RowVatAmount, or RowVatExcludedAmount *
    # RowVatRatePercent when the rows carry no VAT amount.
    groups = {}
    for line in lines:
        if line["tax_percent"] is None or line.get("_rounding") or line["line_total"] is None:
            continue
        key = (line["tax_percent"], line["tax_category"])
        group = groups.setdefault(key, {"base": Decimal(0), "tax": Decimal(0), "all_tax": True})
        group["base"] += line["line_total"]
        if line["tax_amount"] is None:
            group["all_tax"] = False
        else:
            group["tax"] += line["tax_amount"]
    result = []
    for (rate, category), group in groups.items():
        tax = group["tax"] if group["all_tax"] else (
            group["base"] * rate / 100).quantize(CENT, rounding=ROUND_HALF_UP)
        result.append({
            "tax_percent": rate,
            "taxable_amount": group["base"],
            "tax_amount": tax,
            "tax_category": category,
        })
    return result


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def _rows(root, doc_rates, warnings, report_missing):
    """The rows as lines, the text before the first priced row, and the
    sublines (SubInvoiceRow) as candidate lines kept apart: whether they are
    booked is decided against the stated total (`_decide_sublines`)."""
    lines = []
    leading_text = []
    sub_lines = []
    for position, row in enumerate(children(root, "InvoiceRow"), start=1):
        subs = children(row, "SubInvoiceRow")
        if subs:
            # guideline §11 / data list: a SubInvoiceRow is usually a summary
            # of the preceding rows ("Sub invoice row is not part of the
            # invoice total calculation"), and an InvoiceRow holding one holds
            # nothing else (XSD choice). *observed*: some senders put a real
            # adjustment there (a credit for an earlier payment), which the
            # stated total includes. Read them as lines; book them only when
            # the total says so.
            for sub_position, sub in enumerate(subs, start=1):
                sub_line = _row_to_line(
                    sub, f"{position}.{sub_position}", doc_rates, None, prefix="Sub"
                )
                if sub_line is not None and sub_line["line_total"] is not None:
                    sub_lines.append(sub_line)
            continue
        line = _row_to_line(row, position, doc_rates, report_missing)
        if line is not None:
            lines.append(line)
            continue
        free_text = _row_text(row)
        if not free_text:
            continue
        # contract §2.2 / guideline §10.1: a row without an amount and without
        # a quantity is text belonging to the row before it.
        if lines:
            previous = lines[-1]
            previous["description"] = "\n".join(
                part for part in (previous["description"], free_text) if part
            )
        else:
            # *observed*: text rows before the first priced row are a heading
            # for the whole invoice, so they go to the note.
            leading_text.append(free_text)
    return lines, leading_text, sub_lines


def _decide_sublines(lines, sub_lines, target, warnings):
    """The sublines to book: none, unless the rows alone miss the stated
    VAT-exclusive total and the rows with the sublines make it.

    *observed*: senders use SubInvoiceRow both for headings and subtotals
    that repeat the rows above (guideline §11) and for a real adjustment the
    total includes (a credit for an earlier payment). The stated total tells
    the two apart; when it tells nothing (no total, or neither sum makes it)
    the sublines are left out as before and the bill shows the difference.
    """
    if not sub_lines:
        return []
    count = len(sub_lines)
    if target is not None:
        rows_total = _line_sum(lines)
        with_subs = rows_total + _line_sum(sub_lines)
        if abs(rows_total - target) > CENT and abs(with_subs - target) <= CENT:
            warnings.append(
                f"{count} subline(s) (SubInvoiceRow) booked as lines: the rows alone "
                f"make {rows_total}, the stated total {target} includes the sublines."
            )
            return sub_lines
    warnings.append(
        f"{count} subline(s) (SubInvoiceRow) were not booked as lines; "
        f"guideline §11: they summarise the rows above them."
    )
    return []


def _row_text(row, prefix=""):
    parts = [text(row, f"{prefix}ArticleName"), text(row, f"{prefix}ArticleDescription")]
    parts += texts(row, f"{prefix}RowFreeText")
    return "\n".join(p for p in parts if p)


def _first_quantity(row, prefix=""):
    for name in _QUANTITY_ELEMENTS:
        for node in children(row, f"{prefix}{name}"):
            if node.text and node.text.strip():
                quantity = values.parse_decimal(node.text, f"lines.{name}")
                codes = (node.get("QuantityUnitCodeUN"), node.get("QuantityUnitCode"))
                return quantity, values.unit_code(*codes), values.unmapped_unit_code(*codes)
    return None, None, None


def _row_to_line(row, position, doc_rates, report_missing, prefix=""):
    """One InvoiceRow as a line. With ``prefix="Sub"`` the same for a
    SubInvoiceRow, whose elements are the row's with "Sub" in front
    (SubRowVatExcludedAmount, SubArticleName, SubInvoicedQuantity, …). A
    subline missing its amount is left out rather than reported
    (``report_missing`` None): it is only a candidate line."""
    p = prefix
    where = f"lines[{position}]"
    has_amount = any(
        node.text and node.text.strip()
        for name in _ROW_AMOUNT_ELEMENTS for node in children(row, f"{p}{name}")
    )
    quantity, unit, unmapped_unit = _first_quantity(row, p)
    if not has_amount and quantity is None:
        return None

    excl = _amount(row, f"{p}RowVatExcludedAmount", f"{where}.line_total")
    row_amount = _amount(row, f"{p}RowAmount", f"{where}.line_total")
    row_vat = _amount(row, f"{p}RowVatAmount", f"{where}.tax_amount")
    rate = values.parse_decimal(text(row, f"{p}RowVatRatePercent"), f"{where}.tax_percent")
    unit_price = _amount(row, f"{p}UnitPriceAmount", f"{where}.price_unit")
    base_quantity = values.parse_decimal(text(row, f"{p}UnitPriceBaseQuantity"),
                                         f"{where}.price_base_quantity")
    discount = _amount(row, f"{p}RowDiscountAmount", f"{where}.discount")

    price = None
    if unit_price is not None:
        # Data list, UnitPriceBaseQuantity (BT-149): the price is per this many
        # units ("eg. 1000").
        price = unit_price / base_quantity if base_quantity and base_quantity > 0 else unit_price

    # contract §2.2: RowVatExcludedAmount, else RowAmount - RowVatAmount.
    if excl is not None:
        line_total = excl
    elif row_amount is not None and row_vat is not None:
        line_total = row_amount - row_vat
    elif quantity is not None and price is not None:
        # Data list, RowVatExcludedAmount: "Amount multiplied from the
        # quantity, unitprice (Row Discount deducted)".
        line_total = (quantity * price - (discount or 0)).quantize(CENT, rounding=ROUND_HALF_UP)
    elif row_amount is not None and (
        rate == 0 or (rate is None and row_vat is None and not doc_rates)
    ):
        # A row with only its total and no VAT on it: the total is the net.
        line_total = row_amount
    elif not has_amount:
        # *observed*: a quantity-only row (e.g. a delivered item at no charge).
        line_total = Decimal(0)
    elif report_missing is None:
        return None
    else:
        # Left empty for an Accountant to fill in; the row is kept.
        report_missing(
            f"{where}.line_total",
            f"Invoice row {position} has amounts but no VAT-exclusive row amount "
            f"(RowVatExcludedAmount, or RowAmount with RowVatAmount).",
        )
        line_total = None

    if quantity is None:
        # *observed*: no quantity element at all but an
        # amount — one unit of it.
        quantity = Decimal(1)
    if quantity < 0 and line_total is not None and line_total > 0:
        # *observed*: a credit row with a negative quantity and an UNSIGNED
        # row total (and VAT) under a negative document total. The data list
        # makes RowVatExcludedAmount quantity x price, so the quantity's sign
        # is the row's: the row is a credit. Left positive, it would add to a
        # credit note's lines what the document takes off.
        line_total = -line_total
        if row_vat is not None and row_vat > 0:
            row_vat = -row_vat
    if quantity < 0:
        # *observed*: credit rows written with a negative quantity and a
        # positive price; the sign belongs to the price/amount.
        quantity = -quantity
        price = -price if price is not None else None
    if price is None and line_total is not None:
        # *observed*: no UnitPriceAmount -> the row amount per unit.
        price = (line_total / quantity).quantize(_PRICE_STEP) if quantity else line_total
    elif line_total and price and (price < 0) != (line_total < 0):
        # *observed*: only the amounts signed (credit rows), the price kept
        # positive. The price follows the row amount so quantity x price and
        # the line total agree in sign.
        price = -price

    if rate is None:
        rate = _fallback_rate(row_vat, line_total, doc_rates)

    free_texts = texts(row, f"{p}RowFreeText")
    name = (text(row, f"{p}ArticleName") or text(row, f"{p}ArticleDescription")
            or (free_texts or [None])[0] or text(row, f"{p}ArticleIdentifier") or "")
    description_parts = []
    article_description = text(row, f"{p}ArticleDescription")
    if article_description and article_description != name:
        description_parts.append(article_description)
    description_parts += [t for t in free_texts if t != name]

    line = {
        "product_code": text(row, f"{p}ArticleIdentifier"),
        "name": name,
        "description": "\n".join(description_parts) or None,
        "quantity": quantity,
        "unit_code": unit,
        # AC-51: the code as written when no unit could be read from it, so
        # the import notes it instead of giving the line some unit.
        "unit_code_unmapped": unmapped_unit,
        "price_unit": price,
        "line_total": line_total,
        "tax_percent": rate,
        "tax_amount": row_vat,
        "tax_category": tax_category(text(row, f"{p}RowVatCode"), rate),
        "expense_category": None,
    }
    line.update(_row_discount(row, p, where, price, line_total, discount))
    return line


def _row_discount(row, prefix, where, price, line_total, discount):
    """finvoice content AC-22: a discounted row's gross price (the unit price,
    per one unit), its stated percentage and its discount amount, the amount
    signed like the row. Nothing for a row without a discount, so such a row
    gives the same line as before."""
    percent = values.parse_decimal(text(row, f"{prefix}RowDiscountPercent"),
                                   f"{where}.discount_percent")
    if percent is None and discount is None:
        return {}
    keys = {"gross_price": price}
    if percent is not None:
        keys["discount_percent"] = percent
    if discount is not None:
        negative = line_total is not None and line_total < 0
        keys["discount_amount"] = -abs(discount) if negative else abs(discount)
    return keys


def _fallback_rate(row_vat, line_total, doc_rates):
    """A row without RowVatRatePercent. *observed*: the row's own VAT amount
    names its rate among the document's rates; with one rate on the document,
    that is the row's."""
    if row_vat is not None and line_total:
        stated = row_vat / line_total * 100
        if doc_rates:
            nearest = min(doc_rates, key=lambda r: abs(r - stated))
            if abs(nearest - stated) <= 1:
                return nearest
        return stated.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if len(doc_rates) == 1:
        return doc_rates[0]
    return None


# Finnish VAT rates, current and earlier: a header-derived rate must be one.
_FINNISH_VAT_RATES = tuple(Decimal(r) for r in ("25.5", "24", "14", "13.5", "10", "0"))


def _header_rate(details):
    """*observed* (Finvoice 1.3 senders): no RowVatRatePercent, no RowVatAmount
    and no VatSpecificationDetails; only InvoiceTotalVatAmount and
    InvoiceTotalVatExcludedAmount. When the two make exactly one Finnish rate
    (to the cent), the document has that one rate."""
    excl = _amount(details, "InvoiceTotalVatExcludedAmount", "totals.tax_exclusive")
    vat = _amount(details, "InvoiceTotalVatAmount", "totals.tax")
    if not excl or vat is None:
        return None
    for rate in _FINNISH_VAT_RATES:
        if abs((excl * rate / 100).quantize(CENT, rounding=ROUND_HALF_UP) - vat) <= CENT:
            return rate
    return None


def _document_allowances(details, lines, excl_total, doc_rates, warnings):
    """guideline §14: InvoiceTotalVatExcludedAmount = rows total -
    DiscountsTotalVatExcludedAmount + ChargesTotalVatExcludedAmount. The
    document-level DiscountDetails / ChargeDetails (BG-20 / BG-21) become lines
    so the lines add up to the VAT-exclusive total."""
    extra = []
    for node in children(details, "DiscountDetails"):
        amount = _amount(node, "Amount", "lines.discount")
        if amount is None:
            continue
        rate = values.parse_decimal(text(node, "VatRatePercent"), "lines.discount.tax_percent")
        extra.append(_allowance_line(
            text(node, "FreeText") or "Discount", -amount, rate, text(node, "VatCategoryCode")))
    for node in children(details, "ChargeDetails"):
        amount = _amount(node, "Amount", "lines.charge")
        if amount is None:
            continue
        rate = values.parse_decimal(text(node, "VatRatePercent"), "lines.charge.tax_percent")
        extra.append(_allowance_line(
            text(node, "ReasonText") or "Charge", amount, rate, text(node, "VatCategoryCode")))
    if not extra:
        single = doc_rates[0] if len(doc_rates) == 1 else None
        discounts = _amount(details, "DiscountsTotalVatExcludedAmount", "lines.discount")
        charges = _amount(details, "ChargesTotalVatExcludedAmount", "lines.charge")
        if discounts:
            extra.append(_allowance_line("Discount", -discounts, single, None))
        if charges:
            extra.append(_allowance_line("Charge", charges, single, None))
    if not extra:
        return []

    rows_total = _line_sum(lines)
    with_extra = rows_total + _line_sum(extra)
    rows_match = excl_total is not None and abs(rows_total - excl_total) <= CENT
    if rows_match and abs(with_extra - excl_total) > CENT:
        # The rows alone already make the VAT-exclusive total, so the stated
        # allowances are informational (already inside the rows).
        warnings.append(
            "Document-level discounts/charges are already included in the rows; not added as lines."
        )
        return []
    return extra


def _allowance_line(name, amount, rate, code):
    return {
        "product_code": None,
        "name": name,
        "description": None,
        "quantity": Decimal(1),
        "unit_code": None,
        "price_unit": amount,
        "line_total": amount,
        "tax_percent": rate,
        "tax_amount": None,
        "tax_category": tax_category(code, rate),
        "expense_category": None,
    }


def _rounding_line(amount):
    # Rounding is outside VAT: 0 %, category Z (contract §2.2, "rate 0
    # without code -> Z"). The VAT breakdown is left as the sender stated it.
    return {
        "product_code": None,
        "name": "Rounding",
        "description": None,
        "quantity": Decimal(1),
        "unit_code": None,
        "price_unit": amount,
        "line_total": amount,
        "tax_percent": Decimal(0),
        "tax_amount": Decimal(0),
        "tax_category": "Z",
        "expense_category": None,
        "_rounding": True,
    }


# ---------------------------------------------------------------------------
# Finishing
# ---------------------------------------------------------------------------


def _amount(element, path, field):
    return values.parse_decimal(text(element, path), field)


def _negate(value):
    if value is None or isinstance(value, bool):
        return value
    return -value


def _negate_document(data):
    for line in data["lines"]:
        for key in ("price_unit", "line_total", "tax_amount"):
            line[key] = _negate(line[key])
        for key in ("gross_price", "discount_amount"):
            if key in line:
                line[key] = _negate(line[key])
    for sub in data["tax_subtotals"]:
        for key in ("taxable_amount", "tax_amount"):
            sub[key] = _negate(sub[key])
    for key in data["totals"]:
        data["totals"][key] = _negate(data["totals"][key])
    meta = data["source_meta"]
    meta["rounding"] = _negate(meta["rounding"])
    meta["paid_amount"] = _negate(meta["paid_amount"])


def _floats(data):
    for line in data["lines"]:
        line.pop("_rounding", None)
        for key in ("quantity", "price_unit", "line_total", "tax_percent", "tax_amount"):
            line[key] = values.to_float(line[key])
        for key in ("gross_price", "discount_percent", "discount_amount"):
            if key in line:
                line[key] = values.to_float(line[key])
    for sub in data["tax_subtotals"]:
        for key in ("tax_percent", "taxable_amount", "tax_amount"):
            sub[key] = values.to_float(sub[key])
    data["totals"] = {key: values.to_float(value) for key, value in data["totals"].items()}
    meta = data["source_meta"]
    meta["rounding"] = values.to_float(meta["rounding"])
    meta["paid_amount"] = values.to_float(meta["paid_amount"])
    return data
