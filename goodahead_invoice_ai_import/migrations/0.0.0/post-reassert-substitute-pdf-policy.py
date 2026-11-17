# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Re-decide core's substitute-PDF switch on every upgrade of this module.

Whether the switch can take effect depends on the core build, which an Odoo
point-upgrade can change without touching this module, so a one-off versioned
migration is not enough. A `0.0.0` folder runs on every upgrade and, in the
`post` stage, after the versioned scripts. The rule itself lives in
`account.move._ai_reassert_substitute_pdf_policy`; an operator's explicit
decision is never overruled.
"""

import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    # Installs are handled by `post_init_hook`.
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})
    outcome = env["account.move"]._ai_reassert_substitute_pdf_policy()
    _logger.info(
        "goodahead_invoice_ai_import: substitute-PDF switch re-decided against "
        "the core running now -> %s",
        outcome,
    )
