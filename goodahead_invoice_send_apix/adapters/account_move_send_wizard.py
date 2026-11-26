# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The ERP's single-invoice Send & Print dialog (19.0 adapter; SCR-04).

Adds the "Send again anyway" tick box of the already-sent alert (AC-31: the
confirmation stays inside the dialog) and shows an accepted APIX send as the
button does: a sticky success notification with the batch id and APIX's
warnings, then the dialog's own closing action.
"""

from odoo import api, fields, models

from ..models.account_move_send import _notification
from .account_move_send import ORIGIN_KEY, RESULTS_KEY


class AccountMoveSendWizard(models.TransientModel):
    _inherit = "account.move.send.wizard"

    apix_resend_confirmed = fields.Boolean(
        string="Send again anyway",
        help="The invoice was already sent; tick to send it through APIX again.",
    )
    apix_resend_needed = fields.Boolean(compute="_compute_apix_resend_needed")

    @api.depends("sending_methods", "extra_edis", "mail_partner_ids", "apix_resend_confirmed")
    def _compute_alerts(self):
        for wizard in self:
            confirmed = wizard.with_context(apix_resend_confirmed=wizard.apix_resend_confirmed)
            super(AccountMoveSendWizard, confirmed)._compute_alerts()

    @api.depends("alerts")
    def _compute_apix_resend_needed(self):
        for wizard in self:
            wizard.apix_resend_needed = "apix_already_sent" in (wizard.alerts or {})

    def _get_sending_settings(self):
        settings = super()._get_sending_settings()
        settings["apix_resend_confirmed"] = self.apix_resend_confirmed
        # emails and downloads from this dialog are Send & Print sends (§2.3)
        settings[ORIGIN_KEY] = "send_print"
        return settings

    def action_send_and_print(self, allow_fallback_pdf=False):
        results = []
        collecting = self.with_context(**{RESULTS_KEY: results})
        action = super(AccountMoveSendWizard, collecting).action_send_and_print(
            allow_fallback_pdf=allow_fallback_pdf
        )
        accepted = [(move, result) for move, result in results if result.outcome == "accepted"]
        if not accepted:
            return action
        move, result = accepted[0]
        kind, message = move.with_env(self.env)._apix_accepted_notice(result)
        return _notification(kind, message, action)
