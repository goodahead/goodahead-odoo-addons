# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ResCompany(models.Model):
    _inherit = "res.company"

    claude_api_key = fields.Char(
        string="Claude API Key",
        groups="base.group_system",
        copy=False,
        help="Anthropic API key. Generate at https://console.anthropic.com → "
             "Settings → API Keys.",
    )
    claude_default_model_id = fields.Many2one(
        comodel_name="goodahead.claude.model",
        string="Default Claude Model",
        help="Default model used when callers don't specify one explicitly. "
             "Individual features may override this.",
    )
    claude_max_output_tokens = fields.Integer(
        string="Claude Max Output Tokens",
        default=4096,
        help="Default cap on tokens Claude may emit per request. Callers may "
             "override per-request.",
    )

    @api.constrains("claude_max_output_tokens")
    def _check_claude_max_output_tokens(self):
        for company in self:
            if company.claude_max_output_tokens <= 0:
                raise ValidationError(_(
                    "Claude Max Output Tokens must be a positive integer."
                ))
