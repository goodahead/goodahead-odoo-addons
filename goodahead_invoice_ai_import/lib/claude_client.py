# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Invoice extraction prompt, response handling and validation.

The Claude transport, API key and model registry belong to `goodahead_claude`.
This module holds the invoice-specific part: the prompt, the PDF framing, the
stop-reason handling and the validation of the extracted JSON. No model id is
hardcoded; the client resolves one from the fetched model registry.
"""

import base64
import json
import logging
import re
from collections.abc import Mapping

from . import pdf_currency

_logger = logging.getLogger(__name__)

# Models that think first count the thinking against max_tokens, so leave room
# for long invoices. Passed per call: the shared client is also used by other
# features, whose defaults must not change.
MAX_TOKENS = 16000
# Thinking makes responses slower, and a timeout is a failed (and billed) import.
REQUEST_TIMEOUT = 300

# Keys of the country-based currency fallback. They are read by this validator,
# by `account_move._ai_post_process` and by the tests, so they are constants.
#
# `currency_symbol` is reported by the model: the currency marking printed on
# the page, or null. It distinguishes "nothing printed" from "something printed
# that could not be placed", which is what makes the fallback safe.
CURRENCY_SYMBOL_KEY = "currency_symbol"
# Set by this module, never by the model: the country the currency was taken from.
CURRENCY_INFERRED_KEY = "currency_inferred_from_country"
# Set by this module from the PDF's text layer (see `pdf_currency`). It outranks
# the model's `currency_symbol`, which can vary between runs on the same
# document. Both answers are stored so an audit can see which one decided.
CURRENCY_MARKING_KEY = "currency_marking"

# "Not reported" and "reported null" are different answers; only null allows
# the fallback. Older stored extractions have no `currency_symbol` key at all.
_ABSENT = object()


def nothing_was_printed(data):
    """Does the page carry no currency marking at all?

    Public because `account_move._ai_post_process` re-checks a marker that
    arrives in stored JSON. When the PDF's text layer gave a verdict, it wins in
    both directions; the model's `currency_symbol` decides only when the text
    layer is unknown (scanned or unreadable PDF).
    """
    verdict = (data.get(CURRENCY_MARKING_KEY) or {})
    if isinstance(verdict, dict):
        text_layer = verdict.get("text_layer")
        if text_layer == pdf_currency.NOTHING_PRINTED:
            return True
        if text_layer == pdf_currency.PRINTED:
            return False

    printed = data.get(CURRENCY_SYMBOL_KEY, _ABSENT)
    if printed is _ABSENT:
        return False  # older stored JSON: the question was never asked
    if printed is None:
        return True
    # Only an empty string counts as "nothing printed"; a non-string value is
    # unreadable and does not allow the fallback.
    return isinstance(printed, str) and not printed.strip()


def currency_marking_verdict(pdf_bytes, data):
    """Read the PDF's text layer and pair the answer with the model's.

    Returns the dict stored under `CURRENCY_MARKING_KEY`, recording both
    sources and which one decides.
    """
    scan = pdf_currency.scan_pdf_for_currency(pdf_bytes)
    reported = data.get(CURRENCY_SYMBOL_KEY, _ABSENT)
    model_reported = reported is not _ABSENT
    model_says_printed = None
    if model_reported:
        model_says_printed = not (
            reported is None
            or (isinstance(reported, str) and not reported.strip())
        )

    decided = scan["verdict"] != pdf_currency.UNKNOWN
    agree = None
    if decided and model_says_printed is not None:
        agree = (scan["verdict"] == pdf_currency.PRINTED) == model_says_printed

    verdict = {
        "decided_by": "text_layer" if decided else "model",
        "text_layer": scan["verdict"],
        "markings": scan["markings"],
        # How much text the verdict was read from, so a `nothing_printed`
        # taken from a near-empty page can be told apart later.
        "alnum": scan["alnum"],
        "model_reported": model_reported,
        "model_symbol": reported if model_reported else None,
        "agree": agree,
    }

    if agree is False:
        # A disagreement is worth a warning; the message names which way it went.
        _logger.warning(
            "AI invoice import: the PDF's text layer and the model disagree "
            "about whether this page prints a currency. Text layer: %s %s. "
            "Model: currency_symbol=%r. The text layer decides (%s)",
            scan["verdict"],
            scan["markings"] or "[]",
            reported,
            "the page is unmarked, so the country fallback may fire"
            if scan["verdict"] == pdf_currency.NOTHING_PRINTED
            else "the page IS marked, so the country fallback is blocked",
        )
    return verdict


def one_country_on_both_sides(data):
    """The 2-letter country both parties are in, or None.

    Both codes come from the document. The company's own country is never
    used in place of a missing buyer country.
    """
    seller = data.get("seller")
    buyer = data.get("buyer")
    if not isinstance(seller, dict) or not isinstance(buyer, dict):
        return None
    seller_cc = str(seller.get("country_code") or "").strip().upper()
    buyer_cc = str(buyer.get("country_code") or "").strip().upper()
    if not seller_cc or seller_cc != buyer_cc:
        return None
    return seller_cc


# Reconciliation tolerance: the greater of two cents (per-line rounding) and
# half a percent (rounding accumulated over a long document). Misread separators,
# transposed digits and dropped lines are all far above that.
TOLERANCE_ABS = 0.02
TOLERANCE_REL = 0.005

# Per-line check: `quantity × price_unit` and `line_total` may differ (line
# discounts are common), but not by 100× or more, which is what a misplaced
# decimal point or thousands separator produces.
LINE_RATIO_LIMIT = 100

# The prompt: country-neutral procedures (document type, currency, dates,
# numbers) with a locale block substituted at named slots. Several slots sit
# inside the JSON schema descriptions, so the block cannot simply be appended.
# The schema keys are the same in every locale, and the composed US prompt is
# pinned byte-identical by a golden-file test.
#
# Slots use `@@name@@` rather than `{name}` because the prompt later goes
# through `str.format` to fill `{categories_block}`.
_SLOT_RE = re.compile(r"@@([a-z_]+)@@")

# Looser than `_SLOT_RE`, to name a malformed marker left after substitution.
_ANY_MARKER_RE = re.compile(r"@@[^@\n]{0,80}@@")

PROCEDURES = """\
You are an expert accounts-payable data extraction specialist. You read vendor \
invoices and credit memos as PDFs and extract structured data. @@locale_origin@@

You must output valid JSON matching the schema below. Extract ALL data visible \
in the document. Do not invent or hallucinate values — if a field is not present \
in the document, use null. Never convert, reformat or "correct" an identifier or \
an account number: copy exactly what is printed.

DOCUMENT TYPE. Answer this question explicitly, before you extract anything \
else: IS THIS DOCUMENT A BILL FOR MONEY WE OWE, OR A CREDIT THAT REDUCES WHAT \
WE OWE? Output "Invoice" for the first and "CreditNote" for the second. 🔴 \
Getting this wrong FLIPS THE SIGN OF THE WHOLE DOCUMENT: a credit read as an \
invoice makes us pay a refund, and an invoice read as a credit cancels a bill \
we really owe. Neither has a visible symptom at import time. Do not decide it \
by matching English words — many of these documents are not in English. Decide \
it from the evidence:
- THE DOCUMENT'S OWN TITLE, IN WHATEVER LANGUAGE. English: "Credit Memo", \
"Credit Note", "Credit Invoice", "RMA credit", "Refund". Finnish: \
"Hyvityslasku". Swedish: "Kreditfaktura". German: "Gutschrift", \
"Stornorechnung". Norwegian and Danish: "Kreditnota". Dutch: "Creditnota". \
French: "Avoir", "Note de crédit". Spanish: "Nota de crédito", "Factura \
rectificativa". Italian: "Nota di credito". Polish: "Faktura korygująca". \
Chinese: 红字发票, 贷记通知单, 退款单, 退货. Japanese: クレジットノート. A plain \
"Invoice" / "Lasku" / "Faktura" / "Rechnung" / 发票 with no correcting word is \
an Invoice. ⚠️ Those are EXAMPLES, not a lookup table: a title in a language \
not listed counts just as much if it means the same thing.
- A DOCUMENT-TYPE CODE. UN/EDIFACT 1001 code 381 (also 261, 262, 396) means \
credit note; 380 means commercial invoice. A national equivalent counts the \
same way.
- A REFERENCE TO AN EARLIER DOCUMENT BEING CORRECTED, REVERSED OR REFUNDED \
("credit against invoice 12345", "korjaus laskuun", "Gutschrift zu Rechnung", \
原发票号). Strong evidence for CreditNote.
- WORDING ABOUT A RETURN, A REFUND, A CORRECTION or A CANCELLATION of goods \
already billed.
- A NEGATIVE PAYABLE TOTAL. Supporting evidence, never proof on its own.

