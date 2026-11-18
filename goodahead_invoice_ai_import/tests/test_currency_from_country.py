# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Currency of a domestic invoice that prints no currency at all.

The rule, one test per row:

    no symbol + no code printed, same country   -> resolve from the country
    a symbol was printed and did not settle     -> still null, still a failure
    an ISO code printed                         -> it wins
    no symbol, same country, model already set  -> mark it anyway, provided it
                                                   is the country's currency

An ambiguous symbol such as `¥` (CNY or JPY) must never be resolved from the
buyer's country; that case is pinned in
`test_ambiguous_symbol_from_another_country_still_fails`.

`TestCurrencyFromCountryRule` runs the real `extract_invoice` against a stub
client, so there is no transport to reach. The ORM classes never call the
extractor and install a guard that fails if they do.
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
from odoo.addons.goodahead_invoice_ai_import.lib.ubl_generator import (
    generate_peppol_invoice_xml,
)


class _StubClient:
    """Stands in for `goodahead.claude.client`: returns a canned body."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def messages_create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


# A freight bill with bare amounts: two 1,874.25 lines, no tax, no currency
# marking of any kind.
def _freight_extraction(**overrides):
    data = {
        "type": "Invoice",
        "invoice_number": "ZZ-FRT-1",
        "issue_date": "2026-10-02",
        "currency": None,
        CURRENCY_SYMBOL_KEY: None,
        "seller": {
            "name": "ZZ Freight Carrier Inc",
            "street": "1 Nowhere Road",
            "city": "Exampleton",
            "state_code": "TN",
            "postal_code": "37999",
            "country_code": "US",
        },
        "buyer": {"name": "ZZ Buyer LLC", "country_code": "US"},
        "lines": [
            {
                "name": "FREIGHT CHARGE %d" % n,
                "quantity": 1.0,
                "unit_code": "C62",
                "price_unit": 1874.25,
                "line_total": 1874.25,
                "tax_percent": 0.0,
                "tax_amount": 0.0,
                "tax_category": "Z",
            }
            for n in (1, 2)
        ],
        "tax_subtotals": [
            {
                "tax_percent": 0.0,
                "taxable_amount": 3748.50,
                "tax_amount": 0.0,
                "tax_category": "Z",
            }
        ],
        "totals": {
            "line_extension": 3748.50,
            "tax_exclusive": 3748.50,
            "tax_inclusive": 3748.50,
            "payable": 3748.50,
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


# Fake country lookup for the database-free tests. The real one
# (`account.move._ai_currency_for_country`) is tested in the ORM class.
_FAKE_COUNTRY_CURRENCY = {"US": "USD", "FI": "EUR", "CN": "CNY"}


def _fake_lookup(country_code):
    return _FAKE_COUNTRY_CURRENCY.get(country_code)


# The shape a real import produces: the prompt's CURRENCY step 4 makes the
# model infer the currency itself, so `currency` is already "USD" while
# `currency_symbol` is null.
def _model_inferred_extraction(**overrides):
    data = _freight_extraction(currency="USD")
    data["seller"].update(
        {
            "name": "ZZ Freight Carrier Inc",
            "state_code": "AR",
            "postal_code": "62701-1234",
            "country_code": "US",
        }
    )
    data["buyer"].update({"name": "ZZ Buyer LLC", "country_code": "US"})
    data.update(overrides)
    return data


# Whether the database trades in a currency at all; a separate question from
# which currency a country uses.
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
    """The rule itself, without database or network."""

    # -- the main case --------------------------------------------------------

    def test_no_symbol_and_both_parties_in_the_same_country_resolves(self):
        """Bare amounts with both addresses in the US resolve to USD."""
        result = _extract(_freight_extraction())
        self.assertEqual(result["data"]["currency"], "USD")
        self.assertEqual(
            result["data"][CURRENCY_INFERRED_KEY],
            "US",
            "the bill has to be able to say WHERE the currency came from",
        )

    # -- the model already inferred it ---------------------------------------

    def test_the_model_s_own_country_inference_is_marked_too(self):
        """A currency the model inferred from the country is still marked.

        A usable `currency` alone does not tell the server who inferred it, so
        it re-asks the same three questions: nothing printed, one country on
        both sides, and the value matches that country's currency."""
        data = _model_inferred_extraction()
        result = _extract(data)
        self.assertEqual(result["data"]["currency"], "USD")
        self.assertEqual(
            result["data"][CURRENCY_INFERRED_KEY],
            "US",
            "the model inferred it from the country and the bill must say so",
        )

    def test_a_currency_the_country_does_not_explain_is_not_marked(self):
        """FI to FI answered as USD was settled by other evidence, not the
        country: it is neither marked nor rewritten."""
        data = _model_inferred_extraction()
        data["seller"]["country_code"] = "FI"
        data["buyer"]["country_code"] = "FI"
        result = _extract(data)
        self.assertEqual(result["data"]["currency"], "USD")
        self.assertNotIn(CURRENCY_INFERRED_KEY, result["data"])

    def test_bank_details_on_an_unmarked_domestic_bill_are_still_marked(self):
        """A bill with an ABA routing number is still marked.

        The server cannot tell whether the bank or the address settled the
        currency, so it marks it and the note only claims what a null symbol
        proves (see `test_the_note_does_not_claim_why`)."""
        data = _model_inferred_extraction()
        data["seller"]["routing_number"] = "021000021"
        result = _extract(data)
        self.assertEqual(result["data"]["currency"], "USD")
        self.assertEqual(result["data"][CURRENCY_INFERRED_KEY], "US")

    def test_a_printed_iso_code_wins(self):
        """A code read off the page is kept and no inference is claimed."""
        result = _extract(
            _freight_extraction(currency="CAD", **{CURRENCY_SYMBOL_KEY: "CAD"})
        )
        self.assertEqual(result["data"]["currency"], "CAD")
        self.assertNotIn(CURRENCY_INFERRED_KEY, result["data"])

    def test_a_printed_iso_code_wins_even_with_no_symbol_reported(self):
        """A settled currency short-circuits the fallback even when the symbol
        field is null, so US-to-US CAD is not rewritten to USD."""
        result = _extract(_freight_extraction(currency="CAD"))
        self.assertEqual(result["data"]["currency"], "CAD")
        self.assertNotIn(CURRENCY_INFERRED_KEY, result["data"])

    # -- the row that must not move ------------------------------------------

    def test_ambiguous_symbol_from_another_country_still_fails(self):
        """A Chinese vendor printing `¥` to a US buyer stays a failure: the
        buyer's country must never decide between CNY and JPY."""
        data = _freight_extraction(
            **{CURRENCY_SYMBOL_KEY: "¥"}
        )
        data["seller"]["name"] = "ZZ Example Catering Machinery Co"
        data["seller"]["country_code"] = "CN"
        with self.assertRaises(ClaudeExtractionError) as caught:
            _extract(data)
        self.assertNotIn(CURRENCY_INFERRED_KEY, caught.exception.data)
        self.assertIsNone(caught.exception.data["currency"])

    def test_ambiguous_symbol_in_the_same_country_still_fails(self):
        """Any printed symbol blocks the server-side fallback, even `$` on a
        US-to-US bill."""
        with self.assertRaises(ClaudeExtractionError):
            _extract(_freight_extraction(**{CURRENCY_SYMBOL_KEY: "$"}))

    def test_different_countries_do_not_infer(self):
        """Two countries give two candidate currencies, so this stays a failure
        with the JSON attached for hand-editing."""
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
        """An unknown country fails and keeps the JSON."""
        data = _freight_extraction()
        data["seller"]["country_code"] = "ZZ"
        data["buyer"]["country_code"] = "ZZ"
        with self.assertRaises(ClaudeExtractionError) as caught:
            _extract(data)
        self.assertNotIn(CURRENCY_INFERRED_KEY, caught.exception.data)

    def test_a_missing_buyer_country_does_not_infer(self):
        """The vendor's country alone is not enough, and the company's own
        country is deliberately not substituted for the buyer's."""
        data = _freight_extraction()
        data["buyer"].pop("country_code")
        with self.assertRaises(ClaudeExtractionError):
            _extract(data)

    # -- the boundaries of the symbol field ----------------------------------

    def test_extraction_json_without_the_symbol_field_does_not_infer(self):
        """An absent `currency_symbol` key (JSON stored without it) is not the same
        as null and must not trigger the rule."""
        data = _freight_extraction()
        data.pop(CURRENCY_SYMBOL_KEY)
        with self.assertRaises(ClaudeExtractionError):
            _extract(data)

    def test_an_unreadable_symbol_value_is_not_permission(self):
        """A non-string symbol value does not license the fallback."""
        for junk in (0, True, [], {}):
            with self.assertRaises(ClaudeExtractionError):
                _extract(_freight_extraction(**{CURRENCY_SYMBOL_KEY: junk}))

    def test_a_whitespace_only_symbol_counts_as_nothing_printed(self):
        """A whitespace-only symbol counts as nothing printed."""
        result = _extract(_freight_extraction(**{CURRENCY_SYMBOL_KEY: "  "}))
        self.assertEqual(result["data"]["currency"], "USD")

    def test_without_the_injected_lookup_nothing_is_ever_inferred(self):
        """Without `currency_from_country` the rule never fires."""
        client = _StubClient(_body(_freight_extraction()))
        with self.assertRaises(ClaudeExtractionError):
            ClaudeInvoiceExtractor(client=client).extract_invoice(b"%PDF")

    def test_a_lookup_returning_junk_is_refused(self):
        """A lookup answer that is not an ISO code is refused, since core's
        `_import_currency` would fall back to the company currency."""
        for junk in ("US Dollar", "$", "usdd", "", None):
            with self.assertRaises(ClaudeExtractionError):
                _extract(_freight_extraction(), lookup=lambda cc, j=junk: j)

    # -- an archived currency: two questions, two answers --------

    def test_an_archived_currency_is_not_written(self):
        """The backstop does not choose a currency the company has archived.

        Core's `_import_currency` would accept an archived row (it searches
        with `active_test=False` and only logs a warning), and with no
        maintained rate the bill would be booked at 1.0. Failing with the JSON
        stored puts a person in front of the configuration problem. Many
        common currencies are archived on a typical database."""
        with self.assertRaises(ClaudeExtractionError):
            _extract(
                _freight_extraction(),
                lookup=lambda cc: "SEK",
                active=lambda name: False,
            )

    def test_an_archived_currency_the_model_already_chose_is_marked(self):
        """When the model already chose the archived currency, the bill is
        still marked: whether we trade in it has no bearing on whether the
        country explains it."""
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
        """Core matches `res.currency.name` exactly, so the answer is
        upper-cased."""
        result = _extract(_freight_extraction(), lookup=lambda cc: "usd")
        self.assertEqual(result["data"]["currency"], "USD")

    # -- the prompt half -----------------------------------------------------

    def test_the_prompt_asks_for_the_symbol_and_states_the_limits(self):
        """The prompt requests the symbol field and forbids the country rule
        when a symbol was printed."""
        self.assertIn('"currency_symbol"', SYSTEM_PROMPT)
        self.assertIn("SAME country", SYSTEM_PROMPT)
        self.assertIn("THIS STEP DOES NOT APPLY WHEN A SYMBOL WAS PRINTED",
                      SYSTEM_PROMPT)

    def test_the_prompt_reads_the_country_out_of_the_address_format(self):
        """A US address with state and ZIP (or ZIP+4) counts as showing its
        country even when "USA" is never printed."""
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertIn("AN ADDRESS SHOWS ITS COUNTRY WHEN ITS OWN FORMAT DOES",
                      flat)
        self.assertIn("a 2-letter US state with a ZIP is US", flat)
        self.assertIn("'62701-1234' and '78701-4321' both count", flat)
        self.assertIn("a ZIP+4 is still a ZIP", flat)
        self.assertIn(
            "Never take it from the currency, the language, or the other "
            "party's address", flat)
        # Asking for the country "as printed" yields null when no country
        # name is printed.
        self.assertNotIn(
            "2-letter ISO code of the vendor's OWN address as printed", flat)

    def test_the_buyers_country_is_never_taken_from_the_sellers(self):
        """The buyer's country must not be copied from the seller's address.

        The same-country check is what guards the whole rule, and a copied
        buyer country would compare a value against itself."""
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertIn(
            "Never take it from the currency, the language, or the SELLER'S "
            "address",
            flat,
        )

    def test_the_symbol_question_is_about_the_whole_page(self):
        """`currency_symbol` asks about the whole page, not only the amounts,
        so null means the same as CURRENCY step 4(a)."""
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
        """The real rule wired to the real ORM lookups."""
        return ClaudeInvoiceExtractor(
            client=None,
            currency_from_country=self.AccountMove._ai_currency_for_country,
            currency_is_active=self.AccountMove._ai_currency_is_active,
        )

    # -- the lookup itself ---------------------------------------------------

    def test_the_lookup_reads_res_country_currency_id(self):
        """The lookup reads `res.country.currency_id`."""
        self.assertEqual(self.AccountMove._ai_currency_for_country("US"), "USD")

    def test_the_lookup_refuses_what_it_cannot_answer(self):
        """Unknown or malformed country codes resolve to None."""
        for junk in ("ZZ", "", None, "USA", "u", 7):
            self.assertIsNone(
                self.AccountMove._ai_currency_for_country(junk),
                "%r must not resolve to a currency" % (junk,),
            )

    def test_the_lookup_answers_for_an_archived_currency_too(self):
        """The country lookup ignores `active`; whether a currency is active
        is answered separately by `_ai_currency_is_active`."""
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
        """Unknown currency names are never reported as active."""
        for junk in ("ZZZ", "", None, "US Dollar", 7):
            self.assertFalse(
                self.AccountMove._ai_currency_is_active(junk),
                "%r must not read as a currency we trade in" % (junk,),
            )

    def test_countries_do_not_all_answer_the_same(self):
        """The lookup reads the row rather than returning a constant."""
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
        return self.env["ai.invoice.import"].create(
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
        """An inferred currency is visible as a chatter note on the bill.

        The marker comes from the real rule and the real `res.country` lookup,
        so the test fails if the rule stops firing or the note stops being
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
        """A model-inferred extraction marked by the real rule and lookup."""
        extracted = _model_inferred_extraction()
        self._resolver()._resolve_currency_from_country(extracted)
        return extracted

    def test_the_bill_says_so_when_the_model_did_the_inferring(self):
        """The note is posted when the model, not the server, inferred the
        currency."""
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
        """The note states only that the currency was not read off the page,
        never that the addresses were the reason (bank details may have
        settled it)."""
        bodies = self._bodies(self._run(self._inferred_live_extraction())[0].move_id)
        self.assertIn("was not read off the page", bodies)
        self.assertNotIn(
            "because the vendor&#39;s address and ours are both there", bodies
        )
        self.assertNotIn(
            "because the vendor's address and ours are both there", bodies
        )

    def test_an_ordinary_import_posts_no_such_note(self):
        """A bill with a printed currency gets no inference note."""
        extracted = _freight_extraction(
            currency="USD", **{CURRENCY_SYMBOL_KEY: "$"}
        )
        rec, result = self._run(extracted)
        self.assertTrue(result.startswith("OK"), result)
        bodies = self._bodies(rec.move_id)
        self.assertNotIn("No currency symbol or ISO code was found", bodies)


@tagged("post_install", "-at_install")
class TestTheMarkerOnReimport(_NoClaudeMixin, TransactionCase):
    """The inference marker is re-validated on re-import from stored JSON.

    `action_reimport_from_json` skips `_validate_extraction`, and the marker can
    reach the stored JSON of a failed import. A user who then hand-edits the
    currency or a country must not get a note claiming a deduction that no
    longer holds.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.company.claude_api_key = "test-key"
        cls.AccountMove = cls.env["account.move"]

    def _marked_extraction(self):
        """A model-inferred extraction marked by the real rule and lookups."""
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
        rec = self.env["ai.invoice.import"].create(
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
        """A re-import whose marker still holds keeps the note."""
        rec = self._failed_import(self._marked_extraction())
        rec.action_reimport_from_json()
        self.assertTrue(rec.move_id, rec.error_message)
        bodies = self._bodies(rec.move_id)
        self.assertIn("was not read off the page", bodies)
        self.assertIn("United States", bodies)

    def test_a_hand_edited_currency_is_not_presented_as_a_deduction(self):
        """A hand-corrected currency that the country does not explain drops
        the note and the marker."""
        # Activated only so the test is about the note, not archived currencies.
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
        """A hand-edited buyer country in another country drops the note."""
        extracted = self._marked_extraction()
        extracted["buyer"]["country_code"] = "CA"
        rec = self._failed_import(extracted)
        rec.action_reimport_from_json()

        self.assertTrue(rec.move_id, rec.error_message)
        self.assertNotIn("was not read off the page", self._bodies(rec.move_id))

    def test_a_hand_edited_symbol_is_not_presented_as_a_deduction(self):
        """A hand-edited symbol means something was printed, so the note is
        dropped."""
        extracted = self._marked_extraction()
        extracted[CURRENCY_SYMBOL_KEY] = "$"
        rec = self._failed_import(extracted)
        rec.action_reimport_from_json()

        self.assertTrue(rec.move_id, rec.error_message)
        self.assertNotIn("was not read off the page", self._bodies(rec.move_id))


@tagged("post_install", "-at_install")
class TestTheCountryWritesItselfOntoTheVendor(_NoClaudeMixin, TransactionCase):
    """A country read from the address format also fills blank vendor fields.

    `_ai_apply_vendor_data` auto-writes `country_id` and `state_id`; the state
    lookup needs the country, so a null `country_code` leaves both blank.
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
        """Country and state are both filled; the state is looked up against
        the country staged in the same loop."""
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
        """With `country_code` null neither country nor state is written."""
        partner = self.env["res.partner"].create(
            {"name": "ZZ Countryless Freight Vendor", "supplier_rank": 1}
        )
        extracted = _model_inferred_extraction()
        extracted["seller"]["country_code"] = None

        self._bill()._ai_apply_vendor_data(partner, extracted)
        self.assertFalse(partner.country_id)
        self.assertFalse(partner.state_id)

    def test_a_vendor_that_already_has_a_country_is_not_overwritten(self):
        """A filled field is never overwritten by the auto-write."""
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
