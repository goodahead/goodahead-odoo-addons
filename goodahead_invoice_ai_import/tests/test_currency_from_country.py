# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The currency of a domestic invoice that prints no currency (19.0.3.3.0).

🔴 WHAT THIS IS BUILT FROM. A freight bill failed because the extraction
returned no currency and a person had to hand-edit the stored JSON. Read
against the PDF: every amount was a bare number — no `$`, no ISO code, no IBAN,
no ABA routing number anywhere — and shipper and bill-to were both US
addresses. The model did exactly
what the CURRENCY procedure told it to.

🔴 THE ONE TEST THAT MATTERS IS THE MIDDLE ROW, and it is
`test_ambiguous_symbol_from_another_country_still_fails`. 19.0.2.3.0 made a null
currency terminal ON PURPOSE, to stop a `¥` from Shanghai becoming USD because
the buyer is in New Jersey — `¥` is CNY or JPY, roughly a factor of twenty.
Resolving that case from the country would be far worse than the failure this
change removes, so it is pinned here before anything else.

The three rows, and every one of them has a test below:

    no symbol + no code printed, same country   → resolve from the country
    a symbol WAS printed and did not settle     → still null, still a failure
    an ISO code printed                         → it wins, as before

🔴 AND A FOURTH ROW, ADDED AFTER THE LIVE RUN. The prompt's CURRENCY step 4
asks the MODEL to do the same inference, so on a real document `currency`
arrives already set and the server-side rule is a BACKSTOP, not the path taken.
The first version marked only its own work, so bill 000000000001 — the one
document this whole feature exists for — was imported with the currency taken
from the country and said nothing about it:

    no symbol, same country, the MODEL already answered → mark it anyway,
    provided the answer is the one that country would have given

🔴 NOTHING HERE MAY REACH THE CLAUDE API, and the two classes guard that
differently BECAUSE THEY TEST DIFFERENT LAYERS.

* `TestCurrencyFromCountryRule` runs the REAL `extract_invoice` against a stub
  client that returns a canned response, which is how `test_claude_client.py`
  has always done it. There is no transport in this module to reach — it has
  owned none since 19.0.1.2.0, `goodahead_claude` does — so a stub client is a
  harder guarantee than a patch: there is nothing for a real call to fall
  through to. Patching `extract_invoice` to raise here would make the rule
  untestable, since the rule lives inside it.
* `TestCurrencyInferenceOnTheBill` never goes near the extractor, so it DOES
  install the `extract_invoice`-raises guard from `test_vendor_identifiers.py`,
  and a path that started calling the API could not pass quietly.

⚠️ The older `lib.claude_client.requests` patch named in some notes no longer
exists and must not be reintroduced.

Fake `ZZ…` vendor names throughout, so a DB-touching assertion cannot collide
with a vendor already in the database.
"""

import json
from unittest.mock import patch

from odoo.tests import BaseCase, TransactionCase, tagged

from odoo.addons.goodahead_invoice_ai_import.lib import claude_client
from odoo.addons.goodahead_invoice_ai_import.lib.claude_client import (
    CURRENCY_INFERRED_KEY,
    CURRENCY_SYMBOL_KEY,
    SYSTEM_PROMPT,
    ClaudeExtractionError,
    ClaudeInvoiceExtractor,
)
from odoo.addons.goodahead_invoice_import.adapters.ubl_generator import (
    generate_peppol_invoice_xml,
)


class _StubClient:
    """Stands in for `goodahead.claude.client`. Returns a canned body and keeps
    the call, so nothing can reach the network — the same stub shape as
    `test_claude_client.py`."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def messages_create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


# An invented freight bill in the shape of the one that failed: two 2,215.75
# lines make the 4,431.50 total printed on it, with no tax and no currency
# marking of any kind.
def _freight_extraction(**overrides):
    data = {
        "type": "Invoice",
        "invoice_number": "ZZ-ZZF-1",
        "issue_date": "2026-02-03",
        "currency": None,
        CURRENCY_SYMBOL_KEY: None,
        "seller": {
            "name": "ZZ Freight Carrier Inc",
            "street": "1 Nowhere Road",
            "city": "Memphis",
            "state_code": "TN",
            "postal_code": "38125",
            "country_code": "US",
        },
        "buyer": {"name": "ZZ Buyer LLC", "country_code": "US"},
        "lines": [
            {
                "name": "FREIGHT CHARGE %d" % n,
                "quantity": 1.0,
                "unit_code": "C62",
                "price_unit": 2215.75,
                "line_total": 2215.75,
                "tax_percent": 0.0,
                "tax_amount": 0.0,
                "tax_category": "Z",
            }
            for n in (1, 2)
        ],
        "tax_subtotals": [
            {
                "tax_percent": 0.0,
                "taxable_amount": 4431.50,
                "tax_amount": 0.0,
                "tax_category": "Z",
            }
        ],
        "totals": {
            "line_extension": 4431.50,
            "tax_exclusive": 4431.50,
            "tax_inclusive": 4431.50,
            "payable": 4431.50,
        },
    }
    data.update(overrides)
    return data


