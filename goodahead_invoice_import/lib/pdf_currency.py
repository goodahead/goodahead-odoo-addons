# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Does this PDF print a currency marking anywhere? Asked of the BYTES.

🔴 WHY THIS FILE EXISTS. 19.0.3.3.0 made a domestic bill that prints no
currency resolvable from the two countries on it, and gated the whole rule on
`currency_symbol` — a field the MODEL reports, meaning "what the page printed
beside the amounts, or null". Any non-empty value closes the gate, deliberately.

Measured on a test import: ZZ Freight LLC bill `000000000001` came back
with `currency_symbol: "USD"`. That page prints no currency marking at all —
`612.84`, `4,218.00`, `2109.00`, `131.70`, `3,758.86-`, every one of them bare,
confirmed over both pages with `pdftotext` and by eye in the preview. So the
model reported a code that is not on the document, the gate closed, and the
chatter note that is the entire visibility of the feature was never posted. The
currency was right and it arrived unmarked, which is the exact silence the
feature exists to remove.

🔴 AND THE SAME DOCUMENT ANSWERED DIFFERENTLY TWICE. An earlier live
run on this bill returned `currency_symbol: null`; a later run
returned `"USD"`. One model-reported observation, seen to flip on identical
bytes, was the single hinge of a rule about money.

"Does this page print a currency marking" is not a judgement. It is a
deterministic question about bytes, and this project has answered it with
`pdftotext` three times in a week to check the model's claims. So ask the
document.

🔴 NO NEW DEPENDENCY. `odoo.tools.pdf` re-exports `PdfReader` from whichever of
pypdf / PyPDF2 the server has (`odoo/tools/pdf/__init__.py:44-54`), and pypdf is
a hard Odoo requirement — `merge_pdf`, `rotate_pdf` and the signature code all
sit on it. `ir.attachment._index` was checked first and is no help: it returns
None for anything whose mimetype does not start with `text/`
(`base/models/ir_attachment.py:434-442`), so a PDF is never indexed at all.
Nothing is added to `external_dependencies`, which matters for a module about to
be open-sourced.

🔴 THE THREE-VALUED ANSWER, and the third value is the point. A scanned or
image-only PDF has no text layer, and the honest answer about it is UNKNOWN,
not "nothing printed" — reading "nothing" off an empty text layer would open the
gate on every scan in the building. `scan_pdf_for_currency` returns a verdict
that names its own source, it rides in the stored extraction JSON, and a year
from now an audit can tell which source decided.

⚠️ WHAT THIS IS NOT, AND IT IS WORTH BEING HONEST ABOUT. The text layer is not
the rendered page. A currency sign drawn as a vector path, or set in a font
whose `ToUnicode` map is wrong for that one glyph, is printed and is not in the
text. That is the dangerous direction, produced by this mechanism rather than by
the model — so it is named rather than waved away.

🔴 AND THE BOUNDS THAT PARAGRAPH USED TO CLAIM WERE BOTH WRONG (19.0.3.19.0).
It said two things bounded the whole class: that a broken font garbles the WHOLE
page and so trips `MIN_TEXT_LAYER_ALNUM`, and that the currency rule needs two
further yeses before it writes anything. A review built the counterexample to the
first and read the resolver for the second:

* A currency typed into a FORM FIELD or drawn by an ANNOTATION is printed, is
  not in the content stream, and leaves a perfectly ordinary un-garbled page
  far over the threshold. Measured here: body 235 alnum, `/FreeText` annotation
  drawing `$1,234.00`, verdict `nothing_printed`, markings `[]` — on pypdf
  6.20.0 and PyPDF2 2.12.1 alike. Fillable vendor bills are ordinary and Odoo
  ships `fill_form_fields_pdf` in the very module this file reads PDFs with.
  **So annotations and AcroForm values are now read too** — see
  `pdf_text_fragments`. The font/vector case stays genuinely unbounded, and is
  named below rather than bounded.
* The second yes — "an already-set currency must equal that country's own" — is
  guarded by `already_set` in `claude_client._resolve_currency_from_country`,
  so on the branch that WRITES a currency it is never asked. The honest
  statement of the bound is in `_CODE_NEAR_AMOUNT_RE` below, where the one
  deliberate under-report is argued, and it no longer claims two yeses.

What is left genuinely unbounded, and is the reason the model's own reading is
kept beside this one in the stored verdict:

* a symbol drawn as VECTOR ART, or set in a font whose `ToUnicode` map is wrong
  for that one glyph;
* a currency named only as a WORD (`"All amounts are in US Dollars"`, `Euro`,
  `kronor`) — nothing in this file matches a spelled-out currency;
