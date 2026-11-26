# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""No posting-time community Finvoice document for a company whose
community sending is stopped: posting there produces nothing of
the community modules."""

from odoo import models

FINVOICE_FORMAT = "finvoice_3_0"


class AccountEdiFormat(models.Model):
    _inherit = "account.edi.format"

    def _get_move_applicability(self, move):
        if self.code == FINVOICE_FORMAT and move.company_id.sudo().goodahead_community_sending_stopped:
            return None
        return super()._get_move_applicability(move)
