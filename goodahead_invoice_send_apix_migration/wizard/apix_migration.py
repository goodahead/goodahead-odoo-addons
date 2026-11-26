# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""SCR-14 — the sending step of the migration tool (contracts/public-api.md
§11.2, sad.md Flow N; AC-42, AC-43).

A thin screen over the bridge (``models/migration_sending.py``): per
sending source, its status and what it would carry over; **Carry over
sending** writes it and stops the source; **Switch sending back** resumes the
source while this add-on's sending is off. Administrators only, one company
at a time, like the rest of the tool.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.apix_connection import NONE, STOPPED
from ..models.migration_sending import MigrationSendingRefused

#: What the sending step shows before any button.
SENDING_VIEWS = [
    ("needs_connection", "Carry over the connection first"),
    ("no_adapter", "No migration adapter"),
    ("nothing", "Nothing to carry over"),
    ("error", "Source problem"),
    ("found", "Sending found"),
]
#: The outcome of the last sending button.
SENDING_RESULTS = [
    ("done", "Carried over"),
    ("partial", "Carried over, some customers not"),
    ("switched_back", "Switched back"),
    ("refused", "Switch back refused"),
]
COUNT_FIELDS = {
    "recipients": "sending_count_recipients",
    "customer_channels": "sending_count_channels",
    "invoice_methods": "sending_count_methods",
    "sent_invoices": "sending_count_sent",
}


