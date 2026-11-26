# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""What an extracted invoice must look like before a bill is built from it.

Pure Python: imports nothing from the ERP. Every source of invoice data (the AI
reading of a PDF today; structured e-invoices next) produces the same
extraction dict, and the checks below are the ones every source has to pass.

Moved verbatim out of `goodahead_invoice_ai_import/lib/claude_client.py`
(core/source split), which re-exports every name here so existing imports keep
working. `ClaudeInvoiceExtractor` subclasses `InvoiceDataValidator` and raises
its own `ClaudeExtractionError` through `error_class`.
"""

import json
import logging
from decimal import ROUND_HALF_UP, Decimal

from . import pdf_currency

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The two keys the country fallback is built on (19.0.3.3.0)
# ---------------------------------------------------------------------------
#
# Named constants rather than literals because THREE layers read them — this
# validator, `account_move._ai_post_process` (which posts the chatter note) and
# the tests — and a string literal that drifts in one of them fails in the
# direction that looks like success: the note silently stops being posted while
# the currency is still being inferred.
#
# `currency_symbol` is an OBSERVATION the model now reports: what the page
# printed beside the amounts, or null. It had to be added, because the
# extraction as it stood carried no way at all to tell "the document printed
# nothing" from "the document printed something I could not place" — and that
# distinction is the entire safety boundary of the fallback below.
CURRENCY_SYMBOL_KEY = "currency_symbol"
# Set BY US, never by the model: the 2-letter country the currency was taken
# from. It rides in the stored extraction JSON (so it is auditable a year
# later) and it is what makes the bill say out loud that nobody printed a
# currency on the document.
CURRENCY_INFERRED_KEY = "currency_inferred_from_country"
# 🔴 SET BY US FROM THE PDF'S OWN BYTES (19.0.3.18.0), and it OUTRANKS
# `currency_symbol` above. See `lib/pdf_currency.py` for what it is built from;
# the short version is that `currency_symbol` is a model-reported observation,
# it was measured wrong on a test import of the one bill this feature exists for, and
# the SAME bytes produced two different answers on two runs. "Does this page
# print a currency marking" is a question about bytes, so it is asked of the
# bytes. The verdict rides in the stored extraction JSON beside the model's own
# answer, so an audit a year later can see which source decided and whether the
# two agreed.
CURRENCY_MARKING_KEY = "currency_marking"

# `data.get(key, _ABSENT)` — "the model did not report it" and "the model
# reported null" are DIFFERENT answers here, and only the second one licenses
# the fallback. Extraction JSON stored before 19.0.3.3.0 has no
# `currency_symbol` key at all, so a re-import of one must not infer.
_ABSENT = object()


def nothing_was_printed(data):
    """Does the page carry NO currency marking at all?

    The first of the three questions that make the marker safe. It is module
    level, and public, because the marker has to be asked about again at a second
    moment: `account_move._ai_post_process` re-validates a marker that arrived
    in stored JSON, and two copies of this reading would be two readings.

    🔴 THE TEXT LAYER WINS, AND IT WINS IN BOTH DIRECTIONS (19.0.3.18.0).

    Until then the only source was `currency_symbol`, which the MODEL reports.
    On ZZ Freight LLC bill `000000000001`, measured on a test import, that
    came back `"USD"` for a page which prints no currency marking anywhere —
    so the gate closed, nothing was inferred and nothing was said, on the one
    document the whole feature was written for. An earlier run on the
    same bytes returned `null`. A rule about money was hinged on a single
    model-reported observation that has been seen to flip.

    So when `pdf_currency` has an answer it is taken, and the model's is kept
    beside it for the audit trail:

    * text layer says NOTHING PRINTED, model says `"USD"` → nothing printed.
      That is today's bug, and taking the text layer is the fix.
    * text layer says PRINTED (`¥`), model says null → something printed.
      That is the dangerous direction, and taking the text layer closes the
      gate and fails the import loudly, which is 19.0.2.3.0's deliberate
      behaviour.

    Both disagreements therefore resolve toward the outcome a human can see:
    one posts a note that was missing, the other restores a failure that was
    about to become a silent guess. That symmetry is the argument for the text
    layer winning outright rather than, say, only when it finds something.

    ⚠️ UNKNOWN IS NOT AN ANSWER. A scanned or image-only PDF has no text layer,
    `pdf_currency` says so, and the model's report decides exactly as it did in
    19.0.3.3.0. Nothing is read off an empty text layer.
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
        return False  # pre-19.0.3.3.0 JSON: the question was never asked
    if printed is None:
        return True
    # The key is there and holds something. Only an empty/whitespace string
    # counts as "nothing printed"; a non-string is an answer we cannot read,
    # and an unreadable answer is not permission.
    return isinstance(printed, str) and not printed.strip()