🔴 TWO CASES PRODUCE A SIGN ERROR, AND THEY ARE DIFFERENT PROBLEMS:
- A CREDIT NOTE THAT PRINTS ITS AMOUNTS POSITIVE. This is normal — it is the \
commonest way credit memos are printed. The absence of minus signs is NOT \
evidence that the document is an invoice. Decide the type from the title, the \
type code and the reference to the original document, and then report the \
amounts as positive magnitudes exactly as printed. "CreditNote" already carries \
the sign.
- AN INVOICE THAT CARRIES A NEGATIVE LINE. A discount, an item returned and \
credited back on the same bill, a deposit or prepayment deducted, a rounding \
adjustment — all of these are ordinary on an INVOICE, and one negative line \
does not make the document a credit note. Judge the document AS A WHOLE: if the \
payable total is positive and the document bills for goods or services, it is \
an "Invoice", and the negative lines KEEP their minus signs inside it.

If the evidence genuinely conflicts — a document headed "Invoice" whose payable \
total is negative and which references an earlier invoice it reverses — call it \
a "CreditNote" and say so in "note". The total and the reference are what the \
accounting means; the heading is a template the vendor reused.

@@locale_tax@@

CURRENCY. Output the ISO 4217 three-letter code the document is denominated in \
— "USD", "EUR", "CNY" — or null. 🔴 A SYMBOL IS NOT A CURRENCY. "$" is the \
United States, Canada, Australia, Hong Kong, Singapore, Mexico and more; "¥" \
and "￥" are used by BOTH China (CNY, also written RMB or 元) AND Japan (JPY), \
which differ by roughly a factor of twenty; "kr" is Swedish, Norwegian or \
Danish; "£" is British but also Egyptian and Lebanese. Work through these steps \
IN ORDER and stop at the first one that settles it:

1. AN ISO CODE PRINTED ON THE DOCUMENT. This is proof and it outranks \
everything below, including the symbol and the vendor's country. "Total EUR \
1.234,56", "Amount due: 500 USD", "Currency: CNY", a column headed "USD" — take \
it verbatim. A document printed by a German vendor that says USD is a USD \
document.
2. THE SYMBOL TOGETHER WITH THE VENDOR'S COUNTRY. Neither half settles it \
alone; together they usually do. "$" on an invoice from a US address is USD; \
"$" from a Canadian address is CAD; "€" is EUR wherever it appears; "฿", "₹", \
"₩" and "₽" each belong to one country. ⚠️ THESE ARE GENUINELY AMBIGUOUS EVEN \
WITH A COUNTRY, so do not stop here for them — go on to step 3: "¥" and "￥" \
(CNY in China, JPY in Japan, and the same glyph is printed in both), "$" with \
no country shown or with a country that uses a dollar other than the US one, \
"kr" (SEK, NOK, DKK), "£" (GBP, EGP, LBP), and 元 or 圆 standing alone.
3. OTHER EVIDENCE ON THE DOCUMENT. Any of these can settle it:
   - The language the document is written in, and its own word for the money: \
     "euroa", "kronor", 人民币, 日元, "yuan", "yen", "renminbi".
   - A VAT or sales-tax rate that identifies a jurisdiction: 25.5% or 24% is \
     Finland, 19% Germany, 20% the UK, 13% or 6% China, and an odd US state \
     rate such as 6.625% is the United States.
   - The bank details: an IBAN's country prefix (FI… Finland, DE… Germany), or \
     a 9-digit ABA routing number, which is a United States bank.
   - A stated exchange rate, or a total printed twice in two currencies. The \
     DOCUMENT currency is the one the line items and the payable total are \
     stated in, never the converted figure shown beside them.
   - The vendor's VAT number prefix and address country.
4. A DOMESTIC DOCUMENT THAT PRINTS NO SYMBOL AND NO CODE AT ALL. Only when \
BOTH of these hold: (a) the document shows NO currency symbol and NO ISO code \
anywhere — the amounts are bare numbers such as "600.07" and "3,748.50" — and \
(b) the vendor's address and the buyer's address are in the SAME country. Then \
report that country's own currency: a domestic bill with unmarked amounts is \
denominated in the domestic currency, which is why nobody printed a symbol on \
it. ⚠️ THIS STEP DOES NOT APPLY WHEN A SYMBOL WAS PRINTED and you could not \
settle which currency it meant — a "$", "¥", "kr", "£" or 元 you could not \
place goes on to step 5, because the country is the wrong tiebreak for an \
ambiguous symbol and a Chinese vendor billing a US buyer must never become USD. \
It also does NOT apply when the two addresses are in different countries, or \
when either country is not shown. 🔴 AN ADDRESS SHOWS ITS COUNTRY WHEN ITS OWN \
FORMAT DOES — "SPRINGFIELD IL 62701-1234" and "AUSTIN TX 78701-4321" are both \
United States addresses whether or not the word USA appears anywhere, and a \
ZIP+4 is still a ZIP — so a domestic \
US bill that never names the country DOES satisfy (b). Read the address; do not \
require the country to be spelled out.
5. NULL. If none of that settles it, output null. Do NOT fall back on USD, on \
the symbol alone, or on the country alone, and do not guess. 🔴 A missing \
currency stops the import and a person looks at the document; a wrong currency \
produces a bill with entirely plausible numbers and the wrong value, and nobody \
ever sees it. A ¥ invoice read as JPY instead of CNY is wrong by about twenty \
times.

CURRENCY SYMBOL. Separately from the code above, report in "currency_symbol" a \
currency symbol or ISO code PRINTED ANYWHERE ON THE DOCUMENT, copied exactly as \
it appears — "$", "US$", "€", "¥", "￥", "kr", "£", "元", "USD", "EUR" — and \
null ONLY when the document carries no currency marking anywhere at all and \
every amount is a bare number. 🔴 ANYWHERE MEANS ANYWHERE, and this is ONE \
question, not two. A marking in a column heading, in a "Currency: USD" line, in \
the header or the footer, in the payment terms or in a total written out in \
words all count exactly as much as one standing beside an amount — a document \
whose table holds bare numbers but whose header says "Invoice Currency: USD" is \
NOT a null. When the page carries more than one marking, report the one beside \
the amounts. This is an OBSERVATION ABOUT THE PAGE, not a conclusion: report the \
marking even when the steps above already settled the code, and report null only \
when there is genuinely nothing anywhere to read. 🔴 Odoo uses this field, and \
only this field, to decide whether it may fall back on the country, and a bill \
Odoo marks that way carries a note telling the reader that NOTHING was printed \
on the page — so a wrong null puts a false statement in front of an accountant. \
If you are unsure whether what you saw is a currency marking, report it rather \
than null.

@@locale_identifiers@@

