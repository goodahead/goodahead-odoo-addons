# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Name the invoice data behind a Finvoice schema problem in plain words.


    explain({"path": "/Finvoice/BuyerPartyDetails/BuyerOrganisationName", ...})
    -> "the customer's name"

``problem`` is a ``SchemaProblem`` dict from ``validate_schema`` or an element
path. A problem inside an invoice row names the line ("the unit code of line
2 (Unit codes)"); an element not in the table falls back to its own name.

Pure Python, no ERP import (receiving ADR-0004).
"""

import re

# Element name -> invoice field, for elements outside the invoice rows.
FIELDS = {
    "FromIdentifier": "your company's e-invoice address",
    "ToIdentifier": "the customer's e-invoice address (invoice contact)",
    "ToIntermediator": "the customer's e-invoice operator (invoice contact)",
    "BuyerOrganisationName": "the customer's name",
    "BuyerPartyIdentifier": "the customer's Business ID",
    "BuyerOrganisationTaxCode": "the customer's VAT number",
    "BuyerStreetName": "the customer's street address",
    "BuyerTownName": "the customer's city",
    "BuyerPostCodeIdentifier": "the customer's post code",
    "BuyerEmailaddressIdentifier": "the customer's email address",
    "SellerOrganisationName": "your company's name",
    "SellerPartyIdentifier": "your company's Business ID",
    "SellerOrganisationTaxCode": "your company's VAT number",
    "SellerStreetName": "your company's street address",
    "SellerTownName": "your company's city",
    "SellerPostCodeIdentifier": "your company's post code",
    "SellerAccountID": "your company's bank account IBAN",
    "SellerBic": "your company's bank account BIC",
    "EpiAccountID": "your company's bank account IBAN",
    "EpiBfiIdentifier": "your company's bank account BIC",
    "EpiRemittanceInfoIdentifier": "the invoice's payment reference",
    "EpiDateOptionDate": "the invoice's due date",
    "InvoiceDueDate": "the invoice's due date",
    "InvoiceNumber": "the invoice number",
    "InvoiceDate": "the invoice date",
    "OriginalInvoiceNumber": "the number of the invoice this credit note reverses",
    # finvoice content
    "DeliveryOrganisationName": "the delivery address on the invoice",
    "DeliveryStreetName": "the delivery address on the invoice",
    "DeliveryTownName": "the delivery address on the invoice",
    "DeliveryPostCodeIdentifier": "the delivery address on the invoice",
    "OrderIdentifier": "the customer reference on the invoice",
    "SellerReferenceIdentifier": "the source document on the invoice",
    "BuyerReferenceIdentifier": "the buyer reference on the invoice",
    "VatFreeText": "the VAT reason or exemption reason code of the tax",
    "VatExemptionReasonCode": "the VAT reason or exemption reason code of the tax",
}

# Element name inside an InvoiceRow -> field of that line ({line} = its number).
ROW_FIELDS = {
    "ArticleIdentifier": "the product code of line {line}",
    "ArticleName": "the product name of line {line}",
    "InvoicedQuantity": "the quantity of line {line}",
    "UnitPriceAmount": "the unit price of line {line}",
    "UnitPriceBaseQuantity": "the price base quantity of line {line}",
    "RowFreeText": "the description of line {line}",
    "RowVatRatePercent": "the VAT rate of line {line}",
    "RowVatCode": "the VAT code of line {line}",
    "EanCode": "the product's barcode on line {line}",
    "RowDiscountPercent": "the discount of line {line}",
    "RowDiscountAmount": "the discount of line {line}",
}
UNIT_CODE = "the unit code of line {line} (Unit codes)"
UNIT_ATTRIBUTE = "QuantityUnitCodeUN"

_ROW = re.compile(r"/InvoiceRow(?:\[(\d+)\])?(?:/|$)")
_INDEX = re.compile(r"\[\d+\]")


def explain(problem):
    """Plain-language name of the invoice field behind ``problem``."""
    if isinstance(problem, dict):
        path, message = problem.get("path") or "", problem.get("message") or ""
    else:
        path, message = str(problem or ""), ""
    path, _sep, attribute = path.partition("@")
    element = _INDEX.sub("", path.rstrip("/").rsplit("/", 1)[-1])
    row = _ROW.search(path)
    if not row:
        return FIELDS.get(element, element)
    line = int(row.group(1) or 1)
    if attribute == UNIT_ATTRIBUTE or f"attribute '{UNIT_ATTRIBUTE}'" in message:
        return UNIT_CODE.format(line=line)
    template = ROW_FIELDS.get(element)
    if not template:
        return element
    return template.format(line=line)
