# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Pre-migrate of goodahead_invoice_import_apix 19.0.1.8.0 (send ADR-0001).

The `root.apix` job channel now belongs to goodahead_apix (its 19.0.1.4.0
pre-migrate moves the external id). This is the same move for an upgrade of
receiving alone, when goodahead_apix is not upgraded in the same run: without
it the channel's external id, no longer in receiving's data, would be deleted
with the channel at the end of the upgrade, and the job functions' new
`goodahead_apix.channel_apix` reference would not resolve.

Rules (as goodahead_apix's script):
- fresh install (no version)                  -> nothing to move
- external id already owned by goodahead_apix -> nothing to move
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