* 🆕 an annotation that carries ONLY an `/AP` APPEARANCE STREAM (19.0.3.20.0,
  review). `_annotation_text` reads `/Contents`, `/V` and `/DV`; it does
  not execute the appearance stream those render from, so a flattened stamp
  drawing `PAID $1,234.00` with no `/Contents` prints money and answers
  `nothing_printed`. Measured, and pinned by
  `test_the_annotation_hole_that_is_still_open`.

It is a second instrument, not a retired one, and a disagreement is logged at
WARNING either way.
"""

import io
import logging
import re

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# What counts as a currency marking
# ---------------------------------------------------------------------------
#
# 🔴 WHICH WAY TO ERR, DECIDED BEFORE THE FIRST REGEX WAS WRITTEN.
#
# Over-reporting — calling a marking on a page that has none — closes the gate
# and costs exactly today's behaviour: the import either fails loudly with the
# JSON stored for hand-editing, or succeeds with the model's own currency and no
# chatter note. Nobody is misled.
#
# Under-reporting — missing a marking that IS printed — opens the gate on a
# document the model could not settle, and lets the two countries pick a
# currency instead. That is the `¥`-from-Shanghai case 19.0.2.3.0 made terminal
# on purpose, and it is a factor of about twenty.
#
# So the bias is TOWARD reporting a marking. Every judgement call below goes
# that way, with ONE deliberate exception which is argued where it is made: a
# bare three-letter ISO code must look like money, because the document this
# whole feature exists for prints `ILS 30572` in a ZZ Freight LLC routing field and
# `ILS` is the Israeli new shekel. A list that matched it would close the gate
# on the one bill the fix is for, which is not caution — it is the bug again
# wearing the safe colour.

# Characters whose ONLY ordinary use on a commercial document is to mark money.
# Matched wherever they appear, with no adjacency requirement: a `£` in a part
# number is not a thing, and if it ever is, over-reporting is the cheap side.
#
# `$` is NOT here. It is the one symbol with real non-currency use (shell and
# template syntax pasted into a description, `$` in a part or SKU code), so it
# gets the digit-adjacency rule below.
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
    # THE DOLLAR CONFUSABLES, added 19.0.3.19.0 (R6). `$` itself is
    # not on this list because it has real non-currency use; these three have
    # none — they are CJK-typography forms, and nobody writes a shell
    # variable or a part number with U+FF04. Measured before the change:
    # `TOTAL ＄1,234.00` → `[]`, which is the dangerous direction. Five
    # fullwidth forms were already here (￥￡￦…), so this is the list being
    # consistent with itself rather than a new policy.
    "＄"  # ＄ fullwidth dollar   U+FF04
    "﹩"  # ﹩ small dollar       U+FE69
    "￠"  # ￠ fullwidth cent     U+FFE0
)
_UNAMBIGUOUS_RE = re.compile("[%s]" % re.escape(UNAMBIGUOUS_SYMBOLS))

# What groups an amount's thousands. Plain space, comma, stop and U+00A0 were
# here from the start; the Swiss apostrophe (`1'234.55`, and its typographic
# twin `1’234.55`) and the two narrow spaces a European generator emits
# (U+202F, U+2009) were added in 19.0.3.19.0, after review measured
# `Total CHF 1'234.55` → `[]` while `1'234.55 CHF` matched. The two spellings
# of one amount disagreeing is what marks it as an oversight rather than a
# decision.
_GROUP_SEP = "[ ,.\u00a0'\u2019\u202f\u2009]"

# An amount that is SHAPED like money: a decimal part of exactly two digits,
# optionally grouped. Every amount on all eight measured invoices has one —
# `263.17`, `4,218.00`, `3,758.86-`, `-2.30`, `1 234,56`. It is what separates
# `USD 612.84` from `ILS 30572`.
_AMOUNT = (
    r"-?\d{1,3}(?:%(sep)s\d{3})*[.,]\d{2}\b-?|-?\d+[.,]\d{2}\b-?"
    % {"sep": _GROUP_SEP}
)

# 🔴 THE SAME AMOUNT WITH NO TRAILING `\b`, AND IT EXISTS BECAUSE `\b` CANNOT
# HOLD BETWEEN A DIGIT AND A CJK IDEOGRAPH (19.0.3.20.0).
#
# `元`, `円` and every CJK ideograph are `\w` to Python's `re`, so `\b` between
# the `0` of `1,234.00` and the `元` that follows it FAILS — two word
# characters have no boundary between them. Measured before this change, all
# three on the dangerous side:
#
#     `1,234.00元`     -> []        <- the ordinary way a Chinese total prints
#     `合计 1,234.00元` -> []
#     `1234.00円`      -> []
#     `1,234.00 元`    -> ['元']    <- only with a space
#
# So the `after` branch of `_YUAN_RE` was dead for every TIGHT form, which is
# the form that is actually printed. `kr` escapes the identical trap by
# accident: 19.0.3.19.0's `_ROUND_AMOUNT` alternative contains `-?\d+`, which
# matches the bare `00` of `1 234,00kr` and lands against the `k`. `_YUAN_RE`
# got no such alternative and therefore no such rescue.
#
# Dropping the `\b` is safe HERE and nowhere else, because the only caller
# constrains what may follow: at most two spaces and then `[元円]`. It is not
# used by `_CODE_NEAR_AMOUNT_RE`, where the boundary is the whole point.
_AMOUNT_TIGHT = (
    r"-?\d{1,3}(?:%(sep)s\d{3})*[.,]\d{2}-?|-?\d+[.,]\d{2}-?"
    % {"sep": _GROUP_SEP}
)

# An amount with NO decimal part: `1 500`, `15'000`, `2026`. Used in exactly
# one place — `_KRONA_RE` — where a letter-adjacency guard already stands
# between it and ordinary prose. It is far too loose to put beside a bare ISO
# code, which is the whole argument of `_CODE_NEAR_AMOUNT_RE` below.
_ROUND_AMOUNT = r"-?\d{1,3}(?:%(sep)s\d{3})+|-?\d+" % {"sep": _GROUP_SEP}

# `$` counts when it sits against an amount on either side. The amount here is
# deliberately LOOSER than `_AMOUNT` — `$5` and `$ 1,200` are currency markings
# whatever their decimals — because `$` is a symbol and the two-decimal rule
# exists only to disambiguate three-letter WORDS.
#
# ⚠️ `(?!\w)` ON THE TRAILING FORM ONLY, and it was added because a real part
# number failed: `ACSS-4832$B` on the Fabrikam invoice's own catalogue has a
# digit immediately before a `$`. A trailing `$` ends a token (`1 234,00 $`);
# one with a letter after it is inside a code. The LEADING form keeps no such
# guard on purpose — `US$100`, `C$50` and `A$12` are all currency, and a part
# number of the shape `X$1` is rarer than those are common, so the leading form
# stays on the over-reporting side where the file's bias belongs.
#
# `\s` is Unicode-aware on `str` patterns, so it already covers U+00A0, U+202F
# and U+2009 — the narrow-space hole review found was in `_AMOUNT`'s
# GROUPING class, not here. Checked rather than assumed.
#
# ⚠️ AND SINCE 19.0.3.20.0 THE CLASS SAYS SO (found in review). The same line
# used to establish that `\s` already covers U+00A0 and then keep U+00A0 in
# the class anyway, as did `_KRONA_RE` and `_YUAN_RE`. The comment and the code
# disagreed about whether the member was needed, which is the shape of defect
# this file has spent three reviews removing. `_GROUP_SEP` keeps its explicit
# members: that one is not built on `\s`.
_DOLLAR_RE = re.compile(r"\$\s{0,2}-?\d|\d\s{0,2}\$(?!\w)")

# 🔴 `kr` AND `元` ARE LETTERS, AND THEY ARE NOT THE SAME CASE. Until
# 19.0.3.19.0 the comment here said both "need an amount beside them and
# nothing alphabetic against them", and review measured that only `kr` does:
# `公元 2026,00 年` → `['元']`. The guard was never written for `_YUAN_RE`.
#
# It is still not written, and that is a decision rather than an omission. A
# Latin-script guard works on `kr` because Swedish puts spaces between words,
# so `kruka` is one token and `1 500 kr` is three. Chinese does not — a total
# runs straight into whatever character follows it, so there is no token
# boundary to hang a guard on — and a trailing letter-guard would therefore
# throw away ordinary Chinese totals, which is the DANGEROUS direction, to buy
# protection against `公元 2026,00 年`, which merely closes the gate. The
# over-report is pinned by a test that names it.
#
# 🔴 AND THE EXAMPLE THE PARAGRAPH ARGUED FROM WAS VACUOUS, IN THREE PLACES AT
# ONCE (19.0.3.20.0). It said a guard "would throw away `1234元整`
# ('1234 yuan exactly'), the ordinary way to write a total on a Chinese
# invoice". `1234元整` has no decimal part, so `_AMOUNT` never reaches it and
# the proposed guard would have cost nothing on that string — the decline was
# justified by a case it does not protect. The same sentence stood in
# the 19.0.3.19.0 notes and in `test_what_it_over_reports_on_
# purpose`. A rationale repeated in three places and true in none of them is,
# word for word, the defect 19.0.3.19.0 §(b) says it is fixing.
#
# What the argument was really about sits three characters to the LEFT of the
# guard — see `_AMOUNT_TIGHT`. `1,234.00元`, which IS the ordinary form, was
# missed by the amount's trailing `\b`, not by the absence of a lookaround.
# That is fixed. What is still missed is the amountless `1234元整`, and it is
# named in `test_the_hole_that_is_still_open` instead of being used as an
# argument for something else.
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

# ISO 4217, the codes that are current legal tender. Derived from CLDR's
# `territory_currencies` (babel 2.x) by keeping the rows with no end date, then
# frozen here: 153 entries, checked in so a reader can see exactly what is
# matched and so a babel upgrade cannot silently change what this module calls
# a currency.
#
# 🔴 HISTORICAL CODES ARE DELIBERATELY OUT. The full CLDR list is 306 and the
# extra 153 are Deutschmarks, escudos and Yugoslav dinars — none of which a 2026
# vendor bill is denominated in, while they bring in `BAD`, `SUR`, `USN`, `NIC`,
# `PES`, `ARM`, `MAF` and `XXX` as things to mistake for money. More codes is
# not more safety here; it is more noise in the direction that hides the fix.
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

# 🔴 THE ONE PLACE THE BIAS IS REVERSED, AND THE REASON IS MEASURED.
#
# Twenty-six of the 153 current codes are also ordinary English words or common
# abbreviations in upper case: ALL, TOP, SOS, CUP, BOB, MAD, PAB, TRY, LAK, DOP,
# SAR, RON, ERN, GEL, ANG, MOP, SLE, ILS … and the last of those is not
# hypothetical. The ZZ Freight LLC bill this fix exists for prints
#
#     *ZZF  PZONE03/09/26       ILS 30572
#
# in a routing field. `ILS` is the Israeli new shekel. A rule that matched any
# bare uppercase triple on the code list would call that a currency marking,
# close the gate, and leave the bill exactly as broken as it is today — while
# looking like caution.
#
# So a bare code must sit against something that is SHAPED LIKE MONEY (two
# decimal places), or be introduced by a word that says a currency follows.
# `ILS 30572` is neither. `USD 612.84`, `Total EUR 1.234,56`, `1,234.56 CAD`
# and `CHECKS IN USD:` are all four.
#
# 🔴 WHAT THIS GIVES UP, AND THE BOUND IT USED TO CLAIM WAS FALSE.
#
# Until 19.0.3.19.0 this paragraph said the under-report was *"bounded, because
# two further questions still have to answer yes before anything is inferred
# (both parties in ONE country, and an already-set currency must equal that
# country's own)"*, and said it here, in the test that names it, and in the
# CHANGELOG. A review read the resolver. The second question
# (`claude_client:1450`) is guarded by `already_set`, so on the branch that
# WRITES a currency (`:1509`) it is never asked. Verified from bytes:
# `Total: CAD 500` on a US-to-US bill with `currency: null` found no marking,
# and `USD` was written under a note saying nothing was printed. A rationale
# repeated in three places and true in none of them is worse than none.
#
# TWO THINGS WERE DONE ABOUT IT, and neither of them is "add `rejected_codes`
# and decline to write". That was the obvious repair and it is measurably
# wrong: `ILS 30572` and `CAD 500` are the same shape AS THAT RULE READS THEM —
# a bare code, a gap, an integer — so a rule OF THAT SHAPE, one that declines
# whenever the carve-out rejected a code, declines on the ZZ Freight LLC bill, which is
# the one document this whole feature exists for.
#
# ⚠️ "ANY RULE" IS WHAT THIS USED TO SAY, AND IT IS FALSE AT BYTE LEVEL
# (19.0.3.20.0). The two strings do differ in a way a rule can
# read: `ILS 30572` is a FIVE-digit ungrouped integer, and every other figure
# on that bill is two-decimal (`612.84`, `4,218.00`, `3,758.86-`, `131.70`),
# while `CAD 500` is three digits. A rule of the form "an integer of four or
# more digits with no grouping separator and no decimal part is not an amount"
# separates them. Whether that is a GOOD rule is a different question — round
# four-digit totals like `1500` exist, and it would be a third amount shape to
# maintain — but it exists, so the claim is narrowed to the rule review
# actually proposed rather than left as an absolute.
#
#   1. The hole was made smaller where it could be, HERE, in the direction the
#      file's bias already goes: `total`, `subtotal`, `balance` and
#      `amount(s) due` were added to the announcing words below, so
#      `Total: CAD 500` IS a marking now. What stays missed is a code with no
#      announcing word at all and no decimals — `CAD 500` alone on a line.
#      `ILS 30572` is still missed, which is the point.
#   2. The remaining bound is stated as it actually is, below and in the two
#      other places it appeared.
#
# THE BOUND, HONESTLY: on the branch that only MARKS an already-set currency,
# two further questions are asked — both parties in one country, and the
# already-set currency equal to that country's own. On the branch that WRITES
# one, exactly one is: both parties in one country (plus "is that currency
# traded on this database", which is about configuration, not about the page).
# So a document that prints `CAD 500` and nothing else, between two US
# addresses, with the model reporting no currency, still gets `USD` written.
# That residual is named here rather than bounded away.
_CODE_NEAR_AMOUNT_RE = re.compile(
    r"\b(%(code)s)\b[^\w\n]{0,4}(?=%(amt)s)"
    r"|(?:%(amt)s)[^\w\n]{0,4}\b(%(code)s)\b" % {"code": _CODE, "amt": _AMOUNT}
)

# Words that ANNOUNCE A CURRENCY — the phrase itself says a currency code is
# what comes next, so no amount is required. Kept SHORT and specific: a bare
# "in" before a triple would match `IN NEW`, `IN USD` and `IN ALL` alike, and
# two of those are not currencies. The Fabrikam invoice prints
# `CHECKS IN USD:` with no number on the line and that is a marking by anyone's
# reading, which is the whole reason this rule does not ask for one.
_CODE_AFTER_CURRENCY_WORD_RE = re.compile(
    r"(?:currency|currencies|denominated\s+in|payable\s+in|paid\s+in|billed\s+in"
    r"|invoiced\s+in|quoted\s+in|amounts?\s+(?:are\s+)?in|totals?\s+in"
    r"|checks?\s+in|cheques?\s+in|funds?\s+in|remit\s+in)"
    r"[^\w\n]{0,4}\b(%(code)s)\b" % {"code": _CODE},
    re.IGNORECASE,
)

# 🔴 THE AMOUNT WORDS ARE A DIFFERENT RULE, AND SINCE 19.0.3.20.0 THEY ARE A
# DIFFERENT REGEX (found in review).
#
# `total`, `subtotal`, `balance` and `amount(s) due` joined the announcing words
# in 19.0.3.19.0 so that `Total: CAD 500` would stop being missed. They do not
# belong there. They do not announce a CURRENCY; they announce A NUMBER, and
# the 26-confusables paragraph above names the money shape as the defence
# against exactly the codes that are also words. Put on a list that requires
# nothing after the code, they took that defence away for their own line:
#
#     `Totals TRY`      -> ['TRY']        `Grand total SAR`  -> ['SAR']
#     `subtotals NOK`   -> ['NOK']
#
# — none of which has an amount anywhere on it, and all of which are ordinary
# things to find in a table header. *(measured before this change)*
#
# So the amount words keep their own alternative and require a NUMBER after the
# code. Not `_AMOUNT`: `Total: CAD 500` is the case review repaired and `500`
# has no decimals, so demanding the two-decimal shape here would undo that
# repair and re-open R2. A digit is the weakest thing that still removes the
# three rows above.
#
# ⚠️ WHAT SURVIVES, NAMED RATHER THAN DISCOVERED LATER: `Balance PEN 12` and
# `Amount due TOP 3` still read as markings. Both have a number after the code
# and both are genuinely shaped like an amount in the invoice's money column,
# so they are the file's declared over-reporting bias doing its job, which
# costs today's behaviour. `Total CUP 50` is the same shape and is pinned in
# `test_what_it_over_reports_on_purpose`.
#
# ⚠️ IT DOES NOT BITE TODAY EITHER WAY, which is why this is a repair of a
# contradiction rather than of a measured failure. *(measured)* all
# seven sample PDFs, the transcribed ZZ Freight LLC block and all eight
# `_EIGHT_REAL_DOCUMENTS` excerpts give identical markings and verdicts under
# the old rule and the new one. The ZZ Freight LLC bill prints `Total Amount Due 612.84`
# — `Amount` is not a code, and `[^\w\n]` cannot cross the newline to the next
# line's tokens — so the one document this fix is for is untouched.
_CODE_AFTER_AMOUNT_WORD_RE = re.compile(
    r"(?:(?:sub|grand\s+)?totals?|balances?|amounts?\s+due)"
    r"[^\w\n]{0,4}\b(%(code)s)\b[^\w\n]{0,4}(?=-?\d)" % {"code": _CODE},
    re.IGNORECASE,
)

# How many distinct markings to keep in the stored verdict. The verdict is for a
# human reading the JSON a year later, and "the page prints `$`" is the whole
# answer — a hundred of them is noise in an attachment.
MAX_RECORDED_MARKINGS = 8

# 🔴 THE THRESHOLD THAT SEPARATES "NOTHING PRINTED" FROM "NO TEXT LAYER", and
# it has to exist. `extract_text()` returns "" for a scanned page, and it
# returns a handful of characters for a scan that happens to carry a stamp, a
# page number or a fax header in a real font. Either of those read as "the
# document prints no currency", which is the dangerous direction with no
# evidence behind it at all.
#
# The smallest text layer across the eight measured sample invoices is about
# 400 alphanumeric characters (a one-page bill with two lines on it); the next
# smallest is about 650 and the ZZ Freight LLC bill is roughly 3,000. 120 is
# comfortably under the smallest of them and comfortably
# over a stamp or a page number. Below it the answer is UNKNOWN and the model is
# asked instead — which is 19.0.3.3.0's behaviour, unchanged.
#
# 🔴 IT GUARDS THE NEGATIVE ANSWER ONLY, SINCE 19.0.3.19.0. Until then the
# threshold ran BEFORE the markings were computed and therefore discarded
# positive evidence it already held: review measured a 42-alnum credit note
# printing `EUR 1.234,00` answering `unknown` while
# `currency_markings_in_text` on the same text answered `['EUR']`. That was a
# second, undeclared reversal of the file's stated bias, and the verdict on a
# page that unambiguously prints a currency depended on how much unrelated
# boilerplate sat around it. A FOUND MARKING NEEDS NO QUORUM — it is evidence
# on its own — so `scan_pdf_for_currency` now matches first and consults the
# threshold only when nothing was found.
#
# ⚠️ IT IS COUNTED OVER THE CONTENT STREAM ALONE, not over annotation or form
# text. The question it asks is "does this file have a readable text layer",
# and a scanned page carrying one filled-in form field has not acquired one, so
# it must still answer UNKNOWN rather than `nothing_printed` about the parts
# nobody can read.
#
# 🔴 THAT LAST SENTENCE USED TO BE STATED ABOUT THE VERDICT, AND IT IS TRUE OF
# THE NEGATIVE ANSWER ONLY (19.0.3.20.0). Markings come first —
# see `scan_pdf_for_currency` — so a money-bearing field is evidence on its own
# and beats the threshold exactly as a money-bearing content stream does.
# *(measured, a page whose content stream is `Page 1 of 2`, 8 alnum, plus one
# widget)*
#
#     `/V (Received 14 February)` -> {'verdict': 'unknown', 'alnum': 8}
#     `/V ($1,234.00)`            -> {'verdict': 'printed', 'markings': ['$']}
#
# The behaviour is right and it is not what the old sentence said. What a
# filled field cannot buy is `alnum`, and therefore it cannot turn a scan into
# a `nothing_printed`. `test_an_annotation_does_not_buy_a_text_layer` asserted
# only the first row, with a non-currency field, so it passed without being
# able to see the second — a control that passed for the wrong reason.
MIN_TEXT_LAYER_ALNUM = 120

# The three answers. Strings rather than booleans because they are WRITTEN INTO
# THE STORED JSON and read by a person.
PRINTED = "printed"
NOTHING_PRINTED = "nothing_printed"
UNKNOWN = "unknown"


def currency_markings_in_text(text):
    """Every distinct currency marking this text prints, in RULE order.

    ⚠️ RULE ORDER, NOT DOCUMENT ORDER — this docstring said "document order"
    until 19.0.3.19.0 and it was never true. The six rules run in sequence and
    the result is deduped across them, so
    `"Paid USD 100.00 then a fee of 5,00 EUR and 3,00 kr"` answers
    `['kr', 'USD', 'EUR']`. It is cosmetic, except that the list is written
    into stored JSON for a person to read a year later and this docstring is
    what tells them how to read it.

    Pure: no PDF, no Odoo, no I/O. That is deliberate — it is the half of this
    file that can be argued about and tested against literal strings, and the
    eight-invoice measurement in the module's tests drives exactly this.
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


