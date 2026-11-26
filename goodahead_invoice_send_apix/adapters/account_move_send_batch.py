# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The ERP's Send & Print for several invoices (19.0 adapter; contract §2.2,
AC-10).

When the batch dialog holds more than one invoice and APIX is among the
methods some of them use, "Send" opens the mass sending wizard for those
invoices instead of the ERP's deferred processing: the Accountant chooses
send now (with per-invoice status) or the background, so no invoice goes
through APIX later without that choice. The invoices that use only the
ERP's own methods continue in the ERP's batch flow; their emails are
recorded as Send & Print sends.
"""

from odoo import _, models
from odoo.exceptions import UserError

from .account_move_send import METHOD_CHANNEL, ORIGIN_KEY

MASS_WIZARD = "goodahead.invoice.send.wizard"


class AccountMoveSendBatchWizard(models.TransientModel):
    _inherit = "account.move.send.batch.wizard"

    def action_send_and_print(self, force_synchronous=False, allow_fallback_pdf=False):
        self.ensure_one()
        moves = self.move_ids
        apix_moves = moves.filtered(self._apix_uses_apix)
        if len(moves) < 2 or not apix_moves:
            return self._apix_erp_batch(moves, force_synchronous, allow_fallback_pdf)
        others = moves - apix_moves
        if others:
            self._apix_defer(others, force_synchronous, allow_fallback_pdf)
        wizard = self.env[MASS_WIZARD].with_context(
            active_model="account.move", active_ids=apix_moves.ids
        ).create({})
        return wizard._reopen()

    def _apix_uses_apix(self, move):
        """Whether one of the invoice's sending methods is APIX."""
        return bool(set(self._get_default_sending_methods(move)) & set(METHOD_CHANNEL))

    def _apix_erp_batch(self, moves, force_synchronous, allow_fallback_pdf):
        """The ERP's own batch flow; deferred invoices remember that the
        dialog started them."""
        wizard = self.with_context(**{ORIGIN_KEY: "send_print"})
        action = super(AccountMoveSendBatchWizard, wizard).action_send_and_print(
            force_synchronous=force_synchronous, allow_fallback_pdf=allow_fallback_pdf
        )
        self._apix_mark_origin(moves)
        return action

    def _apix_defer(self, moves, force_synchronous, allow_fallback_pdf):
        """The ERP's batch processing for the invoices of a mixed selection
        that use only the ERP's own methods, as the ERP's batch action does it
        (a batch dialog of one invoice cannot compute its alerts in 19.0)."""
        sender = self.env["account.move.send"].with_context(**{ORIGIN_KEY: "send_print"})
        if force_synchronous:
            sender._generate_and_send_invoices(moves, allow_fallback_pdf=allow_fallback_pdf)
            return
        cron = self.env.ref("account.ir_cron_account_move_send")
        if not cron.sudo().active:
            raise UserError(_(
                "Batch invoice sending is unavailable. Please, contact your system "
                "administrator to activate the cron to enable batch sending of invoices."
            ))
        moves.sending_data = {
            "author_user_id": self.env.user.id,
            "author_partner_id": self.env.user.partner_id.id,
        }
        self._apix_mark_origin(moves)
        cron._trigger()

    def _apix_mark_origin(self, moves):
        for move in moves.filtered("sending_data"):
            move.sending_data = dict(move.sending_data, **{ORIGIN_KEY: "send_print"})
