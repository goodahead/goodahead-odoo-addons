# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Split a Finvoice file into messages, detect versions, validate against the XSD.

🔴 A FINVOICE FILE IS NOT ONE XML DOCUMENT. guideline §17: "The file sent to the
service provider may include several Finvoice messages. Each message must
include a transmission frame (SOAP)", and §17.1: "The actual invoice is
transmitted outside the frame." The §17.2 example shows a SOAP-ENV:Envelope
followed by a second ``<?xml ...?>`` declaration and the ``<Finvoice>`` root —
so a bank or operator file is several XML documents concatenated, which no XML
parser accepts as-is. ``parse`` therefore removes the XML declarations, wraps
the whole text in one synthetic root and collects every ``Finvoice`` element,
which handles:

- one bare message (the common APIX package),
- SOAP frame + message pairs, one or many (§17),
- a message placed inside a SOAP Body or any other wrapper (observed).

Element lookup is namespace-agnostic (by local name): Finvoice itself has no
namespace (§2, ``xsi:noNamespaceSchemaLocation``), but some senders add a
default one (observed).
"""

import functools
import os
import re
from dataclasses import dataclass, field

from lxml import etree

from .errors import UNREADABLE, UNSUPPORTED_VERSION, FinvoiceError

# guideline §2: the four versions the schema has been used for, all accepted.
# 1.3 since spec AC-33: *observed*, operators still deliver it, and every
# element read here has the same name in 1.3 as in 2.0.
SUPPORTED_VERSIONS = ("1.3", "2.0", "2.01", "3.0")

XSD_PATH = os.path.join(os.path.dirname(__file__), "schema", "Finvoice3.0.xsd")

#: The largest file read at all. Checked before anything else looks at the
#: bytes: a Finvoice file comes from outside (mail, operator, upload), and
#: every later step costs time in proportion to its size. A real Finvoice
#: file with hundreds of invoices stays far below this. Callers may pass a
#: smaller ``max_bytes`` (the receiving add-ons read theirs from a system
#: parameter).
MAX_FILE_BYTES = 20 * 1024 * 1024

# Declarations and DOCTYPEs are found with plain linear scans, not with
# regular expressions over the whole file: a pattern like
# ``<\?xml\s[^>]*?\?>`` retried at every unclosed opener costs time in the
# square of the file size, so a small crafted file could hold a worker for
# hours. Only the tail of an encoding declaration is matched with a regular
# expression, anchored at a position the scan found.
_XML_DECL_OPEN = "<?xml"
_XML_DECL_OPEN_BYTES = b"<?xml"
_DECL_ENCODING_TAIL_RE = re.compile(rb"""encoding\s*=\s*["']([A-Za-z0-9._-]+)["']""")
_DOCTYPE_OPEN_RE = re.compile(r"<!DOCTYPE", re.IGNORECASE)
_ASCII_SPACE = b" \t\n\r\x0b\x0c"
_WRAPPER = "FinvoiceFileWrapper"


@dataclass
class FinvoiceMessage:
    """One Finvoice message from a file.

    ``element`` is the ``Finvoice`` root (lxml), ``version`` its Version
    attribute, ``index`` its 0-based position in the file and ``xml`` the
    message serialised on its own (UTF-8 bytes), which is what is stored and
    validated.
    """

    version: str
    element: object = field(repr=False)
    index: int = 0
    xml: bytes = field(default=b"", repr=False)


def parse(xml_bytes, max_bytes=None):
    """Return the Finvoice messages in ``xml_bytes``, in file order.

    Raises ``FinvoiceError``: ``finvoice.unreadable`` when the bytes are not XML
    or hold no Finvoice message, or are larger than ``max_bytes`` (default
    ``MAX_FILE_BYTES``), ``finvoice.unsupported_version`` when any
    message is not version 1.3, 2.0, 2.01 or 3.0. A file is accepted or refused as a
    whole, so a batch never imports half-way.
    """
    roots = _finvoice_roots(xml_bytes, max_bytes)
    messages = []
    for index, root in enumerate(roots):
        version = (root.get("Version") or "").strip()
        if version not in SUPPORTED_VERSIONS:
            shown = version or "missing"
            raise FinvoiceError(
                UNSUPPORTED_VERSION,
                f"Finvoice version {shown} is not supported; only 1.3, 2.0, 2.01 "
                f"and 3.0 can be imported."
                + (f" (message {index + 1} of {len(roots)})" if len(roots) > 1 else ""),
                field="version",
            )
        messages.append(
            FinvoiceMessage(
                version=version,
                element=root,
                index=index,
                xml=etree.tostring(root, encoding="UTF-8", xml_declaration=True),
            )
        )
    return messages


def validate_schema(xml_bytes, xsd_path=None):
    """Validate every Finvoice 3.0 message in ``xml_bytes`` against the XSD.

    Returns a list of ``SchemaProblem`` dicts ``{path, line, message}``, empty
    when all 3.0 messages are valid.
    ``path`` is the element path inside the message, e.g.
    ``/Finvoice/BuyerPartyDetails/BuyerOrganisationName`` (empty when the
    validator names no element); ``line`` is message-relative. Messages of
    other versions are not checked (there is no reference schema for them
    here). Receiving treats a non-empty list as a warning, sending as a hard
    stop.
    """
    return [problem for _index, _count, problem in _schema_problems(xml_bytes, xsd_path)]


def validate_schema_text(xml_bytes, xsd_path=None):
    """The reader's original shape of ``validate_schema``: one string per problem.

    ``"line N: message"``, prefixed with ``"message K: "`` when the file holds
    several messages (receiving contracts/public-api.md §2: "3.0 only"; a
    warning, never a hard stop for a received invoice).
    """
    problems = []
    for index, count, problem in _schema_problems(xml_bytes, xsd_path):
        prefix = f"message {index + 1}: " if count > 1 else ""
        problems.append(f"{prefix}line {problem['line']}: {problem['message']}")
    return problems


def _schema_problems(xml_bytes, xsd_path):
    """Yield ``(message index, message count, SchemaProblem)`` per XSD error."""
    schema = _load_schema(xsd_path or XSD_PATH)
    roots = _finvoice_roots(xml_bytes)
    for index, root in enumerate(roots):
        if (root.get("Version") or "").strip() != "3.0":
            continue
        # Re-parse the message on its own so line numbers and element paths
        # are message-relative and nothing from the wrapper or a SOAP frame
        # takes part.
        doc = etree.fromstring(etree.tostring(root), _safe_parser())
        if schema.validate(doc):
            continue
        for error in schema.error_log:
            problem = {"path": error.path or "", "line": error.line, "message": error.message}
            yield index, len(roots), problem


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _safe_parser():
    # No DTD, no entity expansion, no network: the file comes from outside.
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
        remove_comments=True,
    )


