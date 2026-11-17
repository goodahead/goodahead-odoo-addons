# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Release ownership of an inbound mail alias shipped by an earlier fork of this
module.

This module ships no `mail.alias`, but an earlier fork of it did, under
`noupdate`, so on databases upgraded from it the xmlid survives. The alias may
be in live use, so only the xmlid is removed and the alias itself is kept, as
if it had been created by hand.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute(
        """
        DELETE FROM ir_model_data
         WHERE module = 'goodahead_invoice_ai_import'
           AND model = 'mail.alias'
     RETURNING name, res_id
        """
    )
    for name, res_id in cr.fetchall():
        _logger.info(
            "goodahead_invoice_ai_import 19.0.3.32.0: released xmlid %s; "
            "mail.alias %s is kept and is no longer owned by this module.",
            name,
            res_id,
        )
