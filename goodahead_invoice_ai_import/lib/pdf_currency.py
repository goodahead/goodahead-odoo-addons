# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Does this PDF print a currency marking anywhere? Read from the text layer.

The model's own `currency_symbol` report can vary between runs on the same
document, so the country-based currency fallback asks the PDF instead. Uses
the `PdfReader` that Odoo already ships (pypdf or PyPDF2); no new dependency.

The answer is three-valued: a scanned or image-only PDF yields UNKNOWN, never
"nothing printed". Annotation and AcroForm values are read as well as the
content stream. Known blind spots, which is why the model's report is kept
beside this verdict: symbols drawn as vector art or in a font with a broken
`ToUnicode` map, currencies named only as words ("US Dollars"), and
annotations that carry only an `/AP` appearance stream.
"""

import io
import logging
import re

_logger = logging.getLogger(__name__)

# The rules lean toward reporting a marking: a false marking only blocks the
# fallback, while a missed one lets the countries pick the currency for a
# document that does print one. The exception is a bare ISO code, which must
# look like money, because many codes are also ordinary words or appear in
# routing fields (e.g. "ILS 40318").

# Characters whose only ordinary use on a commercial document is to mark money,
# matched anywhere. `$` has non-currency uses and gets its own rule below.
UNAMBIGUOUS_SYMBOLS = (
    "€"  # € euro
    "£"  # £ pound
    "¥"  # ¥ yen/yuan
    "￥"  # ￥ fullwidth yen
    "￡"  # ￡ fullwidth pound
    "₩"  # ₩ won
    "￦"  # ￦ fullwidth won
    "₽"  # ₽ rouble
    "₹"  # ₹ rupee
    "₪"  # ₪ new shekel
    "₫"  # ₫ dong
    "₺"  # ₺ lira
    "₴"  # ₴ hryvnia
    "₦"  # ₦ naira
    "₱"  # ₱ peso
    "฿"  # ฿ baht
    "৳"  # ৳ taka
    "﷼"  # ﷼ rial
    "₡"  # ₡ colón
    "₲"  # ₲ guaraní
    "₵"  # ₵ cedi
    "₸"  # ₸ tenge
    "₾"  # ₾ lari
    "₼"  # ₼ manat
    "₿"  # ₿ bitcoin
    "₨"  # ₨ rupee sign
    "¢"  # ¢ cent
    "₭"  # ₭ kip
    "₮"  # ₮ tugrik
    "₰"  # ₰ pfennig
    "៛"  # ៛ riel
    "¤"  # ¤ generic currency sign
    # Fullwidth and small dollar/cent forms have no non-currency use.
    "＄"  # ＄ fullwidth dollar   U+FF04
    "﹩"  # ﹩ small dollar       U+FE69
    "￠"  # ￠ fullwidth cent     U+FFE0
)
_UNAMBIGUOUS_RE = re.compile("[%s]" % re.escape(UNAMBIGUOUS_SYMBOLS))

# Thousands separators: space, comma, stop, no-break and narrow spaces, and the
# Swiss apostrophe in both forms.
_GROUP_SEP = "[ ,.\u00a0'\u2019\u202f\u2009]"

# An amount shaped like money: exactly two decimals, optionally grouped. This
# separates `USD 600.07` from `ILS 40318`.
_AMOUNT = (
    r"-?\d{1,3}(?:%(sep)s\d{3})*[.,]\d{2}\b-?|-?\d+[.,]\d{2}\b-?"
    % {"sep": _GROUP_SEP}
)

# `_AMOUNT` without the trailing `\b`, for `_YUAN_RE` only. CJK ideographs are
# `\w` to `re`, so `\b` fails between `1,234.00` and a directly following `元`.
_AMOUNT_TIGHT = (
    r"-?\d{1,3}(?:%(sep)s\d{3})*[.,]\d{2}-?|-?\d+[.,]\d{2}-?"
    % {"sep": _GROUP_SEP}
)

# An amount with no decimal part, used only by `_KRONA_RE`, which has a
# letter-adjacency guard. Too loose to put beside a bare ISO code.
_ROUND_AMOUNT = r"-?\d{1,3}(?:%(sep)s\d{3})+|-?\d+" % {"sep": _GROUP_SEP}

# `$` against a digit on either side. The trailing form must not be followed by
# a word character, so part numbers like `ZZQ-7710$K` do not match; the
# leading form has no guard, so `US$100` and `C$50` count.
_DOLLAR_RE = re.compile(r"\$\s{0,2}-?\d|\d\s{0,2}\$(?!\w)")

# `kr` and `元`/`円` need an amount beside them. Only `kr` also has a letter
# guard: Chinese text has no spaces between words, so a guard would drop
# ordinary totals such as `1,234.00元`. The resulting over-report (e.g.
# `公元 2026,00 年`) only blocks the fallback. An amount without decimals
# (`1234元整`) is not matched.
_KRONA_RE = re.compile(
    r"(?:(?:%(amt)s|%(round)s)\s{0,2}(?<![^\W\d_])kr\.?(?![^\W\d_])"
    r"|(?<![^\W\d_])kr\.?(?![^\W\d_])\s{0,2}(?:%(amt)s|%(round)s))"
    % {"amt": _AMOUNT, "round": _ROUND_AMOUNT},
    re.IGNORECASE,
)
_YUAN_RE = re.compile(
    r"(?:(?:%(tight)s)\s{0,2}(?P<after>[元円])"
    r"|(?P<before>[元円])\s{0,2}(?:%(amt)s))"
    % {"tight": _AMOUNT_TIGHT, "amt": _AMOUNT}
)

# Current ISO 4217 codes, frozen from CLDR's `territory_currencies` (rows with
# no end date). Historical codes are left out: they add words such as `BAD` or
# `SUR` to mistake for money.
ISO_4217_CURRENT = frozenset((
    "AED", "AFN", "ALL", "AMD", "AOA", "ARS", "AUD", "AWG", "AZN", "BAM", "BBD", "BDT",
    "BGN", "BHD", "BIF", "BMD", "BND", "BOB", "BRL", "BSD", "BTN", "BWP", "BYN", "BZD",
    "CAD", "CDF", "CHF", "CLP", "CNY", "COP", "CRC", "CUP", "CVE", "CZK", "DJF", "DKK",
    "DOP", "DZD", "EGP", "ERN", "ETB", "EUR", "FJD", "FKP", "GBP", "GEL", "GHS", "GIP",
    "GMD", "GNF", "GTQ", "GYD", "HKD", "HNL", "HTG", "HUF", "IDR", "ILS", "INR", "IQD",
    "IRR", "ISK", "JMD", "JOD", "JPY", "KES", "KGS", "KHR", "KMF", "KPW", "KRW", "KWD",
    "KYD", "KZT", "LAK", "LBP", "LKR", "LRD", "LSL", "LYD", "MAD", "MDL", "MGA", "MKD",
    "MMK", "MNT", "MOP", "MRU", "MUR", "MVR", "MWK", "MXN", "MYR", "MZN", "NAD", "NGN",
    "NIO", "NOK", "NPR", "NZD", "OMR", "PAB", "PEN", "PGK", "PHP", "PKR", "PLN", "PYG",
    "QAR", "RON", "RSD", "RUB", "RWF", "SAR", "SBD", "SCR", "SDG", "SEK", "SGD", "SHP",
    "SLE", "SOS", "SRD", "SSP", "STN", "SYP", "SZL", "THB", "TJS", "TMT", "TND", "TOP",
    "TRY", "TTD", "TWD", "TZS", "UAH", "UGX", "USD", "UYU", "UZS", "VES", "VND", "VUV",
    "WST", "XAF", "XCD", "XCG", "XOF", "XPF", "YER", "ZAR", "ZMW",
))

_CODE = "(?:%s)" % "|".join(sorted(ISO_4217_CURRENT))

# A bare ISO code counts only next to a money-shaped amount (two decimals).
# Many codes are also words or abbreviations (ALL, TOP, CUP, TRY, ILS …) and
# appear in carrier routing fields such as `ILS 40318`. Known gap: a code with
# a round amount and no announcing word (`CAD 500` alone) is not a marking,
# and the country fallback may then write the domestic currency.
_CODE_NEAR_AMOUNT_RE = re.compile(
    r"\b(%(code)s)\b[^\w\n]{0,4}(?=%(amt)s)"
    r"|(?:%(amt)s)[^\w\n]{0,4}\b(%(code)s)\b" % {"code": _CODE, "amt": _AMOUNT}
)

# Phrases that announce a currency code (`CHECKS IN USD:`), so no amount is
# required. Kept specific: a bare "in" would also match `IN ALL`.
_CODE_AFTER_CURRENCY_WORD_RE = re.compile(
    r"(?:currency|currencies|denominated\s+in|payable\s+in|paid\s+in|billed\s+in"
    r"|invoiced\s+in|quoted\s+in|amounts?\s+(?:are\s+)?in|totals?\s+in"
    r"|checks?\s+in|cheques?\s+in|funds?\s+in|remit\s+in)"
    r"[^\w\n]{0,4}\b(%(code)s)\b" % {"code": _CODE},
    re.IGNORECASE,
)

# Amount words (`total`, `balance`, `amount due`) before a code, followed by a
# number of any shape (`Total: CAD 500`). The number is required so that table
# headers such as `Totals TRY` do not count.
_CODE_AFTER_AMOUNT_WORD_RE = re.compile(
    r"(?:(?:sub|grand\s+)?totals?|balances?|amounts?\s+due)"
    r"[^\w\n]{0,4}\b(%(code)s)\b[^\w\n]{0,4}(?=-?\d)" % {"code": _CODE},
    re.IGNORECASE,
)

# How many distinct markings to keep in the stored verdict.
MAX_RECORDED_MARKINGS = 8

# Minimum alphanumeric characters in the content stream before "nothing
# printed" is believed; below it the answer is UNKNOWN (a scan with a stamp or
# page number). Real one-page invoices have several hundred. It guards only the
# negative answer: a found marking is evidence on its own. Annotation and form
# text does not count toward it, so a filled field cannot make a scan look like
# it has a text layer.
MIN_TEXT_LAYER_ALNUM = 120

# The three verdicts, stored as strings in the extraction JSON.
PRINTED = "printed"
NOTHING_PRINTED = "nothing_printed"
UNKNOWN = "unknown"


def currency_markings_in_text(text):
    """Every distinct currency marking in ``text``, in rule order (not document order).

    Pure function: no PDF, no Odoo, no I/O.
    """
    if not isinstance(text, str) or not text:
        return []
    found = []
    seen = set()

    def _keep(marking):
        marking = marking.strip()
        if not marking:
            return
        key = marking.upper()
        if key in seen:
            return
        seen.add(key)
        found.append(marking)

    for match in _UNAMBIGUOUS_RE.finditer(text):
        _keep(match.group(0))
    if _DOLLAR_RE.search(text):
        _keep("$")
    if _KRONA_RE.search(text):
        _keep("kr")
    for match in _YUAN_RE.finditer(text):
        # The character, not the amount it was found beside — one branch has it
        # after the number and the other before.
        _keep(match.group("after") or match.group("before"))
    # Each of these has the code in one of its alternatives, so exactly one
    # group is set per match.
    for regex in (
        _CODE_NEAR_AMOUNT_RE,
        _CODE_AFTER_CURRENCY_WORD_RE,
        _CODE_AFTER_AMOUNT_WORD_RE,
    ):
        for match in regex.finditer(text):
            for group in match.groups():
                if group:
                    _keep(group)
    return found


# Caps on annotation input, far above any real document, so a huge `/Annots`
# array or `/Contents` string cannot make the regex scan slow. The content
# stream itself is not capped.
MAX_ANNOTATIONS_PER_PAGE = 2000
MAX_ANNOTATION_CHARS = 20000


def _annotation_string(value):
    """One annotation or field value as text, whatever pypdf handed back."""
    try:
        value = value.get_object()
    except AttributeError:
        pass
    if isinstance(value, str):
        return value[:MAX_ANNOTATION_CHARS]
    if not isinstance(value, bytes):
        # A field value may legally be held in a stream object.
        get_data = getattr(value, "get_data", None)
        if get_data is None:
            return ""
        try:
            value = get_data()
        except Exception:  # noqa: BLE001 - an unreadable stream is not a file
            return ""
        if not isinstance(value, bytes):
            return ""
    # UTF-16 PDF strings carry a BOM by specification; without one, Python's
    # utf-16 codec would assume little-endian and produce mojibake. So decode
    # UTF-16 only on a BOM, then try UTF-8, then Latin-1 (which never fails).
    if value[:2] in (b"\xfe\xff", b"\xff\xfe"):
        try:
            return value.decode("utf-16")[:MAX_ANNOTATION_CHARS]
        except UnicodeDecodeError:
            pass
    try:
        return value.decode("utf-8")[:MAX_ANNOTATION_CHARS]
    except UnicodeDecodeError:
        return value.decode("latin-1")[:MAX_ANNOTATION_CHARS]


def _annotation_text(reader):
    """Every string an annotation or AcroForm field puts on these pages.

    `extract_text()` does not walk `/Annots`, so a currency typed into a
    fillable bill would otherwise be missed. All annotation subtypes are
    counted (comments and stamps included), except hidden ones (`/F` bit 2;
    NoView annotations still print). `/DV` is read only when there is no `/V`.
    `/AP` appearance streams are not read.
    """
    out = []
    seen = set()

    def _add(value):
        text = _annotation_string(value).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)

    def _value_key(node):
        # With both present, the renderer shows `/V`.
        return "/V" if "/V" in node else "/DV"

    for page in reader.pages:
        try:
            annots = page.get("/Annots") or []
            try:
                annots = annots.get_object()
            except AttributeError:
                pass
            for ref in list(annots)[:MAX_ANNOTATIONS_PER_PAGE]:
                try:
                    annot = ref.get_object()
                except Exception:  # noqa: BLE001 - a broken annot is not a file
                    continue
                if not hasattr(annot, "get"):
                    continue
                try:
                    hidden = bool(int(annot.get("/F") or 0) & 2)
                except (TypeError, ValueError):
                    hidden = False
                if hidden:
                    continue
                for key in ("/Contents", _value_key(annot)):
                    if key in annot:
                        _add(annot[key])
        except Exception:  # noqa: BLE001 - one bad page is not a bad file
            continue

    # The AcroForm tree as well, because a field's `/V` can live on a PARENT
    # field that no page annotation repeats.
    try:
        fields = reader.trailer["/Root"]["/AcroForm"]["/Fields"]
        try:
            fields = fields.get_object()
        except AttributeError:
            pass
        stack = list(fields)
        # Counts pops, bounding the walk against a cyclic `/Kids` graph.
        visited = 0
        while stack and visited < 10000:
            visited += 1
            node = stack.pop()
            try:
                node = node.get_object()
            except AttributeError:
                pass
            if not hasattr(node, "get"):
                continue
            key = _value_key(node)
            if key in node:
                _add(node[key])
            kids = node.get("/Kids")
            if kids:
                try:
                    kids = kids.get_object()
                except AttributeError:
                    pass
                stack.extend(kids)
    except Exception:  # noqa: BLE001 - no AcroForm, or an unreadable one
        pass
    return "\n".join(out)


def pdf_text_fragments(pdf_bytes):
    """``(content_stream_text, annotation_text)``, or None when unreadable.

    None (unknown) for an encrypted, truncated or malformed file. The halves
    are kept apart because only the first counts toward `MIN_TEXT_LAYER_ALNUM`;
    both are scanned for markings.
    """
    if not pdf_bytes:
        return None
    # Imported here so the module stays importable without Odoo. Odoo's reader
    # uses `strict=True`; a malformed PDF that raises yields UNKNOWN.
    from odoo.tools.pdf import PdfReader  # noqa: PLC0415

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - one bad page is not a bad file
                continue
        return "\n".join(pages), _annotation_text(reader)
    except Exception as error:  # noqa: BLE001
        # The model decides instead.
        _logger.warning(
            "AI invoice import: could not read the PDF's text layer (%s) — the "
            "currency-marking check falls back to the model's own report",
            error,
        )
        return None


def scan_pdf_for_currency(pdf_bytes):
    """Does this PDF print a currency marking? ``{"verdict", "markings", "alnum"}``.

    ``verdict`` is `PRINTED`, `NOTHING_PRINTED` or `UNKNOWN`; ``alnum`` is the
    content stream's alphanumeric character count. Markings are checked before
    the text-layer threshold, which guards only the negative answer.
    """
    fragments = pdf_text_fragments(pdf_bytes)
    if fragments is None:
        return {"verdict": UNKNOWN, "markings": [], "alnum": 0}
    text, annotations = fragments
    alnum = sum(1 for char in text if char.isalnum())
    markings = currency_markings_in_text(
        "\n".join(part for part in (text, annotations) if part)
    )
    if markings:
        return {
            "verdict": PRINTED,
            "markings": markings[:MAX_RECORDED_MARKINGS],
            "alnum": alnum,
        }
    if alnum < MIN_TEXT_LAYER_ALNUM:
        # A scan or image-only export: expected, so INFO rather than WARNING.
        _logger.info(
            "AI invoice import: the PDF has no usable text layer (%d "
            "alphanumeric characters, %d needed) and no currency marking in "
            "its annotations — the currency-marking check falls back to the "
            "model's own report",
            alnum,
            MIN_TEXT_LAYER_ALNUM,
        )
        return {"verdict": UNKNOWN, "markings": [], "alnum": alnum}
    return {"verdict": NOTHING_PRINTED, "markings": [], "alnum": alnum}