CONTACT DETAILS. Extract the VENDOR'S OWN phone, email and website into \
"seller". These are how the same vendor is recognised on the next bill, so a \
contact belonging to somebody else is worse than none at all. Work through \
these steps IN ORDER and stop at the first one that settles each field:

1. WHOSE CONTACT IS IT? Take only a phone, email or URL printed as the \
VENDOR'S own — in the letterhead, beside the vendor's address, in the footer of \
the vendor's template. Do NOT take: the buyer's own details (they are usually \
printed on the same page, under "Bill To" or "Ship To"); a carrier's or \
freight forwarder's tracking line; a bank's phone number from the remittance \
block; a payment processor's support address; or a generic third-party line \
such as "Questions about this delivery? Call …" that names a company other \
than the vendor. If you cannot tell whose a detail is, it is null.
2. WHICH ONE, WHEN SEVERAL ARE PRINTED? Prefer the ACCOUNTS-RECEIVABLE or \
REMITTANCE contact — "AR@…", "billing@…", "accounting@…", "invoices@…", \
"Accounts Receivable", "For billing questions". That is the contact that \
belongs with a bill. If there is none, take the vendor's main or general \
contact. Report ONE value per field, not a list.
3. THE WEBSITE may be taken either from a URL printed on the document or from \
the domain of the vendor's OWN email address — if the vendor's address is \
"orders@example.com", the website is "example.com". Do not derive a website \
from an email at a free provider (gmail.com, yahoo.com, outlook.com, qq.com, \
163.com and the like): those belong to the mailbox, not to the company.
4. NULL. If the document does not print it, or you are unsure, use null. All \
three are OPTIONAL — a missing one never stops the import, and a wrong one \
quietly files the next bill against another company.

@@locale_payment@@

DATES. Output every date as YYYY-MM-DD. A numeric date such as 06/03/2026 does \
not say what it means — it is 6 March in some countries and 3 June in others, \
and reading it the wrong way produces a valid-looking date that is months out. \
Work through these steps IN ORDER and stop at the first one that settles it:

1. THE DATE ITSELF. This is proof and it outranks everything below.
   - A component greater than 12 can only be the day: 13/06/2026 is 2026-06-13, \
     and that also fixes the order as DD/MM for that document.
   - A spelled-out or abbreviated month is already unambiguous: "6 Mar 2026" \
     and "March 6, 2026" are both 2026-03-06.
   - A 4-digit year first means ISO order: 2026-03-06 is 6 March 2026.
   - A TWO-digit year is not settled by this step. "06/03/26" could be any of \
     three orders; carry it to step 2 and, if that does not settle it, to \
     step 4 — do not let it fall through to the country.
   - ⚠️ IF TWO DATES ON THE SAME DOCUMENT ARE BOTH PROOF AND THEY DISAGREE — \
     say it carries both 13/06/2026 and 06/15/2026 — then the document really \
     does mix formats and no order can be assumed anywhere on it. Go straight \
     to step 4 for every date on it that is not itself proof. Do not stop at \
     whichever one you happened to read first.
2. THE REST OF THE DOCUMENT. Still evidence, not inference.
   - Another date that step 1 settles fixes the order for the other dates THE \
     VENDOR'S OWN SYSTEM PRINTED — the invoice date, due date, delivery date \
     and order date on the vendor's own layout, which are normally generated \
     together and formatted the same way. 🔴 This is about where a date came \
     from, not about how it looks. A date QUOTED from somewhere else — a \
     carrier's ship date, a customer's purchase-order date, a date inside a \
     copied reference or a line description — may well be in the other \
     convention, and settles nothing about the vendor's own fields. If you \
     cannot tell whether a date is the vendor's own, it is not evidence here.
   - A format label printed next to the field ("Date (DD/MM/YYYY)").
   - The interval between the invoice date and the due date. Ordinary terms are \
     days or weeks, not months, and a due date usually falls after the invoice \
     date — though a back-dated or already-overdue bill is a real thing, so a \
     negative interval is a weak signal and not a disproof on its own. If one \
     reading gives 14 days and the other gives an interval of many months, the \
     sensible one is correct. If the document states terms ("Net 30", "Payment \
     terms: 14 days", "Due on receipt"), the reading whose interval matches the \
     stated terms is correct, and that is the strong form of this check.
   - The language the document is written in, and the wording of its own date \
     labels.
3. THE VENDOR'S COUNTRY — A PRIOR ONLY, NEVER PROOF. US vendors usually print \
MM/DD/YYYY; vendors in Europe, China and most of the rest of the world usually \
print DD/MM/YYYY. Establish the country from whatever the document shows: the \
vendor's address, a VAT number's country prefix, the currency, the language. \
🔴 This is a tendency and nothing more. An exporter frequently formats an \
invoice for its BUYER rather than for home, so a Chinese or Finnish vendor \
billing a US customer may well print MM/DD/YYYY. Lean on the country only when \
steps 1 and 2 gave you nothing, the country is clear, and nothing else on the \
document pulls the other way.
4. NULL. If it is still ambiguous, output null for that date. In particular: a \
non-US vendor billing a US customer, with an ambiguous date and no other \
signal, is a NULL — the vendor's country says DD/MM, the buyer's convention \
says MM/DD, and neither of those is evidence, so there is nothing to choose \
between. The same applies when the country is absent, or when the address, \
currency and language disagree about it. Do not guess and do not split the \
difference. A missing date is visible and someone fixes it; a wrong date looks \
correct and nobody ever sees it.

NUMBERS. Output every amount and quantity as a JSON number with a dot as the \
decimal separator — never as a string, never with a thousands separator, never \
with a currency symbol. Decide which separator is which BEFORE stripping \
anything, because getting it wrong is an error of a factor of a thousand:
- Both a comma and a dot present: the LAST one is the decimal separator. \
  "1,234.56" is 1234.56 and "1.234,56" is also 1234.56.
- One separator with exactly two digits after it: that is the decimal \
  separator. "1234,56" is 1234.56.
- One separator with exactly three digits after it ("1.234", "1,234"): for an \
  AMOUNT that is normally a thousands separator, so both of those are 1234. If \
  that reading is genuinely in doubt, settle it from the other amounts on the \
  same document — the line totals, tax and total must add up, and only one \
  reading makes them add up.
- 🔴 FOR A QUANTITY, THE SAME THREE DIGITS ARE NORMALLY DECIMAL, NOT THOUSANDS. \
  Invoices rarely bill a thousand of anything and routinely bill fractions, so \
  "2.500" hours is 2.5 hours and "1.250 LB" is 1.25 pounds — reading those as \
  2500 and 1250 is the same error of a thousand arriving from the other side. \
  Settle it with the line's own arithmetic: quantity × unit price must equal \
  the line total, and only one reading makes it. Use the thousands reading for \
  a quantity only when the arithmetic demands it.
- A space or an apostrophe between digit groups is always a thousands \
  separator: "1 234,56" and "1'234.56" are both 1234.56.
A trailing or leading minus, a figure in parentheses, or a "CR" suffix all mean \
a negative amount; see the credit-note rule below for how to report it.

For product_code: extract any article/product/item code, SKU or part number \
shown on the line. This is the VENDOR's code for the item, not ours. Use null \
if no code is visible.

For expense_category: classify each line into one of the available categories \
listed below. Pick the single best match. If none fits well, use "other".

{categories_block}

