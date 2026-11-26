# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The community backend, paused per company.

A company whose receiving state is live in the APIX receiving add-on is
received by that add-on only, so for its backend the community modules:

* do not list (`action_einvoice_fetch`, the recurring fetch, `list_invoices`);
* refuse the re-fetch (`action_einvoice_refetch`);
* do not download (`download_invoice`): a download queued before the pause
  that the pause could not cancel (already running) is the only way in.

Companies in shadow or off keep receiving through the community modules as
before. The names overridden here are the community connector's; the
migration tool refuses to switch when they change
(`_migration_community_status`).
"""

import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ApixBackend(models.Model):
    _inherit = "apix.backend"

    goodahead_receiving_live = fields.Boolean(
        string="Received by the APIX receiving add-on",
        compute="_compute_goodahead_receiving_live",
        help="This company's e-invoices are received by the APIX receiving add-on: "
        "fetching and re-fetching here are paused.",
    )

    def _compute_goodahead_receiving_live(self):
        live = self._goodahead_live_backends()
        for backend in self:
            backend.goodahead_receiving_live = backend in live

    def _goodahead_live_backends(self):
        """The backends in ``self`` whose company receives live through the
        APIX receiving add-on."""
        if not self:
            return self
        companies = (
            self.env["goodahead.apix.connection"]
            .sudo()
            .search(
                [("company_id", "in", self.sudo().company_id.ids), ("receiving_state", "=", "live")]
            )
            .company_id
        )
        return self.filtered(lambda backend: backend.sudo().company_id in companies)

    def action_einvoice_fetch(self):
        live = self._goodahead_live_backends()
        for backend in live:
            _logger.info(
                "APIX community fetch skipped for company %s: "
                "received by the APIX receiving add-on",
                backend.sudo().company_id.id,
            )
        return super(ApixBackend, self - live).action_einvoice_fetch()

    def action_einvoice_refetch(self):
        live = self._goodahead_live_backends()
        if live:
            raise UserError(
                _(
                    "Re-fetching is disabled for %s: its e-invoices are received by the APIX "
                    "receiving add-on. Switch back in the migration tool first.",
                    ", ".join(live.sudo().company_id.mapped("name")),
                )
            )
        return super().action_einvoice_refetch()

    def list_invoices(self, refetch=False):
        self.ensure_one()
        if self._goodahead_live_backends():
            _logger.info(
                "APIX community listing skipped for company %s: "
                "received by the APIX receiving add-on",
                self.sudo().company_id.id,
            )
            return "SKIPPED: received by the APIX receiving add-on"
        return super().list_invoices(refetch=refetch)

    def download_invoice(self, storage_id, storage_key):
        self.ensure_one()
        if self._goodahead_live_backends():
            _logger.info(
                "APIX community download of %s skipped for company %s: received by the APIX "
                "receiving add-on",
                storage_id,
                self.sudo().company_id.id,
            )
            return "SKIPPED: received by the APIX receiving add-on"
        return super().download_invoice(storage_id, storage_key)
