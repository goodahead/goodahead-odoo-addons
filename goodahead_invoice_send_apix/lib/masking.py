# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The developer-view record of one APIX call.

    text = mask_exchange(method, url, status, body)

The transfer id (``TraID``, ``uid``) and the request digest (``d``) in the
query string are replaced by ``***``; a binary body (the zip package) is
replaced by its size and SHA-256; a text body (an APIX answer, which holds no
secret) is kept. The transfer key is never part of a call, so it can never
reach the record.

Pure Python, no ERP import (receiving ADR-0004).
"""

import hashlib

# The query string is split by hand: the sending libraries import nothing of
# the network stack, not even urllib.parse (test_readiness_rules, purity).
MASK = "***"
SECRET_PARAMETERS = frozenset({"traid", "uid", "d"})
_ZIP_MAGIC = b"PK\x03\x04"


def mask_exchange(method, url, status, body):
    """Return the masked record of the call as plain text."""
    lines = [f"{method} {mask_url(url)}"]
    if status is not None:
        lines.append(f"Status: {status}")
    shown = _body(body)
    if shown:
        lines += ["", shown]
    return "\n".join(lines)


def mask_url(url):
    """``url`` with the values of ``TraID``, ``uid`` and ``d`` (any case)
    replaced by ``***``; a URL without a query string is returned unchanged."""
    url = url or ""
    base, sep, rest = url.partition("?")
    if not sep:
        return url
    query, hash_sep, fragment = rest.partition("#")
    masked = "&".join(_mask_pair(pair) for pair in query.split("&"))
    return f"{base}?{masked}{hash_sep}{fragment}"


def _mask_pair(pair):
    name, sep, _value = pair.partition("=")
    if name.lower() not in SECRET_PARAMETERS:
        return pair
    return f"{name}{sep or '='}{MASK}"


def _body(body):
    if not body:
        return ""
    if isinstance(body, str):
        return body
    data = bytes(body)
    if data.startswith(_ZIP_MAGIC):
        return _summary(data)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return _summary(data)


def _summary(data):
    return f"<binary body: {len(data)} bytes, sha256 {hashlib.sha256(data).hexdigest()}>"
