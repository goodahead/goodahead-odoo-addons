# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Pure-Python Finvoice reader (2.0 / 2.01 / 3.0) and 3.0 writer.

Contracts: receiving contracts/public-api.md §2, sending §6.

No ERP imports here: this package is copied unchanged to every
version branch and tested without a database. It lives in
``goodahead_einvoice_base`` so receiving and sending share one reader and one
schema; ``goodahead_invoice_import_finvoice.lib.finvoice`` re-exports it.

    messages = parse(xml_bytes)             # list[FinvoiceMessage]
    data = to_invoice_data(messages[0])     # invoice data, contract §1.2
    problems = validate_schema(xml_bytes)   # 3.0 only; [{path, line, message}]
    lines = validate_schema_text(xml_bytes) # the same as "line N: message" strings
    xml_bytes = write(values)               # one 3.0 message, sending §6.2 values
    text = explain(problems[0])             # the invoice field behind a problem
"""

from . import errors, gtin, mapping, parser, references, rows, units, values, vat, writer
from .errors import (
    MISSING_FIELD,
    NOT_AN_INVOICE,
    UNREADABLE,
    UNSUPPORTED_VERSION,
    FinvoiceError,
)
from .explain import explain
from .mapping import tax_category, to_invoice_data
from .parser import (
    SUPPORTED_VERSIONS,
    XSD_PATH,
    FinvoiceMessage,
    parse,
    validate_schema,
    validate_schema_text,
)
from .writer import write

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
    "explain",
    "gtin",
    "mapping",
    "parse",
    "parser",
    "references",
    "rows",
    "tax_category",
    "to_invoice_data",
    "units",
    "validate_schema",
    "validate_schema_text",
    "values",
    "vat",
    "write",
    "writer",
]