Output JSON schema:
{{
  "type": "Invoice" or "CreditNote", decided by the DOCUMENT TYPE procedure above,
  "invoice_number": "string",
  "issue_date": "YYYY-MM-DD resolved by the DATES procedure above, or null if it does not settle",
  "due_date": "YYYY-MM-DD resolved by the DATES procedure above, or null if it does not settle",
  "currency": "ISO 4217 3-letter code resolved by the CURRENCY procedure above, or null if it does not settle",
  "currency_symbol": "a currency symbol or code PRINTED ANYWHERE ON THE DOCUMENT, verbatim, resolved by the CURRENCY SYMBOL procedure above, or null ONLY if the document carries no currency marking anywhere at all",
  "seller": {{
    "name": "string",
@@locale_seller_identifiers@@
    "street": "string or null",
    "city": "string or null",
@@locale_seller_region@@
    "postal_code": "string or null",
    "country_code": "2-letter ISO code of the vendor's OWN address, or null if the address does not identify one. An address identifies its country when its own format does, even with no country name printed: a 2-letter US state with a ZIP is US — 5 digits or ZIP+4, so '62701-1234' and '78701-4321' both count — and a Canadian province with a 'A1A 1A1' postal code is CA. That is reading the address, not inferring. Never take it from the currency, the language, or the other party's address",
    "phone": "the VENDOR'S OWN phone number as printed, resolved by the CONTACT DETAILS procedure above, or null",
    "email": "the VENDOR'S OWN email address, resolved by the CONTACT DETAILS procedure above, or null",
    "website": "the vendor's own website domain or URL, resolved by the CONTACT DETAILS procedure above, or null",
@@locale_seller_bank@@
  }},
  "buyer": {{
    "name": "string",
@@locale_buyer_identifier@@
    "street": "string or null",
    "city": "string or null",
@@locale_buyer_region@@
    "postal_code": "string or null",
    "country_code": "2-letter ISO code of the BUYER'S OWN address, read the same way as the seller's above — the address format identifies the country even when no country name is printed — or null if it does not. Never take it from the currency, the language, or the SELLER'S address: whether the two parties are in the same country decides whether the currency may be taken from it, and a buyer country copied from the vendor's makes that comparison a value against itself"
  }},
  "payment_reference": "string or null",
@@locale_payment_means@@
  "note": "free text notes or null",
  "lines": [
    {{
      "product_code": "seller's article/SKU code or null",
      "name": "article/product/service name",
      "description": "additional description or null",
      "quantity": number,
@@locale_unit_code@@
      "price_unit": number (price per unit, tax excluded),
      "line_total": number (quantity * price_unit - discount, tax excluded),
@@locale_tax_fields@@
      "expense_category": "category code from the list above"
    }}
  ],
  "tax_subtotals": [
    {{
      "tax_percent": number,
      "taxable_amount": number,
      "tax_amount": number,
      "tax_category": "S/Z/E/AE"
    }}
  ],
  "totals": {{
    "line_extension": number (sum of all line totals, tax excluded),
    "tax_exclusive": number (total before tax),
    "tax_inclusive": number (total including tax),
    "payable": number (amount to pay)
  }}
}}

Important rules:
- All monetary amounts must be numbers, not strings, read by the NUMBERS \
procedure above
- All dates must be YYYY-MM-DD, resolved by the DATES procedure above, and null \
when that procedure does not settle them
- "type" is decided by the DOCUMENT TYPE procedure above, not by looking for an \
English word
- For a credit memo, report the amounts as POSITIVE magnitudes. The document \
type ("CreditNote") already says it is a credit, so a minus sign as well would \
state the credit twice. If the invoice itself prints negatives, drop the signs. \
This applies ONLY to a "CreditNote": a negative line on an "Invoice" keeps its \
minus sign
- "currency" is resolved by the CURRENCY procedure above, and is null when that \
procedure does not settle it. Never default it to USD
- "currency_symbol" records what the PAGE printed, never what you concluded. It \
is null only when the document carries no symbol and no ISO code anywhere
- Copy identifiers and account numbers EXACTLY as printed. Do not add or remove \
hyphens, do not add a country prefix that is not there, and do not convert \
between identifier types
@@locale_rules@@
- Output ONLY the JSON object, no markdown, no explanation
"""

# The slots in template order; also the field list of
# `goodahead.invoice.prompt.profile`.
SLOT_NAMES = tuple(dict.fromkeys(_SLOT_RE.findall(PROCEDURES)))

LOCALE_US = "us"
LOCALE_EU = "eu"
# Names no country; used when no block fits the company's country.
LOCALE_GENERIC = "generic"

_US_BLOCK = {
    "locale_origin": """\
Most documents \
are from United States vendors; some are from vendors abroad, so handle both \
without forcing one into the other's vocabulary.""",
    "locale_tax": """\
TAX. US vendors charge SALES AND USE TAX, levied by state and often by county \
or city, shown as one or more lines such as "Sales Tax", "NJ Sales Tax", \
"State Tax", "County Tax". It is NOT value-added tax: there is no input/output \
mechanism and no VAT registration number involved. Report the combined rate \
actually applied to each line in "tax_percent" and the amount in "tax_amount". \
If several jurisdictions are listed separately for the same line, report their \
SUM as one rate and one amount, and list each distinct rate once in \
"tax_subtotals". A line that is not taxed (resale certificate, exempt goods, \
shipping in some states) gets tax_percent 0 and tax_category "Z"; a document \
with no tax at all is normal. For a non-US vendor charging VAT or GST, report \
that rate in the same fields.

TAX CATEGORY uses these codes regardless of country — they are the interchange \
format, not a claim about the tax system:
- "S" standard rated (any non-zero sales tax, VAT or GST)
- "Z" zero rated / not taxed
- "E" exempt (resale certificate, exempt organisation)
- "AE" reverse charge (non-US only; never applies to US sales tax)""",
    "locale_identifiers": """\
IDENTIFIERS. For a US vendor, "vat" is the federal EIN / Taxpayer \
Identification Number when one is printed (format 12-3456789) — many US \
invoices show none, which is normal, so use null rather than guessing. \
"business_id" is a state registration, resale or seller's permit number if \
shown. For a non-US vendor, "vat" is the VAT number with its country prefix \
(e.g. FI12345671) and "business_id" the national registration number.""",
    "locale_payment": """\
BANK AND REMITTANCE DETAILS. Extract whatever the document gives for paying it:
- US bank transfer / ACH / wire: "routing_number" is the 9-digit ABA routing \
  or transit number (sometimes labelled ABA, RTN, routing transit); \
  "account_number" is the vendor's bank account number. Copy the digits exactly \
  and do not pad, group or truncate them.
- Non-US bank transfer: "iban" and "bic" (SWIFT).
- "remit_to_address" is the mailing address for a CHECK when it differs from \
  the vendor's main address — US invoices often print "Remit To" or "Please \
  mail payments to" separately, and paying to the wrong one delays the payment.
Use null for anything not printed. Do NOT put a routing number in "iban", or an \
IBAN in "account_number"; if you are unsure which a number is, use null.

For payment references, preserve the exact format from the document. A US \
invoice usually has no structured payment reference — an invoice or account \
number quoted under "please reference" is the closest thing, and null is fine. \
For a non-US vendor, copy a national or ISO 11649 ("RF…") reference verbatim.""",
    "locale_seller_identifiers": """\
    "vat": "US federal EIN as printed (e.g. 12-3456789), or a non-US VAT number with its country prefix, or null",
    "business_id": "state registration / resale / seller's permit number, or a non-US national registration id, exactly as printed, or null",""",
    "locale_seller_region": """\
    "state_code": "2-letter US state code (e.g. NJ) or null",""",
    "locale_seller_bank": """\
    "routing_number": "9-digit US ABA routing number or null",
    "account_number": "US bank account number or null",
    "iban": "non-US IBAN or null",
    "bic": "non-US BIC/SWIFT or null",
    "remit_to_address": "the check remittance address if printed separately, as one string, or null\"""",
    "locale_buyer_identifier": """\
    "vat": "EIN or VAT number or null",""",
    "locale_buyer_region": """\
    "state_code": "2-letter US state code or null",""",
    "locale_payment_means": """\
  "payment_means_code": "30 for bank transfer/ACH, 20 for check, 48 for card, or null",""",
    "locale_unit_code": """\
      "unit_code": "UN/ECE Rec 20 code: C62=each/piece, HUR=hour, LBR=pound, ONZ=ounce, FOT=foot, INH=inch, GLL=US gallon, CS=case, BX=box, KGM=kg, MTR=meter, LTR=liter",""",
    "locale_tax_fields": """\
      "tax_percent": number (combined sales-tax rate on this line; VAT/GST for a non-US vendor),
      "tax_amount": number (tax amount for this line),
      "tax_category": "S=standard/taxed, Z=zero or untaxed, E=exempt, AE=reverse charge (non-US only)",""",
    "locale_rules": """\
- A non-US VAT number keeps its country prefix (e.g. FI, DE, SE, NL)""",
}

