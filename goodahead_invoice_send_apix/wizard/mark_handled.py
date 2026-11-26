# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""SCR-12 — Mark handled: the Accountant says why a failed send needs no
more attention (AC-33, Flow K). The invoice leaves the failed-sends list
until a later send of it fails or is refused."""

from odoo import _, api, fields, models

from ..models.goodahead_invoice_send_attempt import STATES, split_reason


class GoodaheadInvoiceSendMarkHandled(models.TransientModel):
    _name = "goodahead.invoice.send.mark.handled"
    _description = "Mark a failed invoice send handled"

    attempt_id = fields.Many2one("goodahead.invoice.send.attempt", required=True, readonly=True)
    move_id = fields.Many2one(related="attempt_id.move_id", string="Invoice")
    outcome = fields.Char(string="Last outcome", compute="_compute_outcome")
    reason = fields.Text()

    @api.depends("attempt_id")
    def _compute_outcome(self):
        states = dict(STATES)
        for wizard in self:
            attempt = wizard.attempt_id
            label = self.env._(states.get(attempt.state, attempt.state or ""))  # pylint: disable=translation-not-lazy
            text = split_reason(attempt.reason or "")[1]
            wizard.outcome = f"{label}: {text}" if text else label

    def action_mark_handled(self):
        self.ensure_one()
        self.attempt_id._mark_handled(self.reason)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "message": _("%s is marked handled.", self.move_id.display_name),
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
