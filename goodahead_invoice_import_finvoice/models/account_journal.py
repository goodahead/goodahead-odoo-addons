# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from odoo import models

from ..lib.sniff import looks_like_finvoice


class AccountJournal(models.Model):
    _inherit = "account.journal"

    def create_document_from_attachment(self, attachment_ids):
        """The bill upload button: Finvoice files become import records.

        In a purchase journal, every uploaded file that looks like Finvoice is
        handed to the Finvoice source — one import record per invoice in the
        file, enqueued on the core's import queue — and the result opens those
        records. Everything else goes on to ``super()`` unchanged: a PDF to the
        AI reading when that add-on is installed (its override takes PDFs and
        passes the rest down, so the order of the two overrides does not
        matter), any other file — a UBL e-invoice, a PDF with no AI part — to
        the ERP's own importer.
        """
        move_type = self.env.context.get("default_move_type", "entry")
        purchase_types = self.env["account.move"].get_purchase_types(include_receipts=True)
        if move_type not in purchase_types:
            return super().create_document_from_attachment(attachment_ids)
        journal = self[:1] or self.browse(self.env.context.get("default_journal_id"))
        if journal and journal.type != "purchase":
            return super().create_document_from_attachment(attachment_ids)
        if not journal:
            journal = self.env["account.journal"].search(
                [
                    *self.env["account.journal"]._check_company_domain(self.env.company),
                    ("type", "=", "purchase"),
                ],
                limit=1,
            )

        attachments = self.env["ir.attachment"].browse(attachment_ids)
        finvoice_files = attachments.filtered(lambda a: looks_like_finvoice(a.raw))
        if not finvoice_files or not journal:
            return super().create_document_from_attachment(attachment_ids)

        Import = self.env["goodahead.invoice.import"]
        company = journal.company_id or self.env.company
        imports = Import.browse()
        for attachment in finvoice_files:
            imports |= Import._finvoice_receive(
                "upload",
                company,
                attachment.name or "finvoice.xml",
                attachment.raw,
                values={"journal_id": journal.id, "upload_move_type": move_type},
                origin_attachment=attachment,
            )

        others = attachments - finvoice_files
        if others:
            super().create_document_from_attachment(others.ids)
        return imports._action_view_imports()
