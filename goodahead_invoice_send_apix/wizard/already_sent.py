# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The already-sent warning.

Shown by "Send via APIX" instead of sending when the invoice already counts
as sent: when and how (APIX, email, the earlier setup), or "before the
add-on was installed, channel unknown". "Send again" sends it with the
confirmation; closing the dialog sends and records nothing.
"""

from odoo import _, api, fields, models


class GoodaheadInvoiceSendAlreadySent(models.TransientModel):
    _name = "goodahead.invoice.send.already.sent"
    _description = "Invoice already sent"

    move_id = fields.Many2one("account.move", required=True, ondelete="cascade")
    message = fields.Text(compute="_compute_message")

    @api.depends("move_id")
    def _compute_message(self):
        for wizard in self:
            info = wizard.move_id._apix_already_sent_info()
            wizard.message = info and "\n".join([
                wizard.move_id._apix_already_sent_message(info),
                _("Sending it again may deliver it to the customer twice."),
            ])

    def action_send_again(self):
        """Send with the confirmation; the outcome notification closes the
        dialog and reloads the invoice."""
        self.ensure_one()
        action = self.move_id.with_context(apix_resend_confirmed=True).action_apix_send()
        if action.get("tag") == "display_notification":
            action["params"]["next"] = {"type": "ir.actions.act_window_close"}
        return action
