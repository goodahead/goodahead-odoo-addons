# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    # `claude_api_key` is declared by `goodahead_claude` (system group only);
    # do not redeclare it here, or the groups would depend on load order.

    claude_invoice_model_id = fields.Many2one(
        "goodahead.claude.model",
        string="Claude Invoice Model",
        help="Model used for invoice extraction, overriding the company's "
        "default Claude model. Leave empty to use the company default "
        "(Settings > Claude), which falls back to the newest model fetched "
        "by \"Sync Models\". Invoice extraction sends a PDF, so the model "
        "chosen here must be one with PDF support.",
    )
    ai_invoice_prompt_profile_id = fields.Many2one(
        "goodahead.invoice.prompt.profile",
        string="Extraction Prompt",
        help="The locale wording the extraction prompt is composed with. Leave "
        "empty to choose it from the company's country: the United States "
        "gets US, EU and EEA countries get EU, and any other country gets the "
        "country-neutral wording.",
    )
