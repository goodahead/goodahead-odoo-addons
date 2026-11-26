# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Hand an inbound alias an older version shipped over to its operator.

This module ships no `mail.alias`: whoever runs it creates the inbound address
by hand. An older fork did ship one, `mail_alias_ai_invoice_import`, under
`noupdate`, so on a database upgraded from it the xmlid outlives the file and
the module still appears to own the address.

That address may carry a deployment's live intake. Deleting it would stop the
intake, so only the xmlid goes: the alias stays as
it is, owned by nobody, exactly like one created by hand. A database that never
had the row (the US deployment) is untouched.
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