@functools.lru_cache(maxsize=4)
def _load_schema(path):
    return etree.XMLSchema(etree.parse(path, _safe_parser()))


def _decode(xml_bytes):
    """Bytes -> text, honouring the declared encoding.

    guideline §9.3: Finvoice uses ISO 8859-15; UTF-8 is common in practice
    (observed). The declared encoding is tried first, then UTF-8, then
    ISO-8859-15 (which decodes any byte string).
    """
    if xml_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return xml_bytes.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise FinvoiceError(UNREADABLE, "The file is not readable text.") from exc
    if xml_bytes.startswith(b"\xef\xbb\xbf"):
        xml_bytes = xml_bytes[3:]
    candidates = []
    for name in _declared_encodings(xml_bytes):
        name = name.decode("ascii").lower()
        if name not in candidates:
            candidates.append(name)
    candidates += ["utf-8", "iso-8859-15"]
    for name in candidates:
        try:
            return xml_bytes.decode(name)
        except (UnicodeDecodeError, LookupError):
            continue
    raise FinvoiceError(UNREADABLE, "The file is not readable text.")  # pragma: no cover


def _declared_encodings(data):
    """The encoding names declared in ``data`` (bytes), in file order.

    Same result as matching ``<\\?xml\\s[^>]*?encoding\\s*=\\s*["']([A-Za-z0-9._-]+)["']``
    over the whole file, in linear time: each declaration is looked at only
    up to the first ``>`` after it, and openers that share that ``>`` with
    one already refused are skipped.
    """
    names = []
    size = len(data)
    search = 0
    gt = -1
    while True:
        start = data.find(_XML_DECL_OPEN_BYTES, search)
        if start < 0:
            break
        body = start + 6
        if start + 5 >= size or data[start + 5] not in _ASCII_SPACE:
            search = start + 1
            continue
        if gt < body:
            gt = data.find(b">", body)
        end = gt if gt >= 0 else size
        found = None
        at = data.find(b"encoding", body, end)
        while at >= 0:
            match = _DECL_ENCODING_TAIL_RE.match(data, at, end)
            if match:
                found = match
                break
            at = data.find(b"encoding", at + 1, end)
        if found:
            names.append(found.group(1))
            search = found.end()
            continue
        if gt < 0:
            break
        # Every opener before ``gt - 5`` sees the same declaration body up
        # to the same ``>``, so it is refused for the same reason.
        search = max(start + 1, gt - 5)
    return names


def _strip_xml_declarations(text):
    """``text`` without its ``<?xml ...?>`` declarations.

    Same result as ``re.sub(r"<\\?xml\\s[^>]*?\\?>", "", text)``, in linear
    time. A declaration ends at the first ``>`` after its opener, and only
    counts when that ``>`` follows a ``?``.
    """
    parts = []
    size = len(text)
    kept = 0
    search = 0
    gt = -1
    while True:
        start = text.find(_XML_DECL_OPEN, search)
        if start < 0:
            break
        if start + 5 >= size or not text[start + 5].isspace():
            search = start + 1
            continue
        if gt < start + 6:
            gt = text.find(">", start + 6)
        if gt < 0:
            # No ``>`` after this opener, so none after any later one either.
            break
        if gt >= start + 7 and text[gt - 1] == "?":
            parts.append(text[kept:start])
            kept = search = gt + 1
            continue
        search = start + 1
    parts.append(text[kept:])
    return "".join(parts)