# EU/EEA wording. Identifiers are never derived from one another (a Finnish VAT
# number and business id share digits but are separate values), and national
# payment references are copied whatever their length: Norwegian and Swedish
# ones are longer than Finnish ones.
_EU_BLOCK = {
    "locale_origin": """\
Most documents \
are from vendors in the European Union; some are from vendors elsewhere, so \
handle both without forcing one into the other's vocabulary.""",
    "locale_tax": """\
TAX. EU vendors charge VALUE-ADDED TAX (VAT — ALV in Finland, Mehrwertsteuer \
in Germany, moms in Sweden), a single rate per line shown against the vendor's \
own VAT registration number. Report the rate applied to each line in \
"tax_percent" and the amount in "tax_amount", and list each distinct rate once \
in "tax_subtotals". Several rates on one document is ordinary — a standard \
rate beside a reduced one for food, books or transport — so do NOT sum them \
into one: a line carries the one rate charged on it. A line at 0% \
(intra-Community supply, export, exempt activity) gets tax_percent 0 and the \
tax_category below; a document with no tax at all is normal. For a vendor \
outside the EU charging sales tax or GST, report that rate in the same fields.

TAX CATEGORY uses these codes regardless of country — they are the interchange \
format, not a claim about the tax system:
- "S" standard rated (any non-zero VAT, GST or sales tax)
- "Z" zero rated / not taxed
- "E" exempt
- "AE" reverse charge — the vendor charges no tax and the BUYER accounts for \
it. An intra-Community supply to a VAT-registered buyer is normally this, and \
the document usually says so ("reverse charge", "käännetty verovelvollisuus", \
"Steuerschuldnerschaft des Leistungsempfängers", "omvänd skattskyldighet"). \
Do not confuse it with "Z": a reverse-charge line is taxable, just not here.""",
    "locale_identifiers": """\
IDENTIFIERS. "vat" is the EU VAT number WITH its country prefix, exactly as \
printed (e.g. FI12345671, DE123456789, SE556123456701). "business_id" is the \
national registration number, also exactly as printed — the Finnish Y-tunnus \
(e.g. 7654321-2), the Swedish organisationsnummer, the German \
Handelsregisternummer. 🔴 THE TWO ARE DIFFERENT FIELDS EVEN WHEN THEY SHARE \
DIGITS — a country's VAT number and its national registration number are often \
built from the same digits, and they are STILL two values: copy each into its \
own field in the form the page prints it, and never derive one from the other. \
Many invoices print only one of them, which is normal — use null for the one \
that is not there rather than constructing it. For a vendor outside the EU, \
"vat" is whatever national tax registration number is printed (a US federal \
taxpayer id, for example) and "business_id" the national registration number.""",
    "locale_payment": """\
BANK AND REMITTANCE DETAILS. Extract whatever the document gives for paying it:
- SEPA / bank transfer: "iban" is the IBAN with its country prefix, and "bic" \
  the BIC/SWIFT. Copy the characters exactly; do not re-space or re-group them.
- A vendor outside the SEPA area may print a local account and a bank or \
  routing code instead: put the account in "account_number" and the bank or \
  routing code in "routing_number".
- "remit_to_address" is a separate postal address for payment when the \
  document prints one. This is uncommon in the EU — use null unless the \
  document really states a different address for paying than for the vendor.
Use null for anything not printed. Do NOT put an IBAN in "account_number", or \
a local bank code in "iban"; if you are unsure which a number is, use null.

For payment references, preserve the exact format from the document. A \
national structured reference ("viitenumero", "KID", "OCR") is copied \
character for character as the page prints it, however long it is; an ISO \
11649 reference starts "RF" and two check digits (e.g. RF1510472) and is \
copied verbatim. Report null rather than an invoice number dressed up as a \
reference.""",
    "locale_seller_identifiers": """\
    "vat": "EU VAT number with its country prefix exactly as printed (e.g. FI12345671), or a non-EU national tax registration number, or null",
    "business_id": "national registration number exactly as printed (e.g. Finnish Y-tunnus 7654321-2), never derived from the VAT number, or null",""",
    "locale_seller_region": """\
    "state_code": "2-letter code of the state, province or region when the address prints one — most EU addresses do not — or null",""",
    "locale_seller_bank": """\
    "iban": "IBAN with its country prefix or null",
    "bic": "BIC/SWIFT or null",
    "account_number": "a local (non-IBAN) bank account number or null",
    "routing_number": "a local bank, clearing or routing code or null",
    "remit_to_address": "a separate postal address for payment if the document prints one, as one string, or null\"""",
    "locale_buyer_identifier": """\
    "vat": "VAT number with its country prefix or null",""",
    "locale_buyer_region": """\
    "state_code": "2-letter state/province/region code if the address prints one, or null",""",
    "locale_payment_means": """\
  "payment_means_code": "30 for bank transfer, 58 for SEPA credit transfer, 20 for cheque, 48 for card, or null",""",
    "locale_unit_code": """\
      "unit_code": "UN/ECE Rec 20 code: C62=each/piece, HUR=hour, KGM=kg, GRM=gram, TNE=tonne, MTR=meter, MTK=square meter, MTQ=cubic meter, LTR=liter, DAY=day, MON=month, etc. — use the code for whatever unit the document prints, including one not listed here",""",
    "locale_tax_fields": """\
      "tax_percent": number (the VAT rate on this line; sales tax or GST for a vendor outside the EU),
      "tax_amount": number (tax amount for this line),
      "tax_category": "S=standard/taxed, Z=zero or untaxed, E=exempt, AE=reverse charge",""",
    "locale_rules": """\
- A VAT number keeps its country prefix (e.g. FI, DE, SE, NL), and is copied as printed — never built out of a national registration number""",
}

