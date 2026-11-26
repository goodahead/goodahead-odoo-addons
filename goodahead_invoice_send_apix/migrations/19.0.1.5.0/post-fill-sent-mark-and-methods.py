# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Post-migrate of goodahead_invoice_send_apix 19.0.1.5.0 (AC-61, AC-62;
promoted from the feature's staged migrations 02 and 03).

A fresh install runs the ``post_init_hook``; a database that installed
19.0.1.4.0 (the columns, but no hook yet) gets the same one-time fill here.
No send door exists before 19.0.1.5.0, so the ERP's sent mark still means
"sent before sending was installed". Set-based SQL, nothing else produced.
"""

from odoo.addons.goodahead_invoice_send_apix.hooks import (
    backfill_transmit_method,
    mark_sent_before_install,
)


def migrate(cr, version):
    if not version:
        return
    mark_sent_before_install(cr)
    backfill_transmit_method(cr)
