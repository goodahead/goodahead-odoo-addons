# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Seed `goodahead.partner.identifier` from the contact book on upgrade.

🔴 WITHOUT THIS THE FEATURE SHIPS DEAD ON EVERY DATABASE THAT ALREADY HAS THE
MODULE. 19.0.3.0.0 matches a vendor on a normalised phone and on an email or
website domain, and both read `goodahead.partner.identifier`. On the morning of
the upgrade that table is empty, so every vendor with `supplier_rank > 0`
carries no identifiers, every new step declines, and the import duplicates vendors
exactly as before the release — with new code in place, a green suite and nothing
reporting a fault. That is the worst shape a defect can take, so the seed is
part of the release rather than an operational step somebody remembers.

The companion `post_init_hook` covers the OTHER case, a fresh install into an
existing database (a reference database, every test copy). A migration script does not
run on an install and a hook does not run on an upgrade, so both exist. The seed
is idempotent, so a database that somehow gets both is unharmed.

⚠️ IT ONLY ADDS. A partner whose phone was corrected since keeps the old
identifier as well — see `res.partner._ai_add_contact_identifiers` for why that
is the point rather than an oversight.
"""

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})
    env["goodahead.partner.identifier"]._seed_from_partners(
        source="seeded from res.partner (19.0.3.0.0)"
    )