def currency_marking_verdict(pdf_bytes, data):
    """Read the PDF's text layer and pair the answer with the model's.

    Returns the dict stored under `CURRENCY_MARKING_KEY`. It records BOTH
    sources and which one `nothing_was_printed` will act on, because the one
    thing a stored answer must never do is make it impossible to tell, later,
    where it came from.
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
        # 🔴 HOW MUCH TEXT THE ANSWER WAS READ OFF (19.0.3.19.0). Without it a
        # `nothing_printed` taken from a 121-character page and one taken from
        # the ZZ Freight LLC bill's roughly 3,000 are indistinguishable in the stored record —
        # and `nothing_printed` is the dangerous verdict, the one that opens
        # the gate. It is the number `MIN_TEXT_LAYER_ALNUM` is compared
        # against, counted over the content stream alone.
        #
        # ⚠️ SUBSCRIPT, NOT `.get(..., 0)` (19.0.3.20.0). All
        # four `return` paths of `scan_pdf_for_currency` set `alnum`, so the
        # default was unreachable — a repair for a state that cannot occur,
        # which reads as if the key were optional and would hide a real
        # regression behind a plausible zero.
        "alnum": scan["alnum"],
        "model_reported": model_reported,
        "model_symbol": reported if model_reported else None,
        "agree": agree,
    }

    if agree is False:
        # 🔴 DISAGREEMENT IS THE FINDING, so it is the one line at WARNING.
        # Both directions are named in the message because they mean opposite
        # things and a reader three months from now will have forgotten which
        # is which.
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

    The second of the three questions. 🔴 Both codes come from the DOCUMENT.
    `company.country_id` is deliberately never substituted for a buyer country
    the model did not report — that would make our own nationality the tiebreak
    for a foreign vendor's unmarked bill.
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


# ---------------------------------------------------------------------------
# Reconciliation tolerance
# ---------------------------------------------------------------------------
#
# 🔴 THE GREATER OF TWO CENTS AND HALF A PERCENT, and both halves are load-bearing.
#
# * The ABSOLUTE floor absorbs per-line rounding. Every amount in this pipeline is
#   written to two decimals (`ubl_generator._fmt_decimal`), and the vendor rounded its
#   own line totals before printing them, so a document can be a cent or two out while
#   being entirely correct. A floor of 0.01 would fail on a single half-cent; 0.02
#   covers the ordinary case without covering anything interesting.
# * The RELATIVE term absorbs the same rounding on a long document. Fifty lines each
#   rounded by half a cent is 0.25, which the floor alone would reject.
#
# What it must NOT absorb is the error class this exists to catch: a thousands
# separator read as a decimal point is a factor of 1000, a transposed pair of digits
# is tens of percent, and a dropped line is a whole line. All of them are orders of
# magnitude above 0.5%. The tolerance is deliberately generous, because a validator
# that fails a correct invoice gets switched off and then catches nothing at all.
TOLERANCE_ABS = 0.02
TOLERANCE_REL = 0.005

# 🔴 THE PER-LINE CHECK IS A RATIO, NOT AN EQUALITY, AND THAT IS NOT A WEAKENING —
# IT IS THE ONLY VERSION OF IT THAT CAN BE TURNED ON HERE.
#
# `quantity × price_unit == line_total` is NOT an invariant of this schema. The schema
# says `line_total` is "quantity * price_unit - discount", and many wholesale vendors
# print a list price and a discount on EVERY line —
# see `ubl_generator.generate_peppol_invoice_xml`, whose whole PriceAmount block exists
# because of it, and `tests/test_ubl_generator.test_discounted_line_emits_net_unit_price`,
# whose fixture is a list price well above its discounted net line. A strict equality here would
# reject every discounted invoice this business receives, which is a far worse outcome
# than the bug it would catch.
#
# A discount can only make `line_total` SMALLER, and never by three orders of magnitude.
# So the check is: the two sides may differ by any ordinary factor, and may not differ
# by 100× or more. A misread thousands separator is 1000×; a misplaced decimal point is
# 10× or 100×; a 99%-plus discount on a vendor bill is not a thing, and the one that
# genuinely is — a free-of-charge line, where `line_total` is 0 — is skipped outright.
LINE_RATIO_LIMIT = 100


class InvoiceDataError(Exception):
    """Raised when extracted invoice data does not pass validation.

    ``data`` carries the parsed JSON when the failure is a post-parse validation
    error (e.g. a missing required field). Callers persist it so a human can see
    exactly what was extracted and fix/re-import it without reading the
    document again.
    """

    def __init__(self, message, data=None):
        super().__init__(message)
        self.data = data


class InvoiceDataValidator:
    """The checks an extraction dict must pass, independent of who produced it.

    ``error_class`` is what every check raises; a source subclass sets its own
    (the AI source raises `ClaudeExtractionError`, a subclass of
    `InvoiceDataError`) so its callers and tests see the exception they always
    saw.
    """

    error_class = InvoiceDataError

    def __init__(self, currency_from_country=None, currency_is_active=None):
        # `currency_from_country(country_code) -> "USD" | None`, injected by
        # `account_move._ai_extract_and_generate_ubl` so that the lookup can
        # read `res.country.currency_id` without this module growing an ORM
        # import. Left None (the default) the fallback below is simply off,
        # which is what every lib-only test gets unless it opts in — so an
        # inference can never happen by accident.
        self.currency_from_country = currency_from_country
        # 🔴 A SECOND, SEPARATE QUESTION.
        # `currency_from_country` answers "what currency does this country
        # use", which is a fact about `res.country` and has nothing to do with
        # this database. `currency_is_active(name) -> bool` answers "do we
        # trade in it", which is a fact about `res.currency.active`.
        #
        # 19.0.3.3.0 collapsed the two into one `None` and that cost both
        # halves at once: the backstop could not write a currency the database
        # had archived (right answer, wrong reason — see
        # `_resolve_currency_from_country`), and the MARKER was suppressed on a
        # currency the model had already chosen, which silences the chatter
        # note for no gain. On a typical database only a handful of countries'
        # currencies are active, so that was the ordinary case.
        #
        # Left None it answers yes, which is what every lib-only test assumed
        # before the split and keeps them measuring the rule rather than the
        # database.
        self.currency_is_active = currency_is_active

    def _currency_is_tradeable(self, currency_name):
        """Does this database trade in that currency?

        A SEPARATE question from "what currency does this country use", and the
        two are asked at different moments — see `__init__`. Left uninjected it
        answers yes, so a lib-only caller measures the rule rather than a
        database it does not have."""
        if not self.currency_is_active:
            return True
        return bool(self.currency_is_active(currency_name))

    def _currency_is_usable(self, currency):
        """The shape core's `_import_currency` can actually match: exactly three
        ASCII letters. Kept in one place because the required-field check, the
        ISO-shape check and the fallback below all have to agree on it."""
        if not isinstance(currency, str):
            return False
        currency = currency.strip()
        return (
            len(currency) == 3 and currency.isascii() and currency.isalpha()
        )

    def _resolve_currency_from_country(self, data):
        """Last resort before a null currency fails the import: a DOMESTIC
        invoice that prints no currency marking at all is in the domestic
        currency. Mutates ``data`` in place and records that it did.

        🔴 THE WHOLE RULE IS THE DIFFERENCE BETWEEN TWO SILENCES, and getting
        it wrong is worse than the failure it replaces.

        * "The page printed nothing" — ZZ Freight LLC bill: amounts
          `612.84`, `4,218.00`, `2109.00`, `131.70`, no `$`, no ISO code, no
          IBAN, no ABA routing number, shipper and bill-to both US addresses.
          Nobody prints a symbol on a domestic bill. That is EVIDENCE, and the
          import used to fail on it and need the stored JSON hand-edited.
        * "The page printed something I could not place" — a `¥` from Shanghai
          billed to New Jersey. 19.0.2.3.0 made that a terminal failure ON
          PURPOSE: `¥` is CNY or JPY, a factor of about twenty, and `$` is USD,
          CAD, AUD, HKD, SGD or MXN. The BUYER's country is the wrong tiebreak
          for the SELLER's symbol, and a wrong currency produces a bill with
          entirely plausible numbers that nobody ever looks at again.

        Only "was anything printed" separates the two, which is why it had to
        be added to the extraction contract. The test is deliberately blunt:
        ANY marking blocks the fallback, not merely the ones on an ambiguity
        list. Keeping such a list server-side would mean maintaining a second
        copy of the prompt's, and the cost of being blunt is only that a
        `$`-marked US bill the model could not settle keeps failing — which is
        today's behaviour, not a regression.

        🔴 AND SINCE 19.0.3.18.0 THAT QUESTION IS ASKED OF THE PDF, NOT THE
        MODEL. `nothing_was_printed` reads `pdf_currency`'s verdict over the
        document's own text layer and falls back to the model's
        `currency_symbol` only when there is no text layer to read. The reason
        is measured and it is this bill: on one run the model reported
        `currency_symbol: "USD"` for a page that prints no currency marking
        anywhere, the gate closed, and the note below was never posted — on the
        one document the feature exists for. Its earlier run on the same bytes
        said `null`.

        The two countries both come from the DOCUMENT. `self.company.country_id`
        is deliberately NOT used as a stand-in for a buyer country the model did
        not report: that would make our own nationality the tiebreak for a
        foreign vendor's unmarked document, which is the step-2 mistake wearing
        a different hat.

        🔴 THIS IS A BACKSTOP, NOT THE PATH A REAL DOCUMENT TAKES, and the
        first version of it got that backwards. The prompt's CURRENCY step 4
        asks the MODEL to do the same inference, and on the one test import
        this feature was written for (bill 000000000001) the model
        did it: `currency` came back already "USD" with `currency_symbol`
        null. The code here then read a usable `currency` and returned at once,
        under a comment reading *"steps 1-3 settled it"* — which was simply
        FALSE. With step 4 in the prompt, a usable `currency` can equally mean
        "the model took it from the country", and `currency` alone cannot tell
        the two apart. So the marker was never set, the chatter note was never
        posted, and the bill said nothing about where its currency came from.

        So an already-set currency is no longer a reason to stop. The same
        three questions are asked either way, and the LAST of them is what
        makes it safe to answer them about a value we did not produce: the
        currency present must be the one this country would have produced. A
        US-to-US bill reading CAD, or a Finnish one reading USD, was settled by
        something other than the country — it is left exactly alone, neither
        rewritten nor marked.

        ⚠️ WHAT THIS DELIBERATELY OVER-REPORTS, decided rather than overlooked:
        a document with no printed marking but an ABA routing number or an IBAN
        is CURRENCY step 3 — the currency came from the BANK, not the address,
        and step 4 is never reached. All three questions above can still answer
        yes, and nothing in the extraction separates the two cases (a
        `routing_number` being present does not mean the model used it). Such a
        bill IS marked. The alternative — suppressing the marker whenever bank
        details appear — would silence a document the model really did resolve
        from the country, and that silence is the exact failure 19.0.2.3.0
        exists to prevent; one extra sentence on a correct bill is the cheaper
        error. What the marker therefore means is "no currency was printed on
        this page, so the value was deduced" — which `currency_symbol: null`
        does prove — and NOT "the address is why". The chatter note in
        `account_move._ai_post_process` is worded to match, and must stay so.
        """
        if not self.currency_from_country:
            return

        # Already set is not already settled — see the block above. It only
        # changes what we may DO with the answer: write it, or merely mark it.
        already_set = self._currency_is_usable(data.get("currency"))

        if not nothing_was_printed(data):
            return

        seller_cc = one_country_on_both_sides(data)
        if not seller_cc:
            return

        resolved = self.currency_from_country(seller_cc)
        if not self._currency_is_usable(resolved):
            # Unknown country, a country with no currency on this database, or
            # a lookup that answered something that is not an ISO code. With no
            # currency of our own this falls through to the required-field
            # check, which fails the import and stores the JSON for
            # hand-editing — today's behaviour, which is the right place to
            # land. With one already set it simply means we cannot say whether
            # the country explains it, so nothing is claimed.
            #
            # ⚠️ IT IS LOGGED EITHER WAY. "We could not say whether the country
            # explains this currency" used to be the one outcome that left no
            # trace anywhere: the success path logs, every failure path raises,
            # and this returned in silence.
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
            # 🔴 THE ONE CHECK THAT MAKES MARKING A VALUE WE DID NOT PRODUCE
            # SAFE. The document settled on a currency this country would not
            # have produced, so the country is not where it came from. Do not
            # mark it, and above all do not rewrite it: a US-to-US bill really
            # denominated in CAD is a CAD bill.
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
            # 🔴 WE DECLINE TO *CHOOSE* A CURRENCY THIS COMPANY HAS SWITCHED
            # OFF — and the reason is NOT the one 19.0.3.3.0 gave.
            #
            # That version said core's `_import_currency` would substitute the
            # company currency for an archived row. It does not. At the pinned
            # revision `189df353`, `account_edi_common.py:744-758` searches
            # `with_context(active_test=False)`, FINDS the archived row, uses
            # it, and only appends "The currency '%s' is not active." to the
            # import log. The company-currency substitution happens only when
            # no row of that name exists at all. The old rationale was an
            # inverted reading of core and is retired.
            #
            # The real reason is narrower and it only applies HERE, on the path
            # where we are the one choosing. `res.currency.active = False` is
            # an administrator saying the company does not trade in it: there
            # is no maintained rate, and an unmaintained rate is 1.0, so the
            # bill's company-currency figures would be its foreign figures.
            # That is the silent money error this whole feature line exists to
            # avoid — and core, which would accept it with a log line, is
            # exactly why the stop has to be ours. Failing with the JSON stored
            # puts a person in front of a configuration problem, which is where
            # a configuration problem belongs.
            #
            # ⚠️ IT DOES NOT APPLY TO THE MARKER. Below this point the currency
            # is already on the extraction and core will import it whatever we
            # decide; suppressing the note would remove the only visibility and
            # change nothing else. On a typical database only a handful of
            # countries' currencies are active, so that suppression was the
            # ordinary case rather than a corner.
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
            raise self.error_class(
                f"Expected JSON object, got {type(data).__name__}"
            )

        # BEFORE the required-field sweep below, because a null currency is
        # exactly what it is there to catch and this is the one case where the
        # document itself answers the question.
        self._resolve_currency_from_country(data)

        # 🔴 `currency` IS REQUIRED, and that is a deliberate change of
        # behaviour (19.0.2.3.0). Until then a null currency fell through to
        # `ubl_generator.DEFAULT_CURRENCY`, which is "USD" — so a Chinese or
        # Finnish bill whose currency the model could not establish became a USD
        # bill with entirely plausible numbers and the wrong value, silently.
        # Core does the same thing one layer further down: `_import_currency`
        # falls back to `env.company.currency_id` for any code it cannot find,
        # with only a log line.
        #
        # The prompt now says to output null when the currency genuinely does
        # not settle, so this turns "we do not know" into a LOUD failure instead
        # of a quiet guess — the same trade `issue_date` has always made. It is
        # cheap: `_process_import` stores the extraction on the exception
        # (`data=data`), marks the record `failed`, and a human can fill the
        # currency in and re-import with NO new API call.
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
            raise self.error_class(
                f"Missing required fields in extraction: {', '.join(missing)}",
                data=data,
            )

        # A symbol, a name or a code in the wrong case is not an ISO 4217 code.
        # "$", "US$", "￥", "RMB" and "usd" all reach core's `_import_currency`,
        # match no `res.currency.name`, and become the company currency with a
        # log line nobody reads. Only the shape is checkable here — "RMB" is
        # three letters and still not a code — but the shape catches the symbols,
        # which are what a model emits when it is copying the document.
        # Via the shared helper (19.0.3.3.0) so the fallback above and this
        # check cannot disagree about what "usable" means — a fallback that
        # produced a value this then rejected would be a louder failure for no
        # reason.
        currency = data["currency"]
        if not self._currency_is_usable(currency):
            raise self.error_class(
                f"Currency {currency!r} is not a 3-letter ISO 4217 code "
                f"(e.g. USD, EUR, CNY)",
                data=data,
            )

        if data["type"] not in ("Invoice", "CreditNote"):
            raise self.error_class(
                f"Invalid document type: {data['type']}. Expected 'Invoice' or 'CreditNote'",
                data=data,
            )

        if not isinstance(data["lines"], list) or not data["lines"]:
            raise self.error_class(
                "No invoice lines extracted from the document", data=data
            )

        seller = data["seller"]
        if not isinstance(seller, dict) or not seller.get("name"):
            raise self.error_class(
                "Seller name is missing from extraction", data=data
            )

        self._validate_arithmetic(data)

    # ------------------------------------------------------------------
    # The arithmetic
    # ------------------------------------------------------------------
    #
    # 🔴 UNTIL 19.0.2.4.0 NOTHING HERE DID ANY ARITHMETIC AT ALL — while the prompt
    # above uses "the line totals, tax and total must add up" as its tiebreak for a
    # thousands separator, and "quantity × unit price must equal the line total" as its
    # tiebreak for a quantity. The prompt already believed the invariant; nothing
    # verified it.
    #
    # Everything below raises `ClaudeExtractionError(msg, data=data)`, which is the
    # cheap kind of failure in this module: Claude has already been billed, so the
    # parsed JSON rides out on the exception, `_process_import` stores it, the record
    # goes to `failed`, and a human fixes the one figure and re-imports with NO second
    # API call. The message names WHICH figure, because "the document does not
    # reconcile" sends somebody back to the PDF with nothing to look for.
    #
    # ⚠️ Every message must therefore stay specific enough to act on, and every check
    # must stay loose enough not to fire on a correct invoice. A validator that fails
    # good documents is switched off, and then it catches nothing.

    def _number(self, container, key, where, data, required=True, nullable=False):
        """One numeric field, or a named failure. A JSON `null` is never acceptable.

        🔴 A NULL AMOUNT IS WHAT THIS EXISTS FOR, and it used to be the quietest
        failure in the module. `ubl_generator._fmt_decimal(None)` returned "0.00", so a
        null `line_total`, `tax_amount`, `taxable_amount` or `totals` entry passed
        validation, passed UBL generation, built a bill, and was wrong with nothing to
        see — which inverts the whole null-rather-than-guess principle the prompt is
        built on, whose entire justification is that a missing value is VISIBLE.
        A null that silently becomes zero is the one kind of missing value that is not.

        `required=False` tolerates an ABSENT key (the generator has a documented
        non-monetary default for those) but never a null one, because absent and null
        were identical downstream: `.get(key, 0)` and `.get(key)` both reached
        `_fmt_decimal` as a zero.

        `nullable=True` is for ONE field, a line's `tax_amount` (19.0.3.29.0):
        a null is returned as `None`, never as a zero. See the call site.
        """
        if key not in container:
            if not required:
                return None
            raise self.error_class(
                f"{where}: {key} is missing from the extraction. It is an amount the "
                f"bill is built from, and a missing amount must not be read as zero.",
                data=data,
            )
        value = container[key]
        if value is None and nullable:
            return None
        if value is None:
            raise self.error_class(
                f"{where}: {key} is null. An amount Claude could not read must not "
                f"become 0.00 on the bill — fill it in below and re-import, or clear "
                f"the whole import if the document really does not state it.",
                data=data,
            )
        # `bool` is an `int` in Python, and `True` would otherwise reconcile as 1.0.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise self.error_class(
                f"{where}: {key} is {value!r}, which is not a number. Amounts must be "
                f"JSON numbers — a string has not been through the NUMBERS procedure "
                f"and its separators mean nothing yet.",
                data=data,
            )
        return float(value)

    def _totals_disagree(self, message, data=None):
        """The lines or the tax do not make the stated totals. A reading this
        is a misreading, so it fails; `SourceDataValidator` lets a source
        whose amounts are all stated build the bill instead."""
        raise self.error_class(message, data=data)

    def _reconciles(self, actual, expected, accumulated=True):
        """Within tolerance? See `TOLERANCE_ABS` / `TOLERANCE_REL`.

        `accumulated=False` drops the relative term, and is for the one check
        where nothing accumulates: `payable` against `tax_inclusive` are the same
        number restated, not a sum of rounded parts, so they agree to the cent or
        one of them was misread. The other two checks add up figures the vendor
        rounded before printing, and need the relative term or they reject correct
        long invoices.
        """
        tolerance = TOLERANCE_ABS
        if accumulated:
            tolerance = max(tolerance, TOLERANCE_REL * abs(expected))
        return abs(actual - expected) <= tolerance

    def _validate_arithmetic(self, data):
        """Reconcile the figures against each other. See the block comment above.

        ⚠️ SIGNS ARE LEFT ALONE ON PURPOSE. A credit note arrives in whichever
        convention the vendor printed — all-positive magnitudes (the common case) or
        all-negative (`ubl_generator._normalise_credit_note_signs` exists for that one)
        — and every check here is linear, so multiplying the whole document by -1
        changes nothing about whether it reconciles. What that does catch, correctly, is
        a document whose SECTIONS disagree about the convention: negative lines under a
        positive total is not a credit note, it is an extraction the generator would
        turn into a bill with a negative line extension and a positive payable.
        """
        totals = data["totals"]
        if not isinstance(totals, dict):
            raise self.error_class(
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
                raise self.error_class(
                    f"Line {index} is {type(line).__name__}, expected an object",
                    data=data,
                )
            where = f"Line {index} ({line.get('name') or 'unnamed'})"
            line_total = self._number(line, "line_total", where, data)
            # 🔴 A NULL LINE TAX IS ACCEPTED (19.0.3.29.0), AND ONLY THAT ONE.
            # A receipt that prints one tax total and none per line is an
            # ordinary document. Claude left the
            # line tax null, which is right, and this used to fail the import.
            # Nothing reads the line tax as money: the document tax below comes
            # from the subtotals, and Odoo's UBL decoder takes each line's tax
            # from its RATE. So the bill is built, and if its total then differs
            # from the printed one, `account.move.ai_total_mismatch` puts the red
            # banner on it (19.0.3.31.0). Without
            # subtotals, a taxed document whose lines carry no tax still fails
            # the tax check below, because the null lines add nothing.
            self._number(line, "tax_amount", where, data, nullable=True)
            quantity = self._number(line, "quantity", where, data, required=False)
            price_unit = self._number(line, "price_unit", where, data, required=False)
            self._number(line, "tax_percent", where, data, required=False)
            line_sum += line_total
            self._validate_line_magnitude(where, quantity, price_unit, line_total, data)

        subtotals = data.get("tax_subtotals") or []
        if not isinstance(subtotals, list):
            raise self.error_class(
                f"'tax_subtotals' is {type(subtotals).__name__}, expected a list",
                data=data,
            )
        subtotal_tax = 0.0
        for index, subtotal in enumerate(subtotals, start=1):
            if not isinstance(subtotal, dict):
                raise self.error_class(
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
            # A document with NO TAX AT ALL is normal here — a resale certificate, an
            # exempt buyer, a vendor outside the state — and the prompt says so, so an
            # empty `tax_subtotals` is not a defect. Fall back to the lines' own tax so
            # that a document which states its tax per line and not in a subtotal block
            # is still reconciled rather than waved through. With neither, this is 0.0
            # and the check below becomes "tax_exclusive == tax_inclusive", which is
            # exactly the right question to ask of an untaxed document.
            tax_total = sum(line.get("tax_amount") or 0.0 for line in data["lines"])
            tax_source = "the lines' own tax amounts (no tax subtotals were extracted)"

        if not self._reconciles(line_sum, line_extension):
            self._totals_disagree(
                f"The line totals do not add up to the document total: the "
                f"{len(data['lines'])} line(s) sum to {line_sum:.2f} but "
                f"totals.line_extension says {line_extension:.2f} (out by "
                f"{line_sum - line_extension:.2f}). One of those two figures is "
                f"misread, or a line is missing.",
                data=data,
            )

        if not self._reconciles(tax_exclusive + tax_total, tax_inclusive):
            self._totals_disagree(
                f"The tax does not add up: totals.tax_exclusive {tax_exclusive:.2f} "
                f"plus {tax_total:.2f} of tax from {tax_source} is "
                f"{tax_exclusive + tax_total:.2f}, but totals.tax_inclusive says "
                f"{tax_inclusive:.2f} (out by "
                f"{tax_exclusive + tax_total - tax_inclusive:.2f}).",
                data=data,
            )

        if not self._reconciles(payable, tax_inclusive, accumulated=False):
            raise self.error_class(
                f"totals.payable {payable:.2f} is not totals.tax_inclusive "
                f"{tax_inclusive:.2f} (out by {payable - tax_inclusive:.2f}). This "
                f"schema has no slot for a prepayment, a rounding line or a credit "
                f"applied against the bill, so a difference here means one of the two "
                f"was misread.",
                data=data,
            )

    def _validate_line_magnitude(self, where, quantity, price_unit, line_total, data):
        """🔴 THE ONLY CHECK IN THIS MODULE THAT CAN EVER SEE A FACTOR-OF-1000 QUANTITY.

        The three document-level sums above cannot. `ubl_generator` emits
        `PriceAmount = line_total / quantity`, so a quantity that is wrong by 1000 is
        accompanied by a unit price that is wrong by 1000 the other way, and the line
        total — the only figure that reaches the sums — is untouched. The whole document
        then reconciles perfectly while the bill carries 1,250 pounds of something at a
        hundredth of its price.

        ⚠️ THE AMOUNT AND QUANTITY ERRORS ARE THEREFORE NOT SYMMETRIC, although the
        NUMBERS block above describes the quantity case as "the same error of a thousand
        arriving from the other side". They are the same MISREADING, and the prompt is
        right to name it that way, but they have opposite consequences downstream: a
        misread AMOUNT breaks the document's own sums and is caught three times over; a
        misread QUANTITY leaves every sum intact and is visible only here.

        See `LINE_RATIO_LIMIT` for why this is a ratio rather than the equality the
        prompt's own tiebreak states — in one sentence, a discounted line legitimately
        breaks that equality and this business receives discounted lines.
        """
        if quantity is None or price_unit is None:
            return
        gross = abs(quantity) * abs(price_unit)
        net = abs(line_total)
        # A zero on either side says nothing: a free-of-charge line, a zero-priced
        # sample, or a line the vendor totals elsewhere. Dividing would also raise.
        if not gross or not net:
            return
        ratio = gross / net if gross > net else net / gross
        if ratio < LINE_RATIO_LIMIT:
            return
        raise self.error_class(
            f"{where}: quantity {quantity:g} × unit price {price_unit:g} is "
            f"{gross:.2f}, which is {ratio:.0f}× the line total {line_total:.2f}. A "
            f"discount cannot account for that; one of the three was read with its "
            f"decimal point or its thousands separator in the wrong place. The "
            f"document's own totals CANNOT catch this — they are built from the line "
            f"total, which is the figure that is right.",
            data=data,
        )


# ---------------------------------------------------------------------------
# Structured sources (core/source split)
# ---------------------------------------------------------------------------


class InvoiceTotalsError(InvoiceDataError):
    """The data is well formed but does not add up (`import.totals_mismatch`,
    contracts/public-api.md §1.4). Every other `InvoiceDataError` is
    `import.invalid_data`."""


class SourceDataValidator(InvoiceDataValidator):
    """The validator `goodahead.invoice.import._import_from_invoice_data` runs.

    The same checks and tolerance as the AI reading (spec §6), with one
    addition: an arithmetic failure is raised as `InvoiceTotalsError`, so the
    import can say *the figures do not add up* rather than *the data is
    malformed*. A missing or non-numeric figure met inside the arithmetic is
    still a shape error."""

    def _number(self, *args, **kwargs):
        try:
            return super()._number(*args, **kwargs)
        except InvoiceDataError as e:
            e._invoice_data_shape_error = True
            raise

    @staticmethod
    def _amounts_stated(data):
        """``source_meta.amounts_stated`` (public-api §1.4): every amount is a
        figure the document states (a Finvoice message), none read off a
        page."""
        meta = data.get("source_meta") if isinstance(data, dict) else None
        return bool(isinstance(meta, dict) and meta.get("amounts_stated"))

    def _validate_line_magnitude(self, where, quantity, price_unit, line_total, data):
        # 🔴 NOT FOR STATED AMOUNTS. The check exists for a
        # decimal point or thousands separator misread off a page. A message
        # states its row total, and a sender that prices per 100 pieces
        # without saying so (1200 pieces at 20.00 for 240.00) is exactly 100x:
        # refused here, though the row total the bill is built from is right.
        if self._amounts_stated(data):
            return
        super()._validate_line_magnitude(where, quantity, price_unit, line_total, data)

    def _validate_arithmetic(self, data):
        self._stated = self._amounts_stated(data)
        return self._validate_arithmetic_classified(data)

    def _totals_disagree(self, message, data=None):
        # 🔴 A STATED DOCUMENT THAT DOES NOT ADD UP BECOMES A DRAFT WITH THE
        # BANNER (spec AC-29), not a failure: nothing was
        # misread, the sender's own figures disagree. The bill carries the
        # rows; `account.move.ai_total_mismatch` compares its total with the
        # stated one and an Accountant reviews it before posting.
        if getattr(self, "_stated", False):
            return
        super()._totals_disagree(message, data=data)

    def _validate_arithmetic_classified(self, data):
        try:
            return super()._validate_arithmetic(data)
        except InvoiceTotalsError:
            raise
        except InvoiceDataError as e:
            if getattr(e, "_invoice_data_shape_error", False):
                raise
            raise InvoiceTotalsError(str(e), data=data) from e


def stated_document_for_the_bill(data, price_digits, discount_digits=None, import_discount=None):
    """The data the bill's document is generated from, for a source whose
    amounts are stated (``source_meta.amounts_stated``). Anything else is
    returned as it is. The stored data is never changed: it stays what the
    sender wrote, and the banner compares the bill with it.

    🔴 THE BILL CARRIES EXACTLY THE ROWS, AND EACH ROW ITS STATED TOTAL.
    The ERP's e-invoice reader keeps a line's unit price to ``price_digits``
    and makes up any difference to the document's VAT-exclusive total with an
    untaxed "Rounding" line. Two things follow:

    - a row whose total is not its quantity times a price the ERP can store
      (7 hours for 120.00 is 17.142857 an hour; 7 x 17.14 is 119.98) is
      written as one unit at its total, the sender's quantity and price kept
      in the line text — otherwise the bill is 119.98 plus a Rounding line;
    - when the lines do not make the stated totals (AC-29: a sender error,
      imported as a draft with the banner), the document's totals and VAT
      breakdown are the lines' own — otherwise the reader adds the
      difference as a Rounding line and forces the stated VAT onto it.

    finvoice content AC-22: a discounted row (``gross_price`` with a stated
    percentage or discount amount) keeps its gross price and a percentage
    when ``import_discount`` (the base's ``rows.import_discount``, passed in
    to keep this module free of the ERP) finds one that makes the stated row
    amount at ``discount_digits`` with the gross price as the ERP stores it
    (``price_digits``); the line is then marked ``price_is_gross`` for the
    document generator. Otherwise it goes like any other row: at its net
    price, or as one unit when that price cannot be stored (review F1).
    """
    meta = data.get("source_meta") if isinstance(data, dict) else None
    if not (isinstance(meta, dict) and meta.get("amounts_stated")):
        return data
    data = json.loads(json.dumps(data))
    for line in data.get("lines") or []:
        if _keep_the_stated_discount(line, price_digits, discount_digits, import_discount):
            continue
        _one_unit_when_the_price_cannot_be_stored(line, price_digits)

    lines = [l for l in data.get("lines") or [] if l.get("line_total") is not None]
    line_sum = round(sum(l["line_total"] for l in lines), 2)
    totals = data.get("totals") or {}
    line_extension = totals.get("line_extension")
    if line_extension is not None and abs(line_sum - line_extension) <= 0.005:
        return data

    breakdown = {}
    for line in lines:
        key = (line.get("tax_percent") or 0.0, line.get("tax_category"))
        entry = breakdown.setdefault(key, {"base": 0.0, "tax": 0.0})
        entry["base"] += line["line_total"]
    subtotals = []
    tax_total = 0.0
    for (rate, category), entry in breakdown.items():
        base = round(entry["base"], 2)
        tax = round(base * rate / 100, 2)
        tax_total += tax
        subtotals.append({
            "tax_percent": rate,
            "taxable_amount": base,
            "tax_amount": tax,
            "tax_category": category,
        })
    data["tax_subtotals"] = subtotals
    tax_inclusive = round(line_sum + tax_total, 2)
    data["totals"] = {
        "line_extension": line_sum,
        "tax_exclusive": line_sum,
        "tax_inclusive": tax_inclusive,
        "payable": tax_inclusive,
    }
    return data


def stored_price(price, price_digits):
    """``price`` as the ERP stores it: rounded half up to ``price_digits``."""
    step = Decimal(1).scaleb(-price_digits)
    return float(Decimal(repr(float(price))).quantize(step, rounding=ROUND_HALF_UP))


def _keep_the_stated_discount(line, price_digits, discount_digits, import_discount):
    """Mark a discounted row to go with its gross price and percentage when
    they make its stated amount; True when marked.

    🔴 CHECKED AT THE PRICE THE ERP STORES (review F1). A gross price stated
    in five decimals (0.79839) is kept in ``price_digits`` (0.80): on 1000
    units that is 760.00 less 5 %, not 758.47. The check runs on the stored
    price, so a row it cannot make falls through to the one-unit rule."""
    if import_discount is None or discount_digits is None or "gross_price" not in line:
        return False
    if line.get("line_total") is None or not line.get("quantity"):
        return False
    price = line["gross_price"]
    if price_digits is not None:
        price = stored_price(price, price_digits)
    choice = import_discount(
        line["quantity"], price, 1, line.get("discount_percent"),
        line.get("discount_amount"), line["line_total"], digits=discount_digits,
    )
    if not choice:
        return False
    line["price_unit"] = float(choice["price"])
    line["discount_percent"] = float(choice["percent"])
    line["price_is_gross"] = True
    return True


def undiscounted_row(quantity, total, price_digits, stated_price=None):
    """``{quantity, price_unit, note}`` of a line without a discount that
    makes ``total``: the net price when it can be stored in
    ``price_digits``, else one unit at the total with the sender's
    ``quantity × price`` as ``note``. ``None`` when nothing needs to change
    (no quantity, or one unit)."""
    if not quantity or total is None or quantity == 1:
        return None
    price = total / quantity
    if round(round(price, price_digits) * quantity, 2) == round(total, 2):
        return {"quantity": quantity, "price_unit": price, "note": None}
    shown_price = stated_price if stated_price is not None else price
    return {"quantity": 1.0, "price_unit": total, "note": f"{quantity:g} × {shown_price:g}"}


def _one_unit_when_the_price_cannot_be_stored(line, price_digits):
    row = undiscounted_row(line.get("quantity"), line.get("line_total"), price_digits,
                           line.get("price_unit"))
    if not row or not row["note"]:
        return
    total = line["line_total"]
    line["description"] = "\n".join(p for p in (line.get("description"), row["note"]) if p)
    line["quantity"] = 1.0
    line["unit_code"] = None
    line["unit_code_unmapped"] = None
    line["price_unit"] = total
    line["line_total"] = total
