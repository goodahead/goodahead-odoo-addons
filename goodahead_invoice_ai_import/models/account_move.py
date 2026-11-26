# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import logging

from markupsafe import Markup

from odoo import api, models

from odoo.addons.goodahead_invoice_import.lib import pdf_currency
from odoo.addons.goodahead_invoice_import.lib.invoice_data import (
    CURRENCY_MARKING_KEY,
    currency_marking_verdict,
)

from ..lib.claude_client import ClaudeInvoiceExtractor, locale_for_country

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    """The AI reading of a PDF. Everything that happens to the extraction after
    it is read — UBL generation, decoding, vendor matching, post-processing —
    lives in `goodahead_invoice_import` (core/source split), on the same model."""

    _inherit = "account.move"

    # ------------------------------------------------------------------
    # Claude extraction → UBL → Odoo import (shared by upload & email)
    # ------------------------------------------------------------------

    @api.model
    def _import_origin_note(self, invoice, attachment, usage, model_name,
                            source_label=None):
        """An AI reading says so, with the model and its token usage. A
        structured source (``source_label`` set, no usage) gets the core's
        neutral note — spec AC-21, the core/source split."""
        if source_label or usage is None:
            return super()._import_origin_note(
                invoice, attachment, usage, model_name, source_label=source_label
            )
        # `no_new_invoice`: on 18.0 a message with attachments makes core try
        # to decode them into the bill and post "already contains lines" when
        # it cannot.
        invoice.with_context(no_new_invoice=True).message_post(
            body=Markup(
                "<p>Imported via AI invoice extraction</p>"
                "<ul>"
                "<li>Model: %s</li>"
                "<li>Input tokens: %s</li>"
                "<li>Output tokens: %s</li>"
                "</ul>"
            ) % (model_name, usage["input_tokens"], usage["output_tokens"]),
            attachment_ids=[attachment.id],
        )

    @api.model
    def _ai_extract_and_generate_ubl(self, pdf_bytes, company):
        """Extract invoice data from PDF via Claude and generate Peppol XML.

        Returns:
            dict with "extracted", "usage", "model_name", "xml_bytes"

        Raises:
            ClaudeExtractionError, UBLGenerationError
        """
        # Shared categories plus this company's own — see
        # `goodahead.expense.category` for why the shipped ones are shared and
        # only the ACCOUNT is per company.
        categories = self.env["goodahead.expense.category"]._for_company(company)
        cat_list = [{"code": c.code, "name": c.name} for c in categories]
        if not cat_list:
            # Not fatal — the prompt falls back to a generic list — but it means
            # every line will land on the journal default, so say so once here
            # rather than leaving it to be inferred from a pile of warnings.
            _logger.warning(
                "AI invoice import: company %s has no expense categories, so "
                "extracted lines cannot be mapped to accounts.",
                company.display_name,
            )

        # 🔴 No hardcoded fallback model. `claude_invoice_model_id` is the
        # per-feature OVERRIDE; when it is empty we pass None and let
        # `goodahead.claude.client._resolve_model` decide — company default,
        # then the newest row in the fetched `goodahead.claude.model` registry,
        # then one on-demand sync, then a UserError naming the fix. The source
        # module fell back to the literal `claude-sonnet-4-20250514`, which is
        # retired: every such call would 404 at the API, which no gate can see.
        client = self.env["goodahead.claude.client"]
        requested_model = company.claude_invoice_model_id.model_api_id or None

        extractor = ClaudeInvoiceExtractor(
            client=client,
            model=requested_model,
            company=company,
            # 19.0.3.3.0. The prompt asks for the same-country rule, and a
            # prompt is not a guarantee: this is the server-side half, and it
            # is injected rather than imported so the lib keeps no ORM
            # dependency. See `_resolve_currency_from_country` for the rule and
            # `_ai_currency_for_country` for the lookup.
            currency_from_country=self._ai_currency_for_country,
            # The SECOND question, separate on purpose: what the
            # country uses, and whether we trade in it, are different facts and
            # only the first one is about the document.
            currency_is_active=self._ai_currency_is_active,
            # the dual-core design: which locale block the prompt is composed with.
            # Chosen from the COMPANY's country, not the vendor's — the block
            # is about the vocabulary this installation's own bills are
            # printed in, and a Finnish company buying from a US vendor still
            # wants the EU block's Y-tunnus and viitenumero wording available.
            # The vendor's country is already an input to the CURRENCY and
            # DATES procedures, which every block carries.
            locale=self._ai_prompt_locale(company),
        )
        result = extractor.extract_invoice(pdf_bytes, cat_list)

        extracted = result["data"]
        usage = result["usage"]
        xml_bytes = self._ai_generate_ubl_from_extracted(extracted, company)

        # Record the model that was actually used, not the one that was asked
        # for: when `requested_model` is None the resolver picked it, and the
        # chatter line "Model: None" would be worse than useless on a bill
        # somebody is auditing a year later.
        model_name = requested_model or client._resolve_model(
            company, requires_pdf=True
        )

        return {
            "extracted": extracted,
            "usage": usage,
            "model_name": model_name,
            "xml_bytes": xml_bytes,
        }

    @api.model
    def _ai_prompt_locale(self, company):
        """Which prompt locale block this company's bills are read with.

        🔴 AN UNSET OR UNKNOWN COUNTRY MUST NOT GET THE US BLOCK, and that is
        the whole of this method. The module was written on a US deployment, so
        "the default" and "the United States" are the same string here unless
        something stops them being — which is exactly the defect the dual-core design had
        just removed from the phone rule, where `US`/`CA` shaping was being
        applied to numbers that had never been North American.

        Both fallbacks are LOUD, because neither is an error and neither has a
        visible symptom. A company with no country, or a country this module
        ships no block for, gets `None` — the country-neutral block — and a
        WARNING saying so. The resulting prompt is weaker than either shipped
        one (it names no national identifier, tax or payment vocabulary) and
        nothing on the imported bill would ever show that, so the log line is
        the only place it can be seen.

        ⚠️ The company's country, NOT the vendor's. The block supplies the
        vocabulary the installation expects to read; the vendor's country is
        already an input to the CURRENCY and DATES procedures, which every
        block carries unchanged.

        A prompt profile picked on the company (Settings ▸ AI Invoice Import)
        overrides all of this, and its answer is either a shipped block's name
        or the profile's own block.
        """
        profile = company.ai_invoice_prompt_profile_id
        if profile:
            return profile._ai_prompt_locale()
        code = company.country_id.code
        if not code:
            _logger.warning(
                "AI invoice import: company %s has no country, so the prompt "
                "is composed from the country-neutral block. No US or EU "
                "identifier, tax or payment wording is applied. Set the "
                "company's country to get the locale block for it.",
                company.display_name,
            )
            return None
        locale = locale_for_country(code)
        if locale is None:
            _logger.warning(
                "AI invoice import: this module ships no prompt locale block "
                "for %s (company %s), so the country-neutral block is used. "
                "That is deliberate — guessing the nearest block would apply "
                "one country's identifier and payment vocabulary to another's "
                "documents.",
                code,
                company.display_name,
            )
        return locale

    @api.model
    def _ai_reask_the_pdf(self, extracted, pdf_bytes):
        """Re-decide the currency question from the BYTES, for a stored
        extraction arriving back through `action_reimport_from_json`.

        Mutates ``extracted`` in place. Two steps, and the second one is what
        19.0.3.19.0 left out.

        🔴 STEP 1 — THE VERDICT. `action_reimport_from_json` never asks the
        PDF: it loads the stored JSON and goes straight on. For every
        extraction stored by 19.0.3.3.0 through 19.0.3.17.0 there is no
        `currency_marking` key at all, so `nothing_was_printed` fell back to
        the model's `currency_symbol` — the instrument this feature line exists
        to stop depending on — while the bytes sat unread on the same record.

        🔴 STEP 2 — AND THE REPAIR WAS ONE-DIRECTIONAL UNTIL 19.0.3.20.0
        (found in review). Step 1 writes the verdict and nothing else. The only
        consumer on this path was `_ai_surviving_currency_marker`, whose first
        two lines return None when the JSON carries no marker — so the repair
        could DROP a marker that was wrong and could never ADD one that was
        missing. **A bill in exactly that state is the case this covers**:
        one imported with the model wrongly reporting
        `currency_symbol: "USD"`, so the gate closed, no marker was ever set,
        and no note was ever posted. Re-imported under 19.0.3.19.0 it would
        have had `text_layer: nothing_printed` computed from its own bytes,
        stored — and still got no note, because nothing re-asked the question
        the marker answers.

        So the resolver is re-run here. It is the same call `extract_invoice`
        makes (`_validate_extraction` → `_resolve_currency_from_country`),
        idempotent, with the two ORM lookups injected exactly as
        `_ai_extract_and_generate_ubl` injects them. **No Claude client is
        needed and none is built** — `ClaudeInvoiceExtractor.__init__` touches
        no network and `extract_invoice` is not called.

        🔴 AND IT RUNS BEFORE THE UBL IS GENERATED, WHICH IS WHY IT IS NOT IN
        `_finalize_import`. The resolver can WRITE a currency, not only mark
        one. `action_reimport_from_json` builds the XML and then calls
        `_finalize_import`, so a currency written in there would land in the
        stored JSON and never reach the bill — the extraction would say EUR
        while the bill said USD. Called from the re-import action instead, both
        directions are consistent. The forward path does not need it: by the
        time `_finalize_import` sees an extraction, `extract_invoice` has
        already written the verdict and run the resolver.

        ⚠️ A STORED VERDICT IS KEPT, BECAUSE IT MAY BE A HAND-EDIT.
        `test_the_marker_is_re_validated_against_the_stored_verdict` depends on
        exactly that latitude, and recomputing over it would take it away
        silently.

        🔴 EXCEPT AN `UNKNOWN` ONE, WHICH IS NOT AN ANSWER (found in review).
        `currency_marking_verdict` always returns a truthy dict, so the old
        "recompute when the stored value is falsy" guard wrote
        `{"text_layer": "unknown", "decided_by": "model", "alnum": 0}`
        permanently on the first re-import whose attachment happened to be
        unreadable at that moment — an encrypted PDF, a truncated upload, a
        record with no `pdf_attachment_id` at all. Every later re-import then
        found a present, truthy key and skipped the recompute, even after the
        attachment was fixed. A degradation that is stored as the answer stops
        being a degradation. UNKNOWN means "nobody could tell", so it is
        re-asked; `nothing_printed` and `printed` are answers and are kept.

        ⚠️ AND THE GUARD NOW SAYS THAT, instead of a third thing (found in review).
        `not extracted.get(KEY)` meant "absent or falsy", the comment
        beside it said "ONLY WHEN THE KEY IS ABSENT" and the test was called
        "never overwritten" — three statements of one guard, which is how the
        fourteen got started. It is expressed as the question it is asking:
        does the stored verdict hold one of the two real ANSWERS?
        """
        if not isinstance(extracted, dict):
            return
        stored = extracted.get(CURRENCY_MARKING_KEY)
        answer = stored.get("text_layer") if isinstance(stored, dict) else None
        # 🔴 THE GUARD IS WRITTEN AS WHAT IT MEANS, which is the third thing
        # review found: it used to read `not extracted.get(KEY)` under a
        # comment saying "only when the key is absent", with a test called
        # "never overwritten", and it actually meant "absent or falsy". Only
        # the two real ANSWERS are kept. Absent, `{}`, something that is not a
        # dict, and `unknown` are all non-answers and are all re-asked.
        if answer not in (pdf_currency.PRINTED, pdf_currency.NOTHING_PRINTED):
            extracted[CURRENCY_MARKING_KEY] = currency_marking_verdict(
                pdf_bytes, extracted
            )
        ClaudeInvoiceExtractor(
            client=None,
            currency_from_country=self._ai_currency_for_country,
            currency_is_active=self._ai_currency_is_active,
        )._resolve_currency_from_country(extracted)
