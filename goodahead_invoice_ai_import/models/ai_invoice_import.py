# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import json
import logging

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class _AiImportDecodeError(Exception):
    """Internal signal to roll back the create-and-decode savepoint on a UBL
    decode error (distinct from a raw DB exception, for a clearer message)."""


# Dedicated queue_job channel for the slow Claude extraction jobs. Give it a
# small capacity in the deploy config (e.g. `root:N,root.ai_import:1`) so these
# jobs cannot starve the worker pool.
AI_IMPORT_CHANNEL = "root.ai_import"


class AiInvoiceImport(models.Model):
    _name = "ai.invoice.import"
    _inherit = ["mail.thread"]
    _description = "AI Invoice Import"
    _order = "create_date desc"

    name = fields.Char(string="Filename", readonly=True)
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("processing", "Processing"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        default="draft",
        readonly=True,
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        readonly=True,
    )
    source_email = fields.Char(readonly=True)
    journal_id = fields.Many2one(
        "account.journal",
        string="Journal",
        readonly=True,
        help="The purchase journal the manual upload targeted. Empty for the "
        "email path, which falls back to the company's first purchase journal.",
    )
    upload_move_type = fields.Char(
        readonly=True,
        help="The move type requested by the manual upload (e.g. in_invoice / "
        "in_refund). Empty for the email path (defaults to in_invoice).",
    )
    pdf_attachment_id = fields.Many2one("ir.attachment", string="PDF", readonly=True)
    xml_attachment_id = fields.Many2one(
        "ir.attachment", string="Generated XML", readonly=True
    )
    extraction_data = fields.Text(
        string="Extraction JSON",
        help="The JSON Claude extracted. Stored even on a failed extraction so "
        "you can see what was parsed, fix it (e.g. add a missing date) and "
        "re-import without calling the AI again. Editable while failed.",
    )
    extraction_model = fields.Char(readonly=True)
    input_tokens = fields.Integer(readonly=True)
    output_tokens = fields.Integer(readonly=True)
    move_id = fields.Many2one("account.move", string="Vendor Bill", readonly=True)
    error_message = fields.Text(readonly=True)

    @api.model
    def _ai_max_pdfs_per_message(self):
        """How many PDFs one inbound email may enqueue. Overridable, and
        settable without code via the system parameter."""
        return int(
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("goodahead_invoice_ai_import.max_pdfs_per_message", "10")
        )

    @api.model
    def _ai_max_pdf_bytes(self):
        """Largest PDF that will be sent for extraction.

        A PDF is base64-encoded into the request, so the payload is about a third
        larger.
        """
        return int(
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("goodahead_invoice_ai_import.max_pdf_bytes", str(15 * 1024 * 1024))
        )

    @api.model
    def message_new(self, msg_dict, custom_values=None):
        """Create import records from inbound email with PDF attachments.

        The company comes from the alias's `alias_defaults` (passed here as
        ``custom_values``), not from the incoming mail server, which has no
        company field; `env.company` here is the scheduler user's company for
        every message. In a multi-company database give each company its own
        alias with `{'company_id': <id>}`. When the alias names no company, a
        warning is logged and noted on the record.
        """
        defaults = {
            "source_email": msg_dict.get("email_from", ""),
        }
        if custom_values:
            defaults.update(custom_values)

        alias_named_company = bool(defaults.get("company_id"))
        if not alias_named_company:
            _logger.warning(
                "AI invoice import: the alias that received mail from %s names "
                "no company, so the bill will be filed in %s. With more than "
                "one company this is how bills reach the wrong chart of "
                "accounts — give each company its own alias and set "
                "`alias_defaults` to {'company_id': <id>}. See docs/SETUP.md.",
                msg_dict.get("email_from", "?"),
                self.env.company.display_name,
            )

        attachments = msg_dict.get("attachments", [])
        pdf_attachments = []
        oversized = []
        for att in attachments:
            att_name = att[0] if att else ""
            att_content = att[1] if len(att) > 1 else b""
            if not att_name or not att_name.lower().endswith(".pdf"):
                continue
            if len(att_content or b"") > self._ai_max_pdf_bytes():
                oversized.append(att_name)
                continue
            pdf_attachments.append((att_name, att_content))

        # Anyone who can mail the alias triggers a paid Claude call per PDF, and
        # `From` headers are forgeable, so the per-message count and size limits
        # are enforced in code regardless of alias configuration.
        if len(pdf_attachments) > self._ai_max_pdfs_per_message():
            limit = self._ai_max_pdfs_per_message()
            skipped = [name for name, _content in pdf_attachments[limit:]]
            pdf_attachments = pdf_attachments[:limit]
            _logger.warning(
                "AI invoice import: message from %s carried %s PDFs; importing "
                "the first %s and ignoring %s.",
                msg_dict.get("email_from", "?"),
                limit + len(skipped),
                limit,
                ", ".join(skipped),
            )
            oversized.extend(skipped)

        if not pdf_attachments:
            if oversized:
                record = super().message_new(msg_dict, custom_values=defaults)
                record.write(
                    {
                        "name": msg_dict.get("subject", "Attachment refused"),
                        "state": "failed",
                        "error_message": (
                            "No PDF was imported from this email. Refused: "
                            + ", ".join(oversized)
                            + ". A PDF above %s MB, or beyond the %s-per-message "
                            "limit, is not sent for extraction."
                            % (
                                self._ai_max_pdf_bytes() // (1024 * 1024),
                                self._ai_max_pdfs_per_message(),
                            )
                        ),
                    }
                )
                return record
            record = super().message_new(msg_dict, custom_values=defaults)
            record.write(
                {
                    "name": msg_dict.get("subject", "No PDF"),
                    "state": "failed",
                    "error_message": "No PDF attachment found in email",
                }
            )
            return record

        first_record = None
        for att_name, att_content in pdf_attachments:
            defaults["name"] = att_name
            record = super().message_new(msg_dict, custom_values=defaults)

            attachment = self.env["ir.attachment"].create(
                {
                    "name": att_name,
                    "raw": att_content,
                    "mimetype": "application/pdf",
                    "res_model": self._name,
                    "res_id": record.id,
                }
            )
            record.pdf_attachment_id = attachment
            record.with_delay(
                description=f"AI Invoice Import: {att_name}",
                identity_key=f"ai_invoice_import:{record.id}",
                channel=AI_IMPORT_CHANNEL,
            )._process_import()

            if not first_record:
                first_record = record

        # State the company on the record, and flag a fallback choice: a bill
        # filed in the wrong company posts without any error.
        if first_record:
            if alias_named_company:
                body = _(
                    "Received by email and filed under %(company)s, as set by "
                    "the alias.",
                    company=first_record.company_id.display_name,
                )
            else:
                body = _(
                    "Received by email and filed under %(company)s. The alias "
                    "that received it names no company, so this is a fallback, "
                    "not a decision — with more than one chart of accounts, give "
                    "each company its own alias and set its Default Values to "
                    "{'company_id': <id>}. The incoming mail server does not "
                    "decide this.",
                    company=first_record.company_id.display_name,
                )
            first_record.message_post(body=Markup("<p>%s</p>") % body)

        if oversized and first_record:
            # Record attachments that were not imported, so no bill goes missing
            # silently.
            first_record.message_post(
                body=Markup("<p>%s</p>")
                % _(
                    "This email also carried attachments that were not sent for "
                    "extraction (too large, or beyond the per-message limit): %s",
                    ", ".join(oversized),
                )
            )

        return first_record

    @api.model
    def _enqueue_from_attachment(self, attachment, company, journal=None,
                                 move_type=None):
        """Create an import record from a manually-uploaded PDF and enqueue it on
        the same queue the email path uses. Returns the created record.

        ``journal``/``move_type`` capture the journal (and move type) the upload
        targeted, so finalization builds the bill on the SAME journal the user
        chose instead of the company's first purchase journal. The uploaded
        ``ir.attachment`` (created client-side with no parent) is reparented onto
        the new import record so it is the record's PDF.
        """
        record = self.create(
            {
                "name": attachment.name or "upload.pdf",
                "company_id": company.id,
                "journal_id": journal.id if journal else False,
                "upload_move_type": move_type or False,
            }
        )
        attachment.write({"res_model": self._name, "res_id": record.id})
        record.pdf_attachment_id = attachment
        record.with_delay(
            description=f"AI Invoice Import: {record.name}",
            identity_key=f"ai_invoice_import:{record.id}",
            channel=AI_IMPORT_CHANNEL,
        )._process_import()
        return record

    def _action_view_imports(self):
        """Return a window action showing this import set (form for one, list for
        many) — used as the manual-upload button's result."""
        action = {
            "type": "ir.actions.act_window",
            "name": _("AI Invoice Imports"),
            "res_model": self._name,
            "context": dict(self.env.context),
        }
        if len(self) == 1:
            action.update(
                {
                    "view_mode": "form",
                    "views": [(False, "form")],
                    "res_id": self.id,
                }
            )
        else:
            action.update(
                {
                    "view_mode": "list,form",
                    "views": [(False, "list"), (False, "form")],
                    "domain": [("id", "in", self.ids)],
                }
            )
        return action

    def action_retry(self):
        """Manual retry button — re-extract (and re-bill) a failed import.

        Restricted to ``failed`` records: retrying while a job is still
        ``processing`` would enqueue a second job and double-bill Claude.
        """
        self.ensure_one()
        if self.move_id:
            raise UserError(
                _(
                    "A vendor bill (%s) is already linked to this import; retry "
                    "is blocked to avoid creating a duplicate bill."
                )
                % self.move_id.display_name
            )
        if self.state != "failed":
            raise UserError(
                _("Only failed imports can be retried (this one is '%s').")
                % self.state
            )
        self.write({"state": "draft", "error_message": False})
        self.with_delay(
            description=f"AI Invoice Import (retry): {self.name}",
            identity_key=f"ai_invoice_import:{self.id}",
            channel=AI_IMPORT_CHANNEL,
        )._process_import()

    def _process_import(self):
        """Main import pipeline — called via queue_job.

        Runs in a single queue_job transaction and must not commit: a mid-job
        commit makes the job's final write hit a serialization error, which
        queue_job postpones without counting retries, so the paid call would
        repeat indefinitely. Failures return "FAILED" so the job is not retried.
        """
        self.ensure_one()

        # Never reprocess a finished record or one that already produced a bill.
        # This check must precede the core-profile check, which writes `failed`.
        if self.state == "done" or self.move_id:
            return "OK: already processed"

        # Refuse before the paid call on an unsupported core, where the
        # vendor-matching hooks would silently do nothing. The refusal is
        # recorded on the record so it can be retried once the core is fixed.
        try:
            self.env["account.move"]._ai_assert_core_profile()
        except UserError as e:
            self._fail(str(e))
            return f"FAILED: {e}"

        company = self.company_id
        # The key field is restricted to `base.group_system`; only its presence
        # is tested here.
        if not company.sudo().claude_api_key:
            self._fail("Claude API key is not configured")
            return "FAILED: No API key"

        if not self.pdf_attachment_id or not self.pdf_attachment_id.raw:
            self._fail("PDF attachment is missing or empty")
            return "FAILED: No PDF"

        pdf_bytes = self.pdf_attachment_id.raw
        AccountMove = self.env["account.move"]
        self.state = "processing"

        # Step 1: Claude extraction + UBL generation. Claude has been billed, so
        # any error is terminal: return FAILED, never retry.
        try:
            ai_result = AccountMove._ai_extract_and_generate_ubl(pdf_bytes, company)
        except Exception as e:
            _logger.exception(
                "AI extraction/UBL generation failed for import %s", self.id
            )
            # Keep whatever Claude parsed so it can be fixed and re-imported
            # without a new API call.
            extra = {}
            partial = getattr(e, "data", None)
            if partial is not None:
                extra["extraction_data"] = json.dumps(
                    partial, indent=2, ensure_ascii=False
                )
            self._fail(f"AI extraction/UBL generation failed: {e}", extra_vals=extra)
            return f"FAILED: {e}"

        return self._finalize_import(ai_result)

    def _finalize_import(self, ai_result):
        """Steps 2-4: persist the extraction, create the vendor bill, post-process.

        Shared by the Claude path (_process_import) and the re-import-from-JSON
        recovery action (which reuses stored, possibly hand-fixed, data with no new
        API call). Returns the OK/FAILED result string.
        """
        self.ensure_one()
        company = self.company_id
        AccountMove = self.env["account.move"]
        extracted = ai_result["extracted"]
        usage = ai_result["usage"]
        xml_bytes = ai_result["xml_bytes"]

        # Before persisting: a hand-edited extraction from a re-import is not
        # re-validated, so drop a currency-inferred marker that no longer holds.
        AccountMove._ai_surviving_currency_marker(extracted)

        # Persist the extraction before the savepoint so it survives a rollback.
        extraction_vals = {
            "extraction_data": json.dumps(extracted, indent=2, ensure_ascii=False),
            "extraction_model": ai_result["model_name"],
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
        }
        self.write(extraction_vals)

        # Use the journal the upload targeted; the email path falls back to the
        # company's first purchase journal.
        journal = self.journal_id
        if not journal:
            journal = self.env["account.journal"].search(
                [("type", "=", "purchase"), ("company_id", "=", company.id)],
                limit=1,
            )
        if not journal:
            self._fail("No purchase journal found")
            return "FAILED: No purchase journal"
        move_type = self.upload_move_type or "in_invoice"

        # Steps 2-3: create a bill and decode the UBL directly into it (not via
        # journal._create_document_from_attachment, whose internal commit fails
        # in the queue-job context). A savepoint, not cr.rollback(), keeps the
        # caller's work intact on the interactive re-import path.
        xml_name = (
            self.name.rsplit(".", 1)[0] + ".xml" if self.name else "invoice.xml"
        )
        try:
            with self.env.cr.savepoint():
                xml_attachment = self.env["ir.attachment"].create(
                    {
                        "name": xml_name,
                        "raw": xml_bytes,
                        "mimetype": "application/xml",
                        "res_model": self._name,
                        "res_id": self.id,
                    }
                )
                move = AccountMove.create(
                    {"move_type": move_type, "journal_id": journal.id}
                )

                # Make the vendor's PDF the main attachment before decoding.
                # Otherwise core's `_import_attachments` renders a substitute
                # "Generated by Odoo" PDF and claims the slot with force=True;
                # an existing main attachment makes core skip that. The
                # `disable_pdf_in_xml` parameter is a global backstop for other
                # paths; keep both.
                pdf_on_move = self.pdf_attachment_id.copy(
                    {
                        "res_model": "account.move",
                        "res_id": move.id,
                    }
                )
                move._message_set_main_attachment_id(pdf_on_move)

                error = AccountMove._ai_decode_ubl_into_move(
                    move, xml_bytes, extracted, attachment=xml_attachment
                )
                if error:
                    raise _AiImportDecodeError(error)
        except _AiImportDecodeError as e:
            self._fail(f"UBL import error: {e}", extra_vals=extraction_vals)
            return f"FAILED: {e}"
        except Exception as e:
            _logger.exception("Odoo import failed for AI invoice import %s", self.id)
            self._fail(f"Odoo import failed: {e}", extra_vals=extraction_vals)
            return f"FAILED: {e}"

        # Link outside the savepoint so a retry cannot create a duplicate bill.
        self.xml_attachment_id = xml_attachment.id
        self.move_id = move.id

        # Step 4: post-processing. On error the bill stays linked and the record
        # is marked failed.
        try:
            with self.env.cr.savepoint():
                # The PDF is already attached above; do not copy it again.
                AccountMove._ai_post_process(
                    move, extracted,
                    usage, ai_result["model_name"], company,
                )
        except Exception as e:
            _logger.exception(
                "Post-import processing failed for import %s (move %s already "
                "created)",
                self.id,
                move.id,
            )
            self._fail(
                f"Post-import processing failed (vendor bill id {move.id} was "
                f"created): {e}"
            )
            return f"FAILED: {e}"

        self.write({"state": "done"})

        return (
            f"OK: Created {move.display_name} | "
            f"Tokens: {usage['input_tokens']} in / {usage['output_tokens']} out"
        )

    def action_reimport_from_json(self):
        """Re-run UBL generation + import from the stored (possibly hand-edited)
        extraction JSON, WITHOUT calling Claude again — a cheap recovery when the
        AI extraction was almost right (e.g. a missing date you can fill in)."""
        self.ensure_one()
        # Server-side guard; the button's visibility rule is UI-only.
        if self.state != "failed":
            raise UserError(
                _("Only failed imports can be re-imported (this one is '%s').")
                % self.state
            )
        if self.move_id:
            raise UserError(
                _(
                    "A vendor bill (%s) is already linked; re-import is blocked to "
                    "avoid creating a duplicate."
                )
                % self.move_id.display_name
            )
        if not self.extraction_data:
            raise UserError(_("There is no stored extraction JSON to re-import."))
        try:
            extracted = json.loads(self.extraction_data)
        except json.JSONDecodeError as e:
            raise UserError(_("The stored extraction JSON is not valid: %s") % e)

        AccountMove = self.env["account.move"]
        self.state = "processing"
        # Re-check the PDF's text layer and re-run the currency resolver before
        # the XML is built, since the resolver may write a currency.
        AccountMove._ai_reask_the_pdf(
            extracted,
            self.pdf_attachment_id.raw if self.pdf_attachment_id else None,
        )
        try:
            xml_bytes = AccountMove._ai_generate_ubl_from_extracted(
                extracted, self.company_id
            )
        except Exception as e:
            _logger.exception("UBL generation from stored JSON failed for %s", self.id)
            self._fail(f"UBL generation from stored JSON failed: {e}")
            return
        self._finalize_import(
            {
                "extracted": extracted,
                "usage": {
                    "input_tokens": self.input_tokens or 0,
                    "output_tokens": self.output_tokens or 0,
                },
                "model_name": self.extraction_model or "manual-reimport",
                "xml_bytes": xml_bytes,
            }
        )

    @api.model
    def _ai_user_works_imports(self, user=None):
        """Whether ``user`` (default: the current one) is somebody who works the
        AI imports — and so sees the failed-imports bubble and the import menus.

        Access rules stay on `group_account_invoice`; notification is narrower.
        Both groups are checked because in 19.0 core neither implies the other.
        Keep in step with `failed_imports_menu.js` and the menu items.
        """
        user = user or self.env.user
        return user.has_group("account.group_account_user") or user.has_group(
            "account.group_account_manager"
        )

    def _fail(self, message, extra_vals=None):
        _logger.error("AI Invoice Import %s failed: %s", self.id, message)
        vals = {"state": "failed", "error_message": message}
        if extra_vals:
            vals.update(extra_vals)
        self.write(vals)