# 🔴 TWO CAPS ON THE ANNOTATION SURFACE, AND IT IS NOT THE SAME TRADE AS THE
# PAGE COUNT (19.0.3.20.0).
#
# `scan_pdf_for_currency` deliberately has no page cap and no early exit: review
# 6 measured 1.61 s over a synthetic 4.1 MB, 360-page PDF against a Claude call
# of 30–120 s in the same method, and a cap would make the recorded `markings`
# depend on where the scan stopped. That argument was made about the CONTENT
# STREAM, before the annotation surface existed. An annotation array is a
# different shape of input: a `/Annots` array of a million references, or one
# 6 MB `/Contents` string, costs real time for no evidence — *(measured, review
# 7)* **4.3 s** of regex for a single 6 MB annotation, with and without a
# currency in it. The `/Kids` walk was already bounded; these two were not.
#
# Both are set far above anything a real document has: a filled vendor bill has
# tens of fields, not thousands, and a typed-in value or a stamp is a line of
# text, not a novel. A currency marking that is only visible past either bound
# is a currency marking nobody reading the page would find either.
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
        # 🔴 A `/V` HELD IN A STREAM (19.0.3.20.0). A generator
        # may put a long field value in an indirect stream object rather than
        # in a string; it is legal, and it used to hit neither branch and
        # return "". *(measured)* `/V 11 0 R` with object 11 a stream holding
        # `$1,234.00` answered `nothing_printed, []` — the dangerous direction,
        # one `get_data` away from being read.
        get_data = getattr(value, "get_data", None)
        if get_data is None:
            return ""
        try:
            value = get_data()
        except Exception:  # noqa: BLE001 - an unreadable stream is not a file
            return ""
        if not isinstance(value, bytes):
            return ""
    # 🔴 THE BOM DECIDES, AND IT USED TO BE TRIED LAST (19.0.3.20.0).
    # The ladder read `("utf-16", "utf-8", "latin-1")`, and Python's
    # `utf-16` codec with no BOM defaults to LITTLE-endian — which a PDF never
    # means — so any even-length byte string that was not UTF-16 decoded to
    # mojibake and the currency in it was lost. *(measured on the helper)*
    # `b"$1,234.00"` → `'$1,234.00'` but `b"$1,234.00 "` → `'ㄤ㈬㐳〮‰'`,
    # markings `[]`: one trailing space, opposite answer, the dangerous
    # direction. PDF text strings that are UTF-16 carry the BOM by
    # specification, so the BOM is the test and the BOM-less case belongs to
    # UTF-8 and then to Latin-1, which maps every byte and ends the ladder.
    #
    # ⚠️ REACHABILITY IS THE MITIGATION, NOT THE DESIGN. pypdf hands back a
    # `TextStringObject` (a `str`) for everything it can decode, including
    # PDFDocEncoding, so this branch fires only on strings pypdf itself gave up
    # on. A review could not reproduce it through a whole PDF. Latent, and
    # cheap enough to not leave standing.
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
    """Every string an ANNOTATION or an ACROFORM FIELD puts on these pages.

    🔴 WHY THIS EXISTS: `extract_text()` walks the CONTENT STREAM, and the
    Form XObjects the content stream invokes. It does not walk `/Annots` — not
    the `/Contents` of a FreeText or a stamp, not the `/V` of an AcroForm field,
    not the `/AP` appearance stream those render from. So a currency typed into
    a fillable vendor bill is printed on the page and absent from the text, on
    an otherwise ordinary page far over `MIN_TEXT_LAYER_ALNUM`. A review built
    it: body 235 alnum, `/FreeText` drawing `$1,234.00`, verdict
    `nothing_printed`, markings `[]`, identical on pypdf 6.20.0 and PyPDF2
    2.12.1. Fillable bills are ordinary — a vendor types into a template and
    saves rather than printing — and Odoo ships `fill_form_fields_pdf` in the
    very module this file reads PDFs with.

    ⚠️ A COMMENT IS COUNTED TOO, DELIBERATELY. A reviewer's sticky note or a
    "PAID $1,234.00" stamp is not page content in any strict sense, and this
    makes no attempt to tell one from the other: `fill_form_fields_pdf` writes
    `/Widget`, a stamp writes `/Stamp`, a typed-in value writes `/FreeText`,
    and deciding per subtype which of them a given generator meant as content
    is exactly the kind of judgement this file exists to avoid. Counting them
    all over-reports at worst, which closes the gate and costs today's
    behaviour; the other way round opens it on a page that prints money.

    🔴 THAT PARAGRAPH DEFENDS COUNTING VISIBLE NON-CONTENT, AND THE CODE ALSO
    USED TO COUNT INVISIBLE CONTENT (19.0.3.20.0). It argues by
    SUBTYPE; the code keys on dictionary keys and never looked at a subtype in
    its life. Two of the three rows review measured are not "a sticky note
    counted too" — they are strings that are not on the page in any sense:

    * **`/F` bit 2 is Hidden.** No viewer renders a hidden annotation and no
      printer prints it. The sweep called it `printed ['$']`. It is skipped
      now. ⚠️ `/F` bit 6 (NoView, 32) is NOT skipped and that is deliberate:
      NoView means "do not show on screen", and such an annotation DOES print.
      The question this file asks is what the page prints.
    * **`/DV` is the field's DEFAULT value, not its value.** A widget with
      `/V (0.00)` and `/DV ($1,234.00)` shows `0.00`; the sweep read the `$`
      off a key the renderer ignores whenever `/V` is present. `/DV` is now
      read only when there is no `/V` — where it genuinely is what the field
      displays — rather than in addition to it.

    Both were in the SAFE direction (they close the gate), so this is a
    reasoning repair rather than a money repair. It is made because the
    paragraph above is the thing that makes the over-report a decision, and a
    decision that names the wrong seam is not one.

    ⚠️ THE `/AP` APPEARANCE STREAM IS STILL NOT WALKED, AND IT IS A RESIDUAL IN
    THE DANGEROUS DIRECTION (found in review). An annotation that carries only an
    `/AP` — a flattened stamp, or a generator that wrote the appearance and no
    `/Contents` — prints money and answers `nothing_printed`. *(measured)* a
    `/FreeText` whose `/AP` draws `PAID $1,234.00` with no `/Contents` and no
    `/V` → `nothing_printed, []`. It sits on the same list as vector art and
    word-named currencies, and `test_the_annotation_hole_that_is_still_open`
    pins the shape of it so a later change has to update a test rather than a
    sentence. ⚠️ `_pdf_with_annotations` builds its `/FreeText` WITH an `/AP`
    drawing the same string; that `/AP` is decoration the code never reads, and
    it is labelled as such in the fixture so nobody reads coverage into it.

    ⚠️ A VALUE THAT DUPLICATES BODY TEXT COSTS NOTHING. `currency_markings_
    in_text` dedupes by marking, so a form field repeating a total already in
    the content stream adds no entry; and this text is NOT counted toward
    `MIN_TEXT_LAYER_ALNUM`, so a filled field cannot make a scanned page look
    like it has a readable text layer. ⚠️ It CAN still make one answer
    `printed` — markings come first, and a money-bearing field is evidence on
    its own. See `MIN_TEXT_LAYER_ALNUM`, where that sentence used to be stated
    about the whole verdict and was true of the negative answer only.
    """
    out = []
    seen = set()

    def _add(value):
        text = _annotation_string(value).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)

    def _value_key(node):
        # `/DV` ONLY WHERE THERE IS NO `/V` — see the docstring. With both
        # present the renderer shows `/V` and `/DV` is the template's
        # placeholder.
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
        # ⚠️ `visited`, NOT `depth` — it counts pops, which is what bounds the
        # walk against a cyclic `/Kids` graph. It was called `depth` and it
        # never measured depth (found in review).
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

    None means UNKNOWN — an encrypted, truncated or malformed file. The two
    halves are kept APART because they answer different questions: the first is
    the evidence that this file has a text layer at all (`MIN_TEXT_LAYER_ALNUM`
    is counted over it alone), the second is extra places a currency can be
    printed. Both are scanned for markings.
    """
    if not pdf_bytes:
        return None
    # Imported inside the function so this module stays importable — and the
    # regexes above stay measurable — without a loaded Odoo. `odoo.tools.pdf`
    # is the house re-export that already handles pypdf vs PyPDF2.
    #
    # ⚠️ IT IS NOT A BARE RE-EXPORT: `odoo/tools/pdf/__init__.py:80-83`
    # SUBCLASSES the reader and hard-codes `strict=True`, where pypdf's own
    # default is False. That is the constructor production uses, so it is the
    # constructor the eight-document table was re-measured through in
    # 19.0.3.19.0 (R12) — same alnum counts, same verdicts, same
    # markings. On a slightly malformed vendor PDF `strict=True` can raise
    # where `strict=False` would recover, and the answer then is UNKNOWN, which
    # is the safe degradation.
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
        # An encrypted, truncated or malformed PDF. Claude read it (we only get
        # here with an extraction in hand), so the file is not the problem —
        # pypdf is. UNKNOWN, loudly, and the model decides.
        _logger.warning(
            "AI invoice import: could not read the PDF's text layer (%s) — the "
            "currency-marking check falls back to the model's own report",
            error,
        )
        return None


def scan_pdf_for_currency(pdf_bytes):
    """Does this PDF print a currency marking? ``{"verdict", "markings", "alnum"}``.

    ``verdict`` is `PRINTED`, `NOTHING_PRINTED` or `UNKNOWN`. ``alnum`` is how
    many alphanumeric characters the content stream gave up — stored since
    19.0.3.19.0 because without it an audit cannot tell a `nothing_printed`
    read off 121 characters from one read off the ZZ Freight LLC bill's roughly 3,000, and
    `nothing_printed` is the dangerous verdict.

    🔴 THE ORDER OF THE TWO QUESTIONS IS THE POINT. Markings first, threshold
    second. A found marking is positive evidence and needs no quorum; the
    threshold exists to stop a NEGATIVE answer being read off an empty text
    layer, and applying it to the positive answer threw away evidence already
    in hand — see `MIN_TEXT_LAYER_ALNUM`.
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
        # A scan, or an image-only export, and nothing was found in it. INFO
        # rather than WARNING: this is the documented degradation to
        # 19.0.3.3.0's behaviour on a file that is simply not the kind this
        # check can read, not a fault.
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
