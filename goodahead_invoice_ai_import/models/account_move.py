# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import json
import logging
import re

from lxml import etree
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.addons.base.models.res_bank import sanitize_account_number
from odoo.exceptions import UserError, ValidationError
from odoo.tools.float_utils import float_compare

from ..lib.bank_numbers import (
    aba_checksum_fails,
    iban_checksum_fails,
    payee_account_number,
)
from ..lib import pdf_currency
from ..lib.claude_client import (
    CURRENCY_INFERRED_KEY,
    CURRENCY_MARKING_KEY,
    CURRENCY_SYMBOL_KEY,
    TOLERANCE_ABS,
    ClaudeInvoiceExtractor,
    currency_marking_verdict,
    locale_for_country,
    nothing_was_printed,
    one_country_on_both_sides,
)
from ..lib.ubl_generator import generate_peppol_invoice_xml

_logger = logging.getLogger(__name__)

# Re-exported for callers that import it from here.
__all__ = ["iban_checksum_fails"]

# Partner fields filled automatically when the vendor's own field is empty, as
# (partner field, extraction key). VAT, bank accounts and the name are
# deliberately absent: they are never written without a person.
_AI_VENDOR_AUTO_FIELDS = (
    ("street", "street"),
    ("street2", "street2"),
    ("city", "city"),
    ("zip", "postal_code"),
    # Country before state: the state is looked up within the vendor's
    # country, which this loop may have just written.
    ("country_id", "country_code"),
    ("state_id", "state_code"),
    ("phone", "phone"),
    ("email", "email"),
    ("website", "website"),
)


