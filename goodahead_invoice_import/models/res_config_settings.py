# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from odoo import fields, models

from .partner_identifier import PUBLIC_EMAIL_DOMAINS_PARAM


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # 🔴 `Char`, NOT `Text`, AND THAT IS NOT A STYLE CHOICE. `res.config.settings`
    # accepts `config_parameter` only on boolean / integer / float / char /
    # selection / many2one / datetime (`base/models/res_config.py:222`); a `Text`
    # field raises `Exception("Field ... must have type ...")` the moment anybody
    # OPENS the settings page — not at install, not in any gate, only in the
    # browser. The multi-line editing a list wants is bought with
    # `widget="text"` on the view instead.
    #
    # 🔴 NOT A COMPANY FIELD. The table it guards has no `company_id` and the
    # lookup is not company-scoped, so two companies with two lists would be two
    # rules over one shared set of identifier rows — see
    # `goodahead.partner.identifier._non_identifying_domains`.
    #
    # ⚠️ THE FIELD NAME STILL SAYS "public email domains" AND THE LABEL DOES
    # NOT. The field is bound to `PUBLIC_EMAIL_DOMAINS_PARAM`, and that key
    # holds operator edits, so it cannot be renamed without abandoning them
    # (see the note on the constant). What the list actually holds is wider
    # than free providers — placeholders and near-misses are in it too — so the
    # label, the help and the methods carry the correction and the key does not.
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
