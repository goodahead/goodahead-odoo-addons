# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import json
import logging
import os

from markupsafe import Markup

from odoo import _, api, fields, models

from odoo.addons.goodahead_invoice_import.lib import import_record as lifecycle
from odoo.addons.goodahead_invoice_import.lib.import_source import (
    STRICT_POLICY,
    SourceReader,
)

from ..lib import finvoice
from ..lib.sniff import looks_like_finvoice

_logger = logging.getLogger(__name__)

XML_MIMETYPE = "application/xml"

#: The key of the Finvoice reader in the core's source dispatch.
FINVOICE_READER = "finvoice"

#: Asked before any reader of unstructured documents (the AI reads PDFs at
#: 100), so a mail's Finvoice file and its PDF image stay together.
FINVOICE_PRIORITY = 10

#: System parameters capping what one file or mail may turn into (0 = off).
#: Defaults are well above a real bank or operator batch.
LIMIT_PARAM = "goodahead_invoice_import_finvoice.%s"
DEFAULT_MAX_MESSAGES_PER_FILE = 500
DEFAULT_MAX_FILES_PER_EMAIL = 20
DEFAULT_MAX_FILE_BYTES = 20 * 1024 * 1024


class GoodaheadInvoiceImport(models.Model):
    """The Finvoice source.

    🔴 A FINVOICE RECORD IS A RECORD WHOSE ``reader`` IS ``finvoice``,
    with its message in `finvoice_attachment_id`. The ``source`` stays
    ``upload`` or ``email`` — where the file came from — so the inbox filters
    on source keep meaning one thing and the APIX source (whose packages also
    hold Finvoice) can reuse the same reader without a third meaning for
    ``source``.

    🔴 NO SHARED HOOK IS OVERRIDDEN HERE. The core owns the mail door
    and the job and dispatches on the reader key; this add-on answers for
    ``finvoice`` in the `_source_*` hooks and hands every other key to
    ``super()``, so it behaves the same whether it loads before or after the
    AI add-on.

    🔴 ONE RECORD, ONE MESSAGE. A Finvoice file may hold several messages
    (guideline §17). The file is split when it arrives: each record keeps its
    own message as a stand-alone XML document, so the job, a retry, the bill's
    main document and the Accountant all see exactly one invoice. Only a file
    that cannot be read at all is kept whole, on one failed record.
    """

    _inherit = "goodahead.invoice.import"

    reader = fields.Selection(
        selection_add=[(FINVOICE_READER, "Finvoice")],
        ondelete={FINVOICE_READER: "set null"},
    )
    finvoice_attachment_id = fields.Many2one(
        "ir.attachment",
        string="Finvoice message",
        readonly=True,
        copy=False,
        ondelete="set null",
        help="The Finvoice message this record imports. Empty for records of "
        "other formats.",
    )
    finvoice_extra_attachment_ids = fields.Many2many(
        "ir.attachment",
        "goodahead_invoice_import_finvoice_extra_rel",
        "import_id",
        "attachment_id",
        string="Other files of the Finvoice package",
        readonly=True,
        copy=False,
        help="Files that came with the Finvoice message (other than its PDF "
        "image) and are attached to the bill.",
    )

    # ------------------------------------------------------------------
    # The Finvoice reader in the core's source dispatch
    # ------------------------------------------------------------------

    @api.model
    def _import_readers(self):
        readers = super()._import_readers()
        readers[FINVOICE_READER] = SourceReader(
            FINVOICE_READER,
            priority=FINVOICE_PRIORITY,
            policy=STRICT_POLICY,
            structured=True,
        )
        return readers

    @api.model
    def _source_accepts_attachment(self, source_key, attachment):
        if source_key != FINVOICE_READER:
            return super()._source_accepts_attachment(source_key, attachment)
        return looks_like_finvoice(attachment[1])

    @api.model
    def _source_claim_companions(self, source_key, documents, pool):
        """A file goes with the Finvoice file of the same name stem
        (``1001.xml`` + ``1001.pdf``), or with the only Finvoice file of the
        mail."""
        if source_key != FINVOICE_READER:
            return super()._source_claim_companions(source_key, documents, pool)
        if len(documents) == 1:
            return list(pool)
        stems = {self._finvoice_stem(name) for name, _c, _m in documents}
        return [item for item in pool if self._finvoice_stem(item[0]) in stems]

    @api.model
    def _source_receive_mail(self, source_key, files, companions, company,
                             values, msg_dict):
        """The Finvoice files of a mail: one import record per invoice,
        source ``email``; each takes its companions — the PDF image as the
        bill's main document, the rest as attachments."""
        if source_key != FINVOICE_READER:
            return super()._source_receive_mail(
                source_key, files, companions, company, values, msg_dict
            )
        max_files = self._finvoice_max_files_per_email()
        if max_files and len(files) > max_files:
            # Anybody can mail the alias: ONE failed record for the mail, no
            # job, and the files are not kept.
            return self._finvoice_refuse(
                "email", company, values, msg_dict.get("subject") or _("Finvoice email"),
                "finvoice.too_many_files",
                _(
                    "This email carries %(count)s Finvoice files; at most %(limit)s "
                    "are imported from one email, so none was: %(files)s.",
                    count=len(files),
                    limit=max_files,
                    files=", ".join(name for name, _c, _m in files[:50]),
                ),
            )
        stems = {self._finvoice_stem(name): name for name, _c, _m in files}
        paired = {name: [] for name, _c, _m in files}
        for other in companions:
            target = stems.get(self._finvoice_stem(other[0]))
            if not target and len(files) == 1:
                target = files[0][0]
            if target:
                paired[target].append(other)
        records = self.browse()
        for name, content, _mimetype in files:
            records |= self._finvoice_receive(
                "email", company, name, content, values=values, extra_files=paired[name]
            )
        return records

    def _source_read(self, source_key):
        if source_key != FINVOICE_READER:
            return super()._source_read(source_key)
        return self._finvoice_import()

    def _source_claims_record(self, source_key):
        if source_key != FINVOICE_READER:
            return super()._source_claims_record(source_key)
        return bool(self.finvoice_attachment_id)

    def _source_bill_attachments(self, source_key):
        """The Finvoice message (the bill's main document when no image came
        with it) and the package's other files."""
        if source_key != FINVOICE_READER:
            return super()._source_bill_attachments(source_key)
        return self.finvoice_attachment_id, self.finvoice_extra_attachment_ids

    # ------------------------------------------------------------------
    # Intake: one file -> one record per invoice
    # ------------------------------------------------------------------

    @api.model
    def _finvoice_receive(self, source, company, filename, raw, values=None,
                          origin_attachment=None, extra_files=None):
        """Turn one Finvoice file into import records and enqueue their jobs.

        ``extra_files`` (``(filename, bytes, mimetype)``) came with the file —
        its PDF image and anything else of the package. With one
        message, the first PDF among them becomes the bill's main document;
        with several, nobody can say which invoice an image belongs to, so
        they are kept as plain attachments of every record.

        ``origin_attachment`` is the uploaded `ir.attachment`; it is moved onto
        the (first) record instead of being left without a parent.

        A file `finvoice.parse` refuses (1.3, not XML ...) gives ONE failed
        record holding the whole file, with the Finvoice reason, and no job.
        Returns the records.
        """
        values = dict(values or {})
        extra_files = list(extra_files or [])
        max_bytes = self._finvoice_max_file_bytes()
        if max_bytes and len(raw or b"") > max_bytes:
            # Refused before it is read; not stored on the record.
            return self._finvoice_refuse(
                source, company, values, filename or "finvoice.xml",
                "finvoice.too_large",
                _(
                    "The Finvoice file %(file)s is %(kb)s KB; files above %(limit)s KB "
                    "are not imported.",
                    file=filename,
                    kb=len(raw) // 1024,
                    limit=max_bytes // 1024,
                ),
            )
        try:
            messages = finvoice.parse(raw, max_bytes=max_bytes or None)
        except finvoice.FinvoiceError as error:
            record = self._finvoice_create_record(
                source, company, filename, raw, values, extra_files, origin_attachment
            )
            record._finvoice_fail(error)
            return record

        max_messages = self._finvoice_max_messages_per_file()
        if max_messages and len(messages) > max_messages:
            # ONE failed record holding the file as received, and no job.
            record = self._finvoice_create_record(
                source, company, filename, raw, values, extra_files, origin_attachment
            )
            code = "finvoice.too_many_messages"
            record._fail(
                code,
                "%s [%s]" % (
                    _(
                        "The Finvoice file %(file)s holds %(count)s invoices; at most "
                        "%(limit)s are imported from one file, so none was.",
                        file=filename,
                        count=len(messages),
                        limit=max_messages,
                    ),
                    code,
                ),
                kind="data",
            )
            return record

        stem = os.path.splitext(filename or "finvoice")[0] or "finvoice"
        several = len(messages) > 1
        records = self.browse()
        shared_extras = None
        for message in messages:
            if several:
                name = f"{stem}-{message.index + 1}.xml"
                content = message.xml
                origin = None
            else:
                name = filename or "finvoice.xml"
                content = raw
                origin = origin_attachment
            # The package's other files are stored ONCE, on the first record,
            # and shared by the others (not one copy per invoice).
            record = self._finvoice_create_record(
                source, company, name, content, values,
                extra_files if shared_extras is None else [], origin,
                main_image=not several,
            )
            if shared_extras is None:
                shared_extras = record.finvoice_extra_attachment_ids
            elif shared_extras:
                record.finvoice_extra_attachment_ids = [(6, 0, shared_extras.ids)]
            if several:
                record.message_post(
                    body=Markup("<p>%s</p>")
                    % _(
                        "Finvoice message %(index)s of %(count)s in %(file)s.",
                        index=message.index + 1,
                        count=len(messages),
                        file=filename,
                    )
                )
            record._enqueue_import(_("Finvoice Import: %s", record.name))
            records |= record
        if several and origin_attachment:
            # The file as received, kept beside its first message.
            origin_attachment.write({"res_model": self._name, "res_id": records[:1].id})
        return records

    @api.model
    def _finvoice_create_record(self, source, company, name, content, values,
                                extra_files, origin_attachment=None, main_image=True):
        vals = dict(values, name=name, reader=FINVOICE_READER)
        files = list(extra_files)
        if not origin_attachment:
            files.insert(0, (name, content, XML_MIMETYPE))
        record = self._create_for_source(source, company, files, vals)
        attachments = self.env["ir.attachment"].search(
            [("res_model", "=", self._name), ("res_id", "=", record.id)], order="id"
        )
        if origin_attachment:
            origin_attachment.write({"res_model": self._name, "res_id": record.id})
            message_attachment = origin_attachment
        else:
            message_attachment = attachments[:1]
            attachments = attachments[1:]
        if not main_image:
            record.pdf_attachment_id = False
        record.write(
            {
                "finvoice_attachment_id": message_attachment.id,
                "finvoice_extra_attachment_ids": [
                    (6, 0, (attachments - record.pdf_attachment_id).ids)
                ],
            }
        )
        return record

    @api.model
    def _finvoice_int_param(self, key, default):
        value = self.env["ir.config_parameter"].sudo().get_param(LIMIT_PARAM % key, str(default))
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            return default

    @api.model
    def _finvoice_max_messages_per_file(self):
        """Most invoices imported from one Finvoice file (default 500)."""
        return self._finvoice_int_param("max_messages_per_file", DEFAULT_MAX_MESSAGES_PER_FILE)

    @api.model
    def _finvoice_max_files_per_email(self):
        """Most Finvoice files imported from one email (default 20)."""
        return self._finvoice_int_param("max_files_per_email", DEFAULT_MAX_FILES_PER_EMAIL)

    @api.model
    def _finvoice_max_file_bytes(self):
        """Largest Finvoice file imported, in bytes (default 20 MiB)."""
        return self._finvoice_int_param("max_file_bytes", DEFAULT_MAX_FILE_BYTES)

    @api.model
    def _finvoice_refuse(self, source, company, values, name, code, message):
        """One failed record, no files kept, no job."""
        record = self._create_for_source(
            source, company, [], dict(values or {}, name=name, reader=FINVOICE_READER)
        )
        record._fail(code, f"{message} [{code}]", kind="data")
        return record

    @api.model
    def _finvoice_stem(self, filename):
        return os.path.splitext((filename or "").strip().lower())[0]

    # ------------------------------------------------------------------
    # The job
    # ------------------------------------------------------------------

    def _finvoice_import(self):
        """Read this record's Finvoice message and import it under the
        reader's policy — the strict one — with the message and
        the package's other files on the bill."""
        self.ensure_one()
        message_attachment = self.finvoice_attachment_id
        try:
            messages = finvoice.parse(message_attachment.raw or b"")
            if len(messages) != 1:
                raise finvoice.FinvoiceError(
                    finvoice.UNREADABLE,
                    _(
                        "The stored Finvoice file holds %s messages; an import "
                        "record imports exactly one.",
                        len(messages),
                    ),
                )
            invoice_data = finvoice.to_invoice_data(messages[0])
        except finvoice.FinvoiceError as error:
            self._finvoice_fail(error)
            return f"FAILED: {error.code}"
        for warning in (invoice_data.get("source_meta") or {}).get("warnings") or []:
            _logger.info("Invoice Import %s, Finvoice warning: %s", self.id, warning)

        main_attachment, extra_attachments = self._source_bill_attachments(FINVOICE_READER)
        result = self._import_from_invoice_data(
            invoice_data,
            policy=self._import_readers()[FINVOICE_READER].policy,
            main_attachment=main_attachment,
            extra_attachments=extra_attachments,
        )
        return result.detail or result.outcome

    def _import_from_invoice_data(self, invoice_data, *args, **kwargs):
        """A Finvoice credit note is linked to the bill it credits."""
        result = super()._import_from_invoice_data(invoice_data, *args, **kwargs)
        if self.move_id and isinstance(invoice_data, dict):
            self._finvoice_link_original_bill(self.move_id, invoice_data)
        return result

    def _finvoice_link_original_bill(self, move, invoice_data):
        """Set the credit note's *reversal of* to the vendor's one posted bill
        whose reference is the message's OriginalInvoiceNumber. Best effort:
        no number, no bill or several bills — nothing is linked and the import
        is unchanged. Never raises."""
        meta = invoice_data.get("source_meta") or {}
        number = (meta.get("original_invoice_number") or "").strip()
        if (
            not number
            or move.move_type != "in_refund"
            or move.state != "draft"
            or move.reversed_entry_id
            or not move.partner_id
        ):
            return
        bills = self.env["account.move"].search(
            [
                ("move_type", "=", "in_invoice"),
                ("state", "=", "posted"),
                ("company_id", "=", move.company_id.id),
                ("commercial_partner_id", "=", move.partner_id.commercial_partner_id.id),
                ("ref", "=", number),
            ],
            limit=2,
        )
        if len(bills) != 1:
            if bills:
                _logger.info(
                    "Invoice Import %s: original invoice %s matches several bills; not linked.",
                    self.id, number,
                )
            return
        try:
            with self.env.cr.savepoint():
                move.reversed_entry_id = bills.id
        except Exception:  # noqa: BLE001 — a link is never worth the import
            _logger.exception("Invoice Import %s: could not link credit note to %s", self.id, number)

    def _finvoice_fail(self, error):
        """A Finvoice refusal on the record: the contract's
        ``import.invalid_data`` with the Finvoice code in the text (public-api
        §2.1), waiting for an Accountant.

        A message that lacks a value still hands over everything else it
        held (``error.data``); that is stored as the record's invoice data,
        so the Accountant fills in the gap and re-imports — the same
        as a failed AI reading keeps what it read."""
        extra_vals = None
        if isinstance(error.data, dict):
            extra_vals = {
                "extraction_data": json.dumps(error.data, indent=2, ensure_ascii=False),
                **lifecycle.invoice_identity_values(error.data),
            }
        self._fail(
            "import.invalid_data",
            _("%(reason)s [%(code)s]", reason=error.message, code=error.code),
            extra_vals=extra_vals,
            kind="data",
        )

    def _import_source_label(self):
        """"Imported from Finvoice (upload)" — neutral, never an AI reading.
       """
        label = super()._import_source_label()
        if self._import_reader() != FINVOICE_READER:
            return label
        return _("Finvoice (%s)", (label or "").lower())
