# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import io
import json
import logging
from datetime import timedelta

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.tools import email_normalize
from odoo.tools.pdf import PdfFileReader

from odoo.addons.goodahead_invoice_import.lib.import_source import (
    AI_POLICY,
    SourceReader,
)

_logger = logging.getLogger(__name__)

#: The key of the AI reader in the core's source dispatch.
AI_READER = "ai"


class GoodaheadInvoiceImport(models.Model):
    """The AI source of the import record: mail intake of PDFs, the Claude read,
    and its token usage. The record itself, its lifecycle and everything after
    the read live in `goodahead_invoice_import` (core/source split). Since the source dispatch the
    core routes mails and jobs here through the ``ai`` reader key."""

    _inherit = "goodahead.invoice.import"

    reader = fields.Selection(
        selection_add=[(AI_READER, "PDF read by AI")],
        ondelete={AI_READER: "set null"},
    )
    extraction_model = fields.Char(readonly=True)
    input_tokens = fields.Integer(readonly=True)
    output_tokens = fields.Integer(readonly=True)

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

        A PDF is base64-encoded into the request, so the API payload is about a
        third larger again. The default is deliberately generous for a vendor
        bill (which is a handful of pages) and still far below anything that
        would be a scanned catalogue.
        """
        return int(
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("goodahead_invoice_ai_import.max_pdf_bytes", str(15 * 1024 * 1024))
        )

    # ------------------------------------------------------------------
    # What may start a paid reading (every entry point)
    # ------------------------------------------------------------------
    #
    # 🔴 EVERY READING IS BILLED, AND ANYBODY CAN SEND MAIL. The limits below
    # are checked where a reading is QUEUED (`_enqueue_import`), so they cover
    # the mail alias, an upload on a purchase journal and a retry alike. A
    # refused PDF becomes a failed record that says why, with no job. Each
    # limit is a system parameter; 0 switches it off.

    @api.model
    def _ai_int_param(self, key, default):
        value = self.env["ir.config_parameter"].sudo().get_param(
            f"goodahead_invoice_ai_import.{key}", str(default)
        )
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            return default

    @api.model
    def _ai_daily_reads_per_company(self):
        """Readings one company may queue in 24 hours (default 1000)."""
        return self._ai_int_param("daily_reads_per_company", 1000)

    @api.model
    def _ai_daily_reads_per_sender(self):
        """Readings one sender may queue in 24 hours (default 200): the mail's
        sender address, or the user who uploaded."""
        return self._ai_int_param("daily_reads_per_sender", 200)

    @api.model
    def _ai_max_pdf_pages(self):
        """Most pages of a PDF sent for reading (default 50)."""
        return self._ai_int_param("max_pdf_pages", 50)

    @api.model
    def _ai_recent_read_days(self):
        """A PDF read in the last this many days is not read again (default 30)."""
        return self._ai_int_param("recent_read_days", 30)

    @api.model
    def _ai_sender_key(self, email):
        return (email_normalize(email or "") or (email or "").strip()).lower()

    @api.model
    def _ai_budget_refusal(self, company, sender_email=None, uploader=None, exclude=None):
        """``(code, message)`` when the daily budget of ``company`` or of the
        sender (``sender_email`` for mail, ``uploader`` for an upload) is spent,
        else None. Counts readings QUEUED in the last 24 hours.

        ⚠️ Not ``user``: Odoo's ``_()`` reads a local named ``user`` to pick the
        language, and None there breaks every message below."""
        since = fields.Datetime.now() - timedelta(days=1)
        domain = [
            ("company_id", "=", company.id),
            ("reader", "=", AI_READER),
            ("queue_job_id", "!=", False),
            ("create_date", ">=", since),
        ]
        if exclude:
            domain.append(("id", "not in", exclude.ids))
        Import = self.sudo().with_context(active_test=False)
        company_limit = self._ai_daily_reads_per_company()
        if company_limit and Import.search_count(domain) >= company_limit:
            return (
                "ai_import.budget_exceeded",
                _(
                    "Not read: %(company)s has used its %(limit)s AI readings for "
                    "the last 24 hours. Retry it later, or raise the limit "
                    "(system parameter goodahead_invoice_ai_import."
                    "daily_reads_per_company).",
                    company=company.display_name,
                    limit=company_limit,
                ),
            )
        sender_limit = self._ai_daily_reads_per_sender()
        if not sender_limit:
            return None
        if sender_email:
            key = self._ai_sender_key(sender_email)
            used = len(
                Import.search(domain + [("source", "=", "email")]).filtered(
                    lambda r: self._ai_sender_key(r.source_email) == key
                )
            )
            who = key
        elif uploader:
            used = Import.search_count(domain + [("source", "!=", "email"),
                                                 ("create_uid", "=", uploader.id)])
            who = uploader.display_name
        else:
            return None
        if used >= sender_limit:
            return (
                "ai_import.budget_exceeded",
                _(
                    "Not read: %(who)s has sent %(limit)s PDFs for AI reading in "
                    "the last 24 hours. Retry it later, or raise the limit "
                    "(system parameter goodahead_invoice_ai_import."
                    "daily_reads_per_sender).",
                    who=who,
                    limit=sender_limit,
                ),
            )
        return None

    @api.model
    def _ai_pdf_page_count(self, raw):
        """Pages of the PDF ``raw``, or None when it cannot be counted (the
        reading then decides, as before)."""
        try:
            return len(PdfFileReader(io.BytesIO(raw), strict=False).pages)
        except Exception:  # noqa: BLE001 - a broken PDF is not a reason to refuse here
            return None

    def _ai_admission_refusal(self):
        """``(code, message)`` when this record's PDF must not be read, else
        None: too large, too many pages, read recently, or over budget."""
        self.ensure_one()
        attachment = self.pdf_attachment_id
        raw = attachment.raw if attachment else b""
        if not raw:
            return None  # the job fails with its own reason
        max_bytes = self._ai_max_pdf_bytes()
        if max_bytes and len(raw) > max_bytes:
            return (
                "ai_import.attachment_refused",
                _(
                    "Not read: the PDF is larger than %(mb)s MB.",
                    mb=max_bytes // (1024 * 1024) or round(max_bytes / (1024 * 1024), 3),
                ),
            )
        max_pages = self._ai_max_pdf_pages()
        pages = self._ai_pdf_page_count(raw) if max_pages else None
        if pages and pages > max_pages:
            return (
                "ai_import.too_many_pages",
                _(
                    "Not read: the PDF has %(pages)s pages; at most %(limit)s are "
                    "sent for AI reading.",
                    pages=pages,
                    limit=max_pages,
                ),
            )
        days = self._ai_recent_read_days()
        if days and attachment.checksum:
            earlier = self.sudo().search(
                [
                    ("id", "!=", self.id),
                    ("company_id", "=", self.company_id.id),
                    ("reader", "=", AI_READER),
                    ("extraction_data", "!=", False),
                    ("pdf_attachment_id.checksum", "=", attachment.checksum),
                    ("create_date", ">=", fields.Datetime.now() - timedelta(days=days)),
                ],
                order="id desc",
                limit=1,
            )
            if earlier:
                return (
                    "ai_import.already_read",
                    _(
                        "Not read: the same PDF was already read on %(record)s. "
                        "Open that import instead; it keeps what was read.",
                        record=earlier.display_name,
                    ),
                )
        if self.source == "email":
            return self._ai_budget_refusal(
                self.company_id, sender_email=self.source_email, exclude=self
            )
        return self._ai_budget_refusal(
            self.company_id, uploader=self.create_uid or self.env.user, exclude=self
        )

    def _import_job_options(self):
        """🔴 ONE TRY. A worker killed mid-call (Odoo's ``limit_time_real``)
        commits nothing, and queue_job runs the job again — up to its default
        of five retries, each another billed request. Retrying a reading is a
        person's decision (*Retry*)."""
        options = super()._import_job_options()
        if self._import_reader() == AI_READER:
            options["max_retries"] = 1
        return options

    def _enqueue_import(self, description):
        """Check the limits of a paid reading before its job is queued."""
        if self._import_reader() != AI_READER:
            return super()._enqueue_import(description)
        refusal = self._ai_admission_refusal()
        if refusal:
            code, message = refusal
            self._fail(code, f"{message} [{code}]")
            return None
        return super()._enqueue_import(description)

    # ------------------------------------------------------------------
    # The AI reader in the core's source dispatch
    # ------------------------------------------------------------------

    @api.model
    def _import_readers(self):
        readers = super()._import_readers()
        # Asked last: any structured reader takes a file before the paid one.
        # 🔴 AI_POLICY, not the default: a bank account printed on a PDF is only
        # looked up on the vendor; a new one becomes a sensitive suggestion.
        readers[AI_READER] = SourceReader(AI_READER, priority=100, policy=AI_POLICY)
        return readers

    @api.model
    def _source_accepts_attachment(self, source_key, attachment):
        if source_key != AI_READER:
            return super()._source_accepts_attachment(source_key, attachment)
        name = attachment[0] or ""
        return name.lower().endswith(".pdf")

    def _source_claims_record(self, source_key):
        """A record from before readers were stored (or made by hand) on the
        AI's own channels is the AI's, as it always was."""
        if source_key != AI_READER:
            return super()._source_claims_record(source_key)
        return self.source in ("upload", "email")

    def _source_read(self, source_key):
        if source_key != AI_READER:
            return super()._source_read(source_key)
        return self._ai_read_and_finalize()

    @api.model
    def _source_receive_mail(self, source_key, files, companions, company,
                             values, msg_dict):
        if source_key != AI_READER:
            return super()._source_receive_mail(
                source_key, files, companions, company, values, msg_dict
            )
        return self._ai_receive_mail(files, company, values, msg_dict)

    @api.model
    def _ai_receive_mail(self, files, company, values, msg_dict):
        """One import record per PDF of an inbound mail, enqueued.

        The company was decided by the core's mail intake — by the alias, see
        the note below on why that is the only thing that can decide it.

        🔴 WHICH COMPANY AN EMAILED BILL BELONGS TO IS DECIDED BY THE ALIAS —
        NOT BY THE INCOMING MAIL SERVER. This was checked rather than assumed,
        because the two sound interchangeable and are not:

        * `fetchmail.server` has **no company field at all** (confirmed in
          `addons/mail/models/fetchmail.py` at the deployed revision, and in the
          database: `fetchmail_server` has no company column). It puts only
          `default_fetchmail_server_id` on the context. So having one incoming
          server per company decides which MAILBOX is collected, which is real
          and useful, and decides nothing about which company the record lands
          in.
        * `env.company` inside this method is therefore the scheduler user's
          company — the SAME one for every message, whichever mailbox it came
          from.

        This database already answers it the right way for the native importer:
        core's own journal aliases carry the company in `alias_defaults` —
        `vendor-bills` has `{'move_type': 'in_invoice', 'company_id': 1, ...}`
        and `vendor-bills-beta-inc` has `company_id: 2`. That second alias
        exists precisely because one address cannot serve two companies.

        Alpha LLC and Beta Inc. have DIFFERENT CHARTS OF ACCOUNTS,
        and a bill imported into the wrong company does NOT fail: that company's
        accounts resolve, the shared expense categories resolve, the bill posts,
        and nothing says it is in the wrong books. It is the quietest possible
        way to get this wrong, and it surfaces at reconciliation.

        So: one alias per company, each with its own `company_id` in
        `alias_defaults`, which core passes here as `custom_values` and which
        `defaults.update(custom_values)` below applies. `docs/SETUP.md` §1
        spells it out, because the aliases are created by hand.

        ⚠️ TO BE PRECISE ABOUT WHAT CHANGED HERE, because the distinction
        matters to anyone judging the risk: that pass-through ALREADY WORKED —
        `defaults.update(custom_values)` predates this release. A company-scoped
        alias has always filed into its own company. What was missing was any
        signal when an alias does NOT name one, and that is the configuration
        that misfiles. So the change below is VISIBILITY, not routing: a warning
        in the log and a note on the record saying which company it landed in
        and whether that was the alias's decision or a fallback.

        Checked on a test database with two aliases and routed test mail:

            alias          env.company in message_new   custom_values      record
            zzprobe-alpha     Alpha LLC               {'company_id': 1}  Alpha
            zzprobe-beta    Alpha LLC  <-- same!    {'company_id': 2}  Beta

        The middle column is the point: `env.company` is Alpha for BOTH,
        so the default is wrong for one of them and only `alias_defaults` saves
        it. Since the source dispatch the core's `message_new` applies it for every reader,
        logs the company-less alias and states the landing company on the
        mail's first record.
        """
        pdf_attachments = []
        oversized = []
        for att_name, att_content, _mimetype in files:
            if len(att_content or b"") > self._ai_max_pdf_bytes():
                oversized.append(att_name)
                continue
            pdf_attachments.append((att_name, att_content))

        # 🔴 THE INBOUND PATH IS AN EXTERNALLY-TRIGGERED, BILLED SURFACE.
        #
        # The module ships no alias (each installation creates one by hand), but
        # whatever alias is pointed at this model, anybody who can send mail to
        # it causes a paid Claude call per PDF, and a `From` header is forgeable
        # — `alias_contact="partners"` is not authentication. These two limits
        # therefore live in the CODE, not in the alias configuration, so they
        # apply however the alias is created and whoever creates it.
        #
        # They are deliberately blunt. The job is to bound the damage of one
        # message, not to decide policy: a cap on how many PDFs one email may
        # enqueue, and a size above which a PDF is not sent at all.
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

        values = dict(values or {}, reader=AI_READER)
        budget = pdf_attachments and self._ai_budget_refusal(
            company, sender_email=values.get("source_email") or msg_dict.get("email_from")
        )
        if budget:
            # Over the budget: ONE failed record for the whole mail and no job,
            # so a flood of mail cannot become a flood of records either.
            code, message = budget
            record = self._create_for_source(
                "email", company, [],
                dict(values, name=msg_dict.get("subject") or "Attachment refused"),
            )
            record._fail(
                code,
                "%s %s [%s]" % (
                    message,
                    _("PDFs not read: %s.", ", ".join(name for name, _c in pdf_attachments)),
                    code,
                ),
            )
            return record
        if not pdf_attachments:
            # Every PDF refused: one failed record that names them (the
            # refused files themselves are not kept, as before).
            record = self._create_for_source(
                "email",
                company,
                [],
                dict(values, name=msg_dict.get("subject") or "Attachment refused"),
            )
            record._fail(
                "ai_import.attachment_refused",
                "No PDF was imported from this email. Refused: "
                + ", ".join(oversized)
                + ". A PDF above %s MB, or beyond the %s-per-message "
                "limit, is not sent for extraction."
                % (
                    self._ai_max_pdf_bytes() // (1024 * 1024),
                    self._ai_max_pdfs_per_message(),
                ),
            )
            return record

        records = self.browse()
        for att_name, att_content in pdf_attachments:
            record = self._create_for_source(
                "email",
                company,
                [(att_name, att_content, "application/pdf")],
                dict(values, name=att_name),
            )
            record._enqueue_import(f"AI Invoice Import: {att_name}")
            records |= record
        first_record = records[:1]

        if oversized and first_record:
            # Say on the record what the email contained and did NOT import.
            # Silently dropping an attachment from a vendor's email is how a
            # bill goes missing without anybody knowing there was one.
            first_record.message_post(
                body=Markup("<p>%s</p>")
                % _(
                    "This email also carried attachments that were not sent for "
                    "extraction (too large, or beyond the per-message limit): %s",
                    ", ".join(oversized),
                )
            )

        return records

    def _ai_read_and_finalize(self):
        """The Claude reader (`_source_read` for ``ai``): key check, PDF
        check, the paid extraction call, then `_finalize_import`.

        🔴 NOT AN OVERRIDE OF `_import_read_and_finalize` ANY MORE.
        That override called no ``super()``, so with the Finvoice add-on also
        installed, which reader a record got depended on load order. The core
        now dispatches on the record's ``reader``."""
        company = self.company_id
        # `.sudo()` for the same reason as in account_journal: the key is
        # `groups="base.group_system"` and a queue_job runs as whoever enqueued
        # it. Only the presence of a key is tested; the value stays inside the
        # client.
        if not company.sudo().claude_api_key:
            self._fail("Claude API key is not configured")
            return "FAILED: No API key"

        if not self.pdf_attachment_id or not self.pdf_attachment_id.raw:
            self._fail("PDF attachment is missing or empty")
            return "FAILED: No PDF"

        # 🔴 ONE BILLED CALL PER JOB. queue_job requeues a job whose worker
        # died and counts it in `retry`; the earlier run may well have been
        # billed. Such a run reads nothing and says so; Retry is a person's call.
        job = self.sudo().queue_job_id
        if job and job.retry:
            message = _(
                "An earlier run of this reading was interrupted (its worker was "
                "stopped), so it may already have been billed. It is not read "
                "again automatically; use Retry."
            )
            self._fail("ai_import.interrupted", f"{message} [ai_import.interrupted]")
            return "FAILED: interrupted earlier"

        pdf_bytes = self.pdf_attachment_id.raw
        AccountMove = self.env["account.move"]
        # No stored "processing": written inside this job's one
        # transaction it was never seen by anybody; the record shows
        # processing through its job instead.

        # Step 1: Claude extraction + UBL generation (shared method). Catch broadly
        # — Claude has been billed, so ANY error here (typed or unexpected) must be
        # terminal: return FAILED so the job is marked done, never retried/re-billed.
        try:
            ai_result = AccountMove._ai_extract_and_generate_ubl(pdf_bytes, company)
        except Exception as e:
            _logger.exception(
                "AI extraction/UBL generation failed for import %s", self.id
            )
            # Persist whatever Claude parsed (a validation error carries the JSON
            # on the exception) so it is visible and can be fixed + re-imported
            # WITHOUT a new API call. Safe: extraction is external, cursor is clean.
            extra = {}
            partial = getattr(e, "data", None)
            if partial is not None:
                extra["extraction_data"] = json.dumps(
                    partial, indent=2, ensure_ascii=False
                )
            self._fail(f"AI extraction/UBL generation failed: {e}", extra_vals=extra)
            return f"FAILED: {e}"

        return self._finalize_import(ai_result)

    def _import_extraction_vals(self, ai_result, extracted):
        vals = super()._import_extraction_vals(ai_result, extracted)
        usage = ai_result.get("usage")
        if usage is None:
            # A structured source (core/source split): no AI reading, so no AI
            # usage is recorded.
            return vals
        vals.update(
            {
                "extraction_model": ai_result["model_name"],
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
            }
        )
        return vals

    def _import_before_reimport(self, extracted):
        super()._import_before_reimport(extracted)
        if self._import_reader() != AI_READER:
            # Another reader's record: its PDF, if any, is an image
            # that came with structured data, not something the AI read.
            return
        AccountMove = self.env["account.move"]
        # 🔴 RE-ASK THE PDF BEFORE THE XML IS BUILT (19.0.3.20.0).
        # This is the path that never asked it: the stored JSON may carry no
        # text-layer verdict at all (every extraction from 19.0.3.3.0 to
        # 19.0.3.17.0), or one that says UNKNOWN because the attachment could
        # not be read at the time. `_ai_reask_the_pdf` re-decides from the
        # bytes on this record AND re-runs the country resolver, so the heal
        # works in both directions — it can add a marker as well as drop one.
        # It is here rather than in `_finalize_import` because the resolver can
        # WRITE a currency, and a currency written after the line below would
        # never reach the bill.
        AccountMove._ai_reask_the_pdf(
            extracted,
            self.pdf_attachment_id.raw if self.pdf_attachment_id else None,
        )

    def _import_reimport_source_label(self, extracted):
        """A re-import of a PDF the AI read keeps the AI note; a structured
        source's record keeps its neutral one (the core's answer)."""
        if "source_meta" in (extracted or {}):
            return super()._import_reimport_source_label(extracted)
        return None

    def _import_reimport_meta(self):
        if self._import_reader() != AI_READER:
            return super()._import_reimport_meta()
        return {
            "usage": {
                "input_tokens": self.input_tokens or 0,
                "output_tokens": self.output_tokens or 0,
            },
            "model_name": self.extraction_model or "manual-reimport",
        }
