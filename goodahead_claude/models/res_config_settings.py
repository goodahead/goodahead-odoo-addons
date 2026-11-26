# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
from odoo import _, fields, models
from odoo.exceptions import UserError

from ..lib.claude_client import ClaudeAPIError


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    claude_api_key = fields.Char(related="company_id.claude_api_key", readonly=False)
    claude_default_model_id = fields.Many2one(
        related="company_id.claude_default_model_id", readonly=False
    )
    claude_max_output_tokens = fields.Integer(
        related="company_id.claude_max_output_tokens", readonly=False
    )

    def action_claude_test_connection(self):
        self.ensure_one()
        if not self.claude_api_key:
            raise UserError(_("Please configure the Claude API key first."))
        try:
            self.env["goodahead.claude.client"].test_connection(company=self.company_id)
        except ClaudeAPIError as e:
            raise UserError(_("Claude API connection failed: %s") % e) from e
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Claude"),
                "message": _("Connection successful."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_claude_sync_models(self):
        self.ensure_one()
        if not self.claude_api_key:
            raise UserError(_("Please configure the Claude API key first."))
        self.env["goodahead.claude.client"]._check_claude_admin()
        try:
            result = self.env["goodahead.claude.client"]._sync_models(
                company=self.company_id
            )
        except ClaudeAPIError as e:
            raise UserError(_("Failed to fetch models: %s") % e) from e

        # 🔴 19.0.1.3.0: the sync now retires models, and it says so. A notification that only ever
        # counts up would hide exactly the half of the sync that takes something away — and
        # when what it took away is the model a company is configured to use, the message is
        # made STICKY, because that one needs reading, not glancing at.
        lines = [_("Synced %s models.") % result["synced"]]
        if result["archived"]:
            lines.append(_("Archived %(n)s no longer offered: %(ids)s") % {
                "n": len(result["archived"]),
                "ids": ", ".join(result["archived"]),
            })
        if result["restored"]:
            lines.append(_("Restored %(n)s listed again: %(ids)s") % {
                "n": len(result["restored"]),
                "ids": ", ".join(result["restored"]),
            })
        for pointer in result["pointers"]:
            lines.append(_(
                "⚠ %(record)s still uses the archived model %(api_id)s (%(field)s) — "
                "choose another."
            ) % {
                "record": pointer["record"],
                "api_id": pointer["model_api_id"],
                "field": pointer["field"],
            })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Claude"),
                "message": "\n".join(lines),
                "type": "warning" if result["pointers"] else "success",
                "sticky": bool(result["pointers"]),
            },
        }
