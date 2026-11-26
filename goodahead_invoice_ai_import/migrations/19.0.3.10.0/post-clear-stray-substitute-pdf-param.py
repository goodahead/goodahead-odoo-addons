# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Undo the `disable_pdf_in_xml = True` that 19.0.3.1.0 wrote on a core that
cannot read it.

🔴 WHY A SECOND MIGRATION AND NOT JUST THE GATE. Gating 19.0.3.1.0 stops the
stray row being written from now on; it does nothing for a database where it has
ALREADY been written, and a migration script never runs twice. *(measured
on a legacy-core test copy)*: upgrading this
module from an earlier fork at 19.0.1.1.3 ran 19.0.3.1.0 and left
`account_edi_ubl_cii.disable_pdf_in_xml = True` on a core that has never heard of
the key. Any deployment that upgraded through 19.0.3.1.0 before 19.0.3.10.0 is in
that state, so the repair has to be its own step.

The rule it applies is narrow and lives in
`account.move._ai_clear_stray_substitute_pdf_param`: only on a core that does not
read the key, only a truthy value (which is the only thing this module writes),
and a record owned by an xmlid is reset to core's shipped `False` rather than
unlinked — a dangling `ir_model_data` row is the shape that cost an earlier fix a whole
cutover `-u`.

⚠️ ON A CORE THAT DOES READ THE KEY THIS DOES NOTHING. There the value is a real
decision — including an administrator's deliberate `False` — and a module that
re-asserted its own preference at every upgrade would be overruling the person
who runs it.
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
