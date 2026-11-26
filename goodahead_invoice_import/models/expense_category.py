# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ExpenseCategory(models.Model):
    """What kind of spend a bill line is, and which account it posts to.

    🔴 MULTI-COMPANY BY DESIGN, because the two companies have DIFFERENT CHARTS
    OF ACCOUNTS. This is the shape that follows from that, and it is worth
    stating because the obvious alternative is wrong:

    * The CATEGORY is shared. "Shipping & Transport" means the same thing to
      Alpha LLC and to Beta Inc., and Claude classifies a line
      against a code, not against an account. A `company_id` of False means
      "available to every company", which is how the shipped rows are loaded.
    * The ACCOUNT is per company, through `company_dependent=True`. One category
      row therefore holds a different `account_id` in each company, resolved by
      `with_company()`. Nothing has to be duplicated and the codes cannot drift
      apart.

    ⚠️ The previous shape made `company_id` required and defaulted it to
    `env.company`, so the thirteen shipped categories landed on whichever
    company happened to install the module. The other company then had NO
    categories: `cat_list` came out empty, the prompt silently fell back to its
    hardcoded list, and every line produced a chatter warning. That is the
    failure this design removes — and where it can still occur (a category with
    no account IN THIS COMPANY) the warning now names the company, because
    "no account configured" was indistinguishable from "wrong company".
    """

    _name = "goodahead.expense.category"
    _description = "AI Invoice Import Expense Category"
    _order = "code"

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True)
    account_id = fields.Many2one(
        "account.account",
        string="Expense Account",
        company_dependent=True,
        help="The account bill lines in this category post to. This is stored "
        "PER COMPANY: set it once while in each company, and each company's "
        "chart of accounts gets its own answer.",
    )
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        help="Leave empty to make this category available to every company — "
        "which is what the shipped categories do. Set it only for a category "
        "that genuinely exists in one company and not the other.",
    )
    active = fields.Boolean(default=True)

    _code_company_uniq = models.Constraint(
        "UNIQUE(code, company_id)",
        "Category code must be unique per company.",
    )

    @api.constrains("code", "company_id", "active")
    def _check_code_is_unambiguous(self):
        """One code must resolve to one category for a given company.

        🔴 The SQL constraint above CANNOT see this case. In PostgreSQL two rows
        with the same `code` and a NULL `company_id` do not collide, because
        NULL is not equal to NULL — so the shipped shared categories are not
        protected by it at all, and neither is a company-specific row that
        shadows a shared code. Both would silently give `_apply_expense_categories`
        two candidates and let `limit=1` pick one by id order.
        """
        for category in self:
            if not category.code:
                continue
            domain = [
                ("code", "=", category.code),
                ("id", "!=", category.id),
            ]
            if category.company_id:
                # A company row may not collide with another row of the same
                # company, nor with a shared row it would shadow.
                domain += [("company_id", "in", [False, category.company_id.id])]
            # A shared row may not collide with anything at all: every company
            # can see it.
            if self.search_count(domain, limit=1):
                raise ValidationError(
                    _(
                        "The expense category code '%s' is already in use by a "
                        "category visible to the same company. Codes must "
                        "resolve to exactly one category, because that is what "
                        "the AI extraction returns.",
                        category.code,
                    )
                )

    @api.model
    def _for_company(self, company):
        """The categories a bill in ``company`` may be classified into: the
        shared ones plus that company's own."""
        return self.search(
            ["|", ("company_id", "=", False), ("company_id", "=", company.id)]
        )
