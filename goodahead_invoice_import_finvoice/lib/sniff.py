# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""A cheap "is this a Finvoice file?" test for the upload and mail routers.

Pure Python, no ERP import. It only decides which router takes a file; the
reading itself (and every refusal with a reason) is `finvoice.parse`'s. So it
answers yes for anything that is XML-looking text holding a ``<Finvoice>``
element anywhere — as the root, after a SOAP frame (guideline §17) or inside a
wrapper — including a version this platform refuses (unknown or missing): such a file must
become a failed import record that says why, not fall through to an importer
that does not know the format.
"""

import re

# `<Finvoice` (optionally namespace-prefixed) followed by whitespace, `>` or
# `/` — so `<FinvoiceSenderInfo>` and friends do not count.
_FINVOICE_ELEMENT_RE = re.compile(rb"<(?:[A-Za-z_][\w.\-]*:)?Finvoice[\s/>]")
_BOMS = (b"\xef\xbb\xbf",)
_PDF_MAGIC = b"%PDF-"


def looks_like_finvoice(raw):
    """True when ``raw`` (bytes or str) looks like XML holding a Finvoice
    message. Never raises."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        return False
    raw = bytes(raw)
    head = raw[:1024]
    for bom in _BOMS:
        if head.startswith(bom):
            head = head[len(bom):]
    head = head.lstrip()
    if head.startswith(_PDF_MAGIC) or not head.startswith(b"<"):
        return False
    return bool(_FINVOICE_ELEMENT_RE.search(raw))
