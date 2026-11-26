# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Name the AI as the reader of the import records it has always read.

Since `goodahead_invoice_import` 19.0.1.4.0 the import job dispatches on the
record's stored `reader` instead of on whichever add-on overrode the job hook
last. Records from before have no reader. Every one of them with a PDF that
came by upload or email was read — or is waiting to be read — by the AI, so it
gets `ai`. A Finvoice record (`finvoice_attachment_id` set) is left alone:
its PDF is the invoice image, and the Finvoice add-on's own migration names it.

A record left empty (no PDF, e.g. a mail that carried none) is still claimed
at run time: the core asks the installed readers, and the AI claims upload and
email records, as before.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute(
        """
        SELECT 1
          FROM information_schema.columns
         WHERE table_name = 'goodahead_invoice_import'
           AND column_name = 'finvoice_attachment_id'
        """
    )
    not_finvoice = "AND finvoice_attachment_id IS NULL" if cr.fetchone() else ""
    cr.execute(
        f"""
        UPDATE goodahead_invoice_import
           SET reader = 'ai'
         WHERE reader IS NULL
           AND pdf_attachment_id IS NOT NULL
           AND source IN ('upload', 'email')
           {not_finvoice}
        """
    )
    _logger.info(
        "goodahead_invoice_ai_import 19.0.4.1.0: %s import record(s) given the "
        "reader 'ai'.",
        cr.rowcount,
    )
