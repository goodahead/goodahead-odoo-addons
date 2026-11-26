# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import logging

from odoo import models

_logger = logging.getLogger(__name__)


class AccountJournal(models.Model):
    _inherit = "account.journal"

    def create_document_from_attachment(self, attachment_ids):
        """Route a manual PDF vendor-bill upload through the SAME async queue as
        the email path instead of decoding it inline in the web request.

        For each uploaded PDF (when the company has a Claude API key and we're
        uploading into a purchase journal), create an ``ai.invoice.import`` and
        enqueue ``_process_import`` — exactly what ``message_new`` does for
        emails. Both entrypoints then share one code path and one behaviour, so
        the queued import is the only place a bill is ever built.

        Non-PDF attachments (e.g. an XML e-invoice), sale journals, or companies
        without an API key fall back to the native inline import.
        """
        company = self.company_id or self.env.company
        move_type = self.env.context.get("default_move_type", "entry")
        purchase_types = self.env["account.move"].get_purchase_types(
            include_receipts=True
        )
        attachments = self.env["ir.attachment"].browse(attachment_ids)

        # 🔴 `.sudo()` on the KEY READ ONLY, and it is load-bearing in this tree.
        #
        # `goodahead_claude` declares `claude_api_key` with
        # `groups="base.group_system"`, and 19.0 enforces field groups on the
        # READ path. The person uploading a vendor bill is an accountant, not a
        # system administrator, so an unprivileged read here does not say "no
        # key" — it either raises or yields False, and the upload would fall
        # silently back to the native inline importer. The symptom would be "the
        # AI import works for admin and not for anyone else", which is a
        # miserable thing to debug.
        #
        # Only the boolean "is a key configured" leaves this expression; the key
        # itself is never read into a variable, logged, or returned.
        route_ai = bool(company.sudo().claude_api_key) and move_type in purchase_types
        pdf_atts = (
            attachments.filtered(
                lambda a: (a.mimetype or "").lower() == "application/pdf"
            )
            if route_ai
            else self.env["ir.attachment"]
        )

        # Nothing eligible for AI import → native behaviour unchanged.
        if not pdf_atts:
            return super().create_document_from_attachment(attachment_ids)

        # Resolve the journal that received the upload (mirroring the native
        # resolution) so the queued import builds the bill on the SAME journal the
        # user chose, not just the company's first purchase journal.
        journal = self or self.browse(self.env.context.get("default_journal_id"))
        if not journal:
            journal = self.env["account.journal"].search(
                [
                    *self.env["account.journal"]._check_company_domain(company),
                    ("type", "=", "purchase"),
                ],
                limit=1,
            )

        AiImport = self.env["goodahead.invoice.import"]
        imports = AiImport
        for att in pdf_atts:
            imports |= AiImport._enqueue_from_attachment(
                att, company, journal=journal, move_type=move_type, reader="ai"
            )

        # Any remaining (non-PDF) attachments still import natively/inline.
        other_atts = attachments - pdf_atts
        if other_atts:
            super().create_document_from_attachment(other_atts.ids)

        return imports._action_view_imports()
