# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The extractor reports cut-off, declined and empty answers explicitly, and
requests its own per-call limits rather than relying on shared defaults.

The extractor is built with a stub client, so nothing here can reach the
network.
"""

import json

from odoo.tests import BaseCase, tagged

from odoo.addons.goodahead_invoice_ai_import.lib.claude_client import (
    SYSTEM_PROMPT,
    ClaudeExtractionError,
    ClaudeInvoiceExtractor,
)


class _StubClient:
    """Stands in for `goodahead.claude.client`, recording how it was called."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def messages_create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


@tagged("post_install", "-at_install")
class TestClaudeClient(BaseCase):
    def _extract(self, body, model=None):
        client = _StubClient(body)
        self.client = client
        ClaudeInvoiceExtractor(client=client, model=model).extract_invoice(
            b"%PDF-1.4 fake"
        )
        return client

    def test_cut_off_answer_is_named(self):
        body = {
            "stop_reason": "max_tokens",
            "content": [{"type": "text", "text": '{"type": "Invoice", "lines": ['}],
            "usage": {"input_tokens": 4000, "output_tokens": 16000},
        }
        with self.assertRaisesRegex(ClaudeExtractionError, "cut off"):
            self._extract(body)

    def test_refusal_is_named(self):
        body = {"stop_reason": "refusal", "content": [], "usage": {}}
        with self.assertRaisesRegex(ClaudeExtractionError, "declined"):
            self._extract(body)

    def test_empty_answer_is_named(self):
        body = {"stop_reason": "end_turn", "content": [], "usage": {}}
        with self.assertRaisesRegex(ClaudeExtractionError, "empty response"):
            self._extract(body)

    def test_limits_are_asked_for_per_call(self):
        """`max_tokens` and `timeout` are passed on each call.

        The shared `goodahead_claude` client has lower defaults and other
        modules use it, so the defaults must not be changed instead.
        """
        body = {"stop_reason": "refusal", "content": [], "usage": {}}
        with self.assertRaises(ClaudeExtractionError):
            self._extract(body)

        self.assertEqual(len(self.client.calls), 1)
        sent = self.client.calls[0]
        self.assertEqual(sent["max_tokens"], 16000)
        self.assertEqual(sent["timeout"], 300)

    def test_the_call_asks_for_a_model_that_can_read_a_pdf(self):
        """The call requests a model with PDF input support, per call, so other
        users of the shared client keep their own model resolution."""
        body = {"stop_reason": "refusal", "content": [], "usage": {}}
        with self.assertRaises(ClaudeExtractionError):
            self._extract(body)

        self.assertIs(self.client.calls[0]["requires_pdf"], True)

    def test_no_model_is_sent_when_none_is_configured(self):
        """`model=None` leaves the choice to the shared client's model
        registry instead of a hard-coded (and eventually retired) model id."""
        body = {"stop_reason": "refusal", "content": [], "usage": {}}
        with self.assertRaises(ClaudeExtractionError):
            self._extract(body)
        self.assertIsNone(self.client.calls[0]["model"])

    def test_configured_model_is_passed_through(self):
        body = {"stop_reason": "refusal", "content": [], "usage": {}}
        with self.assertRaises(ClaudeExtractionError):
            self._extract(body, model="claude-opus-4-7")
        self.assertEqual(self.client.calls[0]["model"], "claude-opus-4-7")

    def test_pdf_is_sent_as_a_document_block(self):
        body = {"stop_reason": "refusal", "content": [], "usage": {}}
        with self.assertRaises(ClaudeExtractionError):
            self._extract(body)
        content = self.client.calls[0]["messages"][0]["content"]
        document = next(b for b in content if b["type"] == "document")
        self.assertEqual(document["source"]["media_type"], "application/pdf")

    # ------------------------------------------------------------------
    # The prompt itself
    #
    # These tests assert on prompt text: an instruction is present, reaches
    # the API call and has not been reverted. They say nothing about how the
    # model actually reads an invoice; that needs real documents and real
    # calls.
    # ------------------------------------------------------------------

    def test_the_date_rule_is_not_single_locale(self):
        """The date rule does not impose a single MM/DD convention; on many
        non-US invoices `06/03/2026` is 3 June."""
        self.assertNotIn("US invoices usually print MM/DD/YYYY, so", SYSTEM_PROMPT)
        self.assertNotIn("03/04/2026 is 2026-03-04", SYSTEM_PROMPT)
        self.assertIn("DATES.", SYSTEM_PROMPT)

    def test_the_date_rule_is_an_ordered_procedure(self):
        """The date rule is an ordered procedure: the date itself, the rest of
        the document, the country as a prior, then null."""
        for fragment in (
            "THE DATE ITSELF",
            "THE REST OF THE DOCUMENT",
            "THE VENDOR'S COUNTRY",
            "IN ORDER",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, SYSTEM_PROMPT)

    def test_the_country_is_a_prior_and_says_so(self):
        """The vendor's country is only a prior: exporters often format
        invoices for the buyer."""
        self.assertIn("A PRIOR ONLY, NEVER PROOF", SYSTEM_PROMPT)
        self.assertIn("formats an invoice for its BUYER rather than for home", SYSTEM_PROMPT)

    def test_the_null_escape_survives_and_covers_the_exporter_case(self):
        """An unsettled date becomes null, including a non-US vendor billing a
        US customer with no other signal; a missing date is visible, a wrong
        one is not."""
        self.assertIn("output null for that date", SYSTEM_PROMPT)
        self.assertIn(
            "a non-US vendor billing a US customer, with an ambiguous date and "
            "no other signal, is a NULL",
            " ".join(SYSTEM_PROMPT.split()),
        )

    def test_the_cross_checks_that_settle_a_date_are_named(self):
        """The concrete evidence that settles a date is named: a component
        above 12, a spelled-out month, ISO order, the vendor's other dates and
        the payment-term interval."""
        flat = " ".join(SYSTEM_PROMPT.split())
        for fragment in (
            "greater than 12 can only be the day",
            "spelled-out or abbreviated month",
            "4-digit year first means ISO order",
            "fixes the order for the other dates THE VENDOR'S OWN SYSTEM PRINTED",
            "Net 30",
            "gives an interval of many months",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, flat)

    def test_the_style_clause_is_about_provenance_not_appearance(self):
        """A proven format only carries over to dates the vendor's own system
        printed, not to dates quoted from elsewhere (e.g. a carrier's ship
        date)."""
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertIn("THE VENDOR'S OWN SYSTEM PRINTED", flat)
        self.assertIn("about where a date came from, not about how it looks", flat)
        self.assertIn("A date QUOTED from somewhere else", flat)

    def test_two_disagreeing_proofs_route_to_null(self):
        """Two contradicting proofs on one document (`13/06/2026` and
        `06/15/2026`) lead to null rather than to whichever was read first."""
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertIn("IF TWO DATES ON THE SAME DOCUMENT ARE BOTH PROOF AND THEY DISAGREE", flat)
        self.assertIn("Go straight to step 4", flat)
        self.assertIn("Do not stop at whichever one you happened to read first", flat)

    def test_a_two_digit_year_does_not_fall_through_to_the_country(self):
        """A two-digit year such as `06/03/26` is not settled by the country
        prior."""
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertIn("A TWO-digit year is not settled by this step", flat)
        self.assertIn("do not let it fall through to the country", flat)

    def test_the_due_date_check_is_a_tiebreak_not_an_axiom(self):
        """A due date before the invoice date is a weak signal, not a
        disproof; back-dated bills exist."""
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertNotIn("A due date never falls before the invoice date", flat)
        self.assertIn("a negative interval is a weak signal and not a disproof on its own", flat)

    def test_the_quantity_rule_does_not_multiply_by_a_thousand(self):
        """For quantities, three digits after a separator are normally decimals
        (`2.500` hours is 2.5).

        Document totals cannot catch this because the unit price is derived
        from the line total; `_validate_line_magnitude` checks it per line (see
        `test_reconciliation.py`).
        """
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertIn(
            "FOR A QUANTITY, THE SAME THREE DIGITS ARE NORMALLY DECIMAL, NOT THOUSANDS",
            flat,
        )
        self.assertIn('"2.500" hours is 2.5 hours', flat)
        self.assertIn("quantity × unit price must equal the line total", flat)

    def test_the_number_rule_is_not_single_locale(self):
        """The decimal separator is identified before stripping thousands
        separators, so `1.234,56` reads as 1234.56."""
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertIn("NUMBERS.", flat)
        self.assertIn('"1.234,56" is also 1234.56', flat)
        self.assertIn("the LAST one is the decimal separator", flat)
        self.assertIn("BEFORE stripping", flat)

    def test_the_prompt_that_is_sent_is_the_prompt_that_is_tested(self):
        """The `system` argument the client receives contains the rules
        asserted above on the module constant."""
        body = {"stop_reason": "refusal", "content": [], "usage": {}}
        with self.assertRaises(ClaudeExtractionError):
            self._extract(body)
        sent = " ".join(self.client.calls[0]["system"].split())
        self.assertIn("A PRIOR ONLY, NEVER PROOF", sent)
        self.assertIn("output null for that date", sent)
        self.assertIn('"1.234,56" is also 1234.56', sent)
        self.assertNotIn("03/04/2026 is 2026-03-04", sent)

    def test_the_prompt_still_formats_with_the_categories_block(self):
        """`SYSTEM_PROMPT` survives `str.format`: every literal brace must be
        doubled, or every extraction fails."""
        filled = SYSTEM_PROMPT.format(categories_block="Available: x")
        self.assertIn("Available: x", filled)
        self.assertIn('"issue_date": "YYYY-MM-DD', filled)
        self.assertNotIn("{categories_block}", filled)

    # ------------------------------------------------------------------
    # The currency and document-type rules in the prompt
    #
    # Same caveat as above: these check prompt text, not model behaviour.
    # ------------------------------------------------------------------

    def test_the_currency_rule_is_not_a_symbol_lookup(self):
        """There is no USD default, and a symbol alone does not identify a
        currency ("$", "kr" are shared by several)."""
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertNotIn("USD for a US invoice unless stated otherwise", flat)
        self.assertIn("CURRENCY.", flat)
        self.assertIn("A SYMBOL IS NOT A CURRENCY", flat)

    def test_the_currency_rule_is_an_ordered_procedure(self):
        """The currency rule is ordered: printed ISO code, symbol plus country,
        other evidence, then null."""
        flat = " ".join(SYSTEM_PROMPT.split())
        for fragment in (
            "AN ISO CODE PRINTED ON THE DOCUMENT",
            "outranks everything below",
            "THE SYMBOL TOGETHER WITH THE VENDOR'S COUNTRY",
            "OTHER EVIDENCE ON THE DOCUMENT",
            "IN ORDER and stop at the first one that settles it",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, flat)

    def test_the_ambiguous_currency_symbols_are_named(self):
        """Symbols that stay ambiguous even with a country (`¥`, `kr`, `£`)
        are named; CNY against JPY is roughly a factor of twenty."""
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertIn("GENUINELY AMBIGUOUS EVEN WITH A COUNTRY", flat)
        for fragment in ("¥", "￥", "kr", "£"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, flat)
        self.assertIn("CNY in China, JPY in Japan", flat)

    def test_the_other_currency_evidence_is_named(self):
        """Other currency evidence is named: a jurisdiction-identifying tax
        rate, an IBAN prefix, an ABA routing number, a dual-currency total."""
        flat = " ".join(SYSTEM_PROMPT.split())
        for fragment in (
            "sales-tax rate that identifies a jurisdiction",
            "IBAN's country prefix",
            "9-digit ABA routing number",
            "stated exchange rate, or a total printed twice in two currencies",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, flat)

    def test_the_currency_null_escape_forbids_the_usd_fallback(self):
        """An unsettled currency is null, never a USD, symbol-only or
        country-only guess."""
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertIn(
            "Do NOT fall back on USD, on the symbol alone, or on the country "
            "alone",
            flat,
        )
        self.assertIn("a wrong currency produces a bill", flat)

    def test_the_document_type_is_an_explicit_question(self):
        """Invoice versus credit note is an explicit question, not an English
        keyword match."""
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertNotIn(
            '"CreditNote" for credit memos, credit notes and refunds (look for',
            flat,
        )
        self.assertIn("DOCUMENT TYPE.", flat)
        self.assertIn(
            "IS THIS DOCUMENT A BILL FOR MONEY WE OWE, OR A CREDIT THAT "
            "REDUCES WHAT WE OWE?",
            flat,
        )
        self.assertIn("FLIPS THE SIGN OF THE WHOLE DOCUMENT", flat)

    def test_the_credit_note_evidence_is_multilingual_and_not_only_words(self):
        """Credit-note evidence is multilingual and marked as examples, and
        also covers type codes and references to a corrected document."""
        flat = " ".join(SYSTEM_PROMPT.split())
        for fragment in (
            "Hyvityslasku",
            "Gutschrift",
            "Kreditfaktura",
            "红字发票",
            "贷记通知单",
            "Nota di credito",
            "Faktura korygująca",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, flat)
        self.assertIn("EXAMPLES, not a lookup table", flat)
        self.assertIn("1001 code 381", flat)
        self.assertIn("A REFERENCE TO AN EARLIER DOCUMENT BEING CORRECTED", flat)

    def test_the_two_sign_error_cases_are_both_stated(self):
        """A credit note with positive amounts is still a credit note, and an
        invoice with one negative line is still an invoice."""
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertIn("A CREDIT NOTE THAT PRINTS ITS AMOUNTS POSITIVE", flat)
        self.assertIn(
            "absence of minus signs is NOT evidence that the document is an "
            "invoice",
            flat,
        )
        self.assertIn("AN INVOICE THAT CARRIES A NEGATIVE LINE", flat)
        self.assertIn(
            "one negative line does not make the document a credit note", flat
        )
        self.assertIn("KEEP their minus signs inside it", flat)

    def test_the_new_rules_reach_the_call_that_is_actually_made(self):
        """The currency and document-type rules reach the `system` argument
        the client receives."""
        body = {"stop_reason": "refusal", "content": [], "usage": {}}
        with self.assertRaises(ClaudeExtractionError):
            self._extract(body)
        sent = " ".join(self.client.calls[0]["system"].split())
        self.assertIn("A SYMBOL IS NOT A CURRENCY", sent)
        self.assertIn("CNY in China, JPY in Japan", sent)
        self.assertIn("Hyvityslasku", sent)
        self.assertIn("AN INVOICE THAT CARRIES A NEGATIVE LINE", sent)
        self.assertNotIn("USD for a US invoice unless stated otherwise", sent)

    def test_the_new_blocks_survive_str_format(self):
        """The CURRENCY and DOCUMENT TYPE blocks survive `str.format`."""
        filled = SYSTEM_PROMPT.format(categories_block="Available: x")
        self.assertIn("Available: x", filled)
        self.assertIn("A SYMBOL IS NOT A CURRENCY", filled)
        self.assertIn("DOCUMENT TYPE.", filled)
        self.assertIn('"invoice_number": "string"', filled)
        self.assertNotIn("{categories_block}", filled)

    # ------------------------------------------------------------------
    # The schema block against the procedures above it
    #
    # The schema is read closest to the output, so where it contradicts a
    # procedure the model tends to follow the schema.
    # ------------------------------------------------------------------

    def test_every_schema_line_with_a_null_procedure_says_or_null(self):
        """Schema lines for dates say "or null" and point to the DATES
        procedure."""
        self.assertNotIn('"issue_date": "YYYY-MM-DD",', SYSTEM_PROMPT)
        for field in ("issue_date", "due_date"):
            with self.subTest(field=field):
                line = next(
                    text for text in SYSTEM_PROMPT.splitlines()
                    if '"%s": ' % field in text
                )
                self.assertIn("or null", line)
                self.assertIn("DATES procedure", line)

    def test_the_sellers_address_may_be_null_like_the_buyers(self):
        """The seller's address fields may be null, like the buyer's, so the
        model is not pushed to invent them."""
        flat = " ".join(SYSTEM_PROMPT.split())
        for field in ("street", "city", "postal_code"):
            with self.subTest(field=field):
                self.assertNotIn('"%s": "string",' % field, flat)

    def test_the_vendors_country_is_never_inferred(self):
        """The vendor's country is never inferred from the currency, language
        or the other party, since the currency and date rules reason from
        it."""
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertNotIn("2-letter ISO (US for a US vendor)", flat)
        self.assertIn(
            "Never take it from the currency, the language, or the other "
            "party's address",
            flat,
        )

    def test_the_important_rules_still_restate_the_date_null(self):
        """The closing rules block restates the date-null rule, the last thing
        the model reads before answering."""
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertIn(
            "All dates must be YYYY-MM-DD, resolved by the DATES procedure "
            "above, and null when that procedure does not settle them",
            flat,
        )

    # ------------------------------------------------------------------
    # Validation: an unknown currency is an explicit failure
    # ------------------------------------------------------------------

    def _ok_extraction(self, **overrides):
        """A complete, arithmetically consistent extraction."""
        data = {
            "type": "Invoice",
            "invoice_number": "INV-1",
            "issue_date": "2026-03-06",
            "currency": "CNY",
            "seller": {"name": "ZZ EXAMPLE CATERING MACHINERY CO"},
            "lines": [{
                "name": "Widget",
                "quantity": 1,
                "price_unit": 10.0,
                "line_total": 10.0,
                "tax_percent": 6.625,
                "tax_amount": 0.66,
            }],
            "tax_subtotals": [{
                "tax_percent": 6.625,
                "taxable_amount": 10.0,
                "tax_amount": 0.66,
                "tax_category": "S",
            }],
            "totals": {
                "line_extension": 10.0,
                "tax_exclusive": 10.0,
                "tax_inclusive": 10.66,
                "payable": 10.66,
            },
        }
        data.update(overrides)
        return {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": json.dumps(data)}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    def test_a_good_extraction_still_validates(self):
        """Control for the tests below: a good extraction validates."""
        client = _StubClient(self._ok_extraction())
        result = ClaudeInvoiceExtractor(client=client).extract_invoice(b"%PDF")
        self.assertEqual(result["data"]["currency"], "CNY")

    def test_a_missing_currency_fails_the_import_loudly(self):
        """A missing currency fails the extraction instead of defaulting.

        The parsed JSON rides out on the exception, so a person can fill in the
        currency and re-import without another API call.
        """
        for value in (None, "", False):
            with self.subTest(value=value):
                client = _StubClient(self._ok_extraction(currency=value))
                with self.assertRaisesRegex(
                    ClaudeExtractionError, "Missing required fields.*currency"
                ):
                    ClaudeInvoiceExtractor(client=client).extract_invoice(b"%PDF")

    def test_a_currency_symbol_is_not_accepted_as_a_code(self):
        """Values that are not 3-letter ISO codes are rejected; core would
        silently fall back to the company currency for them.
        """
        for value in ("$", "￥", "US$", "EURO", 840, "元"):
            with self.subTest(value=value):
                client = _StubClient(self._ok_extraction(currency=value))
                with self.assertRaisesRegex(
                    ClaudeExtractionError, "not a 3-letter ISO 4217 code"
                ):
                    ClaudeInvoiceExtractor(client=client).extract_invoice(b"%PDF")

    def test_the_failed_extraction_rides_out_on_the_exception(self):
        """The parsed JSON is attached to the exception for hand-editing."""
        client = _StubClient(self._ok_extraction(currency=None))
        with self.assertRaises(ClaudeExtractionError) as caught:
            ClaudeInvoiceExtractor(client=client).extract_invoice(b"%PDF")
        self.assertEqual(caught.exception.data["invoice_number"], "INV-1")

    def test_a_transport_failure_becomes_a_named_extraction_error(self):
        """Any exception from the shared client is re-raised as a
        ClaudeExtractionError."""

        class _Exploding:
            def messages_create(self, **kwargs):
                raise RuntimeError("no model is available for company X")

        with self.assertRaisesRegex(ClaudeExtractionError, "no model is available"):
            ClaudeInvoiceExtractor(client=_Exploding()).extract_invoice(b"%PDF")
