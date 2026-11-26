# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Remove a `disable_pdf_in_xml = True` written on a core that cannot read it.

An earlier version wrote the parameter unconditionally. On a core that does not
read the key the row only misleads, so
`account.move._ai_clear_stray_substitute_pdf_param` resets it (to core's
default when the record is owned by an xmlid). On a core that does read the key
nothing is changed.
"""

import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})
    outcome = env["account.move"]._ai_clear_stray_substitute_pdf_param()
    _logger.info(
        "goodahead_invoice_ai_import 19.0.3.10.0: stray substitute-PDF "
        "parameter -> %s",
        outcome,
    )
