# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Invoice extraction prompt, response handling and validation.

🔴 This module no longer speaks HTTP. In this tree `goodahead_claude` owns the
Claude transport, the per-company API key and the `goodahead.claude.model`
registry, so the source module's own `requests` client, `test_connection`,
`list_models` and `DEFAULT_MODEL` were all deleted rather than ported. What is
left is the part that is actually about invoices: the prompt, the PDF framing,
the stop-reason handling and the schema validation.

`DEFAULT_MODEL` in particular is NOT reintroduced here. It was
`claude-sonnet-4-20250514` in the source, which is retired; `goodahead_claude`
resolves the model from the fetched registry precisely because a hardcoded
literal had already gone stale once and every unnamed call hit a 404 that no
gate could see.
"""

import base64
import json
import logging
import re
from collections.abc import Mapping

# 🔴 RE-EXPORTED, NOT DUPLICATED (core/source split). The currency keys, the three
# currency questions, the reconciliation tolerances and the validators moved to
# the import core, which every invoice source shares. Every name that used to be
# importable from here still is.
from odoo.addons.goodahead_invoice_import.lib.invoice_data import (  # noqa: F401
    _ABSENT,
    CURRENCY_INFERRED_KEY,
    CURRENCY_MARKING_KEY,
    CURRENCY_SYMBOL_KEY,
    LINE_RATIO_LIMIT,
    TOLERANCE_ABS,
    TOLERANCE_REL,
    InvoiceDataError,
    InvoiceDataValidator,
    currency_marking_verdict,
    nothing_was_printed,
    one_country_on_both_sides,
)

_logger = logging.getLogger(__name__)

# Newer models (Opus 5.5 and up) always think first, and the thinking counts
# against max_tokens: leave room for it on a long, many-line invoice.
#
# 🔴 Passed PER CALL. `goodahead_claude`'s own default is 4096 and the company
# default on this instance is 8000; raising either would also raise it for
# fraud detection, which shares the client.
MAX_TOKENS = 16000
# Thinking makes a response slower; a timeout is a failed (and billed) import.
# Per call for the same reason — the shared default is 120 s.
REQUEST_TIMEOUT = 300


# ---------------------------------------------------------------------------
# The prompt: four country-neutral PROCEDURES with a locale block slotted in
# ---------------------------------------------------------------------------
#
# 🔴 the dual-core design. The module is going open-source and runs on a Finnish
# deployment as well as a US one, so the US tax, identifier and payment wording
# had to stop being the only wording there is.
#
# WHAT STAYS, AND WHY IT IS NOT A LOCALE QUESTION. The four ordered procedures
# — DOCUMENT TYPE, CURRENCY, DATES, NUMBERS — are country-neutral BY
# CONSTRUCTION, not by luck. Each of them names several countries on purpose:
# DATES step 3 is a two-sided prior ("US vendors usually print MM/DD/YYYY;
# vendors in Europe, China and most of the rest of the world usually print
# DD/MM/YYYY") whose whole point is that neither side is proof; CURRENCY step 3
# lists a Finnish VAT rate beside a US state rate and an IBAN prefix beside an
# ABA routing number, because the model is being told to read evidence, not to
# apply a nationality. Moving any of that into a locale block would make each
# deployment blind to the other half of its own evidence, which is the defect
# 19.0.2.2.0 was written to remove. They stay in all three blocks.
#
# 🔴 WHERE THE US WORDING ACTUALLY SITS — AND IT IS NOT THREE TIDY BLOCKS.
# the dual-core design names three contiguous regions (tax, identifiers, payment) and says
# the extraction schema is unaffected. Measured against this file, the US
# wording sits in THIRTEEN places, and NINE of them are inside the JSON schema
# and the `Important rules:` tail that §8 says must not fork. The schema KEYS
# do not fork — that promise holds and `test_the_schema_keys_are_identical_in_
# every_locale` pins it — but their DESCRIPTIONS are where "2-letter US state
# code", "9-digit US ABA routing number" and "GLL=US gallon" live. So the
# locale block is a set of named slots substituted at thirteen points, not a
# paragraph appended after the procedures.
#
# That also means the composition F31 writes as
# `instructions = procedures + locale_block(country) + categories_block`
# cannot be a concatenation here without reordering the prompt — and the
# composed US prompt has to stay BYTE-IDENTICAL, because the US deployment is in
# production on it (`tests/data/system_prompt_us_55ac5e7.txt`).
#
# WHY `@@name@@` AND NOT `{name}`. `SYSTEM_PROMPT` is run through `str.format`
# later, to fill `{categories_block}`, and the schema is full of doubled braces
# for exactly that reason. A second `{}` placeholder would have to survive the
# first pass and be escaped out of the second, and a `%`/`$` template would
# collide with the prompt's own "6.625%" and "$". A marker that is not
# templating syntax at all has no escaping rules to get wrong, and
# `build_system_prompt` raises if one is left unfilled.
_SLOT_RE = re.compile(r"@@([a-z_]+)@@")

# ⚠️ DELIBERATELY LOOSER THAN `_SLOT_RE`, and that is its only reason to exist.
# It is used to NAME what survived substitution, and anything `_SLOT_RE` matches
# has by then been substituted — so the error message has to be built with a
# regex that catches the marker `_SLOT_RE` could not parse.
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
anywhere — the amounts are bare numbers such as "629.12" and "5,006.68" — and \
(b) the vendor's address and the buyer's address are in the SAME country. Then \
report that country's own currency: a domestic bill with unmarked amounts is \
denominated in the domestic currency, which is why nobody printed a symbol on \
it. ⚠️ THIS STEP DOES NOT APPLY WHEN A SYMBOL WAS PRINTED and you could not \
settle which currency it meant — a "$", "¥", "kr", "£" or 元 you could not \
place goes on to step 5, because the country is the wrong tiebreak for an \
ambiguous symbol and a Chinese vendor billing a US buyer must never become USD. \
It also does NOT apply when the two addresses are in different countries, or \
when either country is not shown. 🔴 AN ADDRESS SHOWS ITS COUNTRY WHEN ITS OWN \
FORMAT DOES — "FAIRVIEW AR 72000-0100" and "RIVERTON NY 14000-0100" are both \
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
    "country_code": "2-letter ISO code of the vendor's OWN address, or null if the address does not identify one. An address identifies its country when its own format does, even with no country name printed: a 2-letter US state with a ZIP is US — 5 digits or ZIP+4, so '72000-0100' and '14000-0100' both count — and a Canadian province with a 'A1A 1A1' postal code is CA. That is reading the address, not inferring. Never take it from the currency, the language, or the other party's address",
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

# The thirteen slots, in the order the template declares them. A prompt
# profile (`goodahead.invoice.prompt.profile`) has one text field per name, so
# this tuple is also that model's field list.
SLOT_NAMES = tuple(dict.fromkeys(_SLOT_RE.findall(PROCEDURES)))

# ---------------------------------------------------------------------------
# The locale blocks
# ---------------------------------------------------------------------------

LOCALE_US = "us"
LOCALE_EU = "eu"
# 🔴 THE THIRD BLOCK IS THE POINT OF THE OTHER TWO. the dual-core design shipped two — the US
# one and "a generic EU/VAT block as the other shipped option" — and left the
# question it raises unanswered: which one does a company in Japan, Brazil or
# nowhere-in-particular get? Two blocks means one of them is the default, and
# the default would have been the US one, because that is the deployment the
# module was written on. That is precisely the defect the dual-core design had just
# removed from the phone rule, where `US`/`CA` shaping was being applied to
# numbers that were never North American. So there is a third block that names
# no country, and `locale_for_country` answers None rather than guessing.
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
(e.g. FI00000158) and "business_id" the national registration number.""",
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

