# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The one exception the Finvoice library raises.

Codes:

- ``finvoice.unreadable`` — the file is not XML, holds no Finvoice message, or a
  value inside it cannot be read (an amount that is not a number, a date that is
  not a date).
- ``finvoice.unsupported_version`` — a Finvoice message whose ``Version`` is not
  1.3, 2.0, 2.01 or 3.0 (missing or unknown).
- ``finvoice.missing_field`` — a value a bill needs is absent after every
  fallback; ``field`` names it in invoice-data terms (``invoice_number``,
  ``seller.name``, ...).
- ``finvoice.not_an_invoice`` — a readable Finvoice message whose
  InvoiceTypeCode says it is not an invoice at all (quotation, order, price
  list, ...; guideline §10.5). Added beside the contract's three so the
  Accountant reads "this is a quotation", not "unreadable".

The message is plain language for the Accountant; the caller surfaces it on the
import record as ``import.invalid_data`` with the code in the text.
"""

UNREADABLE = "finvoice.unreadable"
UNSUPPORTED_VERSION = "finvoice.unsupported_version"
MISSING_FIELD = "finvoice.missing_field"
NOT_AN_INVOICE = "finvoice.not_an_invoice"


class FinvoiceError(Exception):
    """A Finvoice message that cannot become invoice data.

    ``code`` is one of the module constants above, ``field`` the invoice-data
    key concerned (``missing_field``, value errors) or None, ``message`` the
    plain-language reason.

    ``data`` is the invoice data mapped from everything the message DID hold,
    with the missing values left empty (``finvoice.missing_field`` only;
    None otherwise). The caller stores it, so an Accountant can fill the gap
    and re-import without the message being read again — the
    same contract as the AI reading's ``ClaudeExtractionError.data``.
    """

    def __init__(self, code, message=None, field=None, data=None):
        self.code = code
        self.field = field
        self.data = data
        self.message = message or _default_message(code, field)
        super().__init__(self.message)

    def __str__(self):
        return self.message

    def __repr__(self):
        return f"FinvoiceError({self.code!r}, field={self.field!r}, message={self.message!r})"


def _default_message(code, field):
    if code == MISSING_FIELD:
        return f"The Finvoice message has no value for {field or 'a required field'}."
    if code == UNSUPPORTED_VERSION:
        return "The Finvoice version is not supported (only 1.3, 2.0, 2.01 and 3.0 are)."
    if code == NOT_AN_INVOICE:
        return "The Finvoice message is not an invoice."
    return "The file is not a readable Finvoice message."
