# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # 🔴 Neither `claude_api_key` nor the "Test Connection" / "Sync Models"
    # buttons are here any more.
    #
    # `goodahead_claude` already exposes all three in its own settings block,
    # against the same key and the same `goodahead.claude.model` registry. The
    # source module carried its own copies because it had its own client; in
    # this tree a second copy would be two buttons that look identical, talk to
    # two different code paths and drift apart — which is exactly how the stale
    # `DEFAULT_MODEL` survived on the source project.
    #
    # What stays is the one genuinely per-feature setting: which model invoice
    # extraction should use, overriding the company default.

    claude_invoice_model_id = fields.Many2one(
        related="company_id.claude_invoice_model_id",
        readonly=False,
    )
    ai_invoice_prompt_profile_id = fields.Many2one(
        related="company_id.ai_invoice_prompt_profile_id",
        readonly=False,
    )
