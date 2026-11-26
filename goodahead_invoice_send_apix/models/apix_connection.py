# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Sending on the shared APIX connection.

- **The sending switch** (``sending_enabled``, AC-37): per company, switched
  by Administrators only (``send.not_allowed``), only on a working connection
  (``send.connection_not_working``) and only while no earlier setup still
  sends for the company (``send.earlier_setup_sending``, AC-39). The guards
  run on every write of the field, so no form save or RPC call skips them.
  Accountants read the switch (the send doors do) but nothing else of it.
- **The background threshold**: Administrators only, at least 1
  (``send.threshold_invalid``).
- **The sending pause** (the circuit): a connection-level failure pauses the
  company's sending for 5 minutes; the pause is a ledger write on the
  connection row only. A passing connection check (or a successful send)
  closes it early. An open pause is "connection trouble" in the attention
  bubble.
"""

from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.fields import Domain

from . import ledger

ADMIN_GROUP = "account.group_account_manager"

NOT_ALLOWED = "send.not_allowed"
CONNECTION_NOT_WORKING = "send.connection_not_working"
EARLIER_SETUP_SENDING = "send.earlier_setup_sending"
THRESHOLD_INVALID = "send.threshold_invalid"
#: Connection-level codes that need an Administrator: the connection is
#: rejected, not only paused.
REJECTING_CODES = ("send.credentials_refused", "send.no_contract")
#: How long a connection-level failure pauses the company's sending.
PAUSE = timedelta(minutes=5)


class SendNotAllowed(AccessError):
    """A non-Administrator switches sending or sets the threshold."""

    code = NOT_ALLOWED


class SendingSwitchRefused(UserError):
    """Sending cannot be switched on: ``code`` says why."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class ThresholdInvalid(ValidationError):
    code = THRESHOLD_INVALID


