# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    # 🔴 `claude_api_key` is NOT declared here, and must not be.
    #
    # `goodahead_claude` already declares it on `res.company` with
    # `groups="base.group_system"`. The source module declared its own with
    # `groups="account.group_account_manager"`, and two modules declaring one
    # field with different `groups=` is NOT an error in Odoo — the later module
    # in load order simply wins, so which guard applies would have depended on
    # dependency ordering rather than on anybody's decision. `base.group_system`
    # was chosen (goodahead_claude's spelling), so the duplicate
    # declaration is deleted rather than reconciled.
    #
    # Anything here needing the key reads `company.sudo().claude_api_key`, or
    # better, lets `goodahead.claude.client` read it.

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
