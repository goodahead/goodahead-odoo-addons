# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Seed `goodahead.partner.identifier` from the contact book on upgrade.

Vendor matching by phone and email/website domain reads this table; without a
seed it is empty after the upgrade and every such match fails silently. Fresh
installs are seeded by `post_init_hook`. The seed is idempotent and only adds.
"""

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})
    env["goodahead.partner.identifier"]._seed_from_partners(
        source="seeded from res.partner (19.0.3.0.0)"
    )
