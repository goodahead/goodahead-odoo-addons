# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The community APIX modules as the sending source ``community``.


* status: ``sending`` while the company has a community backend and is not
  stopped; ``stopped`` after the stop (or without a backend while the
  company still has community sending data to carry); ``none`` otherwise;
* read: the company's community sending data as plain values — customers'
  e-invoice addresses and operators, their community transmit methods, the
  invoices' transmit methods and the invoices the community modules sent;
* stop: this adapter's per-company flag; the community send entry points and
  posting-time e-invoice documents skip a stopped company, and its queued
  community sends are cancelled;
* resume: the flag cleared.

Read-only towards everything else: it never sends, never
calls APIX, never writes attempts, partner fields or transmit methods, never
touches another company and never deletes the community's records.
"""

import logging
from datetime import datetime, time

from odoo import _, api, models

_logger = logging.getLogger(__name__)

#: This source's code in the migration registry (the community receiving
#: source registers it).
COMMUNITY = "community"
#: The keys ``sending_read`` returns.
READ_KEYS = ("recipients", "customer_channels", "invoice_methods", "sent_invoices")
#: Community transmit-method code -> this add-on's transmit method. Other
#: codes (post, portal, ...) have no APIX or email channel: not carried.
COMMUNITY_METHODS = {"einvoice": "einvoice", "printing_service": "letter", "mail": "email"}
CUSTOMER_TYPES = ("out_invoice", "out_refund")
#: Queue-job states a stop cancels (queue_job's own "Cancel").
CANCELLABLE_JOB_STATES = ("wait_dependencies", "pending", "enqueued", "failed")


class GoodaheadApixConnection(models.Model):
    _inherit = "goodahead.apix.connection"

    # ------------------------------------------------------------------
    # Sending source methods
    # ------------------------------------------------------------------

    @api.model
    def _migration_community_sending_status(self, company):
        """``(sending | stopped | none, reason)`` for ``company``; read-only."""
        if company.sudo().goodahead_community_sending_stopped:
            return "stopped", _("the community APIX modules no longer send for this company")
        if self._community_sending_backend(company):
            return "sending", _(
                "the community APIX modules can still send e-invoices for this company"
            )
        if self._community_company_data(company):
            return "stopped", _(
                "the community APIX modules have no backend for this company, so they cannot "
                "send; their sending data can be carried over"
            )
        return "none", ""

    @api.model
    def _migration_community_sending_read(self, company):
        """The company's community sending data, exactly ``READ_KEYS``."""
        moves = self._community_customer_moves(company)
        return {
            "recipients": self._community_recipients(company),
            "customer_channels": self._community_channels(company),
            "invoice_methods": [
                {"move_id": move.id, "method": COMMUNITY_METHODS[move.transmit_method_id.code]}
                for move in moves
                if move.transmit_method_id.code in COMMUNITY_METHODS
            ],
            "sent_invoices": self._community_sent(company),
        }

    @api.model
    def _migration_community_sending_stop(self, company):
        """Stop the community modules sending for ``company``; idempotent."""
        company.sudo().goodahead_community_sending_stopped = True
        jobs = self._community_queued_sends(company)
        jobs.button_cancelled()
        _logger.info(
            "APIX community sending stopped for company %s: %s queued community sends cancelled",
            company.id, len(jobs),
        )
        return True

    @api.model
    def _migration_community_sending_resume(self, company):
        """Let the community modules send for ``company`` again; idempotent."""
        company.sudo().goodahead_community_sending_stopped = False
        _logger.info("APIX community sending resumed for company %s", company.id)
        return True

    # ------------------------------------------------------------------
    # Reading the community data (sudo, the company's rows only)
    # ------------------------------------------------------------------

    @api.model
    def _community_sending_backend(self, company):
        return self.env["apix.backend"].sudo().search([("company_id", "=", company.id)], limit=1)

    @api.model
    def _community_company_data(self, company):
        """Whether the community modules hold sending data of ``company``
        itself (customer channels, invoice transmit methods, sent invoices);
        e-invoice addresses are shared by every company and do not count."""
        return bool(
            self._community_channels(company)
            or self._community_customer_moves(company)
            or self._community_sent(company)
        )

    @api.model
    def _community_partners(self, company, domain):
        Partner = self.env["res.partner"].sudo().with_company(company)
        return Partner.search([("company_id", "in", [False, company.id])] + domain)

    @api.model
    def _community_recipients(self, company):
        partners = self._community_partners(company, [("edicode", "!=", False)])
        return [
            {
                "partner_id": partner.id,
                "edicode": partner.edicode,
                "operator_identifier": partner.einvoice_operator_id.identifier or "",
            }
            for partner in partners
        ]

    @api.model
    def _community_channels(self, company):
        partners = self._community_partners(
            company, [("customer_invoice_transmit_method_id", "!=", False)]
        )
        rows = []
        for partner in partners:
            method = COMMUNITY_METHODS.get(partner.customer_invoice_transmit_method_id.code)
            if not method:
                continue
            rows.append({"partner_id": partner.id, "company_id": company.id, "channel": method})
        return rows

    @api.model
    def _community_customer_moves(self, company):
        return self.env["account.move"].sudo().search([
            ("company_id", "=", company.id),
            ("move_type", "in", CUSTOMER_TYPES),
            ("transmit_method_id", "!=", False),
        ])

    @api.model
    def _community_sent(self, company):
        """One row per invoice the community modules sent: from its binding
        (batch id, document id, credits) or, without one, its sent date."""
        bindings = self.env["apix.account.invoice"].sudo().search([
            ("odoo_id.company_id", "=", company.id),
            ("odoo_id.move_type", "in", CUSTOMER_TYPES),
        ])
        rows = {}
        for binding in bindings:
            move = binding.odoo_id
            rows.setdefault(move.id, _sent_row(
                move,
                binding.create_date,
                binding.apix_batch_id,
                binding.apix_accepted_document_id,
                binding.apix_cost_in_credits,
            ))
        unbound = self.env["account.move"].sudo().search([
            ("company_id", "=", company.id),
            ("move_type", "in", CUSTOMER_TYPES),
            ("date_einvoice_sent", "!=", False),
            ("id", "not in", list(rows)),
        ])
        for move in unbound:
            rows[move.id] = _sent_row(move, False, False, False, 0.0)
        return list(rows.values())

    @api.model
    def _community_queued_sends(self, company):
        jobs = self.env["queue.job"].sudo().search([
            ("model_name", "=", "account.move"),
            ("method_name", "=", "einvoice_send"),
            ("state", "in", list(CANCELLABLE_JOB_STATES)),
        ])
        return jobs.filtered(lambda job: job.records.exists().company_id == company)


def _sent_row(move, fallback_date, batch_id, document_id, credits):
    sent_on = move.date_einvoice_sent
    when = datetime.combine(sent_on, time(12)) if sent_on else fallback_date
    channel = "letter" if move.transmit_method_id.code == "printing_service" else "einvoice"
    return {
        "move_id": move.id,
        "date": when or False,
        "channel": channel,
        "batch_id": batch_id or "",
        "document_id": document_id or "",
        "credits": credits or 0.0,
    }
