# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The community send entry points skip a company whose community sending
is stopped: the button refuses, a community
send job queued before the stop sends nothing."""

import logging

from odoo import _, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = "account.move"

    def _community_sending_stopped(self):
        return self.filtered(lambda move: move.company_id.sudo().goodahead_community_sending_stopped)

    def action_einvoice_send(self):
        stopped = self._community_sending_stopped()
        if stopped:
            raise UserError(_(
                "The community APIX modules no longer send invoices for %s: send them with "
                "\"Send via APIX\".",
                ", ".join(stopped.company_id.mapped("name")),
            ))
        return super().action_einvoice_send()

    def einvoice_send(self):
        stopped = self._community_sending_stopped()
        for move in stopped:
            _logger.info(
                "Community APIX send of invoice %s skipped: community sending is stopped for "
                "company %s", move.id, move.company_id.id,
            )
        return super(AccountMove, self - stopped).einvoice_send()