def _strip_doctypes(text):
    """``text`` without its DOCTYPE declarations.

    Same result as ``re.sub(r"<!DOCTYPE[^>\\[]*(\\[[^\\]]*\\])?\\s*>", "", text,
    flags=re.IGNORECASE)``, in linear time: the next ``>``, ``[`` and ``]``
    are found once and reused by every opener that shares them.
    """
    parts = []
    size = len(text)
    kept = 0
    search = 0
    next_gt = next_lb = next_rb = -2
    after_rb = {}
    while True:
        opener = _DOCTYPE_OPEN_RE.search(text, search)
        if not opener:
            break
        start, body = opener.start(), opener.end()
        if next_gt != -1 and next_gt < body:
            next_gt = text.find(">", body)
        if next_lb != -1 and next_lb < body:
            next_lb = text.find("[", body)
        stops = [pos for pos in (next_gt, next_lb) if pos >= body]
        if not stops:
            # Neither ``>`` nor ``[`` follows: no later opener can end either.
            break
        stop = min(stops)
        end = None
        if text[stop] == ">":
            end = stop + 1
        else:
            if next_rb != -1 and next_rb <= stop:
                next_rb = text.find("]", stop + 1)
            if next_rb >= 0:
                if next_rb not in after_rb:
                    pos = next_rb + 1
                    while pos < size and text[pos].isspace():
                        pos += 1
                    after_rb[next_rb] = pos
                pos = after_rb[next_rb]
                if pos < size and text[pos] == ">":
                    end = pos + 1
        if end is None:
            search = start + 1
            continue
        parts.append(text[kept:start])
        kept = search = end
    parts.append(text[kept:])
    return "".join(parts)


def _finvoice_roots(xml_bytes, max_bytes=None):
    if isinstance(xml_bytes, str):
        xml_bytes = xml_bytes.encode("utf-8")
    if not isinstance(xml_bytes, (bytes, bytearray)):
        raise FinvoiceError(UNREADABLE, "The file is empty, not a Finvoice message.")
    limit = MAX_FILE_BYTES if max_bytes is None else max_bytes
    if len(xml_bytes) > limit:
        # Before anything else reads the bytes, so a huge file costs nothing.
        raise FinvoiceError(
            UNREADABLE,
            f"The file is {len(xml_bytes) // 1024} KB, larger than the "
            f"{limit // 1024} KB a Finvoice file may have here, so it was not read.",
        )
    if not bytes(xml_bytes).strip():
        raise FinvoiceError(UNREADABLE, "The file is empty, not a Finvoice message.")
    xml_bytes = bytes(xml_bytes)
    if b"<" not in xml_bytes:
        raise FinvoiceError(UNREADABLE, "The file is not XML, so not a Finvoice message.")

    # Declarations are matched on the bytes (they are ASCII) before decoding,
    # and removed from the decoded text: several of them in one file is exactly
    # the §17 multi-message layout.
    text = _decode(xml_bytes)
    text = _strip_xml_declarations(text)
    text = _strip_doctypes(text)
    wrapped = f"<{_WRAPPER}>{text}</{_WRAPPER}>".encode("utf-8")
    try:
        wrapper = etree.fromstring(wrapped, _safe_parser())
    except etree.XMLSyntaxError as exc:
        raise FinvoiceError(
            UNREADABLE,
            f"The file is not well-formed XML, so not a readable Finvoice message ({exc}).",
        ) from exc

    roots = []
    for element in wrapper.iter():
        if not isinstance(element.tag, str):
            continue
        if etree.QName(element).localname != "Finvoice":
            continue
        # A Finvoice element nested inside another one (e.g. in
        # ExternalSpecificationDetails, which allows any content) is not a
        # message of its own.
        if any(
            isinstance(a.tag, str) and etree.QName(a).localname == "Finvoice"
            for a in element.iterancestors()
        ):
            continue
        roots.append(element)
    if not roots:
        raise FinvoiceError(
            UNREADABLE,
            "The file is XML but holds no Finvoice message (no <Finvoice> element).",
        )
    return roots


# ---------------------------------------------------------------------------
# Namespace-agnostic lookup helpers, shared with mapping.py
# ---------------------------------------------------------------------------


def children(element, name):
    """Direct children named ``name`` (any namespace)."""
    if element is None:
        return []
    return [c for c in element if isinstance(c.tag, str) and etree.QName(c).localname == name]


def find_all(element, path):
    """All elements at a ``A/B/C`` path below ``element`` (any namespace)."""
    current = [element] if element is not None else []
    for step in path.split("/"):
        current = [c for parent in current for c in children(parent, step)]
    return current


def find(element, path):
    found = find_all(element, path)
    return found[0] if found else None


def text(element, path=None):
    """Stripped text at ``path`` (or of ``element``), None when absent or empty."""
    node = find(element, path) if path else element
    if node is None or node.text is None:
        return None
    value = node.text.strip()
    return value or None


def texts(element, path):
    """Every non-empty stripped text at ``path``."""
    result = []
    for node in find_all(element, path):
        if node.text and node.text.strip():
            result.append(node.text.strip())
    return result
