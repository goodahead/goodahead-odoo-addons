# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Release the shipped expense categories from whichever company installed them.

🔴 WHY THIS IS NEEDED AND THE DATA FILE IS NOT ENOUGH.

Before 19.0.2.0.0 `goodahead.expense.category.company_id` was `required=True`
with `default=lambda self: self.env.company`, so the thirteen shipped categories
were created against whichever company happened to be active during the install.
The other company then had none at all: the prompt silently fell back to its
hardcoded list and every line produced a chatter warning.

The field is now optional, and `company_id = False` means "available to every
company" — but `data/expense_category.xml` does not MENTION `company_id`, so
re-loading it on upgrade leaves the existing value exactly where it is. An
unmentioned field is not a cleared field. Hence this script.

⚠️ IT TOUCHES ONLY THE RECORDS THIS MODULE SHIPPED, found through `ir_model_data`
rather than by matching on `code`. A category somebody created by hand and
deliberately scoped to one company is theirs, and silently making it visible to
the other company would be a worse bug than the one being fixed.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute(
        """
        UPDATE goodahead_expense_category c
           SET company_id = NULL
          FROM ir_model_data d
         WHERE d.module = 'goodahead_invoice_ai_import'
           AND d.model  = 'goodahead.expense.category'
           AND d.res_id = c.id
           AND c.company_id IS NOT NULL
        """
    )
    shared = cr.rowcount

    # The account was already `company_dependent`, so it lives in
    # `ir_property` / the per-company column and is untouched by the above —
    # whatever account the installing company had set still resolves for that
    # company, and the other company simply has none yet. Report the gap rather
    # than inventing an account: the two companies have different charts, and
    # guessing across them is exactly the mistake this release exists to stop.
    cr.execute(
        """
        SELECT count(*)
          FROM goodahead_expense_category c
          JOIN ir_model_data d
            ON d.res_id = c.id
           AND d.module = 'goodahead_invoice_ai_import'
           AND d.model  = 'goodahead.expense.category'
        """
    )
    total = cr.fetchone()[0]

    _logger.info(
        "AI invoice import: %s of %s shipped expense categories released to all "
        "companies. Each company still needs its own accounts set on them — the "
        "account is per company, and the charts differ.",
        shared,
        total,
    )