# Same schema keys as the other blocks, but no country's tax, identifier or
# payment vocabulary is assumed.
_GENERIC_BLOCK = {
    "locale_origin": """\
Documents arrive \
from vendors in different countries, so do not force one country's vocabulary \
onto another's document.""",
    "locale_tax": """\
TAX. Report whatever tax the document charges on each line: the rate in \
"tax_percent" and the amount in "tax_amount", and each distinct rate once in \
"tax_subtotals". Countries differ in how they charge it — one rate per line \
against a registration number, or several local rates added together on the \
same line — so read what the document shows rather than assuming a system. If \
several jurisdictions are listed separately for the same line AND the document \
presents their total as the tax on that line, report their sum as one rate and \
one amount; if the document states separate rates for separate lines, keep \
them apart. A line charged nothing gets tax_percent 0 and the tax_category \
below; a document with no tax at all is normal.

TAX CATEGORY uses these codes regardless of country — they are the interchange \
format, not a claim about the tax system:
- "S" standard rated (any non-zero tax)
- "Z" zero rated / not taxed
- "E" exempt
- "AE" reverse charge — the vendor charges no tax and the BUYER accounts for \
it. Only where the document says so; it is not the same as "Z".""",
    "locale_identifiers": """\
IDENTIFIERS. "vat" is the vendor's tax registration number as printed — a VAT \
number with a country prefix, a national taxpayer identification number, \
whatever form the country uses. "business_id" is a separate company, trade or \
registration number when the document prints one. 🔴 COPY EACH EXACTLY AND \
NEVER DERIVE ONE FROM THE OTHER, even where a country's two numbers are built \
from the same digits. Many invoices print neither, which is normal: use null \
rather than guessing or constructing a value.""",
    "locale_payment": """\
BANK AND REMITTANCE DETAILS. Extract whatever the document gives for paying it:
- An IBAN goes in "iban" and its BIC/SWIFT in "bic".
- A local account number goes in "account_number", and the bank, clearing or \
  routing code that goes with it in "routing_number".
- "remit_to_address" is a separate postal address for payment when the \
  document prints one that differs from the vendor's own address.
Copy every number exactly: do not pad, group, re-space or truncate it. Use null \
for anything not printed. Do NOT put an IBAN in "account_number", or a bank \
code in "iban"; if you are unsure which a number is, use null.

For payment references, preserve the exact format from the document. A \
structured national reference is copied character for character; an ISO 11649 \
reference starts "RF" and two check digits and is copied verbatim; where the \
document has no reference of its own, null is the right answer and an invoice \
number is not a substitute.""",
    "locale_seller_identifiers": """\
    "vat": "the vendor's tax registration number exactly as printed, with any country prefix it carries, or null",
    "business_id": "a separate company/trade registration number exactly as printed, never derived from the tax number, or null",""",
    "locale_seller_region": """\
    "state_code": "2-letter code of the state, province or region when the address prints one, or null",""",
    "locale_seller_bank": """\
    "routing_number": "a bank, clearing or routing code or null",
    "account_number": "a local (non-IBAN) bank account number or null",
    "iban": "IBAN with its country prefix or null",
    "bic": "BIC/SWIFT or null",
    "remit_to_address": "a separate postal address for payment if the document prints one, as one string, or null\"""",
    "locale_buyer_identifier": """\
    "vat": "the buyer's tax registration number or null",""",
    "locale_buyer_region": """\
    "state_code": "2-letter state/province/region code if the address prints one, or null",""",
    "locale_payment_means": """\
  "payment_means_code": "30 for a bank transfer, 20 for a cheque, 48 for a card, or null",""",
    "locale_unit_code": """\
      "unit_code": "UN/ECE Rec 20 code: C62=each/piece, HUR=hour, KGM=kg, GRM=gram, TNE=tonne, MTR=meter, MTK=square meter, MTQ=cubic meter, LTR=liter, DAY=day, MON=month, etc. — use the code for whatever unit the document prints, including one not listed here",""",
    "locale_tax_fields": """\
      "tax_percent": number (the tax rate charged on this line),
      "tax_amount": number (tax amount for this line),
      "tax_category": "S=standard/taxed, Z=zero or untaxed, E=exempt, AE=reverse charge",""",
    "locale_rules": """\
- A tax registration number keeps whatever country prefix the document prints, and is copied as printed""",
}

LOCALE_BLOCKS = {
    LOCALE_US: _US_BLOCK,
    LOCALE_EU: _EU_BLOCK,
    LOCALE_GENERIC: _GENERIC_BLOCK,
}

# EU plus the rest of the EEA, which shares the VAT, IBAN/BIC and payment
# reference vocabulary. Other VAT countries get the generic block.
_EU_EEA = frozenset("""
    AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI
    ES SE IS LI NO
""".split())


def locale_for_country(country_code):
    """Which locale block a 2-letter ISO country code selects, or None.

    None means no block fits that country; the caller logs it and falls back
    to `LOCALE_GENERIC`. Canada, for example, gets None: it has no EIN, ABA
    routing number or state sales tax.
    """
    code = str(country_code or "").strip().upper()
    if len(code) != 2:
        return None
    if code == "US":
        return LOCALE_US
    if code in _EU_EEA:
        return LOCALE_EU
    return None


def build_system_prompt(locale=None):
    """The procedures with one locale block substituted in.

    ``locale`` is a shipped block's name (`LOCALE_US`, …) or a mapping of every
    name in `SLOT_NAMES` to its text, as a custom prompt profile passes it.

    Returns the prompt still carrying `{categories_block}`. An unknown or
    missing ``locale`` composes `LOCALE_GENERIC`, never `LOCALE_US`, and logs a
    warning.
    """
    if isinstance(locale, Mapping):
        return compose_system_prompt(locale, "custom")
    raw = "" if not locale else str(locale).strip()
    if not raw:
        _logger.warning(
            "AI invoice import: no prompt locale was named, so the "
            "country-neutral block is composed. No US or EU identifier, tax or "
            "payment wording is applied — the prompt is weaker than either "
            "shipped one, and nothing on the resulting bill would show that."
        )
        key = LOCALE_GENERIC
    else:
        key = raw.lower()

    block = LOCALE_BLOCKS.get(key)
    if block is None:
        _logger.warning(
            "AI invoice import: %r is not a prompt locale this module ships "
            "(known: %s) — composing the country-neutral block. No US wording "
            "is applied.",
            locale,
            ", ".join(sorted(LOCALE_BLOCKS)),
        )
        block = LOCALE_BLOCKS[LOCALE_GENERIC]
    return compose_system_prompt(block, key)


def compose_system_prompt(block, label):
    """Substitute ``block`` into the procedures; ``label`` names it in errors.

    Braces in the block are escaped, because the result later goes through
    `str.format` to fill `{categories_block}`.
    """
    block = {
        name: str(text).replace("{", "{{").replace("}", "}}")
        for name, text in block.items()
    }
    # Checked before substituting, so a short block raises a clear ValueError
    # rather than a KeyError from inside the lambda.
    missing = sorted(set(_SLOT_RE.findall(PROCEDURES)) - set(block))
    if missing:
        raise ValueError(
            "the %r prompt locale block fills none of these slots the prompt "
            "template declares: %s" % (label, ", ".join(missing))
        )

    prompt = _SLOT_RE.sub(lambda m: block[m.group(1)], PROCEDURES)
    if "@@" in prompt:
        # A marker the slot regex cannot parse, such as `@@locale-tax@@`.
        raise ValueError(
            "the %r prompt locale left a slot unfilled: %s"
            % (label, _unfilled_markers(prompt))
        )
    return prompt


def _unfilled_markers(prompt):
    """Describe the markers left in ``prompt`` after substitution."""
    found = sorted(set(_ANY_MARKER_RE.findall(prompt)))
    if found:
        return ", ".join(found)
    at = prompt.index("@@")
    return ("no @@name@@ marker parses; the text around it reads %r"
            % prompt[max(0, at - 40):at + 40])


# The US prompt. The prompt actually sent is `build_system_prompt(self.locale)`,
# chosen per company; the two coincide only for a US company.
SYSTEM_PROMPT = build_system_prompt(LOCALE_US)


class ClaudeExtractionError(Exception):
    """Raised when Claude API call fails or returns invalid data.

    ``data`` carries the parsed JSON when validation failed after parsing, so
    callers can store it for correction and re-import without a new API call.
    """

    def __init__(self, message, data=None):
        super().__init__(message)
        self.data = data


