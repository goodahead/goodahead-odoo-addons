# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""SCR-05 entry point in the Invoicing settings: the company's APIX status
and a button opening its connection. Credentials are never settings fields,
so they never travel through `res.config.settings`."""

from odoo import _, api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    apix_connection_id = fields.Many2one(
        "goodahead.apix.connection",
        string="APIX connection",
        compute="_compute_apix_connection_id",
    )
    apix_status = fields.Selection(related="apix_connection_id.status", string="APIX status")
    apix_status_message = fields.Char(related="apix_connection_id.status_message", string="APIX status message")

    @api.depends("company_id")
    def _compute_apix_connection_id(self):
        Connection = self.env["goodahead.apix.connection"]
        for settings in self:
            settings.apix_connection_id = Connection.search([("company_id", "=", settings.company_id.id)], limit=1)

    def action_open_apix_connection(self):
        self.ensure_one()
        action = {
            "type": "ir.actions.act_window",
            "name": _("APIX connection"),
            "res_model": "goodahead.apix.connection",
            "view_mode": "form",
            "target": "current",
        }
        if self.apix_connection_id:
            action["res_id"] = self.apix_connection_id.id
            return action
        action["context"] = {
            "default_company_id": self.company_id.id,
            "default_business_id": self.company_id.company_registry or "",
        }
        return action