class GoodaheadApixConnection(models.Model):
    _inherit = "goodahead.apix.connection"

    sending_enabled = fields.Boolean(
        string="Sending",
        default=False,
        copy=False,
        readonly=True,
        tracking=True,
        help="Invoices of this company can be sent through APIX. Switched on and off "
        "by an accounting administrator.",
    )
    background_threshold = fields.Integer(
        string="Background threshold",
        default=20,
        required=True,
        groups=ADMIN_GROUP,
        help="The mass sending wizard recommends sending in the background for "
        "selections larger than this many invoices.",
    )
    send_paused_until = fields.Datetime(
        string="Sending paused until",
        readonly=True,
        copy=False,
        help="Set when APIX refused or could not be reached for the whole company; "
        "queued sends wait until then.",
    )
    send_circuit_open = fields.Boolean(
        string="Sending paused",
        compute="_compute_send_circuit_open",
    )
    credit_balance = fields.Float(
        string="Credit balance",
        readonly=True,
        copy=False,
        groups=ADMIN_GROUP,
        help="The company's APIX credit balance from the last send answer.",
    )
    credit_balance_date = fields.Datetime(
        string="Credit balance date",
        readonly=True,
        copy=False,
        groups=ADMIN_GROUP,
    )

    @api.depends("send_paused_until")
    def _compute_send_circuit_open(self):
        for connection in self:
            connection.send_circuit_open = connection._apix_send_circuit_open()

    @api.constrains("background_threshold")
    def _check_background_threshold(self):
        for connection in self.sudo():
            if connection.background_threshold < 1:
                raise ThresholdInvalid(_("The background threshold must be at least 1."))

    # ------------------------------------------------------------------
    # The sending switch
    # ------------------------------------------------------------------

    def write(self, vals):
        if "sending_enabled" in vals:
            self._apix_check_switch_allowed()
            if vals["sending_enabled"]:
                self.filtered(lambda c: not c.sending_enabled)._apix_check_can_switch_on(
                    vals.get("status")
                )
        return super().write(vals)

    @api.model
    def _apix_for_company(self, company):
        """The company's connection (sudo), or an empty recordset."""
        return self.sudo().search([("company_id", "=", company.id)], limit=1)

    @api.model
    def _apix_sending_enabled(self, company):
        """Whether ``company`` sends through APIX — what the send doors and
        readiness read. No connection counts as off."""
        return bool(self._apix_for_company(company).sending_enabled)

    def action_apix_sending_on(self):
        self.write({"sending_enabled": True})
        return True

    def action_apix_sending_off(self):
        """Queued sends stay; their jobs fail as
        ``send.sending_off_since_queued`` (Flow D)."""
        self.write({"sending_enabled": False})
        return True

    def _apix_check_switch_allowed(self):
        if self.env.su or self.env.user.has_group(ADMIN_GROUP):
            return
        raise SendNotAllowed(_("Only accounting administrators can switch APIX sending on or off."))

    def _apix_check_can_switch_on(self, new_status=None):
        """``new_status``: the status written in the same call, if any."""
        for connection in self:
            if (new_status or connection.status) != "working":
                raise SendingSwitchRefused(
                    CONNECTION_NOT_WORKING,
                    _("Check the APIX connection of %s first: sending can only be switched "
                      "on while the connection is working.", connection.company_id.name),
                )
            senders = self._apix_earlier_senders(connection.company_id)
            if senders:
                raise SendingSwitchRefused(
                    EARLIER_SETUP_SENDING, _earlier_senders_message(self.env, senders, connection)
                )

    @api.model
    def _apix_earlier_senders(self, company):
        """Earlier sending setups still sending for ``company``, as
        ``[{code, label, reason, no_adapter?}]`` (``no_adapter``: a connector
        found for the company that no migration adapter can stop). Empty
        here; the migration bridge answers."""
        return []

    # ------------------------------------------------------------------
    # The sending pause
    # ------------------------------------------------------------------

    def _apix_send_circuit_open(self):
        self.ensure_one()
        paused = self.send_paused_until
        return bool(paused and paused > fields.Datetime.now())

    def _apix_open_send_circuit(self, code, message):
        """Pause the company's sending (own transaction, connection row only —
        never ``account.move``). Refused credentials and a missing contract
        also reject the connection; a blip only pauses."""
        self.ensure_one()
        vals = {
            "send_paused_until": fields.Datetime.now() + PAUSE,
            "status_message": message,
        }
        if code in REJECTING_CODES:
            vals["status"] = "rejected"
        self._apix_ledger_write(vals)

    def _apix_close_send_circuit(self):
        self.ensure_one()
        if self.send_paused_until:
            self._apix_ledger_write({"send_paused_until": False})

    def _apix_record_credit_balance(self, saldo):
        self.ensure_one()
        self._apix_ledger_write({
            "credit_balance": saldo,
            "credit_balance_date": fields.Datetime.now(),
        })

    def _apix_ledger_write(self, vals):
        with ledger.ledger_env(self.env) as env:
            env[self._name].browse(self.id).write(vals)
        self.invalidate_recordset(list(vals))

    def _check_connection(self):
        super()._check_connection()
        if self.status == "working" and self.send_paused_until:
            # this transaction has just written the connection row: the
            # ledger's cursor would wait on it until its lock timeout, so the
            # pause ends here, with the check
            self.write({"send_paused_until": False})

    @api.model
    def _attention_connection_domain(self):
        """An open sending pause is connection trouble too."""
        return list(Domain.OR([
            super()._attention_connection_domain(),
            [("send_paused_until", ">", fields.Datetime.now())],
        ]))


def _earlier_senders_message(env, senders, connection):
    """Why the switch is refused: setups the migration tool can
    carry over and stop, and connectors it cannot (no sending adapter)."""
    company = connection.company_id.name
    stoppable = [sender["label"] for sender in senders if not sender.get("no_adapter")]
    unstoppable = [sender["label"] for sender in senders if sender.get("no_adapter")]
    texts = []
    if stoppable:
        texts.append(env._(
            "%(setups)s still sends invoices for %(company)s. Carry it over with the migration "
            "tool first — it switches the earlier sending off for this company.",
            setups=", ".join(stoppable), company=company,
        ))
    if unstoppable:
        texts.append(env._(
            "%(setups)s is set up for %(company)s and may still send its invoices, and it has "
            "no sending adapter, so the migration tool cannot carry it over or switch its "
            "sending off. First remove its APIX credentials for %(company)s (archived records "
            "count too), uninstall it, or install its sending adapter.",
            setups=", ".join(unstoppable), company=company,
        ))
    return " ".join(texts)