# 🔴 THE VOCABULARY HERE IS TAKEN FROM AN EARLIER FORK OF THIS MODULE, NOT
# INVENTED. *(read, 2026-10-10)* That fork's 90-line prompt is what a real
# Finnish deployment ran. The ISO 11649 RF form, the Y-tunnus example
# `0000007-8`, the `FI00000158` VAT example and `58 for SEPA` all come from it.
#
# 🔴 AND TWO THINGS CAME ACROSS THAT SHOULD NOT HAVE, BOTH FOUND BY A REVIEW FINDING
# AND BOTH REPAIRED IN 19.0.3.14.0. They are listed here rather than in the
# changelog alone because the next person to copy from that fork will reach for
# the same two lines:
#
# A. The fork's *"Finnish national references: numeric string, 2-20 digits"* was
#    copied with the word "Finnish" dropped. `_EU_EEA` carries `NO` and `SE`,
#    and a Norwegian KID or a Swedish OCR reference runs to **25** digits — so
#    the block stated a Finland-only fact for a continent, inside the very
#    commit whose purpose was to stop one country's facts reaching another's
#    documents. `payment_reference` is optional, `_validate_extraction` never
#    looks at it and `ubl_generator` omits `cbc:PaymentID` when it is null, so
#    the bill would have imported clean, been paid, and been unmatchable at the
#    vendor's bank. The generic block states no length and is the right version.
# B. Item 1 below was left behind as an INSTRUCTION and then restated as a
#    PREMISE: *"A Finnish VAT number is that country's business id with the
#    hyphen removed and FI in front"*, one clause from the ban on doing it. A
#    model told how to derive an identifier and then told not to has been given
#    a rope. The fact that the two numbers share digits is what matters and is
#    all that is said now.
#
# ⚠️ WHAT WAS DELIBERATELY NOT TAKEN FROM IT, and each of these is a defect
# this tree has already paid to fix:
#
# 1. *"For Finnish Y-tunnus without prefix, convert: remove hyphen, prepend
#    'FI'"* — an instruction to REWRITE an identifier, contradicted by its own
#    `business_id` line ("exactly as printed") four lines above it. This module
#    keys vendor matching on identifiers copied verbatim
#    (`goodahead.partner.identifier`), so a rewritten one is a key that matches
#    nothing, and the PROCEDURES' "Never convert, reformat or 'correct' an
#    identifier" rule is older and right.
# 2. `"issue_date": "YYYY-MM-DD"` with no "or null" — the exact schema line
#    19.0.2.2.0 fixed here, where the schema contradicted the procedure three
#    hundred words above it.
# 3. `"currency": "3-letter ISO code"` with no procedure and no null — the
#    guess 19.0.2.3.0 made into a loud failure.
# 4. Its document-type rule, a three-line keyword match, which this tree's
#    DOCUMENT TYPE procedure already absorbed and widened (its "hyvityslasku,
#    Gutschrift, kreditnota" are all in the procedure, beside nine more
#    languages and a UN/EDIFACT code).
# 5. Its Peppol EAS map, which 19.0.2.1.0 records as wrong in a way that broke
#    a real import — not prompt text, but the same "their fork is older"
#    reasoning applies and it is why nothing here is copied without a reason.
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
printed (e.g. FI00000158, DE123456789, SE556123456701). "business_id" is the \
national registration number, also exactly as printed — the Finnish Y-tunnus \
(e.g. 0000007-8), the Swedish organisationsnummer, the German \
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
    "vat": "EU VAT number with its country prefix exactly as printed (e.g. FI00000158), or a non-EU national tax registration number, or null",
    "business_id": "national registration number exactly as printed (e.g. Finnish Y-tunnus 0000007-8), never derived from the VAT number, or null",""",
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

# 🔴 NAMES NO COUNTRY, ON PURPOSE. It is not a weaker US block: every schema
# key is here and every procedure is above it. What it does not do is tell the
# model that one country's identifier, tax or payment vocabulary is the one to
# expect — because on a deployment whose country nobody has established, that
# would be a guess dressed as an instruction.
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

# EU plus the rest of the EEA. The block is about VAT vocabulary, IBAN/BIC and
# structured payment references, which Norway, Iceland and Liechtenstein share
# with the EU proper, so the set is wider than the political one on purpose.
#
# ⚠️ IT IS A LIST OF COUNTRIES THAT HAVE BEEN THOUGHT ABOUT, NOT A LIST OF
# COUNTRIES THAT USE VAT. Most of the world charges VAT or GST; this set names
# the ones whose identifier and payment wording the EU block actually fits. A
# country that is not here gets the generic block and a log line, which is the
# honest answer, rather than the nearest-looking one.
_EU_EEA = frozenset("""
    AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI
    ES SE IS LI NO
