# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import logging

from odoo import models

_logger = logging.getLogger(__name__)


class AccountJournal(models.Model):
    _inherit = "account.journal"

    def create_document_from_attachment(self, attachment_ids):
        """Queue uploaded PDF vendor bills as AI imports, like the email path.

        Non-PDF attachments, sale journals and companies without an API key
        fall back to the native inline import.
        """
        company = self.company_id or self.env.company
        move_type = self.env.context.get("default_move_type", "entry")
        purchase_types = self.env["account.move"].get_purchase_types(
            include_receipts=True
        )
        attachments = self.env["ir.attachment"].browse(attachment_ids)

        # `claude_api_key` is restricted to base.group_system, so an accountant's
        # read would fail and silently fall back to the native importer. Only
        # the "is a key configured" boolean leaves this expression.
        route_ai = bool(company.sudo().claude_api_key) and move_type in purchase_types
        pdf_atts = (
            attachments.filtered(
                lambda a: (a.mimetype or "").lower() == "application/pdf"
            )
            if route_ai
            else self.env["ir.attachment"]
        )

        if not pdf_atts:
            return super().create_document_from_attachment(attachment_ids)

        # Mirror the native journal resolution so the bill lands on the journal
        # the user uploaded into.
        journal = self or self.browse(self.env.context.get("default_journal_id"))
        if not journal:
            journal = self.env["account.journal"].search(
                [
                    *self.env["account.journal"]._check_company_domain(company),
                    ("type", "=", "purchase"),
                ],
                limit=1,
            )

        AiImport = self.env["ai.invoice.import"]
        imports = AiImport
        for att in pdf_atts:
            imports |= AiImport._enqueue_from_attachment(
                att, company, journal=journal, move_type=move_type
            )

        other_atts = attachments - pdf_atts
        if other_atts:
            super().create_document_from_attachment(other_atts.ids)

        return imports._action_view_imports()
