# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The extractor names a cut-off or declined answer instead of failing on it
later as a JSON parse error or an "empty response", and it asks for the limits
this feature needs rather than the shared defaults.

🔴 NOTHING HERE TOUCHES THE NETWORK. The extractor is constructed with a stub
client, so there is no `requests` to patch and no way for a real call to slip
through. That is the point of injecting the client rather than importing it:
on the source project this file patched `claude_client.requests`, which only
works while the module owns its own transport — and a patch that silently stops
matching is how you discover you have been billing yourself in a test suite.
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
        """🔴 The guard on the shared client.

        `goodahead_claude` defaults to 4096 output tokens and a 120 s timeout,
        and `goodahead_fraud_detection` calls the same client. This feature
        needs 16000 and 300 s, and it must get them by ASKING on its own call —
        never by moving those defaults, which would move fraud detection too.

        If anybody ever "simplifies" this by setting the limits on the company
        or in `lib/claude_client.py`'s DEFAULT_*, this test fails.
        """
        body = {"stop_reason": "refusal", "content": [], "usage": {}}
        with self.assertRaises(ClaudeExtractionError):
            self._extract(body)

        self.assertEqual(len(self.client.calls), 1)
        sent = self.client.calls[0]
        self.assertEqual(sent["max_tokens"], 16000)
        self.assertEqual(sent["timeout"], 300)

    def test_the_call_asks_for_a_model_that_can_read_a_pdf(self):
        """🔴 The capability that was synced and never read.

        `goodahead_claude.sync_models` has always stored
        `capabilities.pdf_input.supported` on `goodahead.claude.model`, and
        nothing anywhere consulted it. So the first sync bringing in a newer
        model without PDF support would have retargeted this prompt — which
        sends a PDF and nothing else — at a model that cannot open the
        document, with no error until the API rejected the call.

        Asked here, per call, for the same reason `max_tokens` and `timeout`
        are: `goodahead_fraud_detection` shares this client, sends text, and
        must keep resolving exactly as it did.
        """
        body = {"stop_reason": "refusal", "content": [], "usage": {}}
        with self.assertRaises(ClaudeExtractionError):
            self._extract(body)

        self.assertIs(self.client.calls[0]["requires_pdf"], True)

    def test_no_model_is_sent_when_none_is_configured(self):
        """`model=None` means "let the registry decide".

        The source module sent a hardcoded `claude-sonnet-4-20250514` here,
        which is retired — every such call 404s at the API, and no gate can see
        it. Passing None hands the decision to
        `goodahead.claude.client._resolve_model`, which reads the fetched
        `goodahead.claude.model` registry.
        """
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
    # 🔴 READ THIS BEFORE TRUSTING ANY TEST BELOW. These tests assert on the
    # TEXT OF A PROMPT. They prove that a particular instruction is present,
    # reaches the API call, and has not been quietly reverted. They prove
    # NOTHING AT ALL about how the model reads an invoice. No test in this file
    # — and no test that can be run without spending money on a real call —
    # can demonstrate that an ambiguous Finnish date comes back as null. That
    # needs real documents put through a real call and the answers looked at.
    #
    # What these tests are for is the failure that has already happened twice on
    # this project: a rule that was decided and then was not in the code. A
    # string test catches a deletion and a rewrite. It catches nothing else, and
    # it must not be reported as if it did.
    # ------------------------------------------------------------------

    def test_the_date_rule_is_not_single_locale(self):
        """The defect: the rule named ONE convention, with a worked example in
        it, and no condition on where the invoice came from.

        On a Finnish or Chinese invoice `06/03/2026` is 3 June. Read under a
        blanket MM/DD rule it becomes 6 March — a three-month error on a due
        date, on a bill that then looks entirely valid. This business buys from
        the United States, from Finland and from China.

        What must not come back: the old unconditional sentence, or any new
        one like it.
        """
        self.assertNotIn("US invoices usually print MM/DD/YYYY, so", SYSTEM_PROMPT)
        self.assertNotIn("03/04/2026 is 2026-03-04", SYSTEM_PROMPT)
        self.assertIn("DATES.", SYSTEM_PROMPT)

    def test_the_date_rule_is_an_ordered_procedure(self):
        """Evidence outranks inference, and the order is stated.

        1 the date itself · 2 the rest of the document · 3 the country, as a
        prior · 4 null. Each of the four must be nameable in the text, because
        a list of facts in no particular order is what the model was given
        before.
        """
        for fragment in (
            "THE DATE ITSELF",
            "THE REST OF THE DOCUMENT",
            "THE VENDOR'S COUNTRY",
            "IN ORDER",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, SYSTEM_PROMPT)

    def test_the_country_is_a_prior_and_says_so(self):
        """🔴 A correction made in review, and the case it is about.

        A vendor abroad can format its invoice for a US buyer. A Chinese or
        Finnish exporter billing a US customer often prints MM/DD/YYYY on
        purpose — formatted for the buyer, not for home. So the sender's
        country is a weak prior, never proof, and the prompt has to say that in
        as many words or the model will treat it as a rule.
        """
        self.assertIn("A PRIOR ONLY, NEVER PROOF", SYSTEM_PROMPT)
        self.assertIn("formats an invoice for its BUYER rather than for home", SYSTEM_PROMPT)

    def test_the_null_escape_survives_and_covers_the_exporter_case(self):
        """The one that would be cheapest to lose and worst to lose.

        A wrong date is invisible; a missing one is not. And the case that will
        actually occur here — a non-US vendor billing a US customer, ambiguous
        date, no other signal — must reach null rather than falling back on the
        country, because there the country and the buyer's convention point
        opposite ways and neither of them is evidence.
        """
        self.assertIn("output null for that date", SYSTEM_PROMPT)
        self.assertIn(
            "a non-US vendor billing a US customer, with an ambiguous date and "
            "no other signal, is a NULL",
            " ".join(SYSTEM_PROMPT.split()),
        )

    def test_the_cross_checks_that_settle_a_date_are_named(self):
        """Evidence the model can actually use, not an exhortation to be careful.

        A component above 12, a spelled-out month, ISO order, the vendor's own
        other dates on the same page, and the invoice-to-due interval against
        stated terms — a 14-day term read the wrong way round becomes an absurd
        one, which is what makes the interval worth checking.

        ⚠️ Two of these fragments were REWRITTEN by the review and this test
        caught it, which is the only reason the list is quoted this literally.
        "every date printed in the same style on that document" became a
        provenance test, and "negative or absurd interval" became a tiebreak
        rather than an absolute. A fragment list that is never wrong is a
        fragment list that is not being read.
        """
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
        """🟡 Review finding: "printed in the same style" is a VISUAL test.

        A US invoice quoting a European carrier's ship date `15/06/2026` gives
        step 1 a proof — a component above 12 — and the clause as first written
        propagated DD/MM from it onto the vendor's own `03/04/2026`. The
        premise behind the clause was always dates the vendor's own system
        generated together; the wording was wider than the premise.
        """
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertIn("THE VENDOR'S OWN SYSTEM PRINTED", flat)
        self.assertIn("about where a date came from, not about how it looks", flat)
        self.assertIn("A date QUOTED from somewhere else", flat)

    def test_two_disagreeing_proofs_route_to_null(self):
        """🟡 Review finding: no branch for a document that mixes formats.

        `13/06/2026` and `06/15/2026` on one page are both step-1 proofs and
        they contradict each other. "Stop at the first step that settles it"
        then means the model stops at whichever it read first and propagates a
        format the document itself disproves.
        """
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertIn("IF TWO DATES ON THE SAME DOCUMENT ARE BOTH PROOF AND THEY DISAGREE", flat)
        self.assertIn("Go straight to step 4", flat)
        self.assertIn("Do not stop at whichever one you happened to read first", flat)

    def test_a_two_digit_year_does_not_fall_through_to_the_country(self):
        """Minor review finding, but it falls the wrong way.

        `06/03/26` is settled by none of step 1's three clauses, so without
        this it dropped quietly to the country prior — which is the one place
        the whole change is trying to keep dates out of.
        """
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertIn("A TWO-digit year is not settled by this step", flat)
        self.assertIn("do not let it fall through to the country", flat)

    def test_the_due_date_check_is_a_tiebreak_not_an_axiom(self):
        """Minor review finding. A back-dated or already-overdue bill is real.

        "A due date never falls before the invoice date" was stated absolutely.
        It costs a null rather than a wrong date, so it fails safe — but an
        absolute that is not true teaches the model to distrust the rest.
        """
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertNotIn("A due date never falls before the invoice date", flat)
        self.assertIn("a negative interval is a weak signal and not a disproof on its own", flat)

    def test_the_quantity_rule_does_not_multiply_by_a_thousand(self):
        """🔴 Review finding, and it is this change's own bug arriving from the
        other side.

        The NUMBERS block covers "every amount AND QUANTITY", and said three
        digits after a separator is normally a thousands separator. So a US
        invoice billing `2.500` hours or `1.250 LB` read as 2500 and 1250 — a
        factor of a thousand, in the quantity, where the amount-side escape
        hatch ("the line totals, tax and total must add up") does not apply.

        ⚠️ THE "NOTHING DOWNSTREAM CATCHES IT" THIS DOCSTRING USED TO CARRY IS
        NO LONGER TRUE, and the correction is worth more than the claim was.
        Something does now — `_validate_line_magnitude`, added in 19.0.2.4.0 —
        but ONLY the per-line check can, and the reason is in the sentence this
        docstring already had: the amount-side escape hatch ("the totals must
        add up") does not apply to a quantity. The generator derives the unit
        price from the line total, so a quantity wrong by a thousand leaves
        every document total intact. See `test_reconciliation.py`.
        """
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertIn(
            "FOR A QUANTITY, THE SAME THREE DIGITS ARE NORMALLY DECIMAL, NOT THOUSANDS",
            flat,
        )
        self.assertIn('"2.500" hours is 2.5 hours', flat)
        self.assertIn("quantity × unit price must equal the line total", flat)

    def test_the_number_rule_is_not_single_locale(self):
        """The same class of error as the date one, and a factor of a thousand.

        `1.234,56` is 1234.56 in Finland and Germany. Under the old rule —
        "strip thousands separators", with `$1,234.56` as its only example —
        there was nothing to stop it being read as 1.23456, or 1234. The
        separator has to be identified before anything is stripped.
        """
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertIn("NUMBERS.", flat)
        self.assertIn('"1.234,56" is also 1234.56', flat)
        self.assertIn("the LAST one is the decimal separator", flat)
        self.assertIn("BEFORE stripping", flat)

    def test_the_prompt_that_is_sent_is_the_prompt_that_is_tested(self):
        """The control, and it is not ceremony.

        Everything above reads the module constant. If the extractor ever stops
        sending that constant — a second prompt, a trimmed one, a per-company
        override — every assertion above would keep passing against text that
        no longer reaches the API. This asserts on the `system` argument the
        stub client actually received.
        """
        body = {"stop_reason": "refusal", "content": [], "usage": {}}
        with self.assertRaises(ClaudeExtractionError):
            self._extract(body)
        sent = " ".join(self.client.calls[0]["system"].split())
        self.assertIn("A PRIOR ONLY, NEVER PROOF", sent)
        self.assertIn("output null for that date", sent)
        self.assertIn('"1.234,56" is also 1234.56', sent)
        self.assertNotIn("03/04/2026 is 2026-03-04", sent)

    def test_the_prompt_still_formats_with_the_categories_block(self):
        """The regression this edit could plausibly cause.

        `SYSTEM_PROMPT` is run through `str.format`, so every literal brace in
        it must be doubled. The JSON schema in the prompt is full of them. A new
        block containing a single `{` raises `KeyError`/`IndexError` on every
        invoice.

        ⚠️ An earlier version of this docstring said no other test here would
        notice, "because they all read the raw constant". That was WRONG, and
        the review replayed it: the `.format()` call is at
        `claude_client.py:293`, OUTSIDE the `try` that wraps the API call, so a
        stray brace makes all seven pre-existing `_extract()` tests error too.
        This test is still worth keeping — it names the cause in one line
        instead of leaving seven unrelated-looking errors to be diagnosed — but
        it is not the only thing standing between a brace and the floor, and
        claiming it was overstated what a passing run proves.
        """
        filled = SYSTEM_PROMPT.format(categories_block="Available: x")
        self.assertIn("Available: x", filled)
        self.assertIn('"issue_date": "YYYY-MM-DD', filled)
        self.assertNotIn("{categories_block}", filled)

    # ------------------------------------------------------------------
    # The currency and document-type rules in the prompt
    #
    # 🔴 THE SAME WARNING AS THE BLOCK ABOVE APPLIES, UNCHANGED. These assert on
    # the TEXT OF A PROMPT. They prove an instruction is present, that it
    # reaches the API call, and that it has not been quietly reverted. They
    # prove NOTHING about how the model reads an invoice. No test here — and no
    # test that avoids paying for a real call — can show that a ¥ invoice from a
    # Chinese vendor comes back "CNY" rather than "JPY", or that a
    # "Hyvityslasku" comes back "CreditNote". That needs real documents put
    # through a real call and the answers looked at.
    # ------------------------------------------------------------------

    def test_the_currency_rule_is_not_a_symbol_lookup(self):
        """The defect: the schema line was the whole of the currency rule, and
        it said `USD for a US invoice unless stated otherwise`.

        A symbol does not identify a currency. "$" is USD, CAD, AUD, HKD, SGD
        and MXN; "kr" is SEK, NOK or DKK. This business buys from the United
        States, from Finland and from China.

        What must not come back: the old default-to-USD sentence.
        """
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertNotIn("USD for a US invoice unless stated otherwise", flat)
        self.assertIn("CURRENCY.", flat)
        self.assertIn("A SYMBOL IS NOT A CURRENCY", flat)

    def test_the_currency_rule_is_an_ordered_procedure(self):
        """Evidence outranks inference, and the order is stated — the same
        shape as the DATES rule, because the model follows an explicit
        precedence better than a list of facts.

        1 a printed ISO code · 2 symbol plus country · 3 other evidence ·
        4 null.
        """
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
        """🔴 The ¥ case, which is the expensive one.

        "¥" and "￥" are printed in both China and Japan, and CNY against JPY
        is roughly a factor of twenty on the same invoice. The symbol plus the
        country is NOT enough there, so the prompt has to name the symbols
        where that shortcut fails rather than leaving the model to notice.
        """
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertIn("GENUINELY AMBIGUOUS EVEN WITH A COUNTRY", flat)
        for fragment in ("¥", "￥", "kr", "£"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, flat)
        self.assertIn("CNY in China, JPY in Japan", flat)

    def test_the_other_currency_evidence_is_named(self):
        """Evidence the model can actually use, not an exhortation to be
        careful: the language, a jurisdiction-identifying tax rate, an IBAN
        country prefix or an ABA routing number, and a dual-currency total."""
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
        """The one that would be cheapest to lose and worst to lose.

        A null currency now stops the import and a person looks at the
        document. A guessed one produces a bill with plausible numbers and the
        wrong value, which nobody ever sees.
        """
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertIn(
            "Do NOT fall back on USD, on the symbol alone, or on the country "
            "alone",
            flat,
        )
        self.assertIn("a wrong currency produces a bill", flat)

    def test_the_document_type_is_an_explicit_question(self):
        """The framing: one of the questions the model has to answer is whether
        the document is an invoice or a credit note.

        The old rule was an English keyword match — "Credit Memo", "Credit
        Note", "RMA credit" — on documents that arrive from Finland and China.
        """
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
        """Vendors here are American, Finnish and Chinese.

        The word list is examples, not a lookup table — and the prompt says so,
        because a list of words is exactly what it replaced. Beside it: a
        document-type code, a reference to the original document, and
        return/refund/correction wording.
        """
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
        """🔴 They are different problems and the prompt must separate them.

        A credit note that prints POSITIVE amounts is the normal case, and the
        absence of minus signs is not evidence that it is an invoice. An
        invoice that carries a NEGATIVE line — a discount, a deposit, a
        returned item credited on the same bill — is still an invoice, and that
        line keeps its sign.
        """
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
        """The control, and it is not ceremony.

        Everything above reads the module constant. If the extractor ever stops
        sending that constant — a second prompt, a trimmed one, a per-company
        override — every assertion above would keep passing against text that
        no longer reaches the API. This asserts on the `system` argument the
        stub client actually received.
        """
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
        """The regression this edit could plausibly cause, and it would be
        total.

        `SYSTEM_PROMPT` goes through `str.format`, so every literal brace in it
        must be doubled. A single stray `{` or `}` in the new CURRENCY or
        DOCUMENT TYPE block raises `KeyError`/`IndexError` inside
        `extract_invoice`, on EVERY invoice.

        ⚠️ As the sibling test above now says, this is not the only thing
        standing between a stray brace and the floor — the `.format()` call is
        outside the `try`, so a brace makes the seven `_extract()` tests error
        as well. It is worth keeping because it names the cause in one line
        instead of leaving seven unrelated-looking errors to be diagnosed.
        """
        filled = SYSTEM_PROMPT.format(categories_block="Available: x")
        self.assertIn("Available: x", filled)
        self.assertIn("A SYMBOL IS NOT A CURRENCY", filled)
        self.assertIn("DOCUMENT TYPE.", filled)
        self.assertIn('"invoice_number": "string"', filled)
        self.assertNotIn("{categories_block}", filled)

    # ------------------------------------------------------------------
    # The schema block against the procedures above it
    #
    # 🔴 THE SCHEMA IS THE LINE CLOSEST TO THE OUTPUT, so where it contradicts
    # a procedure three hundred words earlier, the schema is what the model
    # follows. `currency` was fixed when it got its procedure; `issue_date` was
    # not, and nothing noticed for two releases.
    # ------------------------------------------------------------------

    def test_every_schema_line_with_a_null_procedure_says_or_null(self):
        """🔴 The defect: `"issue_date": "YYYY-MM-DD"`, flat, no null.

        DATES step 4 says to output null for a date that does not settle, and
        the whole point of the 19.0.2.2.0 rewrite was that a missing date is
        visible and a guessed one is not. The schema line then told the model,
        in the line it reads last, that the field is a string. Compare
        `due_date` ("or null") and `currency` ("or null if it does not
        settle") — the same block, two conventions.
        """
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
        """The same shape, found by auditing the rest of the block.

        `seller.street` / `city` / `postal_code` were declared bare `"string"`
        while the buyer's identical fields said `"string or null"`, and no
        procedure anywhere requires a vendor to print an address. The generator
        has always skipped an absent one (`if party_data.get("street")`), so the
        only thing the bare declaration could buy was an invented value.
        """
        flat = " ".join(SYSTEM_PROMPT.split())
        for field in ("street", "city", "postal_code"):
            with self.subTest(field=field):
                self.assertNotIn('"%s": "string",' % field, flat)

    def test_the_vendors_country_is_never_inferred(self):
        """🔴 The one with teeth, because the country is an INPUT to two
        procedures.

        `"country_code": "2-letter ISO (US for a US vendor)"` reads as a
        default in the same voice as the `USD for a US invoice unless stated
        otherwise` that 19.0.2.3.0 deleted. CURRENCY step 2 and DATES step 3
        both reason FROM the vendor's country — so a country inferred from the
        currency, and then used to settle the currency, is a circle that
        produces a confident answer out of nothing.

        ⚠️ The sentence this pins was REWORDED in 19.0.3.3.0, when the country
        stopped being read "as printed" and started being read out of the
        address format (see
        `test_the_prompt_reads_the_country_out_of_the_address_format`). The ban
        is unchanged and in fact wider — "the other party's address" covers the
        buyer and more — so only the literal moved, not what this test guards.
        """
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertNotIn("2-letter ISO (US for a US vendor)", flat)
        self.assertIn(
            "Never take it from the currency, the language, or the other "
            "party's address",
            flat,
        )

    def test_the_important_rules_still_restate_the_date_null(self):
        """⚠️ It is kept, and the reason it is kept has changed.

        Until the schema line above was fixed, this bullet was the ONLY thing
        pulling the other way and was load-bearing on its own. It is now a
        restatement rather than a contradiction — which is why it is safe to
        keep, and why it would still be wrong to remove: the rules block is the
        last thing the model reads before it answers, and restating a rule
        there costs nothing.
        """
        flat = " ".join(SYSTEM_PROMPT.split())
        self.assertIn(
            "All dates must be YYYY-MM-DD, resolved by the DATES procedure "
            "above, and null when that procedure does not settle them",
            flat,
        )

    # ------------------------------------------------------------------
    # Validation: a currency we do not know is a LOUD failure
    # ------------------------------------------------------------------

    def _ok_extraction(self, **overrides):
        """A complete, self-consistent extraction.

        ⚠️ IT GREW IN 19.0.2.4.0, and the growth is the point rather than
        housekeeping. It used to be one line with no `line_total` and a `totals`
        carrying `payable` alone, and it passed — because `_validate_extraction`
        checked presence and types and did no arithmetic whatever. A fixture
        that thin could not have told a good extraction from a broken one, so
        neither could the validator it was the control for.
        """
        data = {
            "type": "Invoice",
            "invoice_number": "INV-1",
            "issue_date": "2026-03-06",
            "currency": "CNY",
            "seller": {"name": "ZZ SHANGHAI KITCHEN EQUIPMENT CO"},
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
        """The control for the three tests below.

        Without it, a validator that rejected EVERYTHING would pass all of them
        and the import would be broken for every invoice.
        """
        client = _StubClient(self._ok_extraction())
        result = ClaudeInvoiceExtractor(client=client).extract_invoice(b"%PDF")
        self.assertEqual(result["data"]["currency"], "CNY")

    def test_a_missing_currency_fails_the_import_loudly(self):
        """🔴 The decision, and it is a deliberate behaviour change.

        Until now a null currency fell through to
        `ubl_generator.DEFAULT_CURRENCY` — "USD". A Chinese bill whose currency
        the model could not establish became a USD bill with plausible numbers
        and roughly one seventh of the right value, with nothing to see.

        A wrong currency is invisible; a missing one is not. So this is the
        same trade `issue_date` has always made, and it is cheap: the parsed
        JSON rides out on the exception, `_process_import` stores it, and a
        human fills the currency in and re-imports with no new API call.
        """
        for value in (None, "", False):
            with self.subTest(value=value):
                client = _StubClient(self._ok_extraction(currency=value))
                with self.assertRaisesRegex(
                    ClaudeExtractionError, "Missing required fields.*currency"
                ):
                    ClaudeInvoiceExtractor(client=client).extract_invoice(b"%PDF")

    def test_a_currency_symbol_is_not_accepted_as_a_code(self):
        """A symbol reaching core's `_import_currency` matches no
        `res.currency.name` and becomes the COMPANY currency, with only a log
        line. Only the shape is checkable here — "RMB" is three letters and
        still not a code — but the shape is what catches a model that copied
        the glyph off the document.
        """
        for value in ("$", "￥", "US$", "EURO", 840, "元"):
            with self.subTest(value=value):
                client = _StubClient(self._ok_extraction(currency=value))
                with self.assertRaisesRegex(
                    ClaudeExtractionError, "not a 3-letter ISO 4217 code"
                ):
                    ClaudeInvoiceExtractor(client=client).extract_invoice(b"%PDF")

    def test_the_failed_extraction_rides_out_on_the_exception(self):
        """What makes the loud failure cheap rather than expensive: Claude has
        already been billed, so the parsed JSON must survive the rejection for
        a human to fix and re-import without a second call."""
        client = _StubClient(self._ok_extraction(currency=None))
        with self.assertRaises(ClaudeExtractionError) as caught:
            ClaudeInvoiceExtractor(client=client).extract_invoice(b"%PDF")
        self.assertEqual(caught.exception.data["invoice_number"], "INV-1")

    def test_a_transport_failure_becomes_a_named_extraction_error(self):
        """Whatever the shared client raises — its own ClaudeAPIError, or the
        UserError it raises when no model can be resolved — reaches the caller
        as a ClaudeExtractionError, because the caller treats every failure
        here as terminal and must not have to know the client's exception
        types."""

        class _Exploding:
            def messages_create(self, **kwargs):
                raise RuntimeError("no model is available for company X")

        with self.assertRaisesRegex(ClaudeExtractionError, "no model is available"):
            ClaudeInvoiceExtractor(client=_Exploding()).extract_invoice(b"%PDF")