class GoodaheadApixMigration(models.TransientModel):
    _inherit = "goodahead.apix.migration"

    step = fields.Selection(
        selection_add=[("sending", "Sending")], ondelete={"sending": "set default"}
    )
    sending_source_code = fields.Selection(
        selection="_selection_sending_source_code",
        string="Sending from",
        help="The earlier APIX sending setup whose customers' details, transmit methods "
        "and sent invoices are carried over.",
    )
    sending_view = fields.Selection(SENDING_VIEWS, compute="_compute_sending")
    sending_banner = fields.Char(compute="_compute_sending")
    sending_status = fields.Selection(
        [("sending", "Still sending"), ("stopped", "Stopped"), ("none", "No sending found")],
        compute="_compute_sending",
    )
    sending_status_reason = fields.Char(compute="_compute_sending")
    sending_source_label = fields.Char(compute="_compute_sending")
    sending_count_recipients = fields.Integer(
        string="Customers with e-invoice details", compute="_compute_sending"
    )
    sending_count_channels = fields.Integer(string="Customer channels", compute="_compute_sending")
    sending_count_methods = fields.Integer(
        string="Invoice transmit methods", compute="_compute_sending"
    )
    sending_count_sent = fields.Integer(string="Invoices sent", compute="_compute_sending")
    sending_can_carry = fields.Boolean(compute="_compute_sending")
    sending_can_switch_back = fields.Boolean(compute="_compute_sending")
    sending_result = fields.Selection(SENDING_RESULTS, readonly=True)
    sending_result_message = fields.Text(readonly=True)

    @api.model
    def _selection_sending_source_code(self):
        Connection = self.env["goodahead.apix.connection"]
        return [
            (code, Connection._migration_source_label(code))
            for code in Connection._migration_sending_sources()
        ]

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        if "sending_source_code" in fields_list and not values.get("sending_source_code"):
            values["sending_source_code"] = self._default_sending_source_code(
                values.get("source_code")
            )
        return values

    @api.model
    def _default_sending_source_code(self, receiving_code):
        """The receiving source when it also sends, else the first sending
        source, else none."""
        codes = self.env["goodahead.apix.connection"]._migration_sending_sources()
        if receiving_code in codes:
            return receiving_code
        return codes[0] if codes else False

    @api.depends("company_id", "sending_source_code", "sending_result")
    def _compute_sending(self):
        for wizard in self:
            values = dict.fromkeys(COUNT_FIELDS.values(), 0)
            values.update(wizard._sending_screen())
            wizard.update(values)

    def _sending_screen(self):
        """The field values of the sending step before any button."""
        self.ensure_one()
        Connection = self.env["goodahead.apix.connection"]
        code = self.sending_source_code
        base = {
            "sending_status": False,
            "sending_status_reason": False,
            "sending_source_label": code and Connection._migration_source_label(code),
            "sending_can_carry": False,
            "sending_can_switch_back": False,
        }
        problem = self._sending_problem()
        if problem:
            return dict(base, sending_view=problem[0], sending_banner=problem[1])
        try:
            state, reason = Connection._migration_sending_status(code, self.company_id)
            Connection._migration_sending_source_checked(code)
            counts = Connection._migration_sending_counts(code, self.company_id)
        except UserError as error:
            return dict(base, sending_view="error", sending_banner=str(error))
        if state == NONE:
            return dict(
                base, sending_view="nothing",
                sending_banner=_("No earlier sending found for this company."),
            )
        values = dict(
            base,
            sending_view="found",
            sending_banner=False,
            sending_status=state,
            sending_status_reason=reason or False,
            sending_can_carry=True,
            sending_can_switch_back=state == STOPPED,
        )
        values.update({field: counts.get(key, 0) for key, field in COUNT_FIELDS.items()})
        return values

    def _sending_problem(self):
        """``(view, banner)`` when the step cannot show a source, else None."""
        connection = self.connection_id
        if not connection or not connection.sudo().transfer_id:
            return "needs_connection", _(
                "Carry over the connection first: the sending step needs this company's "
                "APIX connection (the receiving carry-over) before it carries sending over."
            )
        if self.sending_source_code:
            return None
        Connection = self.env["goodahead.apix.connection"]
        found, _problems = Connection._migration_detect_all(self.company_id)
        sending = Connection._migration_sending_sources()
        unknown = [code for code in found if code not in sending]
        if unknown:
            return "no_adapter", _(
                "No migration adapter exists for %s — nothing carried over.",
                ", ".join(Connection._migration_source_label(code) for code in unknown),
            )
        return "nothing", _("No earlier sending found for this company.")

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------

    def action_open_sending(self):
        return self._reopen("sending")

    def action_sending_carry_over(self):
        self.ensure_one()
        problem = self._sending_problem()
        if problem:
            raise UserError(problem[1])
        Connection = self.env["goodahead.apix.connection"]
        result = Connection._migration_sending_carry_over(
            self.sending_source_code, self.company_id
        )
        message = _(
            "Carried over — earlier sending stopped for %s. You can now switch sending on.",
            self.company_id.name,
        )
        not_carried = self.env["res.partner"].sudo().browse(result["not_carried"])
        if not not_carried:
            self.write({"sending_result": "done", "sending_result_message": message})
            return self._reopen("sending")
        self.write({
            "sending_result": "partial",
            "sending_result_message": "\n".join([
                message,
                _(
                    "%(count)s customers not carried — their operator is not on the operator "
                    "list: %(names)s",
                    count=len(not_carried),
                    names=", ".join(not_carried.mapped("display_name")),
                ),
            ]),
        })
        return self._reopen("sending")

    def action_sending_switch_back(self):
        self.ensure_one()
        Connection = self.env["goodahead.apix.connection"]
        try:
            Connection._migration_sending_switch_back(self.sending_source_code, self.company_id)
        except MigrationSendingRefused:
            self.write({
                "sending_result": "refused",
                "sending_result_message": _(
                    "Switch sending off for %s first: a company never has two senders.",
                    self.company_id.name,
                ),
            })
            return self._reopen("sending")
        self.write({
            "sending_result": "switched_back",
            "sending_result_message": _(
                "%(source)s sends again for %(company)s. Sending through this add-on stays "
                "refused until you carry over again.",
                source=Connection._migration_source_label(self.sending_source_code),
                company=self.company_id.name,
            ),
        })
        return self._reopen("sending")

    def action_open_sending_settings(self):
        """SCR-09: the connection's sending settings."""
        self.ensure_one()
        self.env["goodahead.apix.connection"]._migration_check_admin()
        return {
            "type": "ir.actions.act_window",
            "name": _("APIX connection"),
            "res_model": "goodahead.apix.connection",
            "res_id": self.connection_id.id,
            "view_mode": "form",
            "views": [(False, "form")],
            "target": "current",
        }
