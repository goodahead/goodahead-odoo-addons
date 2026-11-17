# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Install-time seeding of `goodahead.partner.identifier` and the substitute-PDF
policy. Upgrades are covered by the matching migration scripts.
"""


def post_init_hook(env):
    env["goodahead.partner.identifier"]._seed_from_partners(
        source="seeded from res.partner (install)"
    )

    # Writes nothing on a core that does not read the parameter.
    env["account.move"]._ai_apply_substitute_pdf_policy()
