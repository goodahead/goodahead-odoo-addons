# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
from odoo import fields, models

from .partner_identifier import PUBLIC_EMAIL_DOMAINS_PARAM


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # The API key and the connection/model buttons live in `goodahead_claude`.

    claude_invoice_model_id = fields.Many2one(
        related="company_id.claude_invoice_model_id",
        readonly=False,
    )
    ai_invoice_prompt_profile_id = fields.Many2one(
        related="company_id.ai_invoice_prompt_profile_id",
        readonly=False,
    )

    # Must be Char: `config_parameter` rejects Text fields when the settings
    # page opens. Multi-line editing comes from `widget="text"` in the view.
    # Deliberately global, not per company: the identifier table it guards is
    # not company-scoped.
    ai_public_email_domains = fields.Char(
        string="Domains That Never Identify a Vendor",
        config_parameter=PUBLIC_EMAIL_DOMAINS_PARAM,
        help=(
            "Domains that must never identify a vendor — free mail providers, "
            "and placeholder domains an import writes — in addition to the "
            "list that ships with this module (gmail.com, suomi24.fi, "
            "online.no, emaildomain.com, …). Separate entries with commas, "
            "spaces or new lines.\n\n"
            "Leaving this empty keeps the shipped list in force — it does NOT "
            "switch the protection off.\n\n"
            "WARNING: an entry written with a leading minus, such as "
            "-gmail.com, REMOVES that provider from the shipped list. Every "
            "contact on that provider then becomes matchable by its email "
            "domain, and if exactly one of them can receive bills, every "
            "invoice printing an address there will be filed against that one "
            "company. Removals are written to the server log. Add, do not "
            "remove, unless you have checked who holds that domain."
        ),
    )