class ClaudeInvoiceExtractor:
    """Turns an invoice PDF into the extraction dict.

    ``client`` is `goodahead.claude.client`, injected so tests can pass a stub
    and never reach the network.
    """

    def __init__(self, client, model=None, company=None,
                 currency_from_country=None, currency_is_active=None,
                 locale=None):
        self.client = client
        self.model = model
        self.company = company
        # A shipped locale name or a custom block mapping (see
        # `build_system_prompt`). None composes the country-neutral block.
        self.locale = locale
        # `currency_from_country(country_code) -> "USD" | None`, injected so
        # this module needs no ORM access. None disables the country fallback.
        self.currency_from_country = currency_from_country
        # `currency_is_active(name) -> bool`: whether this database trades in
        # the currency. A separate question from the country's currency; None
        # answers yes.
        self.currency_is_active = currency_is_active

    def extract_invoice(self, pdf_bytes, expense_categories=None):
        """Send PDF to Claude API and extract structured invoice data.

        Args:
            pdf_bytes: raw PDF file content
            expense_categories: list of {"code": "...", "name": "..."} dicts

        Returns:
            dict with "data" (extracted invoice) and "usage" (token counts)

        Raises:
            ClaudeExtractionError: on API failure or invalid response
        """
        categories_block = self._build_categories_block(expense_categories)
        system_prompt = build_system_prompt(self.locale).format(
            categories_block=categories_block
        )
        pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")

        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": "Extract all invoice data from this document.",
                    },
                ],
            }
        ]

        # `max_tokens`, `timeout` and `requires_pdf` are passed per call so the
        # shared client's defaults stay untouched for its other users. A None
        # model lets the client resolve the company default or the newest
        # registered model; `requires_pdf` keeps it from picking one that cannot
        # read PDFs.
        try:
            result = self.client.messages_create(
                messages=messages,
                system=system_prompt,
                model=self.model,
                max_tokens=MAX_TOKENS,
                company=self.company,
                timeout=REQUEST_TIMEOUT,
                requires_pdf=True,
            )
        except Exception as e:
            # The caller treats every failure here as terminal; only the
            # message matters.
            raise ClaudeExtractionError(f"Claude API request failed: {e}") from e

        usage = result.get("usage", {}) or {}
        usage = {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        }

        # Name the two endings that otherwise surface as a baffling JSON parse
        # error (a cut-off answer) or an "empty response" (a decline).
        stop_reason = result.get("stop_reason")
        if stop_reason == "max_tokens":
            raise ClaudeExtractionError(
                f"Claude's answer was cut off at the {MAX_TOKENS}-token limit. "
                f"Usage: {usage['input_tokens']} in / {usage['output_tokens']} out"
            )
        if stop_reason == "refusal":
            raise ClaudeExtractionError("Claude declined to process this document.")

        text_content = ""
        for block in result.get("content", []):
            if block.get("type") == "text":
                text_content += block.get("text", "")

        if not text_content.strip():
            raise ClaudeExtractionError(
                "Claude returned empty response. "
                f"Usage: {usage['input_tokens']} in / {usage['output_tokens']} out"
            )

        try:
            cleaned = text_content.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1]
                cleaned = cleaned.rsplit("```", 1)[0]
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise ClaudeExtractionError(
                f"Claude returned invalid JSON: {e}\n"
                f"Raw response:\n{text_content[:500]}"
            ) from e

        # Only this method holds both the PDF bytes and the extraction. The
        # verdict is stored in `data`, so the re-import path can read it
        # without the PDF.
        if isinstance(data, dict):
            data[CURRENCY_MARKING_KEY] = currency_marking_verdict(pdf_bytes, data)

        self._validate_extraction(data)

        return {"data": data, "usage": usage}

    def _build_categories_block(self, categories):
        if not categories:
            return "Available expense categories: goods, services, transport, it_software, office, marketing, rent, other"
        lines = ["Available expense categories:"]
        for cat in categories:
            lines.append(f'- "{cat["code"]}": {cat["name"]}')
        return "\n".join(lines)

    def _currency_is_tradeable(self, currency_name):
        """Does this database trade in that currency? Yes when not injected."""
        if not self.currency_is_active:
            return True
        return bool(self.currency_is_active(currency_name))

    def _currency_is_usable(self, currency):
        """Exactly three ASCII letters, the shape core's `_import_currency` matches."""
        if not isinstance(currency, str):
            return False
        currency = currency.strip()
        return (
            len(currency) == 3 and currency.isascii() and currency.isalpha()
        )

    def _resolve_currency_from_country(self, data):
        """A domestic invoice that prints no currency is in the domestic currency.

        Applies only when the page prints no currency marking at all (see
        `nothing_was_printed`) and seller and buyer are in the same country,
        both taken from the document. Any printed marking blocks it: an
        ambiguous symbol such as `¥` or `$` must not be settled by the buyer's
        country. Mutates ``data`` in place.

        The prompt asks the model for the same inference, so an already-set
        currency is only marked as inferred, never rewritten, and only when it
        equals the country's own currency. The marker means "no currency was
        printed, so the value was deduced", not "the address is why"; the
        chatter note in `account_move._ai_post_process` is worded to match.
        """
        if not self.currency_from_country:
            return

        # An already-set currency may be marked but never written.
        already_set = self._currency_is_usable(data.get("currency"))

        if not nothing_was_printed(data):
            return

        seller_cc = one_country_on_both_sides(data)
        if not seller_cc:
            return

        resolved = self.currency_from_country(seller_cc)
        if not self._currency_is_usable(resolved):
            # No usable currency for the country. With no currency set, the
            # required-field check below fails the import and stores the JSON.
            _logger.info(
                "AI invoice import: both parties are in %s and nothing was "
                "printed, but %r gives no usable currency — the bill is left "
                "exactly as the extraction had it (%s)",
                seller_cc,
                seller_cc,
                "currency already %r" % data.get("currency")
                if already_set
                else "no currency",
            )
            return

        resolved = resolved.strip().upper()
        if already_set and data["currency"].strip().upper() != resolved:
            # Something other than the country settled the currency: leave it
            # alone and do not mark it.
            _logger.info(
                "AI invoice import: nothing printed and both parties in %s, "
                "but the extraction says %s and %s would give %s — something "
                "other than the country settled it, so it is left alone and "
                "not marked",
                seller_cc,
                data["currency"],
                seller_cc,
                resolved,
            )
            return

        if not already_set and not self._currency_is_tradeable(resolved):
            # Do not choose an archived currency. Core's `_import_currency`
            # would accept it with only a log line, and with no maintained rate
            # (1.0) the company-currency figures would silently be wrong. This
            # does not apply when the extraction already set the currency.
            _logger.warning(
                "AI invoice import: nothing printed and both parties in %s, "
                "whose currency is %s — but %s is archived on this database, "
                "so it is not written. Activate it or set the currency by "
                "hand on the stored JSON",
                seller_cc,
                resolved,
                resolved,
            )
            return

        data["currency"] = resolved
        data[CURRENCY_INFERRED_KEY] = seller_cc
        _logger.info(
            "AI invoice import: no currency symbol or code on the document and "
            "both parties are in %s — currency %s is the one "
            "res.country.currency_id gives for it (%s)",
            seller_cc,
            resolved,
            "already set by the extraction" if already_set else "set here",
        )

    def _validate_extraction(self, data):
        """Validate that Claude's extraction has required fields."""
        if not isinstance(data, dict):
            raise ClaudeExtractionError(
                f"Expected JSON object, got {type(data).__name__}"
            )

        # Before the required-field check, which would reject a null currency.
        self._resolve_currency_from_country(data)

        # `currency` is required: downstream, a missing or unknown code would
        # silently become a default currency. Failing stores the extraction so
        # it can be corrected and re-imported without a new API call.
        required_fields = [
            "type",
            "invoice_number",
            "issue_date",
            "currency",
            "seller",
            "lines",
            "totals",
        ]
        missing = [f for f in required_fields if not data.get(f)]
        if missing:
            raise ClaudeExtractionError(
                f"Missing required fields in extraction: {', '.join(missing)}",
                data=data,
            )

        # A symbol such as "$" or "￥" would match no currency in core and
        # silently become the company currency.
        currency = data["currency"]
        if not self._currency_is_usable(currency):
            raise ClaudeExtractionError(
                f"Currency {currency!r} is not a 3-letter ISO 4217 code "
                f"(e.g. USD, EUR, CNY)",
                data=data,
            )

        if data["type"] not in ("Invoice", "CreditNote"):
            raise ClaudeExtractionError(
                f"Invalid document type: {data['type']}. Expected 'Invoice' or 'CreditNote'",
                data=data,
            )

        if not isinstance(data["lines"], list) or not data["lines"]:
            raise ClaudeExtractionError(
                "No invoice lines extracted from the document", data=data
            )

        seller = data["seller"]
        if not isinstance(seller, dict) or not seller.get("name"):
            raise ClaudeExtractionError(
                "Seller name is missing from extraction", data=data
            )

        self._validate_arithmetic(data)

    # Arithmetic checks. Each failure carries the parsed JSON and names the
    # figure at fault, so it can be corrected and re-imported without a new API
    # call. The checks are deliberately loose enough not to fail correct invoices.

    def _number(self, container, key, where, data, required=True, nullable=False):
        """One numeric field, or a named failure.

        A null amount fails rather than becoming 0.00 downstream.
        `required=False` tolerates an absent key, never a null one.
        `nullable=True` returns a null as `None` (used for a line's tax amount).
        """
        if key not in container:
            if not required:
                return None
            raise ClaudeExtractionError(
                f"{where}: {key} is missing from the extraction. It is an amount the "
                f"bill is built from, and a missing amount must not be read as zero.",
                data=data,
            )
        value = container[key]
        if value is None and nullable:
            return None
        if value is None:
            raise ClaudeExtractionError(
                f"{where}: {key} is null. An amount Claude could not read must not "
                f"become 0.00 on the bill — fill it in below and re-import, or clear "
                f"the whole import if the document really does not state it.",
                data=data,
            )
        # `bool` is an `int` in Python, and `True` would otherwise reconcile as 1.0.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ClaudeExtractionError(
                f"{where}: {key} is {value!r}, which is not a number. Amounts must be "
                f"JSON numbers — a string has not been through the NUMBERS procedure "
                f"and its separators mean nothing yet.",
                data=data,
            )
        return float(value)

    def _reconciles(self, actual, expected, accumulated=True):
        """Within tolerance? See `TOLERANCE_ABS` / `TOLERANCE_REL`.

        `accumulated=False` drops the relative term, for comparing two figures
        that restate the same number rather than a sum of rounded parts.
        """
        tolerance = TOLERANCE_ABS
        if accumulated:
            tolerance = max(tolerance, TOLERANCE_REL * abs(expected))
        return abs(actual - expected) <= tolerance

    def _validate_arithmetic(self, data):
        """Reconcile lines, tax and totals against each other.

        Signs are left alone: every check is linear, so an all-negative credit
        note reconciles like an all-positive one, while sections that disagree
        about the sign are caught.
        """
        totals = data["totals"]
        if not isinstance(totals, dict):
            raise ClaudeExtractionError(
                f"'totals' is {type(totals).__name__}, expected an object with "
                f"line_extension / tax_exclusive / tax_inclusive / payable",
                data=data,
            )
        line_extension = self._number(totals, "line_extension", "totals", data)
        tax_exclusive = self._number(totals, "tax_exclusive", "totals", data)
        tax_inclusive = self._number(totals, "tax_inclusive", "totals", data)
        payable = self._number(totals, "payable", "totals", data)

        line_sum = 0.0
        for index, line in enumerate(data["lines"], start=1):
            if not isinstance(line, dict):
                raise ClaudeExtractionError(
                    f"Line {index} is {type(line).__name__}, expected an object",
                    data=data,
                )
            where = f"Line {index} ({line.get('name') or 'unnamed'})"
            line_total = self._number(line, "line_total", where, data)
            # A null line tax is accepted: receipts often print tax only for the
            # whole document. The document tax comes from the subtotals and
            # Odoo takes each line's tax from its rate; a resulting total
            # mismatch is flagged by `account.move.ai_total_mismatch`.
            self._number(line, "tax_amount", where, data, nullable=True)
            quantity = self._number(line, "quantity", where, data, required=False)
            price_unit = self._number(line, "price_unit", where, data, required=False)
            self._number(line, "tax_percent", where, data, required=False)
            line_sum += line_total
            self._validate_line_magnitude(where, quantity, price_unit, line_total, data)

        subtotals = data.get("tax_subtotals") or []
        if not isinstance(subtotals, list):
            raise ClaudeExtractionError(
                f"'tax_subtotals' is {type(subtotals).__name__}, expected a list",
                data=data,
            )
        subtotal_tax = 0.0
        for index, subtotal in enumerate(subtotals, start=1):
            if not isinstance(subtotal, dict):
                raise ClaudeExtractionError(
                    f"Tax subtotal {index} is {type(subtotal).__name__}, expected an "
                    f"object",
                    data=data,
                )
            where = f"Tax subtotal {index}"
            self._number(subtotal, "taxable_amount", where, data)
            subtotal_tax += self._number(subtotal, "tax_amount", where, data)
            self._number(subtotal, "tax_percent", where, data, required=False)

        if subtotals:
            tax_total = subtotal_tax
            tax_source = f"the {len(subtotals)} tax subtotal(s)"
        else:
            # No subtotals: use the lines' own tax. An untaxed document then
            # reduces to "tax_exclusive == tax_inclusive".
            tax_total = sum(line.get("tax_amount") or 0.0 for line in data["lines"])
            tax_source = "the lines' own tax amounts (no tax subtotals were extracted)"

        if not self._reconciles(line_sum, line_extension):
            raise ClaudeExtractionError(
                f"The line totals do not add up to the document total: the "
                f"{len(data['lines'])} line(s) sum to {line_sum:.2f} but "
                f"totals.line_extension says {line_extension:.2f} (out by "
                f"{line_sum - line_extension:.2f}). One of those two figures is "
                f"misread, or a line is missing.",
                data=data,
            )

        if not self._reconciles(tax_exclusive + tax_total, tax_inclusive):
            raise ClaudeExtractionError(
                f"The tax does not add up: totals.tax_exclusive {tax_exclusive:.2f} "
                f"plus {tax_total:.2f} of tax from {tax_source} is "
                f"{tax_exclusive + tax_total:.2f}, but totals.tax_inclusive says "
                f"{tax_inclusive:.2f} (out by "
                f"{tax_exclusive + tax_total - tax_inclusive:.2f}).",
                data=data,
            )

        if not self._reconciles(payable, tax_inclusive, accumulated=False):
            raise ClaudeExtractionError(
                f"totals.payable {payable:.2f} is not totals.tax_inclusive "
                f"{tax_inclusive:.2f} (out by {payable - tax_inclusive:.2f}). This "
                f"schema has no slot for a prepayment, a rounding line or a credit "
                f"applied against the bill, so a difference here means one of the two "
                f"was misread.",
                data=data,
            )

    def _validate_line_magnitude(self, where, quantity, price_unit, line_total, data):
        """Catch a quantity or unit price misread by a factor of 100 or more.

        The document sums cannot see this: the line total is right, and the
        generated unit price is derived from it. See `LINE_RATIO_LIMIT`.
        """
        if quantity is None or price_unit is None:
            return
        gross = abs(quantity) * abs(price_unit)
        net = abs(line_total)
        # A zero on either side (free-of-charge line) says nothing.
        if not gross or not net:
            return
        ratio = gross / net if gross > net else net / gross
        if ratio < LINE_RATIO_LIMIT:
            return
        raise ClaudeExtractionError(
            f"{where}: quantity {quantity:g} × unit price {price_unit:g} is "
            f"{gross:.2f}, which is {ratio:.0f}× the line total {line_total:.2f}. A "
            f"discount cannot account for that; one of the three was read with its "
            f"decimal point or its thousands separator in the wrong place. The "
            f"document's own totals CANNOT catch this — they are built from the line "
            f"total, which is the figure that is right.",
            data=data,
        )
