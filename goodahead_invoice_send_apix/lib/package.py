# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The per-invoice APIX package.

    zip_bytes = build_package(message_bytes, pdf_bytes, invoice_number)

One zip per invoice (SAD §4) holding ``<safe invoice number>.xml`` (the
Finvoice message) and ``invoice.pdf`` (the image the message names with
``InvoiceUrlText`` ``file://invoice.pdf``). Member order, timestamps and
permissions are fixed, so a package regenerated from the same message and PDF
is byte-identical - the developer view can show that what was rebuilt is
what was sent.

Pure Python, no ERP import (receiving ADR-0004).
"""

import io
import re
import zipfile

PDF_NAME = "invoice.pdf"
# The earliest date a zip can hold; never the clock, so packages repeat.
FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)
_FILE_MODE = 0o644 << 16
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_NAME_MAX = 100


def build_package(message_bytes, pdf_bytes, invoice_number):
    """Return the zip (bytes) with the message and its PDF image."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _add(archive, f"{safe_file_name(invoice_number)}.xml", message_bytes)
        _add(archive, PDF_NAME, pdf_bytes)
    return buffer.getvalue()


def safe_file_name(invoice_number):
    """The invoice number as a file name: ASCII letters, digits, ``.``, ``_``
    and ``-`` only, every other run of characters one ``_``, no leading dots
    or underscores (no path can be formed); ``invoice`` when nothing is left.
    """
    name = _UNSAFE_RE.sub("_", str(invoice_number or ""))
    name = name.strip("._")[:_NAME_MAX]
    return name or "invoice"


def _add(archive, name, data):
    info = zipfile.ZipInfo(name, date_time=FIXED_DATE_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = _FILE_MODE
    info.create_system = 3  # Unix, whatever machine builds it
    archive.writestr(info, bytes(data or b""))
