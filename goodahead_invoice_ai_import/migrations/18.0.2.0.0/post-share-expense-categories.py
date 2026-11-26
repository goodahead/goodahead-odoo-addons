# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Make the shipped expense categories available to every company.

`company_id` used to be required and defaulted to the installing company, so
other companies had no categories. The data file does not mention `company_id`,
so reloading it does not clear the value. Only records shipped by this module
(found through `ir_model_data`) are released; hand-made, company-scoped
categories are left alone.
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

    # The account is company-dependent and stays as it was; other companies
    # must set their own, since their charts of accounts differ.
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
