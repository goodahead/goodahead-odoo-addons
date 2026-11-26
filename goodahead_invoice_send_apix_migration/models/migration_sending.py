# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The sending step of the migration tool.

**Carry over** reads what an earlier sending setup knows for one company
(``_migration_<code>_sending_read``: exactly four keys, anything else
refuses), writes it — customers' e-invoice addresses and operators, their
channels, the invoices' transmit methods, one ``sent`` delivery-history
entry per invoice it sent — and then stops that setup sending for the
company (``_migration_<code>_sending_stop``). Repeatable: a sent invoice is
recorded once per source (the ``_migration_once`` index).

**Switch sending back** resumes one source for one company, only while this
add-on's sending is off for it (``migration.sending_on``): never two
senders. It undoes nothing the carry-over wrote.

Only the company's own rows are written; an operator identifier that is not
on the operator list is reported as not carried, nothing is guessed. The
adapter itself never writes.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from odoo.addons.goodahead_invoice_send_apix.models.account_move import CUSTOMER_TYPES

_logger = logging.getLogger(__name__)

SENDING_ON = "migration.sending_on"
#: The keys ``_migration_<code>_sending_read`` returns — and only these.
READ_KEYS = ("recipients", "customer_channels", "invoice_methods", "sent_invoices")
#: Transmit method -> the customer's channel.
METHOD_TO_CHANNEL = {"einvoice": "apix", "letter": "apix_letter", "email": "email"}
SENT_CHANNELS = ("einvoice", "letter", "email", "unknown")


