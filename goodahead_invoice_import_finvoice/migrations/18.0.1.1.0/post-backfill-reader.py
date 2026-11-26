# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Name Finvoice as the reader of the records T9 created (T9b).

Since `goodahead_invoice_import` 19.0.1.4.0 the import job dispatches on the
record's stored `reader`. A record with a Finvoice message is a Finvoice
record, whatever else it carries — set unconditionally, so it wins whichever
of this and the AI add-on's backfill runs first.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """
        UPDATE goodahead_invoice_import
           SET reader = 'finvoice'
         WHERE finvoice_attachment_id IS NOT NULL
           AND reader IS DISTINCT FROM 'finvoice'
        """
    )
    _logger.info(
        "goodahead_invoice_import_finvoice 19.0.1.1.0: %s import record(s) given "
        "the reader 'finvoice'.",
        cr.rowcount,
    )
