# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Re-export of the Finvoice reader, moved to ``goodahead_einvoice_base``.

The reader and its schema now live in
``goodahead_einvoice_base/lib/finvoice`` so receiving and sending share one
copy. This shim keeps the old import path
(``odoo.addons.goodahead_invoice_import_finvoice.lib.finvoice``) and its
submodules working with the very same objects.

``validate_schema`` here keeps the receiving contract's shape (a list of
``"line N: message"`` strings, receiving contracts/public-api.md §2): it is the
base's ``validate_schema_text``. The element-path form is the base's
``validate_schema``.
"""

from . import errors, mapping, parser, values
from ._base import finvoice as _finvoice

MISSING_FIELD = _finvoice.MISSING_FIELD
NOT_AN_INVOICE = _finvoice.NOT_AN_INVOICE
SUPPORTED_VERSIONS = _finvoice.SUPPORTED_VERSIONS
UNREADABLE = _finvoice.UNREADABLE
UNSUPPORTED_VERSION = _finvoice.UNSUPPORTED_VERSION
XSD_PATH = _finvoice.XSD_PATH
FinvoiceError = _finvoice.FinvoiceError
FinvoiceMessage = _finvoice.FinvoiceMessage
parse = _finvoice.parse
tax_category = _finvoice.tax_category
to_invoice_data = _finvoice.to_invoice_data
validate_schema = _finvoice.validate_schema_text
validate_schema_text = _finvoice.validate_schema_text

__all__ = [
    "MISSING_FIELD",
    "NOT_AN_INVOICE",
    "SUPPORTED_VERSIONS",
    "UNREADABLE",
    "UNSUPPORTED_VERSION",
    "XSD_PATH",
    "FinvoiceError",
    "FinvoiceMessage",
    "errors",
    "mapping",
    "parse",
    "parser",
    "tax_category",
    "to_invoice_data",
    "validate_schema",
    "validate_schema_text",
    "values",
]