class MigrationSendingRefused(UserError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class GoodaheadApixConnection(models.Model):
    _inherit = "goodahead.apix.connection"

    # ------------------------------------------------------------------
    # Reading a source
    # ------------------------------------------------------------------

    @api.model
    def _migration_sending_source_checked(self, code):
        """Refuse a code that is no installed sending source (no adapter
        exists for that connector: nothing is carried over, AC-43) and an
        incomplete source."""
        if code not in self._migration_sending_sources():
            raise UserError(
                _("No sending adapter exists for %s — nothing was carried over.", code)
            )
        missing = self._migration_sending_incomplete(code)
        if missing:
            raise UserError(
                _(
                    "The migration source %(source)s is incomplete (no %(methods)s): "
                    "update its add-on.",
                    source=self._migration_source_label(code),
                    methods=", ".join(missing),
                )
            )

    @api.model
    def _migration_sending_read(self, code, company):
        """The source's sending data for ``company`` with exactly
        ``READ_KEYS`` (missing keys are empty lists)."""
        self._migration_sending_source_checked(code)
        values = self._migration_sending_method(code, "sending_read")(company) or {}
        extra = set(values) - set(READ_KEYS)
        if extra:
            _logger.error(
                "APIX migration source %s returned sending keys it must not: %s",
                code, sorted(extra),
            )
            raise UserError(
                _(
                    "The migration source %s returned more than the sending data. "
                    "Nothing was carried over; update its add-on.",
                    self._migration_source_label(code),
                )
            )
        return {key: list(values.get(key) or []) for key in READ_KEYS}

    @api.model
    def _migration_sending_counts(self, code, company):
        """``{key: number of rows}`` for the tool's sending step."""
        self._migration_check_admin()
        self._migration_check_company(company)
        values = self._migration_sending_read(code, company)
        return {key: len(rows) for key, rows in values.items()}

    # ------------------------------------------------------------------
    # Carry over
    # ------------------------------------------------------------------

    @api.model
    def _migration_sending_carry_over(self, code, company):
        """Write the source's sending data for ``company``, then stop the
        source sending for it. Returns what was carried."""
        self._migration_check_admin()
        self._migration_check_company(company)
        values = self._migration_sending_read(code, company)
        recipients, not_carried = self._migration_write_recipients(company, values["recipients"])
        result = {
            "recipients": recipients,
            "not_carried": not_carried,
            "customer_channels": self._migration_write_channels(
                company, values["customer_channels"]
            ),
            "invoice_methods": self._migration_write_methods(company, values["invoice_methods"]),
            "sent_invoices": self._migration_record_sent(company, code, values["sent_invoices"]),
        }
        self._migration_sending_method(code, "sending_stop")(company)
        connection = self._apix_for_company(company)
        if connection:
            connection.message_post(body=_(
                "Sending carried over from %(source)s and stopped there: %(recipients)s "
                "customers, %(channels)s customer channels, %(methods)s invoice transmit "
                "methods, %(sent)s sent invoices.",
                source=self._migration_source_label(code),
                recipients=result["recipients"],
                channels=result["customer_channels"],
                methods=result["invoice_methods"],
                sent=result["sent_invoices"],
            ))
        _logger.info("APIX migration: sending carried over from %s for company %s", code, company.id)
        return result

    @api.model
    def _migration_partner(self, company, partner_id):
        partner = self.env["res.partner"].sudo().browse(partner_id).exists()
        if partner.company_id and partner.company_id != company:
            return partner.browse()
        return partner

    @api.model
    def _migration_move(self, company, move_id):
        move = self.env["account.move"].sudo().browse(move_id).exists()
        if move.company_id != company or move.move_type not in CUSTOMER_TYPES:
            return move.browse()
        return move

    @api.model
    def _migration_write_recipients(self, company, rows):
        """E-invoice address and operator, the operator matched on the list
        by identifier; unmatched partners are returned, not written."""
        Operator = self.env["res.partner.operator.einvoice"].sudo().with_context(active_test=False)
        written, not_carried = 0, []
        for row in rows:
            partner = self._migration_partner(company, row.get("partner_id"))
            if not partner:
                continue
            operator = Operator.search(
                [("identifier", "=", row.get("operator_identifier") or "")], limit=1
            )
            if not operator or not row.get("edicode"):
                not_carried.append(partner.id)
                continue
            partner.write({"edicode": row["edicode"], "einvoice_operator_id": operator.id})
            written += 1
        return written, not_carried

    @api.model
    def _migration_write_channels(self, company, rows):
        written = 0
        for row in rows:
            channel = METHOD_TO_CHANNEL.get(row.get("channel"))
            partner = self._migration_partner(company, row.get("partner_id"))
            if row.get("company_id") != company.id or not channel or not partner:
                continue
            partner.with_company(company).write({"invoice_sending_method": channel})
            written += 1
        return written

    @api.model
    def _migration_write_methods(self, company, rows):
        written = 0
        for row in rows:
            move = self._migration_move(company, row.get("move_id"))
            method = row.get("method")
            if not move or method not in METHOD_TO_CHANNEL:
                continue
            if move.transmit_method != method:
                move.write({"transmit_method": method})
            written += 1
        return written

    @api.model
    def _migration_record_sent(self, company, code, rows):
        """One ``sent`` attempt per invoice and source, ``origin =
        migration`` (the ledger returns the existing one on a repeat)."""
        Attempt = self.env["goodahead.invoice.send.attempt"]
        recorded = 0
        for row in rows:
            move = self._migration_move(company, row.get("move_id"))
            if not move:
                continue
            channel = row.get("channel") if row.get("channel") in SENT_CHANNELS else "unknown"
            values = {
                "migration_source": code,
                "apix_batch_id": row.get("batch_id") or False,
                "apix_document_id": row.get("document_id") or False,
                "apix_credits": row.get("credits") or 0.0,
            }
            date = fields.Datetime.to_datetime(row.get("date"))
            if date:
                values["end_date"] = date
            Attempt._ledger_record(move, channel, "sent", "migration", self.env.user, values)
            recorded += 1
        return recorded

    # ------------------------------------------------------------------
    # Switch sending back (Flow N)
    # ------------------------------------------------------------------

    @api.model
    def _migration_sending_switch_back(self, code, company):
        """Let the earlier setup send for ``company`` again — only while this
        add-on's sending is off for it."""
        self._migration_check_admin()
        self._migration_check_company(company)
        self._migration_sending_source_checked(code)
        if self._apix_sending_enabled(company):
            raise MigrationSendingRefused(
                SENDING_ON,
                _("Switch sending off for %s first: a company never has two senders.",
                  company.name),
            )
        self._migration_sending_method(code, "sending_resume")(company)
        _logger.info("APIX migration: sending switched back to %s for company %s", code, company.id)
        return True