def _body(data):
    return {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": json.dumps(data)}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


# The injected lookup, faked so the rule can be tested without a database. The
# REAL one (`account.move._ai_currency_for_country`, which reads
# `res.country.currency_id`) has its own test in the second class — a fake that
# agreed with nothing real would make every test here vacuous.
_FAKE_COUNTRY_CURRENCY = {"US": "USD", "FI": "EUR", "CN": "CNY"}


def _fake_lookup(country_code):
    return _FAKE_COUNTRY_CURRENCY.get(country_code)


# 🔴 THE SHAPE A REAL IMPORT ACTUALLY PRODUCES, and it is not the one above.
#
# One live call, on a bill of this shape (ZZ Freight LLC, 000000000001). The model was
# asked the new CURRENCY step 4 and it DID IT: the answer came back with
# `currency` already "USD" and `currency_symbol` null. The server-side rule
# then saw a usable currency, took its first early return, and the bill said
# nothing at all — on the exact document the feature was written for.
#
# The two addresses are in the shape the page printed (city, state, ZIP+4);
# the word USA appears nowhere on it, so the country codes are the model reading the address format.
def _model_inferred_extraction(**overrides):
    data = _freight_extraction(currency="USD")
    data["seller"].update(
        {
            "name": "ZZ Freight Carrier Inc",
            "state_code": "AR",
            "postal_code": "72000-0001",
            "country_code": "US",
        }
    )
    data["buyer"].update({"name": "ZZ Buyer LLC", "country_code": "US"})
    data.update(overrides)
    return data


# Whether this database trades in a currency at all. A SECOND question from
# "what currency does this country use", and the two have to be asked
# separately — see `test_an_archived_currency_the_model_already_chose_is_marked`
# for why. The default says yes, which is what every test written before
# 19.0.3.3.0 first assumed.
def _always_active(_name):
    return True


def _extract(data, lookup=_fake_lookup, active=_always_active):
    client = _StubClient(_body(data))
    return ClaudeInvoiceExtractor(
        client=client,
        currency_from_country=lookup,
        currency_is_active=active,
    ).extract_invoice(b"%PDF-1.4 fake")


@tagged("post_install", "-at_install")
class TestCurrencyFromCountryRule(BaseCase):
    """The rule itself. No database, no network — a failure here is
    unambiguous."""

    # -- the case that prompted the change -----------------------------------

    def test_no_symbol_and_both_parties_in_the_same_country_resolves(self):
        """The ZZ Freight LLC bill: bare amounts, both addresses US."""
        result = _extract(_freight_extraction())
        self.assertEqual(result["data"]["currency"], "USD")
        self.assertEqual(
            result["data"][CURRENCY_INFERRED_KEY],
            "US",
            "the bill has to be able to say WHERE the currency came from",
        )

    # -- the case the FIRST version of the change missed ----------------------

    def test_the_model_s_own_country_inference_is_marked_too(self):
        """🔴 THE PATH A REAL DOCUMENT TAKES, which is not the one above.

        The live import of bill 000000000001 came back with `currency` already
        "USD" and `currency_symbol` null: the prompt's step 4 told the model to
        take the currency from the country and it did. The server-side resolver
        then read a usable `currency`, took its first early return — commented
        *"steps 1-3 settled it"* — and set no marker, so the bill the whole
        feature was written for said nothing about where its currency came
        from.

        A usable `currency` no longer means "steps 1-3 settled it". The server
        cannot tell the two apart from `currency` alone, so it asks the same
        three questions it would have asked itself: nothing printed, one
        country on both sides, and the value present is the one that country
        would have produced."""
        data = _model_inferred_extraction()
        result = _extract(data)
        self.assertEqual(result["data"]["currency"], "USD")
        self.assertEqual(
            result["data"][CURRENCY_INFERRED_KEY],
            "US",
            "the model inferred it from the country and the bill must say so",
        )

    def test_a_currency_the_country_does_not_explain_is_not_marked(self):
        """🔴 THE CONTROL on the test above, and the whole safety of it.

        A Finnish vendor billing a Finnish buyer, nothing printed, and the
        model answered USD. Finland's currency is EUR, so whatever settled that
        document it was NOT the country — step 3's language, VAT rate or
        exchange-rate evidence did. Marking it would make the note say
        something the document does not support, and rewriting it to EUR would
        be far worse. Neither happens."""
        data = _model_inferred_extraction()
        data["seller"]["country_code"] = "FI"
        data["buyer"]["country_code"] = "FI"
        result = _extract(data)
        self.assertEqual(result["data"]["currency"], "USD")
        self.assertNotIn(CURRENCY_INFERRED_KEY, result["data"])

    def test_bank_details_on_an_unmarked_domestic_bill_are_still_marked(self):
        """🔴 THE CASE DECIDED DELIBERATELY, written down so the decision is
        not re-made by accident.

        A document with no symbol and no code but a 9-digit ABA routing number
        is CURRENCY step 3: the currency came from the BANK, not from the
        address, and step 4 is never reached. All three of the server's
        conditions still hold, and no evidence in the extraction separates the
        two — `routing_number` being present does not mean the model used it.

        The decision is to MARK IT, and to word the note so that it is true
        either way. Marking it costs one extra sentence on a bill whose
        currency is almost certainly right; NOT marking it would mean a
        document the model really did resolve from the country goes by in
        silence whenever it happens to print a bank number, which is exactly
        the invisibility 19.0.2.3.0 exists to prevent. The note therefore says
        the currency was NOT read off the page — which is what
        `currency_symbol: null` actually proves — and never claims which
        evidence settled it. `test_the_note_does_not_claim_why` pins the
        wording."""
        data = _model_inferred_extraction()
        data["seller"]["routing_number"] = "021000021"
        result = _extract(data)
        self.assertEqual(result["data"]["currency"], "USD")
        self.assertEqual(result["data"][CURRENCY_INFERRED_KEY], "US")

    def test_a_printed_iso_code_wins(self):
        """🔴 THE CONTROL for the test above. Same document, same two US
        addresses — but the model read a code off the page. Step 1 outranks
        everything, so nothing may touch it and nothing may claim an
        inference."""
        result = _extract(
            _freight_extraction(currency="CAD", **{CURRENCY_SYMBOL_KEY: "CAD"})
        )
        self.assertEqual(result["data"]["currency"], "CAD")
        self.assertNotIn(CURRENCY_INFERRED_KEY, result["data"])

    def test_a_printed_iso_code_wins_even_with_no_symbol_reported(self):
        """A second control on the same point, with the symbol field null: a
        settled currency must short-circuit the fallback before it ever looks
        at the countries, or a US-to-US bill reading CAD would be rewritten to
        USD."""
        result = _extract(_freight_extraction(currency="CAD"))
        self.assertEqual(result["data"]["currency"], "CAD")
        self.assertNotIn(CURRENCY_INFERRED_KEY, result["data"])

    # -- the row that must NOT move ------------------------------------------

    def test_ambiguous_symbol_from_another_country_still_fails(self):
        """🔴 THE TEST THAT MATTERS MOST. A Shanghai vendor billing a New
        Jersey buyer prints `¥`, which is CNY or JPY — a factor of about
        twenty. Making that USD because the BUYER is American is worse than any
        failure, and it is precisely what 19.0.2.3.0 refuses to guess."""
        data = _freight_extraction(
            **{CURRENCY_SYMBOL_KEY: "¥"}
        )
        data["seller"]["name"] = "ZZ Shanghai Kitchen Equipment Co"
        data["seller"]["country_code"] = "CN"
        with self.assertRaises(ClaudeExtractionError) as caught:
            _extract(data)
        self.assertNotIn(CURRENCY_INFERRED_KEY, caught.exception.data)
        self.assertIsNone(caught.exception.data["currency"])

    def test_ambiguous_symbol_in_the_same_country_still_fails(self):
        """A `$` on a US-to-US bill the model could not settle. The
        server-side rule is deliberately blunter than the prompt's — ANY
        printed symbol blocks the fallback, so there is no second ambiguity
        list to keep in step with the first. The cost is only that this case
        keeps failing, which is today's behaviour."""
        with self.assertRaises(ClaudeExtractionError):
            _extract(_freight_extraction(**{CURRENCY_SYMBOL_KEY: "$"}))

    def test_different_countries_do_not_infer(self):
        """A Finnish vendor billing a US buyer, nothing printed. Two countries
        means two candidate currencies and no evidence choosing between them,
        so this stays a failure with the JSON stored for hand-editing."""
        data = _freight_extraction()
        data["seller"]["country_code"] = "FI"
        with self.assertRaises(ClaudeExtractionError) as caught:
            _extract(data)
        self.assertNotIn(CURRENCY_INFERRED_KEY, caught.exception.data)
        self.assertTrue(
            caught.exception.data["lines"],
            "the parsed JSON must ride out on the exception so a human can fix "
            "the one field and re-import with no second API call",
        )

    def test_a_country_the_lookup_cannot_resolve_fails_as_before(self):
        """An unknown country, or one with no usable currency on this database.
        Today's behaviour is the right place to land: fail, store the JSON."""
        data = _freight_extraction()
        data["seller"]["country_code"] = "ZZ"
        data["buyer"]["country_code"] = "ZZ"
        with self.assertRaises(ClaudeExtractionError) as caught:
            _extract(data)
        self.assertNotIn(CURRENCY_INFERRED_KEY, caught.exception.data)

    def test_a_missing_buyer_country_does_not_infer(self):
        """"Both parties are in the same country" is evidence; "the vendor is
        American" on its own is the country-alone guess step 5 forbids. Our own
        `company.country_id` is deliberately not substituted here — that would
        make OUR nationality the tiebreak for a foreign vendor's bill."""
        data = _freight_extraction()
        data["buyer"].pop("country_code")
        with self.assertRaises(ClaudeExtractionError):
            _extract(data)

    # -- the boundaries of the new field -------------------------------------

    def test_extraction_json_without_the_symbol_field_does_not_infer(self):
        """🔴 ABSENT IS NOT NULL. Stored extraction JSON from before
        19.0.3.3.0 has no `currency_symbol` key at all, so the question of
        whether the page printed anything was never asked. Treating that as
        "nothing was printed" would silently apply the new rule to every old
        record, including the cross-border ones."""
        data = _freight_extraction()
        data.pop(CURRENCY_SYMBOL_KEY)
        with self.assertRaises(ClaudeExtractionError):
            _extract(data)

    def test_an_unreadable_symbol_value_is_not_permission(self):
        """A non-string answer is an answer we cannot read, and an unreadable
        answer must not license the fallback."""
        for junk in (0, True, [], {}):
            with self.assertRaises(ClaudeExtractionError):
                _extract(_freight_extraction(**{CURRENCY_SYMBOL_KEY: junk}))

    def test_a_whitespace_only_symbol_counts_as_nothing_printed(self):
        """The one direction that is safe to be lenient in: `" "` is the page
        printing nothing."""
        result = _extract(_freight_extraction(**{CURRENCY_SYMBOL_KEY: "  "}))
        self.assertEqual(result["data"]["currency"], "USD")

    def test_without_the_injected_lookup_nothing_is_ever_inferred(self):
        """🔴 THE CONTROL ON THE WIRING. `currency_from_country` defaults to
        None, so a caller that does not opt in gets 19.0.2.3.0's behaviour
        exactly. If this passed with the lookup absent, every test above would
        be measuring something other than the rule."""
        client = _StubClient(_body(_freight_extraction()))
        with self.assertRaises(ClaudeExtractionError):
            ClaudeInvoiceExtractor(client=client).extract_invoice(b"%PDF")

    def test_a_lookup_returning_junk_is_refused(self):
        """A resolver that answered "US Dollar" or "$" would otherwise write a
        value core's `_import_currency` cannot match, which becomes the company
        currency with a log line nobody reads."""
        for junk in ("US Dollar", "$", "usdd", "", None):
            with self.assertRaises(ClaudeExtractionError):
                _extract(_freight_extraction(), lookup=lambda cc, j=junk: j)

    # -- an archived currency: two questions, two answers --------

    def test_an_archived_currency_is_not_written(self):
        """🔴 WE DECLINE TO CHOOSE A CURRENCY THE COMPANY HAS SWITCHED OFF, and
        the reason is NOT the one 19.0.3.3.0 gave.

        That version said core's `_import_currency` would substitute the company
        currency for an archived row. It does not: at the pinned revision
        `189df353` it searches `with_context(active_test=False)`, FINDS the
        archived row, uses it, and only appends *"The currency '%s' is not
        active."* to the import log. The substitution happens only when no row
        of that name exists at all.

        So core would accept this, which is exactly why the stop has to be ours.
        `res.currency.active = False` is an administrator saying the company
        does not trade in it: there is no maintained rate, and an unmaintained
        rate is 1.0, so the bill's company-currency figures would be the foreign
        figures. That is the silent money error this whole feature line exists
        to avoid, and we would be the one CHOOSING it. Failing with the JSON
        stored puts a person in front of a configuration problem, which is where
        it belongs.

        ⚠️ Not an edge case: on a typical single-currency database only a few
        countries' currencies are active; EUR, CAD, GBP, CNY and SEK can all be
        archived."""
        with self.assertRaises(ClaudeExtractionError):
            _extract(
                _freight_extraction(),
                lookup=lambda cc: "SEK",
                active=lambda name: False,
            )

    def test_an_archived_currency_the_model_already_chose_is_marked(self):
        """🔴 THE CONTROL, AND THE HALF 19.0.3.3.0 GOT WRONG IN BOTH DIRECTIONS.

        Here we are choosing nothing. The model returned SEK, core will import
        SEK — archived or not, see the test above — and the only thing on offer
        is whether the bill SAYS the value was deduced. Declining the marker
        removes the one piece of visibility and changes nothing else, so an
        SE→SE bare-amount bill landed in archived SEK with nothing on it saying
        where the currency came from: the exact invisibility 19.0.2.3.0 exists
        to remove, produced by the guard meant to protect it.

        Whether we trade in a currency has no bearing on whether the country
        explains the one already on the document."""
        data = _model_inferred_extraction(currency="SEK")
        result = _extract(
            data, lookup=lambda cc: "SEK", active=lambda name: False
        )
        self.assertEqual(result["data"]["currency"], "SEK")
        self.assertEqual(
            result["data"][CURRENCY_INFERRED_KEY],
            "US",
            "the bill must still say the currency was not read off the page",
        )

    def test_a_lower_case_lookup_answer_is_normalised(self):
        """Core matches `res.currency.name` EXACTLY, so `"usd"` would match
        nothing."""
        result = _extract(_freight_extraction(), lookup=lambda cc: "usd")
        self.assertEqual(result["data"]["currency"], "USD")

    # -- the prompt half -----------------------------------------------------

    def test_the_prompt_asks_for_the_symbol_and_states_the_limits(self):
        """The server-side rule is useless if the model never reports the
        field, and dangerous if the prompt tells it to apply the country rule
        to an ambiguous symbol. Both halves shipped together; this pins that
        they stay together."""
        self.assertIn('"currency_symbol"', SYSTEM_PROMPT)
        self.assertIn("SAME country", SYSTEM_PROMPT)
        self.assertIn("THIS STEP DOES NOT APPLY WHEN A SYMBOL WAS PRINTED",
                      SYSTEM_PROMPT)

    def test_the_prompt_reads_the_country_out_of_the_address_format(self):
        """🔴 The document that caused this whole change prints NO country.

        ZZ Freight LLC bill 000000000001: both parties printed as `CITY ST
        00000-0000`, and the word USA appears nowhere on either page (checked
        with pdftotext, both pages). The first
        version of this change asked for the country "as printed", which on
        that document is null on both sides — so condition (b) failed, the
        currency stayed null, and the feature did not fix the invoice it was
        written for.

        A 2-letter state with a 5-digit ZIP IS the country. Reading it is not
        the inference the next assertion forbids: that one is about taking the
        country from the currency, the language, or the other party, which
        would make the rule circular."""
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertIn("AN ADDRESS SHOWS ITS COUNTRY WHEN ITS OWN FORMAT DOES",
                      flat)
        self.assertIn("a 2-letter US state with a ZIP is US", flat)
        # 🔴 ZIP+4, not a bare 5 digits. The first version of this wording said
        # "5-digit ZIP" and quoted its examples with the +4 trimmed off — but the
        # page prints ZIP+4 on both addresses, so a literal reader
        # could have declined to match the very document this was written for.
        self.assertIn("'72000-0100' and '14000-0100' both count", flat)
        self.assertIn("a ZIP+4 is still a ZIP", flat)
        self.assertIn(
            "Never take it from the currency, the language, or the other "
            "party's address", flat)
        # And the wording it replaced must not come back: "as printed" on the
        # country is what produced the null.
        self.assertNotIn(
            "2-letter ISO code of the vendor's OWN address as printed", flat)

    def test_the_buyers_country_is_never_taken_from_the_sellers(self):
        """🔴 THE SAME-COUNTRY TEST IS THE ONLY THING GUARDING THE WHOLE RULE,
        AND THE BUYER'S HALF OF IT HAD NO BAN.

        The seller's schema line ends *"Never take it from the currency, the
        language, or the other party's address"*, which blocks
        seller-from-buyer. The buyer's line said only "read the same way as the
        seller's above" — so nothing blocked buyer-FROM-seller, and that is the
        direction that manufactures a false match: `seller_cc != buyer_cc` is
        the single condition standing between this feature and the cross-border
        guess 19.0.2.3.0 exists to prevent, and a buyer code copied from the
        seller's address makes that comparison a value against itself.

        The scenario is ordinary, not exotic. A Canadian vendor bills a US
        customer; the ship-to block is one line with no postal code; the amounts
        are bare. Following "read the same way as the seller's", the model
        returns CA for both — and CAD is written on a USD invoice, with a
        chatter note asserting both parties are in Canada. Nothing server-side
        can see it, because the server is told the countries both came from the
        document.

        ⚠️ `test_the_vendors_country_is_never_inferred` in
        `test_claude_client.py` matches the SELLER's wording and would pass with
        the buyer line deleted entirely. This one names the buyer's."""
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertIn(
            "Never take it from the currency, the language, or the SELLER'S "
            "address",
            flat,
        )

    def test_the_symbol_question_is_about_the_whole_page(self):
        """🔴 ONE QUESTION, NOT TWO. The field said *"EXACTLY as it is printed
        beside the amounts … or null when the document prints none of them
        anywhere"*, which are two different questions with two different
        answers.

        A US bill with `Invoice Currency: USD` in the header and bare numbers in
        the line table answers the first with null and the second with "USD".
        Under null, the server marks the bill and posts a note whose first
        clause — *"No currency symbol or ISO code was found anywhere on this
        document"* — is contradicted by the PDF the same sentence tells the
        reader to check.

        "Anywhere" is the reading the server rule needs, and it is the reading
        the prompt's own CURRENCY step 4(a) already uses (*"the document shows
        NO currency symbol and NO ISO code anywhere"*). Making them one question
        makes `currency_symbol: null` mean exactly step 4(a), which is what both
        the note and the resolver assume."""
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertIn("PRINTED ANYWHERE ON THE DOCUMENT", flat)
        self.assertNotIn("EXACTLY as it is printed beside the amounts", flat)
        self.assertNotIn("as PRINTED beside the amounts", flat)


class _NoClaudeMixin:
    def setUp(self):
        super().setUp()

        def _forbidden(*args, **kwargs):
            raise AssertionError(
                "a test reached the Claude extractor — nothing in this class "
                "may call the API, and a path that now does is a real defect"
            )

        guard = patch.object(
            claude_client.ClaudeInvoiceExtractor, "extract_invoice", _forbidden
        )
        guard.start()
        self.addCleanup(guard.stop)


@tagged("post_install", "-at_install")
class TestCurrencyInferenceOnTheBill(_NoClaudeMixin, TransactionCase):
    """The ORM lookup, and the note that keeps the inference visible."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.company.claude_api_key = "test-key"
        cls.AccountMove = cls.env["account.move"]
        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", cls.company.id)],
            limit=1,
        )

    def _resolver(self):
        """The real rule wired to the real ORM lookups, exactly as
        `_ai_extract_and_generate_ubl` wires it."""
        return ClaudeInvoiceExtractor(
            client=None,
            currency_from_country=self.AccountMove._ai_currency_for_country,
            currency_is_active=self.AccountMove._ai_currency_is_active,
        )

    # -- the lookup itself ---------------------------------------------------

    def test_the_lookup_reads_res_country_currency_id(self):
        """The claim, checked against this database rather than
        assumed: `res.country.currency_id` is a real core field
        (`base/models/res_country.py:60`) and it is populated."""
        self.assertEqual(self.AccountMove._ai_currency_for_country("US"), "USD")

    def test_the_lookup_refuses_what_it_cannot_answer(self):
        """🔴 THE CONTROL. A lookup that returned USD for everything would make
        the test above pass and would turn every unmarked foreign bill into a
        dollar bill."""
        for junk in ("ZZ", "", None, "USA", "u", 7):
            self.assertIsNone(
                self.AccountMove._ai_currency_for_country(junk),
                "%r must not resolve to a currency" % (junk,),
            )

    def test_the_lookup_answers_for_an_archived_currency_too(self):
        """🔴 TWO QUESTIONS, AND 19.0.3.3.0 ANSWERED THEM WITH ONE VALUE.

        "What currency does this country use" is a fact about `res.country` and
        does not depend on what this database happens to have switched on.
        "May we write it on a bill" is a fact about `res.currency.active`.
        Collapsing them into a single `None` made the archived case both fail
        the backstop AND suppress the chatter note on a currency the model had
        already chosen — see
        `test_an_archived_currency_the_model_already_chose_is_marked`.

        Measured on a US reference database: all 251 countries carry a `currency_id`, and
        exactly 16 of those currencies are active. CAD, EUR, GBP, CNY and SEK
        are archived, so this is the ordinary case rather than a corner."""
        AM = self.AccountMove
        currency = (
            self.env["res.currency"]
            .with_context(active_test=False)
            .search([("name", "=", "CAD")], limit=1)
        )
        self.assertTrue(currency, "CAD must exist on any Odoo database")
        currency.active = False
        self.assertEqual(
            AM._ai_currency_for_country("CA"),
            "CAD",
            "the country's currency is a fact about the country",
        )
        self.assertFalse(AM._ai_currency_is_active("CAD"))
        currency.active = True
        self.assertTrue(AM._ai_currency_is_active("CAD"))

    def test_the_active_question_refuses_what_it_cannot_answer(self):
        """🔴 THE CONTROL. A predicate that said yes to everything would make
        the test above pass and would let the backstop write a currency core
        has no row for at all — which IS the case where core substitutes the
        company currency."""
        for junk in ("ZZZ", "", None, "US Dollar", 7):
            self.assertFalse(
                self.AccountMove._ai_currency_is_active(junk),
                "%r must not read as a currency we trade in" % (junk,),
            )

    def test_countries_do_not_all_answer_the_same(self):
        """A second control on the same point, and the one that proves the
        lookup is reading the row rather than a constant."""
        self.assertNotEqual(
            self.AccountMove._ai_currency_for_country("US"),
            self.AccountMove._ai_currency_for_country("GB"),
        )

    # -- the note ------------------------------------------------------------

    def _new_import(self):
        att = self.env["ir.attachment"].create(
            {
                "name": "zz-freight.pdf",
                "raw": b"%PDF-1.4 fake",
                "mimetype": "application/pdf",
            }
        )
        return self.env["goodahead.invoice.import"].create(
            {
                "name": "zz-freight.pdf",
                "company_id": self.company.id,
                "pdf_attachment_id": att.id,
            }
        )

    def _run(self, extracted):
        rec = self._new_import()
        ai_result = {
            "extracted": extracted,
            "usage": {"input_tokens": 10, "output_tokens": 20},
            "model_name": "test-model",
            "xml_bytes": generate_peppol_invoice_xml(extracted),
        }
        with patch.object(
            type(self.AccountMove),
            "_ai_extract_and_generate_ubl",
            return_value=ai_result,
        ):
            result = rec._process_import()
        return rec, result

    def _bodies(self, move):
        return "\n".join(
            (m.body or "") for m in move.message_ids
        )

    def test_the_bill_says_the_currency_was_not_printed(self):
        """🔴 19.0.2.3.0's principle is that a wrong currency is invisible and
        nobody ever sees it — the terminal failure was what supplied the
        visibility. Resolving the case has to supply it some other way, and
        this note is it.

        The marker is NOT hand-written into the fixture: the extraction goes
        through the real rule, with the real `res.country` lookup injected, so
        this test fails if the rule stops firing OR if the note stops being
        posted."""
        extracted = _freight_extraction()
        self._resolver()._resolve_currency_from_country(extracted)
        self.assertEqual(extracted["currency"], "USD", "precondition")

        rec, result = self._run(extracted)
        self.assertTrue(result.startswith("OK"), result)
        move = rec.move_id
        self.assertTrue(move)
        self.assertEqual(move.currency_id.name, "USD")

        bodies = self._bodies(move)
        self.assertIn("No currency symbol or ISO code was found", bodies)
        self.assertIn("United States", bodies)
        self.assertIn("USD", bodies)
        self.assertIn(
            CURRENCY_INFERRED_KEY,
            rec.extraction_data or "",
            "the stored JSON has to carry it too, so it is auditable later",
        )

    def _inferred_live_extraction(self):
        """The live 000000000001 answer, run through the REAL rule with the
        REAL `res.country` lookup. Nothing is hand-written into it, so this
        fails if the rule stops marking the model's own inference."""
        extracted = _model_inferred_extraction()
        self._resolver()._resolve_currency_from_country(extracted)
        return extracted

    def test_the_bill_says_so_when_the_model_did_the_inferring(self):
        """🔴 THE ONE THE LIVE RUN FAILED. `currency` arrives already "USD"
        because the prompt's step 4 did the work, and the bill must still carry
        the note — the visibility cannot depend on which layer happened to
        resolve it."""
        extracted = self._inferred_live_extraction()
        self.assertEqual(
            extracted.get(CURRENCY_INFERRED_KEY), "US", "precondition"
        )

        rec, result = self._run(extracted)
        self.assertTrue(result.startswith("OK"), result)
        move = rec.move_id
        self.assertEqual(move.currency_id.name, "USD")

        bodies = self._bodies(move)
        self.assertIn("No currency symbol or ISO code was found", bodies)
        self.assertIn("United States", bodies)
        self.assertIn("USD", bodies)

    def test_the_note_does_not_claim_why(self):
        """🔴 THE WORDING IS PART OF THE DECISION, not prose.

        The note now fires on a document whose currency may have come from the
        BANK DETAILS (CURRENCY step 3) rather than from the address, and the
        server cannot tell those apart. So it may state only what
        `currency_symbol: null` actually proves — that nothing was printed and
        the value was therefore deduced — and may not name the address as the
        reason. The sentence it replaced said *"because the vendor's address
        and ours are both there"*, which on an ABA-routing-number bill is
        simply untrue."""
        bodies = self._bodies(self._run(self._inferred_live_extraction())[0].move_id)
        self.assertIn("was not read off the page", bodies)
        self.assertNotIn(
            "because the vendor&#39;s address and ours are both there", bodies
        )
        self.assertNotIn(
            "because the vendor's address and ours are both there", bodies
        )

    def test_an_ordinary_import_posts_no_such_note(self):
        """🔴 THE CONTROL. The same document with the currency printed on it.
        A note posted unconditionally would pass the test above and would tell
        every accountant, on every bill, that the currency was guessed — which
        is how a warning stops being read."""
        extracted = _freight_extraction(
            currency="USD", **{CURRENCY_SYMBOL_KEY: "$"}
        )
        rec, result = self._run(extracted)
        self.assertTrue(result.startswith("OK"), result)
        bodies = self._bodies(rec.move_id)
        self.assertNotIn("No currency symbol or ISO code was found", bodies)


@tagged("post_install", "-at_install")
class TestTheMarkerOnReimport(_NoClaudeMixin, TransactionCase):
    """🔴 THE MARKER IS A CLAIM, AND A CLAIM HAS TO HOLD WHEN IT IS MADE — NOT
    ONLY WHEN IT WAS WRITTEN.

    `_resolve_currency_from_country` asks three questions and sets the marker
    only if all three answer yes. It runs inside `_validate_extraction`, and
    `action_reimport_from_json` does not go there: it generates UBL from the
    stored JSON and goes straight to `_finalize_import` → `_ai_post_process`,
    which first trusted `extracted[CURRENCY_INFERRED_KEY]` blind.

    The two values the note interpolates — the marker and `currency` — are read
    from the same JSON and nothing keeps them in step. They come apart on an
    ORDINARY failure, because the resolver runs FIRST in `_validate_extraction`,
    before the required-field sweep, the shape checks and `_validate_arithmetic`
    — every one of which raises with `data=data`, and `ai_invoice_import.py`
    persists exactly that `data`. So the marker reaches `extraction_data` on a
    failed import, and the stored JSON is precisely what a person then edits by
    hand — correcting the currency by editing the JSON is how this feature was
    born.

    A hand-typed CAD then arrives at a note reading *"it was deduced as CAD …
    both in United States, whose currency is CAD"* — two false claims, in the
    direction that looks like success, on the one mechanism this feature has for
    being seen at all.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.company.claude_api_key = "test-key"
        cls.AccountMove = cls.env["account.move"]

    def _marked_extraction(self):
        """The live 000000000001 shape, marked by the REAL rule and the REAL
        lookups — never hand-written, so a rule that stopped firing fails here
        rather than passing on a fixture."""
        extracted = _model_inferred_extraction()
        ClaudeInvoiceExtractor(
            client=None,
            currency_from_country=self.AccountMove._ai_currency_for_country,
            currency_is_active=self.AccountMove._ai_currency_is_active,
        )._resolve_currency_from_country(extracted)
        self.assertEqual(
            extracted.get(CURRENCY_INFERRED_KEY), "US", "precondition"
        )
        return extracted

    def _failed_import(self, extracted):
        att = self.env["ir.attachment"].create(
            {
                "name": "zz-freight.pdf",
                "raw": b"%PDF-1.4 fake",
                "mimetype": "application/pdf",
            }
        )
        rec = self.env["goodahead.invoice.import"].create(
            {
                "name": "zz-freight.pdf",
                "company_id": self.company.id,
                "pdf_attachment_id": att.id,
            }
        )
        rec.write(
            {
                "state": "failed",
                "error_message": "line totals do not add up",
                "extraction_data": json.dumps(
                    extracted, indent=2, ensure_ascii=False
                ),
            }
        )
        return rec

    def _bodies(self, move):
        return "\n".join((m.body or "") for m in move.message_ids)

    def test_a_re_import_that_still_holds_keeps_the_note(self):
        """🔴 THE CONTROL, AND IT COMES FIRST. Re-validating the marker must not
        become "drop the marker on every re-import": a person's own recovery
        path is a re-import, and a bill that loses the note there loses it on
        exactly the documents this feature was written for."""
        rec = self._failed_import(self._marked_extraction())
        rec.action_reimport_from_json()
        self.assertTrue(rec.move_id, rec.error_message)
        bodies = self._bodies(rec.move_id)
        self.assertIn("was not read off the page", bodies)
        self.assertIn("United States", bodies)

    def test_a_hand_edited_currency_is_not_presented_as_a_deduction(self):
        """🔴 A USER'S HAND-EDIT WORKFLOW, END TO END.

        A user opens the stored JSON to fix the arithmetic, sees on the PDF that the
        bill is in CAD, corrects `currency`, and leaves the key they have never
        heard of alone. USD is the currency of the United States and CAD is not,
        so the third of the three questions already answers no — nothing asked
        it."""
        # CAD is archived on this database (16 of 251 country currencies are
        # active), and core imports an archived currency perfectly well. It is
        # activated here only so this test is about the NOTE and cannot fail for
        # a reason that belongs to another test.
        self.env["res.currency"].with_context(active_test=False).search(
            [("name", "=", "CAD")], limit=1
        ).active = True

        extracted = self._marked_extraction()
        extracted["currency"] = "CAD"
        rec = self._failed_import(extracted)
        rec.action_reimport_from_json()

        self.assertTrue(rec.move_id, rec.error_message)
        self.assertEqual(rec.move_id.currency_id.name, "CAD")
        bodies = self._bodies(rec.move_id)
        self.assertNotIn("was not read off the page", bodies)
        self.assertNotIn("deduced as", bodies)
        self.assertNotIn(
            CURRENCY_INFERRED_KEY,
            rec.extraction_data or "",
            "a claim that no longer holds must not survive into the stored "
            "JSON either, or the next re-import inherits it again",
        )

    def test_a_hand_edited_country_is_not_presented_as_a_deduction(self):
        """The second question, re-asked. A person corrects the bill-to country
        because the ship-to block was misread; the two parties are no longer in
        one country, so "the vendor's address and ours are both in X" is no
        longer a thing the document says."""
        extracted = self._marked_extraction()
        extracted["buyer"]["country_code"] = "CA"
        rec = self._failed_import(extracted)
        rec.action_reimport_from_json()

        self.assertTrue(rec.move_id, rec.error_message)
        self.assertNotIn("was not read off the page", self._bodies(rec.move_id))

    def test_a_hand_edited_symbol_is_not_presented_as_a_deduction(self):
        """The first question, re-asked. If the JSON now says a symbol WAS
        printed, the note's opening clause — *"No currency symbol or ISO code was
        printed anywhere on this document"* — is contradicted by the very field
        it is standing on."""
        extracted = self._marked_extraction()
        extracted[CURRENCY_SYMBOL_KEY] = "$"
        rec = self._failed_import(extracted)
        rec.action_reimport_from_json()

        self.assertTrue(rec.move_id, rec.error_message)
        self.assertNotIn("was not read off the page", self._bodies(rec.move_id))


@tagged("post_install", "-at_install")
class TestTheCountryWritesItselfOntoTheVendor(_NoClaudeMixin, TransactionCase):
    """⚠️ ONE SCHEMA SENTENCE SWITCHED ON TWO MASTER-DATA WRITES.

    `_AI_VENDOR_AUTO_FIELDS` carries `("country_id", "country_code")` and
    `("state_id", "state_code")`, and `_ai_apply_vendor_data` writes both onto a
    vendor whose fields are blank with nobody in the loop. Before this PR a US
    bill that never prints "USA" gave `country_code: null`, so `country_id` was
    skipped — and `state_id` silently declined with it, because
    `_ai_resolve_location` needs a country to scope the state lookup.

    So reading the country out of the address format does not only feed the
    currency rule: it turns on a vendor write that had been dormant on every
    domestic US bill. It is wanted — the comment ordering country before state
    exists precisely for this — but it was not covered anywhere, and an
    auto-write nobody has a test for is an auto-write nobody will notice
    changing.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.AM = cls.env["account.move"]
        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", cls.env.company.id)],
            limit=1,
        )

    def _bill(self):
        return self.AM.create(
            {"move_type": "in_invoice", "journal_id": self.journal.id}
        )

    def test_a_country_read_off_the_address_fills_the_vendor_country_and_state(
        self,
    ):
        """Both fields, and the ORDER is what makes the second one work: the
        state is looked up against the country this same loop has just staged.
        A `country_code` of null leaves BOTH blank."""
        partner = self.env["res.partner"].create(
            {"name": "ZZ Blank Freight Vendor", "supplier_rank": 1}
        )
        self.assertFalse(partner.country_id, "precondition")
        self.assertFalse(partner.state_id, "precondition")

        self._bill()._ai_apply_vendor_data(
            partner, _model_inferred_extraction()
        )
        self.assertEqual(partner.country_id.code, "US")
        self.assertEqual(partner.state_id.code, "AR")

    def test_without_a_country_on_the_bill_neither_is_written(self):
        """🔴 THE CONTROL, and it is the behaviour this PR changed. With
        `country_code` null the state declines too — so the test above is
        measuring the new schema sentence, not a pre-existing write."""
        partner = self.env["res.partner"].create(
            {"name": "ZZ Countryless Freight Vendor", "supplier_rank": 1}
        )
        extracted = _model_inferred_extraction()
        extracted["seller"]["country_code"] = None

        self._bill()._ai_apply_vendor_data(partner, extracted)
        self.assertFalse(partner.country_id)
        self.assertFalse(partner.state_id)

    def test_a_vendor_that_already_has_a_country_is_not_overwritten(self):
        """A filled field is never replaced by an auto-write; a difference goes
        to the suggestion queue for a person. This is the rule that makes the
        whole auto-write defensible, so it is pinned next to it."""
        finland = self.env["res.country"].search([("code", "=", "FI")], limit=1)
        partner = self.env["res.partner"].create(
            {
                "name": "ZZ Already Placed Vendor",
                "supplier_rank": 1,
                "country_id": finland.id,
            }
        )
        self._bill()._ai_apply_vendor_data(
            partner, _model_inferred_extraction()
        )
        self.assertEqual(partner.country_id, finland)