class AccountMove(models.Model):
    _inherit = "account.move"

    # Restricted to accounting users: the form reads these fields for anyone
    # opening any move, and the target models (which may hold vendor bank
    # details) are only readable by accounting. The count needs it too, since
    # its compute reads the suggestions.
    _AI_VENDOR_GROUPS = "account.group_account_invoice"

    ai_vendor_candidate_ids = fields.One2many(
        "goodahead.vendor.candidate",
        "move_id",
        string="Suggested vendors",
        groups=_AI_VENDOR_GROUPS,
    )
    ai_vendor_suggestion_ids = fields.One2many(
        "goodahead.vendor.data.suggestion",
        "move_id",
        string="Vendor detail suggestions",
        groups=_AI_VENDOR_GROUPS,
    )
    ai_vendor_suggestion_count = fields.Integer(
        compute="_compute_ai_vendor_suggestion_count",
        string="Details that differ",
        groups=_AI_VENDOR_GROUPS,
    )
    ai_vendor_review_deferred = fields.Boolean(
        string="Vendor review deferred",
        copy=False,
        help="Set by “Not now”. The suggestions stay pending in the Vendor "
        "Data Suggestions queue, and this bill stops asking about them.",
    )

    # The printed total and tax, compared live against the bill so a
    # discrepancy shows as a banner instead of being repaired with extra lines.
    # `ai_check_total` keeps bills imported without these figures quiet.
    ai_document_total = fields.Monetary(
        string="Invoice total (as printed)",
        currency_field="currency_id",
        copy=False,
        readonly=True,
        help="The payable total printed on the imported invoice.",
    )
    ai_document_tax = fields.Monetary(
        string="Invoice tax (as printed)",
        currency_field="currency_id",
        copy=False,
        readonly=True,
        help="The tax printed on the imported invoice.",
    )
    ai_check_total = fields.Boolean(copy=False, readonly=True)
    ai_total_mismatch = fields.Boolean(
        string="Total differs from the invoice",
        compute="_compute_ai_total_mismatch",
    )
    ai_tax_mismatch = fields.Boolean(
        string="Tax differs from the invoice",
        compute="_compute_ai_total_mismatch",
    )

    @api.depends(
        "ai_check_total", "ai_document_total", "ai_document_tax",
        "amount_total", "amount_tax",
    )
    def _compute_ai_total_mismatch(self):
        """Flag when the bill's total or tax differs from the printed figures.

        Uses the absolute tolerance only: these are the same figures restated,
        so a relative tolerance would let large differences through.
        """
        for move in self:
            if not move.ai_check_total:
                move.ai_total_mismatch = False
                move.ai_tax_mismatch = False
                continue
            # Compare at currency rounding so a difference of exactly the
            # tolerance does not trip on floating-point noise.
            rounding = move.currency_id.rounding or 0.01
            total_off = float_compare(
                abs(move.amount_total - move.ai_document_total),
                TOLERANCE_ABS,
                precision_rounding=rounding,
            ) > 0
            tax_off = float_compare(
                abs(move.amount_tax - move.ai_document_tax),
                TOLERANCE_ABS,
                precision_rounding=rounding,
            ) > 0
            move.ai_tax_mismatch = tax_off
            move.ai_total_mismatch = total_off or tax_off

    # Vendor history shown beside the vendor name. Zero posted bills is the
    # state that matters: either a new supplier or the matcher picked the
    # wrong contact.

    ai_from_import = fields.Boolean(
        string="Came from the AI import",
        compute="_compute_ai_vendor_bill_count",
        help="This bill was created by the AI PDF import, so its vendor was "
        "chosen by a matcher rather than typed by a person.",
    )
    ai_vendor_bill_count = fields.Integer(
        string="Previous bills from this vendor",
        compute="_compute_ai_vendor_bill_count",
        help="POSTED vendor bills and refunds this company has already had "
        "from this vendor, not counting this one.",
    )
    ai_vendor_draft_bill_count = fields.Integer(
        string="Draft bills from this vendor",
        compute="_compute_ai_vendor_bill_count",
        help="DRAFT vendor bills and refunds this company already has from "
        "this vendor, not counting this one. Reported beside the posted number "
        "and never added into it — a draft is not evidence that this vendor "
        "has been paid before.",
    )
    ai_can_create_vendor = fields.Boolean(
        string="Can create the vendor from the invoice",
        compute="_compute_ai_can_create_vendor",
        help="There is a readable extraction on this bill carrying a seller "
        "name, so a contact could be created from it.",
    )

    @api.depends("partner_id", "state")
    def _compute_ai_can_create_vendor(self):
        """Whether the stored extraction has a seller name to create a contact from."""
        for move in self:
            if move.partner_id or move.state != "draft" or not move._origin.id:
                move.ai_can_create_vendor = False
                continue
            seller = (move._origin._ai_stored_extraction() or {}).get("seller")
            move.ai_can_create_vendor = bool(
                isinstance(seller, dict) and (seller.get("name") or "").strip()
            )

    @api.depends("ai_vendor_suggestion_ids.state")
    def _compute_ai_vendor_suggestion_count(self):
        for move in self:
            move.ai_vendor_suggestion_count = len(
                move.ai_vendor_suggestion_ids.filtered(
                    lambda row: row.state == "pending"
                )
            )

    @api.depends("commercial_partner_id", "company_id")
    def _compute_ai_vendor_bill_count(self):
        """Compute import origin and posted/draft bill counts (not stored).

        Not stored because it would recompute on every bill state change. Uses
        ``_origin`` because a ``NewId`` cannot be used in a SQL domain.
        """
        saved = self._origin.filtered("id")
        imported_ids = set()
        if saved:
            # A linked `ai.invoice.import` row marks a bill as AI-imported.
            imported_ids = {
                move.id
                for move, in self.env["ai.invoice.import"]
                .sudo()
                ._read_group(
                    [("move_id", "in", saved.ids)], groupby=["move_id"]
                )
            }

        counted = self.filtered(
            lambda move: move._origin.id in imported_ids
            and move.move_type in ("in_invoice", "in_refund")
            and move.commercial_partner_id
        )

        # Counted per company: in multi-company databases a vendor may have
        # history in one company and none in the other.
        totals = {}
        for company in counted.company_id:
            group = counted.filtered(lambda move: move.company_id == company)
            for partner, states in self._ai_bill_counts(
                group.commercial_partner_id, company=company
            ).items():
                totals[(company, partner)] = states

        for move in self:
            move.ai_from_import = move._origin.id in imported_ids
            if move not in counted:
                move.ai_vendor_bill_count = 0
                move.ai_vendor_draft_bill_count = 0
                continue
            states = totals.get((move.company_id, move.commercial_partner_id), {})
            # Exclude this bill per state by subtraction; a domain would mean a
            # query per record, or bills of the same vendor excluding each other.
            own_state = move._origin.state
            move.ai_vendor_bill_count = max(
                states.get("posted", 0) - (1 if own_state == "posted" else 0), 0
            )
            move.ai_vendor_draft_bill_count = max(
                states.get("draft", 0) - (1 if own_state == "draft" else 0), 0
            )

    @api.model
    def _ai_posted_bill_counts(self, partners, company=None):
        """Return ``{partner: posted bill/refund count}`` for ``company``.

        Shared by the candidate cards and the form line so both always agree.
        Counts real bills rather than ``supplier_rank``, and includes the
        caller's own bill; callers subtract it when needed.
        """
        return {
            partner: states.get("posted", 0)
            for partner, states in self._ai_bill_counts(
                partners, company=company
            ).items()
        }

    @api.model
    def _ai_draft_bill_counts(self, partners, company=None):
        """Return ``{partner: draft bill/refund count}`` for ``company``.

        Reported beside the posted count, never added to it: a draft is not
        evidence the vendor was paid before.
        """
        return {
            partner: states.get("draft", 0)
            for partner, states in self._ai_bill_counts(
                partners, company=company
            ).items()
        }

    @api.model
    def _ai_bill_counts(self, partners, company=None):
        """Return ``{partner: {"posted": n, "draft": n}}`` in one query.

        The single definition of "a bill from this vendor" behind both count
        helpers. Other states are absent from the inner dict.
        """
        if not partners:
            return {}
        company = company or self.env.company
        domain = [
            ("move_type", "in", ("in_invoice", "in_refund")),
            ("state", "in", ("posted", "draft")),
            ("company_id", "=", company.id),
            ("commercial_partner_id", "in", partners.ids),
        ]
        counts = {}
        for partner, state, count in self.env["account.move"]._read_group(
            domain,
            groupby=["commercial_partner_id", "state"],
            aggregates=["__count"],
        ):
            counts.setdefault(partner, {})[state] = count
        return {partner: counts.get(partner, {}) for partner in partners}

    def action_ai_review_vendor_suggestions(self):
        """Open this bill's pending suggestions in the review dialog."""
        self.ensure_one()
        return self.env["goodahead.vendor.data.review"]._open_for(
            self.ai_vendor_suggestion_ids, move=self
        )

    def action_ai_defer_vendor_suggestions(self):
        """Defer the review; the rows stay pending for the vendor-data queue."""
        self.ai_vendor_review_deferred = True
        return True

    @api.model
    def _ai_as_number(self, value):
        """Return a figure from the stored JSON as a float, or None.

        Mirrors the UBL generator's coercion so hand-edited string amounts
        still count; booleans are refused.
        """
        if value is None or isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @api.model
    def _ai_printed_tax(self, extracted):
        """Return the printed tax as a signed magnitude, or None if none is stated.

        Uses the tax subtotals when present, else the line taxes. The sign
        follows ``payable`` because credit notes arrive in either convention.
        """
        subtotals = [s for s in extracted.get("tax_subtotals") or [] if isinstance(s, dict)]
        if subtotals:
            amounts = [s.get("tax_amount") for s in subtotals]
        else:
            amounts = [
                line.get("tax_amount")
                for line in extracted.get("lines") or []
                if isinstance(line, dict)
            ]
        amounts = [self._ai_as_number(a) for a in amounts]
        amounts = [a for a in amounts if a is not None]
        if not amounts:
            return None
        payable = self._ai_as_number((extracted.get("totals") or {}).get("payable"))
        sign = -1.0 if payable is not None and payable < 0 else 1.0
        return sign * sum(amounts)

    def _ai_record_printed_totals(self, invoice, extracted):
        """Store the printed total and tax for the mismatch banner.

        No line is added to the bill. Nothing is stored when the payable is
        missing or not a number.
        """
        if invoice.state != "draft":
            return
        payable = self._ai_as_number((extracted.get("totals") or {}).get("payable"))
        if payable is None:
            return
        printed_tax = self._ai_printed_tax(extracted)
        invoice.write(
            {
                "ai_document_total": abs(payable),
                "ai_document_tax": printed_tax or 0.0,
                "ai_check_total": True,
            }
        )

    @api.model
    def _ai_extract_and_generate_ubl(self, pdf_bytes, company):
        """Extract invoice data from PDF via Claude and generate Peppol XML.

        Returns:
            dict with "extracted", "usage", "model_name", "xml_bytes"

        Raises:
            ClaudeExtractionError, UBLGenerationError
        """
        categories = self.env["goodahead.expense.category"]._for_company(company)
        cat_list = [{"code": c.code, "name": c.name} for c in categories]
        if not cat_list:
            # Not fatal, but every line will land on the journal default.
            _logger.warning(
                "AI invoice import: company %s has no expense categories, so "
                "extracted lines cannot be mapped to accounts.",
                company.display_name,
            )

        # An empty override lets `goodahead.claude.client` resolve the model
        # (company default, then the newest synced model).
        client = self.env["goodahead.claude.client"]
        requested_model = company.claude_invoice_model_id.model_api_id or None

        extractor = ClaudeInvoiceExtractor(
            client=client,
            model=requested_model,
            company=company,
            # Server-side enforcement of the prompt's same-country currency
            # rule, injected so the lib stays ORM-free.
            currency_from_country=self._ai_currency_for_country,
            # Separate question: whether the database trades in it.
            currency_is_active=self._ai_currency_is_active,
            # Locale block chosen from the company's country, not the vendor's.
            locale=self._ai_prompt_locale(company),
        )
        result = extractor.extract_invoice(pdf_bytes, cat_list)

        extracted = result["data"]
        usage = result["usage"]
        xml_bytes = self._ai_generate_ubl_from_extracted(extracted, company)

        # Record the model actually used, including a resolved default.
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
        """Return the prompt locale block for this company's bills.

        A company prompt profile wins. Otherwise the company's country picks
        the block; no country or an unsupported one gets the country-neutral
        block (never the US one) and a warning, since nothing on the bill would
        show the weaker prompt.
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
    def _ai_currency_for_country(self, country_code):
        """Return the ISO code of a country's own currency, or None.

        Answers for archived currencies too; whether the database trades in it
        is asked separately by ``_ai_currency_is_active``.
        """
        code = str(country_code or "").strip().upper()
        if len(code) != 2:
            return None
        country = self.env["res.country"].search([("code", "=", code)], limit=1)
        currency = country.currency_id
        if not currency:
            return None
        return currency.name

    @api.model
    def _ai_reask_the_pdf(self, extracted, pdf_bytes):
        """Re-decide the currency question from the PDF bytes on a re-import.

        Mutates ``extracted``. Recomputes the currency-marking verdict unless a
        real answer (printed / nothing printed) is stored, since a stored
        verdict may be a hand-edit while ``unknown`` is not an answer. Then
        re-runs the country currency resolver. It must run before the UBL is
        generated, because the resolver can write a currency.
        """
        if not isinstance(extracted, dict):
            return
        stored = extracted.get(CURRENCY_MARKING_KEY)
        answer = stored.get("text_layer") if isinstance(stored, dict) else None
        # Only the two real answers are kept; absent, malformed or `unknown`
        # verdicts are re-asked.
        if answer not in (pdf_currency.PRINTED, pdf_currency.NOTHING_PRINTED):
            extracted[CURRENCY_MARKING_KEY] = currency_marking_verdict(
                pdf_bytes, extracted
            )
        ClaudeInvoiceExtractor(
            client=None,
            currency_from_country=self._ai_currency_for_country,
            currency_is_active=self._ai_currency_is_active,
        )._resolve_currency_from_country(extracted)

    @api.model
    def _ai_surviving_currency_marker(self, extracted):
        """Return the currency-inferred marker if it still holds, else drop it.

        On a re-import the stored JSON may have been hand-edited, so the three
        claims behind the marker are re-checked: nothing was printed, both
        parties are in the marker's country, and that country's currency is
        the extraction's currency. A marker that fails is removed from
        ``extracted`` so it does not persist into the stored extraction.
        Archived currencies are not checked, since core imports them anyway.
        """
        marker = extracted.get(CURRENCY_INFERRED_KEY)
        if not marker:
            return None

        marker = str(marker).strip().upper()
        currency = str(extracted.get("currency") or "").strip().upper()
        holds = (
            nothing_was_printed(extracted)
            and one_country_on_both_sides(extracted) == marker
            and currency
            and self._ai_currency_for_country(marker) == currency
        )
        if holds:
            return marker

        _logger.info(
            "AI invoice import: the stored extraction is marked as inferred "
            "from %s but that no longer holds (currency %r, text-layer verdict "
            "%r, model currency_symbol %r) — the marker is dropped and no note "
            "is posted",
            marker,
            extracted.get("currency"),
            (extracted.get(CURRENCY_MARKING_KEY) or {}).get("text_layer")
            if isinstance(extracted.get(CURRENCY_MARKING_KEY), dict)
            else None,
            extracted.get(CURRENCY_SYMBOL_KEY),
        )
        extracted.pop(CURRENCY_INFERRED_KEY, None)
        return None

    @api.model
    def _ai_currency_is_active(self, currency_name):
        """Whether the database trades in that currency (active ``res.currency``).

        Core imports archived currencies too (it searches with
        ``active_test=False``), so declining one is our own rule: an archived
        currency has no maintained rate. Only the currency-writing path asks.
        """
        name = str(currency_name or "").strip()
        if not name:
            return False
        currency = (
            self.env["res.currency"]
            .with_context(active_test=False)
            .search([("name", "=", name)], limit=1)
        )
        return bool(currency) and currency.active

    @api.model
    def _ai_generate_ubl_from_extracted(self, extracted, company):
        """Generate Peppol UBL XML from an extracted invoice dict (no API call)."""
        self._ai_drop_invalid_bank_numbers(extracted)
        # Falls back to the company's currency, never a hardcoded one.
        return generate_peppol_invoice_xml(
            extracted, default_currency=company.currency_id.name
        )

    @api.model
    def _ai_drop_invalid_bank_numbers(self, extracted):
        """Move bank numbers that fail their checksum out of the seller data.

        A rejected value is kept under ``seller["<field>_rejected"]`` and
        reported on the bill. Mutates ``extracted``. US account numbers have no
        checksum, so only the routing number can be verified for US vendors.
        """
        seller = extracted.get("seller")
        if not isinstance(seller, dict):
            return

        # Run every check on every field: the model may put an IBAN in the
        # wrong field. Both checks are shape-gated, so they ignore values of the
        # other kind.
        for field in ("iban", "account_number", "routing_number"):
            value = seller.get(field)
            if not value:
                continue
            if iban_checksum_fails(value) or aba_checksum_fails(value):
                seller[f"{field}_rejected"] = value
                seller[field] = None

        # A US account without a valid routing number cannot be paid; drop both.
        if seller.get("routing_number_rejected") and seller.get("account_number"):
            seller["account_number_rejected"] = seller["account_number"]
            seller["account_number"] = None

    @api.model
    def _ai_decode_ubl_into_move(self, move, xml_bytes, extracted, attachment=None,
                                 new=True):
        """Run Odoo's native UBL decoder on ``move``; return an error or None.

        Avoids ``journal._create_document_from_attachment``, whose internal
        commit fails the access check in a queue job. The ``ai_seller_*``
        context must be set before ``_get_edi_decoder`` so every downstream
        partner lookup inherits it.
        """
        # On an unrecognised core the hooks would be silent no-ops and core
        # would auto-create the vendor, so refuse. See models/core_profile.py.
        self._ai_assert_core_profile()

        try:
            xml_tree = etree.fromstring(xml_bytes)
        except etree.XMLSyntaxError as e:
            return f"Generated UBL XML is malformed: {e}"

        ubl_file_data = {
            "name": "invoice.xml",
            "raw": xml_bytes,
            "xml_tree": xml_tree,
            "type": "xml",
            "filename": "invoice.xml",
            "mimetype": "application/xml",
            # The decoder accesses these by key (not .get) — always provide them.
            "attachment": attachment or self.env["ir.attachment"],
        }

        seller = extracted.get("seller")
        seller = seller if isinstance(seller, dict) else {}
        self_ctx = self.with_context(
            ai_seller_country_code=(seller.get("country_code") or "").strip() or None,
            ai_seller_business_id=(seller.get("business_id") or "").strip() or None,
            # The website has no UBL element core decodes, so it is relayed
            # here for the partner lookup.
            ai_seller_website=(seller.get("website") or None)
            if isinstance(seller.get("website"), str)
            else None,
            # The whole seller dict, so the vendor matcher works on either
            # import pipeline.
            ai_seller=seller,
            # Marks the decode as ours. `account.edi.common` is a shared
            # abstract model, and vendor creation is suppressed only for an AI
            # extraction — not for a genuine Peppol document.
            ai_invoice_import=True,
        )
        # Odoo 18 contract: the decoder is returned as a callable, and it
        # returns True when it imported the document.
        decoder = super(AccountMove, self_ctx)._get_edi_decoder(ubl_file_data, new)
        if not decoder:
            return "No UBL decoder found for the generated XML"
        if not decoder(move, ubl_file_data, new):
            return "Odoo's UBL importer could not read the generated XML"
        return None

    @api.model
    def _ai_post_process(self, invoice, extracted, usage, model_name, company):
        """Post-process an AI-imported invoice: partner, categories, chatter.

        Called after the native UBL decoder has populated the invoice.
        """
        # Match an existing partner; never create one.
        if not invoice.partner_id:
            self._create_partner_from_extraction(invoice, extracted)

        # Fill empty vendor fields and queue contradictions as suggestions;
        # nothing is overwritten.
        if invoice.partner_id:
            invoice._ai_apply_vendor_data(invoice.commercial_partner_id, extracted)

        self._apply_expense_categories(invoice, extracted, company)

        # Whatever the decoder made of the document's bank details, the account
        # on the bill has to be the payee's.
        self._ai_enforce_document_bank_side(invoice, extracted)

        # A US vendor's account also needs the routing number the decoder drops.
        self._ai_record_routing_number(invoice, extracted)

        seller = extracted.get("seller") or {}
        for field, label in (
            ("iban", "IBAN"),
            ("routing_number", "routing number"),
            ("account_number", "account number"),
        ):
            rejected = seller.get(f"{field}_rejected")
            if not rejected:
                continue
            if field == "account_number":
                # Rejected together with the routing number; it has no
                # checksum of its own.
                body = Markup(
                    "<p>The account number read from the document (%s) was not "
                    "used either: it came with a routing number that failed its "
                    "checksum, and a US account needs both.</p>"
                ) % rejected
            else:
                body = Markup(
                    "<p>The %s read from the document (%s) fails its checksum, "
                    "so it was not used. Check it against the PDF and add the "
                    "vendor's account by hand if needed.</p>"
                ) % (label, rejected)
            invoice.message_post(body=body)

        # Note on the bill when the currency was deduced rather than printed.
        # The marker is re-checked first, because a re-import uses the stored
        # (possibly hand-edited) JSON without re-validating it.
        inferred_from = self._ai_surviving_currency_marker(extracted)
        if inferred_from:
            country = self.env["res.country"].search(
                [("code", "=", str(inferred_from).strip().upper())], limit=1
            )
            # Say which instrument decided that nothing was printed.
            verdict = extracted.get(CURRENCY_MARKING_KEY)
            read_the_bytes = (
                isinstance(verdict, dict)
                and verdict.get("decided_by") == "text_layer"
            )
            source = (
                _(
                    "That is this document's own text layer, annotations and "
                    "form fields included."
                )
                if read_the_bytes
                else _(
                    "This document has no readable text layer, so that is the "
                    "extraction's own reading rather than a check of the bytes."
                )
            )
            invoice.message_post(
                body=Markup("<p>%s</p>")
                % _(
                    "No currency symbol or ISO code was found anywhere on this "
                    "document, so the currency was not read off the page — it "
                    "was deduced as %(currency)s. The vendor's address and ours "
                    "are both in %(country)s, whose currency is %(currency)s. "
                    "%(evidence)s Check it against the PDF before posting.",
                    currency=extracted.get("currency") or "",
                    country=country.display_name or inferred_from,
                    # Not `source`: that is the name of `_()`'s own first
                    # parameter in 18.0.
                    evidence=source,
                )
            )

        # Store the printed total and tax for the mismatch banner.
        self._ai_record_printed_totals(invoice, extracted)

        # Save extraction JSON as attachment
        extraction_json = json.dumps(extracted, indent=2, ensure_ascii=False)
        json_attachment = self.env["ir.attachment"].create(
            {
                "name": "extraction.json",
                "raw": extraction_json.encode("utf-8"),
                "mimetype": "application/json",
                "res_model": "account.move",
                "res_id": invoice.id,
            }
        )

        # The remit-to address is posted, not written to the partner: it is
        # where payment goes, not where the vendor is.
        remit_to = (seller.get("remit_to_address") or "").strip()
        if remit_to:
            invoice.message_post(
                body=Markup("<p>%s</p>")
                % _(
                    "The vendor asks for payment to be remitted to: %(address)s",
                    address=remit_to,
                )
            )

        # Post chatter message with token usage. `no_new_invoice`: on 18.0 a
        # message with attachments makes core try to decode them into the bill
        # and post "already contains lines" when it cannot.
        invoice.with_context(no_new_invoice=True).message_post(
            body=Markup(
                "<p>Imported via AI invoice extraction</p>"
                "<ul>"
                "<li>Model: %s</li>"
                "<li>Input tokens: %s</li>"
                "<li>Output tokens: %s</li>"
                "</ul>"
            ) % (model_name, usage["input_tokens"], usage["output_tokens"]),
            attachment_ids=[json_attachment.id],
        )

        # Deliberately no approval step is completed here: an AI-imported bill
        # enters the review queue like any other, because review is the check
        # that catches extraction errors.

    @api.model
    def _ai_routing_field(self):
        """The bank-account field that holds a US routing number, or None.

        Odoo 18.0 has no such field in ``base``; ``l10n_us`` adds
        ``aba_routing``.
        """
        fields = self.env["res.partner.bank"]._fields
        return "aba_routing" if "aba_routing" in fields else None

    @api.model
    def _ai_record_routing_number(self, invoice, extracted):
        """Store the US routing number on the bank account so it is payable.

        Core's UBL import reads the account number but not
        ``FinancialInstitutionBranch/ID``, so it is written to the routing
        field here (``aba_routing``, from ``l10n_us``). An existing routing
        number is never overwritten. Without ``l10n_us`` there is nowhere to
        store it, so it is stated on the bill instead of being dropped.
        """
        seller = (extracted or {}).get("seller") or {}
        routing = (seller.get("routing_number") or "").strip()
        if not routing:
            return

        account_number = sanitize_account_number(payee_account_number(seller))
        if not account_number:
            return

        field = self._ai_routing_field()
        # `l10n_us` accepts 1 to 9 digits and nothing else. The value is checked
        # here rather than by catching the constraint: in 18.0 a write that fails
        # validation has already put the value in the cache, and it would be
        # flushed at commit anyway.
        digits = re.sub(r"\D", "", routing)
        storable = bool(re.fullmatch(r"\d{1,9}", digits))
        if not field or not storable:
            reason = (
                _("no routing number field exists (install US accounting, l10n_us)")
                if not field
                else _("it is not a number of at most nine digits")
            )
            self._ai_post_once(
                invoice,
                _(
                    "The routing number read from the document is %(routing)s. "
                    "It is not saved on the bank account because %(reason)s.",
                    routing=routing,
                    reason=reason,
                ),
            )
            return

        # The account on the bill, plus any account of the vendor carrying this
        # number — the decoder may have attached one without the bill stating
        # it (a received credit note does exactly that).
        candidates = invoice.partner_bank_id
        vendor = invoice.commercial_partner_id
        if vendor:
            candidates |= self.env["res.partner.bank"].search(
                [
                    ("sanitized_acc_number", "=", account_number),
                    ("partner_id", "child_of", vendor.id),
                ]
            )

        for bank in candidates:
            if bank.sanitized_acc_number != account_number or bank[field]:
                continue
            bank[field] = digits

    @api.model
    def _ai_post_once(self, invoice, text):
        """Post ``text`` on ``invoice`` unless an identical note is already there.

        For notes a re-import or an applied suggestion would otherwise repeat.
        """
        body = Markup("<p>%s</p>") % text
        if any(str(body) == str(message.body) for message in invoice.message_ids):
            return
        invoice.message_post(body=body)

    def _ai_enforce_document_bank_side(self, invoice, extracted=None):
        """Keep ``partner_bank_id`` on the payee's side of the document.

        The payee is ``bank_partner_id``: the vendor on a bill, the company on
        a received credit note. A wrong account is replaced and the change is
        posted to the chatter. When we are the payee, an account carrying a
        number the other party also holds is rejected too, since an earlier
        import may have copied a vendor's number onto the company partner.
        """
        bank = invoice.partner_bank_id
        if not bank:
            return

        expected = invoice.bank_partner_id
        we_are_the_payee = expected == invoice.company_id.partner_id
        counterparty = (
            invoice.commercial_partner_id
            if we_are_the_payee
            else invoice.company_id.partner_id
        )
        # Archived ones count: a vendor account someone archived off our partner
        # is still that vendor's account, and the number is what identifies it.
        theirs = set(
            counterparty.with_context(active_test=False).bank_ids.mapped(
                "sanitized_acc_number"
            )
        )
        if we_are_the_payee:
            # On a document we received, the printed account is the sender's.
            seller = (extracted or {}).get("seller") or {}
            theirs.add(sanitize_account_number(payee_account_number(seller)))
        theirs.discard(False)

        # The number test applies only when we are the payee. On a bill we pay
        # the printed number is the vendor's by definition, and a stray copy on
        # the company partner says nothing about the vendor's own row.
        number_is_suspect = we_are_the_payee and bank.sanitized_acc_number in theirs

        if (
            bank.partner_id.commercial_partner_id == expected.commercial_partner_id
            and not number_is_suspect
        ):
            return

        replacement = (
            expected.bank_ids.filtered_domain(
                [
                    *self.env["res.partner.bank"]._check_company_domain(
                        invoice.company_id
                    ),
                    ("active", "=", True),
                ]
            )
            # Same asymmetry as above.
            .filtered(
                lambda b: not we_are_the_payee
                or b.sanitized_acc_number not in theirs
            )
            # The order core's own compute would use: matching currency first,
            # then an account we are allowed to pay out of.
            .sorted(
                lambda b: (
                    bool(b.currency_id) and b.currency_id != invoice.currency_id,
                    not b.allow_out_payment,
                )
            )[:1]
        )

        # Never log the account number itself.
        _logger.warning(
            "AI import: %s carried bank account id %s of %s; expected an "
            "account of %s that is not also the other party's — replaced with %s",
            invoice.display_name,
            bank.id,
            bank.partner_id.display_name,
            expected.display_name,
            replacement.id or "nothing",
        )
        invoice.partner_bank_id = replacement
        # Report which of the two cases happened.
        if bank.partner_id.commercial_partner_id != expected.commercial_partner_id:
            reason = Markup(
                "<p>The bank account on this document belonged to %s, not to "
                "the party being paid (%s) — removed and replaced with %s.</p>"
            ) % (
                bank.partner_id.display_name,
                expected.display_name,
                replacement.display_name or "no account",
            )
        else:
            reason = Markup(
                "<p>The bank account on this document is recorded against %s, "
                "the party being paid, but the same number is also held by the "
                "other party — so it cannot be told apart from an account "
                "copied onto the wrong partner by an earlier import. Replaced "
                "with %s; check it against the document.</p>"
            ) % (
                expected.display_name,
                replacement.display_name or "no account",
            )
        invoice.message_post(body=reason)

    @api.model
    def _ai_expense_category_for(self, code, company):
        """Return the expense category a code resolves to for ``company``.

        A company-specific category wins over a shared one. Two searches,
        because ``order="company_id desc"`` sorts NULLs first in PostgreSQL.
        """
        Category = self.env["goodahead.expense.category"]
        return Category.search(
            [("code", "=", code), ("company_id", "=", company.id)], limit=1
        ) or Category.search(
            [("code", "=", code), ("company_id", "=", False)], limit=1
        )

    @api.model
    def _apply_expense_categories(self, invoice, extracted_data, company):
        """Apply expense category accounts to invoice lines without products."""
        lines_data = extracted_data.get("lines", [])
        invoice_lines = invoice.invoice_line_ids.filtered(
            lambda l: l.display_type not in ("line_section", "line_note")
        )

        warnings = []
        for idx, (line_data, inv_line) in enumerate(zip(lines_data, invoice_lines)):
            if inv_line.product_id:
                continue

            cat_code = line_data.get("expense_category")
            if not cat_code:
                continue

            category = self._ai_expense_category_for(cat_code, company)

            # `account_id` is company-dependent and a queue job's env company
            # may differ from the bill's.
            account = category.with_company(company).account_id if category else None

            if not account:
                line_name = line_data.get("name", f"Line {idx + 1}")
                # Distinguish a missing category from one with no account in
                # this company.
                if not category:
                    reason = (
                        f"no category with code '{cat_code}' is available to "
                        f"{company.display_name}"
                    )
                else:
                    reason = (
                        f"category '{cat_code}' has no account set for "
                        f"{company.display_name} (it may be set for another "
                        f"company — the account is per company)"
                    )
                warnings.append(
                    f"Line {idx + 1} '{line_name}': {reason} — using journal default"
                )
                continue

            inv_line.account_id = account

        if warnings:
            invoice.message_post(
                body=Markup(
                    "<p><strong>Expense category warnings:</strong></p><ul>%s</ul>"
                ) % Markup("").join(Markup("<li>%s</li>") % w for w in warnings),
            )

    @api.model
    def _ai_match_existing_vendor(self, seller, company=None):
        """Find an existing vendor for the extracted seller.

        Delegates to ``res.partner._ai_find_vendor``, which serves both import
        pipelines. Pass the bill's ``company``: a queue job's env company may
        differ from it.
        """
        return self.env["res.partner"]._ai_find_vendor(seller, company=company)

    def action_ai_create_vendor_from_extraction(self):
        """Create the vendor from the stored extraction, on explicit request.

        Not automatic creation: a person presses the button. It first looks
        for an existing vendor via ``res.partner._ai_find_vendor`` to avoid
        duplicates, refuses when the seller resolves only to a party that can
        never be a vendor (such as the company's own partner), records who
        created it, and refuses on a bill that already has a vendor.
        """
        self.ensure_one()
        if self.partner_id:
            raise UserError(
                _(
                    "This bill already has a vendor (%s). Change it on the "
                    "field rather than creating another contact.",
                    self.partner_id.display_name,
                )
            )
        if self.state != "draft":
            raise UserError(_("Only a draft bill's vendor can be set this way."))

        extracted = self._ai_stored_extraction()
        seller = (extracted or {}).get("seller")
        seller = seller if isinstance(seller, dict) else {}
        name = (seller.get("name") or "").strip()
        if not name:
            raise UserError(
                _(
                    "Nothing was read off this document that could name a "
                    "vendor, so there is nothing to create a contact from."
                )
            )

        Partner = self.env["res.partner"]
        company = self.company_id or self.env.company

        # Reuse an existing vendor first, to avoid duplicates.
        existing = Partner._ai_find_vendor(seller, company=company)
        if existing:
            self.partner_id = existing
            # Posted here: `_ai_apply_vendor_data` skips its note when there
            # is nothing to fill.
            self.message_post(
                body=Markup("<p>%s</p>")
                % _(
                    "%(user)s asked for a contact to be created from this "
                    "bill; %(partner)s already existed, so the bill was filed "
                    "against it instead of creating a second one.",
                    user=self.env.user.display_name,
                    partner=existing.display_name,
                )
            )
            self._ai_apply_vendor_data(existing.commercial_partner_id, extracted)
            self.ai_vendor_candidate_ids.unlink()
            return True

        # Refuse a seller that resolves only to a party that cannot be a vendor.
        refused, reason = Partner._ai_vendor_refusal_for_seller(
            seller, company=company
        )
        if reason:
            raise UserError(Partner._ai_refused_vendor_message(refused, reason))

        vendor, refused_vat = self._ai_create_vendor(
            self._ai_vendor_vals_from_seller(seller)
        )
        self.partner_id = vendor

        # Posted here: a contact created from this document leaves nothing for
        # `_ai_apply_vendor_data` to fill, so its note would be skipped.
        body = Markup("<p><strong>%s</strong></p><p>%s</p>") % (
            _(
                "%(user)s created this contact from bill %(bill)s, using "
                "what was read off the document.",
                user=self.env.user.display_name,
                bill=self.display_name,
            ),
            _(
                "Nothing was invented: the name, address and tax id come "
                "from this invoice, and no bank account was written. Check "
                "them against the document before posting.",
            ),
        )
        if refused_vat:
            body += Markup("<p>%s</p>") % _(
                "The tax id %(vat)s read off the document was not stored: "
                "Odoo's VAT check does not accept it for this contact's "
                "country. It is waiting in Vendor Data Suggestions; compare "
                "it with the document before applying it.",
                vat=refused_vat,
            )
        self.message_post(body=body)

        # Still run for the identifiers, so the next bill matches by itself.
        self._ai_apply_vendor_data(vendor, extracted)
        self.ai_vendor_candidate_ids.unlink()
        return True

    def _ai_create_vendor(self, vals):
        """Create the contact; drop a tax id that Odoo's VAT check refuses.

        With ``base_vat`` installed, a number that is not a VAT number in
        Odoo's eyes (a Canadian business number, a US EIN, a misread digit)
        makes ``create`` raise, and the person pressing the button gets a
        validation error instead of a contact. The rest of the document is
        still good, so the contact is created without the tax id, which stays
        on the bill as a suggestion. Returns ``(partner, refused_vat)``.
        """
        Partner = self.env["res.partner"]
        vat = vals.get("vat")
        if not vat:
            return Partner.create(vals), False
        try:
            with self.env.cr.savepoint():
                return Partner.create(vals), False
        except ValidationError:
            _logger.info(
                "AI invoice import: tax id %r refused by the VAT check; "
                "creating the contact without it.",
                vat,
            )
        vals = {key: value for key, value in vals.items() if key != "vat"}
        return Partner.create(vals), vat

    def _ai_vendor_vals_from_seller(self, seller):
        """Return ``res.partner`` values for a contact created from ``seller``.

        The tax id is included because a new contact has no previous value to
        protect; the bank account is never written. ``supplier_rank`` is left
        for core to increment on confirmation.
        """
        country = self._ai_resolve_location(
            "country_id", (seller.get("country_code") or "").strip() or None
        )
        state = self._ai_resolve_location(
            "state_id", (seller.get("state_code") or "").strip() or None,
            country=country,
        )
        vals = {
            "name": (seller.get("name") or "").strip(),
            # A seller block is a company, whatever older rows' flags say.
            "is_company": True,
            "street": (seller.get("street") or "").strip() or False,
            "city": (seller.get("city") or "").strip() or False,
            "zip": (seller.get("postal_code") or "").strip() or False,
            "country_id": country.id if country else False,
            "state_id": state.id if state else False,
            "phone": (seller.get("phone") or "").strip() or False,
            "email": (seller.get("email") or "").strip() or False,
            "website": (seller.get("website") or "").strip() or False,
        }
        vat = "".join((seller.get("vat") or "").split()).upper()
        if vat:
            vals["vat"] = vat
        business_id = (seller.get("business_id") or "").strip()
        if business_id:
            # Lets the next bill match by registration number.
            vals["company_registry"] = business_id
        return {key: value for key, value in vals.items() if value is not False}

    def _create_partner_from_extraction(self, invoice, extracted_data):
        """Match an existing vendor for the extracted seller; never create one.

        A bill without a vendor is preferred over a duplicate vendor: an
        unfiled bill is visible, a wrongly filed one is not. When nothing
        matches, a chatter note lists what was read and the closest existing
        vendors. Creation is only possible on request, through
        ``action_ai_create_vendor_from_extraction``.
        """
        seller = extracted_data.get("seller", {}) or {}
        name = (seller.get("name") or "").strip()
        if not name:
            return

        # Pass the bill's company: a queue job's env company may differ.
        existing = self._ai_match_existing_vendor(
            seller, company=invoice.company_id
        )
        if existing:
            invoice.partner_id = existing
            return

        self._ai_post_unrecognised_vendor(invoice, seller)

    @api.model
    def _ai_post_unrecognised_vendor(self, invoice, seller):
        """Post the unrecognised-vendor note and store the candidate rows."""
        read_off = [
            (_("Name"), seller.get("name")),
            (_("Phone"), seller.get("phone")),
            (_("Email"), seller.get("email")),
            (_("Website"), seller.get("website")),
            (_("Street"), seller.get("street")),
            (_("City"), seller.get("city")),
            (_("State"), seller.get("state_code")),
            (_("ZIP"), seller.get("postal_code")),
            (_("Country"), seller.get("country_code")),
            (_("Tax ID"), seller.get("vat")),
            (_("Business id"), seller.get("business_id")),
        ]
        # `Markup(...) % value` escapes the values, which come from the PDF.
        rows = Markup("").join(
            Markup("<li><strong>%s:</strong> %s</li>") % (label, value)
            for label, value in read_off
            if isinstance(value, str) and value.strip()
        )
        body = Markup(
            "<p><strong>%s</strong></p><p>%s</p><p>%s</p><ul>%s</ul>"
        ) % (
            _("This vendor was not recognised, so the bill has no vendor yet."),
            _(
                "No new contact was created for it on purpose: an invented "
                "vendor carries no history, no payment terms and no bank "
                "details, and splits the aged payable across two records. "
                "Pick the right vendor below, or create one deliberately."
            ),
            _("Read off the invoice:"),
            rows,
        )

        # The bill's company, not the uploader's.
        candidates = self._ai_vendor_candidates(seller, company=invoice.company_id)
        self.env["goodahead.vendor.candidate"]._build_for_move(invoice, candidates)
        if candidates:
            lines = Markup("").join(
                Markup("<li><strong>%s</strong>%s — %s, %s</li>")
                % (
                    candidate["partner"].display_name,
                    # `self.env._`, not `_()`: in a generator expression 18.0's
                    # `_()` cannot find the language from the frame.
                    self.env._(" (archived)") if candidate["archived"] else "",
                    candidate["reason"],
                    # No plural forms in `_()`, so two translatable strings.
                    self.env._("1 posted bill")
                    if candidate["bill_count"] == 1
                    else self.env._("%s posted bills", candidate["bill_count"]),
                )
                for candidate in candidates
            )
            body += Markup("<p>%s</p><ul>%s</ul>") % (
                _("Closest existing vendors:"),
                lines,
            )

        invoice.message_post(body=body)

    @api.model
    def _ai_vendor_candidates(self, seller, limit=3, company=None):
        """Return the best existing partners to offer for an unrecognised vendor.

        A list of ``{"partner", "reason", "bill_count", "draft_bill_count",
        "archived"}`` ranked by ``supplier_rank desc, id``. Signals: a shared
        phone or non-public domain, then a loose name match on the full name
        and on its longest word. Archived partners are included and marked.
        ``company`` is the bill's company and defaults to ``env.company``.
        """
        seller = seller or {}
        Partner = self.env["res.partner"].with_context(active_test=False)
        Identifier = self.env["goodahead.partner.identifier"]

        reasons = {}

        def remember(partners, reason):
            for partner in partners:
                reasons.setdefault(partner.id, reason)

        phone = Identifier._normalise_phone(seller.get("phone"))
        remember(Identifier._partners_for("phone", [phone]), _("same phone"))

        domains = [
            Identifier._normalise_domain(seller.get("email")),
            Identifier._normalise_domain(seller.get("website")),
        ]
        remember(Identifier._partners_for("domain", domains), _("same domain"))

        name = str(seller.get("name") or "").strip()
        if name:
            # A generous cap; the ranking below picks the offered ones.
            # `parent_id = False` rather than `is_company`, which many vendor
            # records leave unset.
            remember(
                Partner.search(
                    [("name", "ilike", name), ("parent_id", "=", False)], limit=20
                ),
                _("similar name"),
            )
            # Names matching only a child contact are offered through their
            # commercial partner, so the bill is filed against the company and
            # bill counts and ranking apply to it.
            remember(
                Partner.search(
                    [("name", "ilike", name), ("parent_id", "!=", False)], limit=20
                ).commercial_partner_id,
                _("similar name on a contact"),
            )
            words = sorted(
                (w for w in re.split(r"[^0-9A-Za-z]+", name) if len(w) >= 3),
                key=len,
                reverse=True,
            )
            if words:
                remember(
                    Partner.search(
                        [("name", "ilike", words[0]), ("parent_id", "=", False)],
                        limit=20,
                    ),
                    _("name contains “%s”", words[0]),
                )

        if not reasons:
            return []

        # The name searches bypass `_partners_for`, so drop the company's own
        # partners, our users and contacts owned by another company here.
        # Another company's own partner is still offered (intercompany bills).
        offered = self.env["res.partner"]._ai_drop_impossible_vendors(
            Partner.browse(list(reasons)), company=company or self.env.company
        )
        ranked = offered.sorted(
            key=lambda partner: (-partner.supplier_rank, partner.id)
        )[:limit]
        if not ranked:
            return []

        # Same helper as the form line, so the numbers always agree.
        states = self._ai_bill_counts(ranked, company=company)
        return [
            {
                "partner": partner,
                "reason": reasons[partner.id],
                "bill_count": states.get(partner, {}).get("posted", 0),
                "draft_bill_count": states.get(partner, {}).get("draft", 0),
                "archived": not partner.active,
            }
            for partner in ranked
        ]

    @api.model
    def _ai_resolve_location(self, field_name, code, country=None):
        """Return the ``res.country`` / ``res.country.state`` for a printed code.

        Looked up, never created: an invented state code is sent to carriers,
        which reject it. A state needs ``country``, because state codes are
        only unique within a country.
        """
        code = (code or "").strip()
        if not code:
            return None
        if field_name == "country_id":
            return self.env["res.country"].search(
                [("code", "=ilike", code)], limit=1
            ) or None
        if not country:
            return None
        return self.env["res.country.state"].search(
            [("code", "=ilike", code), ("country_id", "=", country.id)], limit=1
        ) or None

    def _ai_stored_extraction(self):
        """Return this bill's stored extraction JSON, or ``{}``.

        Prefers the (editable) import record and falls back to the
        ``extraction.json`` attachment.
        """
        self.ensure_one()
        record = self.env["ai.invoice.import"].search(
            [("move_id", "=", self.id)], order="id desc", limit=1
        )
        candidates = [record.extraction_data] if record else []
        attachment = self.env["ir.attachment"].search(
            [
                ("res_model", "=", "account.move"),
                ("res_id", "=", self.id),
                ("name", "=", "extraction.json"),
            ],
            order="id desc",
            limit=1,
        )
        if attachment:
            candidates.append(attachment.raw)
        for raw in candidates:
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if isinstance(data, dict):
                return data
        return {}

    def _ai_apply_vendor_data(self, partner, extracted, origin=None):
        """Fill the vendor's empty fields from this invoice and queue the rest.

        Empty fields are written; differing values become pending suggestions;
        a bank account or tax id always becomes a pending row; the name is
        never touched. The invoice's phone and domain are always added as
        identifiers. Returns the summary (``filled``, ``learned``,
        ``conflicts``, ``withheld``) the chatter note is built from.
        """
        self.ensure_one()
        seller = (extracted or {}).get("seller") or {}
        partner = partner.commercial_partner_id if partner else partner
        summary = {
            "filled": [],
            "learned": [],
            "conflicts": [],
            "withheld": [],
        }
        if not partner or not seller:
            return summary

        Partner = self.env["res.partner"]
        labels = {
            name: description["string"]
            for name, description in Partner.fields_get(
                [field for field, _key in _AI_VENDOR_AUTO_FIELDS],
                attributes=["string"],
            ).items()
        }

        to_write = {}
        pending = []
        for field_name, key in _AI_VENDOR_AUTO_FIELDS:
            raw = seller.get(key)
            # A model's reading may put a non-string in a text field; skip it.
            if not isinstance(raw, str) or not raw.strip():
                continue
            raw = raw.strip()
            label = labels.get(field_name, field_name)

            if field_name in ("state_id", "country_id"):
                # The vendor's country, or the one staged earlier in this loop.
                country = partner.country_id or self.env["res.country"].browse(
                    to_write.get("country_id")
                )
                resolved = self._ai_resolve_location(field_name, raw, country)
                if not resolved:
                    summary["withheld"].append(
                        (
                            label,
                            raw,
                            _(
                                "Odoo has no such record and one is never "
                                "created here — an invented state is a code "
                                "the carrier rejects."
                            ),
                        )
                    )
                    continue
                current = partner[field_name]
                if not current:
                    to_write[field_name] = resolved.id
                    summary["filled"].append((label, resolved.display_name))
                elif current != resolved:
                    # Store the code, not the display name, so `action_apply`
                    # can resolve it again.
                    pending.append(
                        {
                            "field_name": field_name,
                            "field_label": label,
                            "invoice_value": resolved.code,
                            "record_value": current.code,
                        }
                    )
                continue

            current = partner[field_name]
            if not current:
                to_write[field_name] = raw
                summary["filled"].append((label, raw))
                continue
            if self._ai_same_contact_value(field_name, current, raw):
                continue
            pending.append(
                {
                    "field_name": field_name,
                    "field_label": label,
                    "invoice_value": raw,
                    "record_value": current,
                    "note": _(
                        "Applying this records the value as another way the "
                        "vendor is reached; it does not replace what is on the "
                        "contact."
                    )
                    if field_name in ("phone", "email", "website")
                    else False,
                }
            )

        if to_write:
            # No sudo: these are real edits to the vendor and the ACL applies.
            partner.write(to_write)

        summary["learned"] = self._ai_learn_identifiers(partner, seller)
        pending.extend(self._ai_sensitive_suggestions(partner, seller, summary))

        rows = self.env["goodahead.vendor.data.suggestion"]._replace_pending(
            self, partner, pending
        )
        summary["conflicts"] = [
            (row.field_label, row.invoice_value, row.record_value)
            for row in rows.filtered(lambda row: not row.is_sensitive)
        ]
        self._ai_post_vendor_data_note(partner, summary, origin=origin)
        return summary

    @api.model
    def _ai_same_contact_value(self, field_name, current, printed):
        """Whether the printed value is the same as the stored one.

        Phones compare on their normalised digits and websites on their host,
        using the matcher's normalisers; emails compare whole.
        """
        Identifier = self.env["goodahead.partner.identifier"]
        if field_name == "phone":
            left = Identifier._normalise_phone(current)
            right = Identifier._normalise_phone(printed)
            return bool(left) and left == right
        if field_name == "website":
            left = Identifier._host_of(current)
            right = Identifier._host_of(printed)
            return bool(left) and left == right
        return str(current).strip().casefold() == printed.strip().casefold()

    def _ai_learn_identifiers(self, partner, seller):
        """Add the invoice's phone and web domain as identifier rows (add-only)."""
        self.ensure_one()
        Identifier = self.env["goodahead.partner.identifier"]
        pairs = Identifier._derive_from_contact(
            phone=seller.get("phone"),
            email=seller.get("email"),
            website=seller.get("website"),
        )
        created = Identifier._add_pairs(
            {partner.id: pairs},
            _("AI import %(bill)s", bill=self.display_name),
        )
        return [(kind, value) for _partner_id, kind, value in created]

    def _ai_sensitive_suggestions(self, partner, seller, summary):
        """Return pending rows for a tax id or bank account, even on empty fields.

        These identify who gets paid, and a changed bank account is the usual
        form of invoice fraud, so they are never written automatically.
        """
        self.ensure_one()
        rows = []

        vat = seller.get("vat")
        if isinstance(vat, str) and vat.strip():
            vat = vat.strip()
            stored = (partner.vat or "").strip()
            if stored.casefold() != vat.casefold():
                rows.append(
                    {
                        "field_name": "vat",
                        "field_label": _("Tax ID"),
                        "invoice_value": vat,
                        "record_value": stored or False,
                        "is_sensitive": True,
                        "note": _(
                            "A tax id is never filled in automatically, empty "
                            "or not — it is a claim about who the vendor is."
                        ),
                    }
                )
                summary["withheld"].append(
                    (
                        _("Tax ID"),
                        vat,
                        _("never automatic — it is waiting in the queue."),
                    )
                )

        account = payee_account_number(seller)
        if account:
            sanitized = sanitize_account_number(account)
            held = partner.with_context(active_test=False).bank_ids.filtered(
                lambda bank: bank.sanitized_acc_number == sanitized
            )
            if not held:
                rows.append(
                    {
                        "field_name": "bank_account",
                        "field_label": _("Bank account"),
                        "invoice_value": account,
                        "record_value": ", ".join(
                            partner.bank_ids.mapped("acc_number")
                        )
                        or False,
                        "is_sensitive": True,
                        "note": _(
                            "Invoice fraud is a changed bank account. Check "
                            "it against something other than this invoice "
                            "before applying it."
                        ),
                    }
                )
                summary["withheld"].append(
                    (
                        _("Bank account"),
                        account,
                        _(
                            "never automatic — invoice fraud is precisely a "
                            "changed bank account."
                        ),
                    )
                )
        return rows

    def _ai_post_vendor_data_note(self, partner, summary, origin=None):
        """Post one note: what was filled, learned, and deliberately not applied.

        Uses ``Markup.format`` so PDF-derived values are escaped; never build
        it with an f-string.
        """
        self.ensure_one()
        if not any(summary[key] for key in ("filled", "learned", "withheld")) and (
            not summary["conflicts"]
        ):
            return

        def bullets(items):
            return Markup("").join(
                Markup("<li><strong>{}:</strong> {}</li>").format(*item)
                for item in items
            )

        body = Markup("<p><strong>{}</strong></p>").format(
            _(
                "Vendor details from this invoice, applied to %(vendor)s",
                vendor=partner.display_name,
            )
        )
        if origin:
            body += Markup("<p>{}</p>").format(origin)
        if summary["filled"]:
            body += Markup("<p>{}</p><ul>{}</ul>").format(
                _("Filled in, because the vendor record had nothing there:"),
                bullets(summary["filled"]),
            )
        if summary["learned"]:
            body += Markup("<p>{}</p><ul>{}</ul>").format(
                _(
                    "Learned, so the next bill from this vendor matches by "
                    "itself:"
                ),
                bullets(
                    (
                        # `self.env._`: `_()` cannot find the language in a
                        # generator expression on 18.0.
                        self.env._("Phone")
                        if kind == "phone"
                        else self.env._("Web domain"),
                        value,
                    )
                    for kind, value in summary["learned"]
                ),
            )
        if summary["conflicts"]:
            body += Markup("<p>{}</p><ul>{}</ul>").format(
                _(
                    "Not applied — the invoice contradicts the vendor record, "
                    "and replacing something a person typed is a deliberate "
                    "act. Waiting for review:"
                ),
                Markup("").join(
                    Markup(
                        "<li><strong>{}:</strong> {} — the record holds {}</li>"
                    ).format(label, printed, held or "—")
                    for label, printed, held in summary["conflicts"]
                ),
            )
        if summary["withheld"]:
            body += Markup("<p>{}</p><ul>{}</ul>").format(
                _("Deliberately not applied:"),
                Markup("").join(
                    Markup("<li><strong>{}</strong> ({}) — {}</li>").format(
                        label, value, reason
                    )
                    for label, value, reason in summary["withheld"]
                ),
            )
        self.message_post(body=body)
