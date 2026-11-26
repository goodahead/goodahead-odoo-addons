# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Pre-migrate of goodahead_apix 19.0.1.4.0 (send SAD §5, ADR-0001; promoted
from the feature's staged migration 01).

The `root.apix` job channel moves down from goodahead_invoice_import_apix to
the connection add-on, so the sending add-on can hang `root.apix.send` under
it without depending on receiving. Without this script goodahead_apix would
load a second `apix` channel under root and hit the channel name constraint.

The channel record lives in goodahead_apix/data/queue_job_data.xml and the
receiving job functions point at `goodahead_apix.channel_apix`. Staged as
19.0.1.3.0; that version had already shipped (the APIX client's sending
calls), so the script runs at 19.0.1.4.0 to reach databases at 19.0.1.3.0.
goodahead_invoice_import_apix 19.0.1.8.0 carries the same move for an
upgrade of receiving alone.

Rules:
- fresh install of goodahead_apix (no version) -> nothing to move
- receiving never installed (no external id)    -> nothing to move
- external id already owned by goodahead_apix   -> nothing to move
"""

OLD_MODULE = "goodahead_invoice_import_apix"
NEW_MODULE = "goodahead_apix"
XMLID_NAME = "channel_apix"


def move_ownership(cr):
    cr.execute(
        """
        UPDATE ir_model_data
           SET module = %s
         WHERE module = %s
           AND name = %s
           AND model = 'queue.job.channel'
           AND NOT EXISTS (
               SELECT 1 FROM ir_model_data
                WHERE module = %s AND name = %s
           )
        """,
        (NEW_MODULE, OLD_MODULE, XMLID_NAME, NEW_MODULE, XMLID_NAME),
    )


def migrate(cr, version):
    if not version:
        return
    move_ownership(cr)
