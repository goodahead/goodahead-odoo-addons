# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""SCR-07 — the migration tool. A thin screen over the connection's
`_migration_*` methods; administrators only, one company at a time. When
several migration sources have credentials for the company, the
Administrator picks one; normally there is one. 🔴 It never holds the
transfer key nor any web password: the carry-over reads the credentials
again from the source."""

from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.apix_connection import SHADOW_MIN_DAYS

STEPS = [
    ("detect", "Detect"),
    ("shadow", "Shadow run"),
    ("live", "Live"),
    ("switch_back", "Switch back"),
]


class GoodaheadApixMigration(models.TransientModel):
    _name = "goodahead.apix.migration"
    _description = "Migration from an earlier APIX connector"

    company_id = fields.Many2one(
        "res.company",
        required=True,
        readonly=True,
        default=lambda self: self.env.company,
    )
    step = fields.Selection(STEPS, required=True, default="detect")
    connection_id = fields.Many2one("goodahead.apix.connection", compute="_compute_connection_id")
    connection_status = fields.Selection(related="connection_id.status")
    connection_status_message = fields.Char(related="connection_id.status_message")
    receiving_since = fields.Datetime(related="connection_id.receiving_state_since")

    # Step 1: which earlier connector, and what it has for this company.
    # 🔴 Never the transfer key nor a web password.
    source_code = fields.Selection(
        selection="_selection_source_code",
        string="Migrate from",
        help="The earlier APIX connector whose transfer credentials are carried over.",
    )
    source_count = fields.Integer(compute="_compute_source", compute_sudo=True)
    source_found = fields.Boolean(compute="_compute_source", compute_sudo=True)
    source_environment = fields.Char(compute="_compute_source", compute_sudo=True)
    source_business_id = fields.Char(compute="_compute_source", compute_sudo=True)
    source_message = fields.Char(compute="_compute_source", compute_sudo=True)

    # Step 3: shadow results.
    count_would_import = fields.Integer(compute="_compute_counts")
    count_would_duplicate = fields.Integer(compute="_compute_counts")
    count_imported_before = fields.Integer(compute="_compute_counts")
    count_would_be_possibly_lost = fields.Integer(compute="_compute_counts")
    count_would_fail = fields.Integer(compute="_compute_counts")
    count_unaccounted = fields.Integer(compute="_compute_counts")
    shadow_days = fields.Integer(compute="_compute_counts")
    shadow_ready = fields.Boolean(compute="_compute_counts")

    # Step 4 / switch back.
    unconfirmed_count = fields.Integer(compute="_compute_switch_back")
    unsettled_ids = fields.Many2many("goodahead.invoice.import", compute="_compute_switch_back")

    @api.model
    def _selection_source_code(self):
        Connection = self.env["goodahead.apix.connection"]
        return [
            (code, Connection._migration_source_label(code))
            for code in Connection._migration_sources()
        ]

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        Connection = self.env["goodahead.apix.connection"]
        Connection._migration_check_admin()
        company = self.env["res.company"].browse(values.get("company_id") or self.env.company.id)
        Connection._migration_check_company(company)
        connection = Connection.sudo().search([("company_id", "=", company.id)], limit=1)
        if "step" in fields_list and not self.env.context.get("default_step"):
            state = connection.receiving_state
            values["step"] = state if state in ("shadow", "live") else "detect"
        if "source_code" in fields_list and not values.get("source_code"):
            values["source_code"] = self._default_source_code(company, connection)
        return values

    @api.model
    def _default_source_code(self, company, connection):
        """The source the connection was migrated from, else the first one
        with credentials for the company, else the only installed one."""
        Connection = self.env["goodahead.apix.connection"]
        sources = Connection._migration_sources()
        if connection.migrated_from in sources:
            return connection.migrated_from
        found, _problems = Connection._migration_detect_all(company)
        if found:
            return next(iter(found))
        return sources[0] if len(sources) == 1 else False

    @api.depends("company_id")
    def _compute_connection_id(self):
        Connection = self.env["goodahead.apix.connection"].sudo()
        for wizard in self:
            wizard.connection_id = Connection.search(
                [("company_id", "=", wizard.company_id.id)], limit=1
            )

    @api.depends("company_id", "source_code")
    def _compute_source(self):
        Connection = self.env["goodahead.apix.connection"]
        for wizard in self:
            found, problems = Connection._migration_detect_all(wizard.company_id)
            detected = found.get(wizard.source_code) if wizard.source_code else None
            message = problems.get(wizard.source_code) or Connection._migration_problem()
            wizard.source_count = len(found)
            wizard.source_found = bool(detected)
            wizard.source_environment = (detected or {}).get("environment") or False
            wizard.source_business_id = (detected or {}).get("business_id") or False
            wizard.source_message = message or False

    @api.depends("company_id", "step")
    def _compute_counts(self):
        now = fields.Datetime.now()
        for wizard in self:
            connection = wizard.connection_id
            counts = connection._migration_shadow_counts() if connection else {}
            for key in (
                "would_import",
                "would_duplicate",
                "imported_before",
                "would_be_possibly_lost",
                "would_fail",
                "unaccounted",
            ):
                wizard[f"count_{key}"] = counts.get(key, 0)
            since = (
                connection.receiving_state_since
                if connection.receiving_state == "shadow"
                else False
            )
            wizard.shadow_days = (now - since).days if since else 0
            wizard.shadow_ready = bool(
                since
                and now - since >= timedelta(days=SHADOW_MIN_DAYS)
                and not counts.get("unaccounted")
            )

    @api.depends("company_id", "step")
    def _compute_switch_back(self):
        for wizard in self:
            connection = wizard.connection_id
            if not connection:
                wizard.unconfirmed_count = 0
                wizard.unsettled_ids = False
                continue
            wizard.unconfirmed_count = len(connection._migration_unconfirmed())
            wizard.unsettled_ids = connection._migration_unsettled()

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------

    def _reopen(self, step=None):
        self.ensure_one()
        if step:
            self.step = step
        return {
            "type": "ir.actions.act_window",
            "name": _("APIX migration tool"),
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "views": [(False, "form")],
            "target": "new",
        }

    def action_carry_over(self):
        self.ensure_one()
        if not self.source_code:
            raise UserError(_("No earlier APIX connector to migrate from is installed."))
        self.env["goodahead.apix.connection"]._migration_carry_over(
            self.company_id, self.source_code
        )
        return self._reopen("shadow")

    def action_connect_manually(self):
        """Nothing to carry over: set the receiving add-on's own connection
        up."""
        self.ensure_one()
        self.env["goodahead.apix.connection"]._migration_check_admin()
        connection = self.connection_id
        action = {
            "type": "ir.actions.act_window",
            "name": _("APIX connection"),
            "res_model": "goodahead.apix.connection",
            "view_mode": "form",
            "views": [(False, "form")],
            "target": "current",
        }
        if connection:
            action["res_id"] = connection.id
        else:
            action["context"] = {"default_company_id": self.company_id.id}
        return action

    def action_open_shadow_results(self):
        """SCR-01 with the Shadow results filter."""
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "goodahead_invoice_import.action_ai_invoice_import"
        )
        action["context"] = {"search_default_shadow_results": 1, "apix_shadow_results": 1}
        action["domain"] = [("company_id", "=", self.company_id.id)]
        return action

    def action_switch_to_live(self):
        self.ensure_one()
        self.connection_id._migration_switch_to_live()
        return self._reopen("live")

    def action_switch_back(self):
        """Refused while marks are unconfirmed; otherwise shows the
        unsettled packages and asks for confirmation."""
        self.ensure_one()
        self.connection_id._migration_check_switch_back()
        return self._reopen("switch_back")

    def action_cancel_switch_back(self):
        self.ensure_one()
        return self._reopen("live")

    def action_confirm_switch_back(self):
        self.ensure_one()
        self.connection_id._migration_switch_back()
        return self._reopen("detect")