""".split())


def locale_for_country(country_code):
    """Which locale block a 2-letter ISO country code selects, or None.

    🔴 None MEANS "NOTHING HERE NAMES A BLOCK FOR THAT COUNTRY", and it is a
    different answer from `LOCALE_GENERIC`. The caller is expected to say so in
    a log line before falling back, because a deployment running the generic
    block is running a prompt weaker than either shipped one and nothing on the
    resulting bill would show it.

    It never answers `LOCALE_US` for a country that is not the United States.
    Canada is the case worth naming: it is the closest country to the US block
    and it has none of the three things that block is about — no EIN, no ABA
    routing number, no state sales tax — so it gets the generic block until
    somebody writes a Canadian one.
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

    ``locale`` is a shipped block's name (`LOCALE_US`, …) or a block itself —
    a mapping of every name in `SLOT_NAMES` to its text, which is what a custom
    prompt profile passes. A mapping is composed as given, with no fallback.

    Returns the prompt still carrying `{categories_block}`: `extract_invoice`
    fills that, and `SYSTEM_PROMPT.format(categories_block=...)` has to keep
    working because it is how every prompt test in this module reads it.

    An unknown or missing ``locale`` composes `LOCALE_GENERIC` — never
    `LOCALE_US` — and says so at WARNING, so a typo in a locale name cannot
    quietly ship one country's prompt to another's deployment.

    🔴 THE MISSING CASE USED TO BE SILENT, AND THE DOCSTRING ABOVE ALREADY
    CLAIMED IT WAS NOT (found in review). `str(locale or "").strip().lower() or
    LOCALE_GENERIC` turned a missing locale into the KNOWN key "generic", so
    `LOCALE_BLOCKS.get` succeeded and the warning branch below was never
    reached. On the Odoo path `_ai_prompt_locale` logs its own line, so the gap
    was invisible there and total everywhere else — and
    `ClaudeInvoiceExtractor(locale=None)` is the documented default, which is
    exactly where "this extractor is running the weakest prompt we ship" most
    needed saying.
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

    🔴 BRACES IN THE BLOCK ARE ESCAPED. The result goes through `str.format`
    to fill `{categories_block}`, so a `{` typed into a custom profile would
    either raise there or swallow text. No shipped block contains a brace, so
    for them this is a no-op and the composed US prompt stays byte-identical.
    """
    block = {
        name: str(text).replace("{", "{{").replace("}", "}}")
        for name, text in block.items()
    }
    # 🔴 CHECKED BEFORE THE SUBSTITUTION, NOT AFTER IT (found in review). A
    # slot the template declares and a block does not fill never reached the
    # `ValueError` below: `_SLOT_RE.sub(lambda m: block[m.group(1)], ...)`
    # raised `KeyError` from inside the lambda first. Loud, so never dangerous
    # — but it is not the error this function documents, and it names a bare
    # dict key rather than saying which locale block is short of what.
    missing = sorted(set(_SLOT_RE.findall(PROCEDURES)) - set(block))
    if missing:
        raise ValueError(
            "the %r prompt locale block fills none of these slots the prompt "
            "template declares: %s" % (label, ", ".join(missing))
        )

    prompt = _SLOT_RE.sub(lambda m: block[m.group(1)], PROCEDURES)
    if "@@" in prompt:
        # A marker the slot regex cannot parse — `@@locale-tax@@`, `@@1@@`.
        # Loud, because the alternative is shipping it to the API.
        raise ValueError(
            "the %r prompt locale left a slot unfilled: %s"
            % (label, _unfilled_markers(prompt))
        )
    return prompt


def _unfilled_markers(prompt):
    """What to print when `@@` survives substitution.

    🔴 IT USED TO PRINT `[]` (found in review). The old message read
    `_SLOT_RE.findall(prompt)` — but `_SLOT_RE` is the regex that just FAILED to
    match, by construction: anything it matches has already been substituted. So
    the one error whose whole job is to name the broken slot could only ever
    name an empty list. Measured, on `@@locale-tax@@`.
    """
    found = sorted(set(_ANY_MARKER_RE.findall(prompt)))
    if found:
        return ", ".join(found)
    at = prompt.index("@@")
    return ("no @@name@@ marker parses; the text around it reads %r"
            % prompt[max(0, at - 40):at + 40])


# The prompt as the US deployment runs it, and as every pre-existing prompt test in
# this module reads it. 🔴 It is NOT necessarily the prompt that is sent: that
# is `build_system_prompt(self.locale)`, chosen from the company's country. The
# two coincide only on a US company, which is why
# `test_the_composed_prompt_is_the_one_sent` asserts on the client's `system`
# argument rather than on this name.
SYSTEM_PROMPT = build_system_prompt(LOCALE_US)


class ClaudeExtractionError(InvoiceDataError):
    """Raised when Claude API call fails or returns invalid data.

    ``data`` carries the parsed JSON when the failure is a post-parse validation
    error (e.g. a missing required field). Callers persist it so a human can see
    exactly what Claude extracted and fix/re-import it without a new API call.
    """


# `test_connection` and `list_models` are deliberately absent: "Test Connection"
# and "Sync Models" live in `goodahead_claude`'s own settings block, against the
# same key. Two buttons doing the same thing against two clients is how the two
# drifted apart on the source project.


class ClaudeInvoiceExtractor(InvoiceDataValidator):
    """Turns an invoice PDF into the extraction dict, using whatever client it
    is given.

    ``client`` is `goodahead.claude.client` (an AbstractModel). It is injected
    rather than imported so that a test can pass a stub and NOTHING reaches the
    network — which is the only way these tests can run at all, since a real
    call costs money and the source project has already had one retry storm.
    """

    error_class = ClaudeExtractionError

    def __init__(self, client, model=None, company=None,
                 currency_from_country=None, currency_is_active=None,
                 locale=None):
        self.client = client
        self.model = model
        self.company = company
        # Which locale block the prompt is composed with — see
        # `build_system_prompt`. It is a plain string rather than a country
        # code because the SELECTION is a policy question
        # (`account_move._ai_prompt_locale` answers it from the company, and
        # logs when it cannot) while the COMPOSITION is a text question, and
        # keeping them apart is what lets a test exercise either one alone.
        # A custom prompt profile passes its own block here, as a mapping.
        #
        # 🔴 Left None it composes the COUNTRY-NEUTRAL block, never the US one.
        # Every lib-only test in this module constructs the extractor without a
        # locale, so a US default would have made the module's own suite
        # measure one country while reporting that it measured the rule.
        self.locale = locale
        # The two injected currency callables are the validator's — see
        # `InvoiceDataValidator.__init__` in the import core.
        super().__init__(
            currency_from_country=currency_from_country,
            currency_is_active=currency_is_active,
        )

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
        # F31's composition, in the one order the text allows:
        # procedures + locale block + categories. The locale block is
        # substituted at thirteen named slots rather than appended, because
        # nine of them are inside the JSON schema — see the block comment above
        # `PROCEDURES`.
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

        # 🔴 `max_tokens` and `timeout` are passed HERE, per call, and never set
        # as defaults on `goodahead_claude`. That client is shared with fraud
        # detection; its defaults are 4096 / 120 s, and moving them would move
        # fraud detection too.
        #
        # `model=self.model` may be None, which is the normal case: the client
        # then resolves the company default and, failing that, the newest model
        # in the fetched registry. A literal model id is deliberately not
        # supplied as a fallback — see the module docstring.
        #
        # `cache_static_system` is left at its default: the system prompt is the
        # same long block on every invoice, so it caches, while the PDF (the
        # part that differs) is in `messages` and does not.
        #
        # 🔴 `requires_pdf=True` is the whole of this feature's claim on the shared
        # client, and it is asked for PER CALL for exactly the reason `max_tokens` and
        # `timeout` are. `goodahead_claude` records `pdf_supported` for every model it
        # syncs and, until 19.0.1.4.0, NOTHING READ IT: the first sync that brought in a
        # newer model without PDF support would have silently retargeted this prompt —
        # which sends a PDF and nothing else — at a model that cannot read the document.
        # Fraud detection sends text, pins its own model, and passes `requires_pdf`
        # nowhere, so it is untouched by this.
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
            # Including `goodahead_claude`'s ClaudeAPIError and the UserError it
            # raises when no model can be resolved. The caller treats every
            # failure here as terminal, so the type does not need preserving —
            # the message does.
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

        # 🔴 THE SEAM, AND IT IS HERE FOR A REASON THE HOUSE RULE NAMES: the
        # plan clusters by intent, the chain cares where a rule SITS. The
        # intent is "the currency rule", which lives in
        # `_resolve_currency_from_country`; the chain needs the PDF BYTES, and
        # this method is the only point that holds them and the extraction at
        # the same time. `_validate_extraction` takes `data` alone and is
        # called from nowhere else, so widening its signature would move the
        # bytes through a method that has no other use for them. The verdict
        # goes INTO `data` instead — which also means it is stored, auditable,
        # and read by the re-import path in
        # `account_move._ai_surviving_currency_marker` without anything having
        # to keep a second copy of the PDF.
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
