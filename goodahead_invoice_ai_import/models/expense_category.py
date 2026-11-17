# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ExpenseCategory(models.Model):
    """What kind of spend a bill line is, and which account it posts to.

    Categories are shared across companies (``company_id`` empty), while the
    account is company-dependent, so companies with different charts of
    accounts classify against the same codes.
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

        The SQL constraint cannot catch this: NULL ``company_id`` values never
        collide, so shared rows and company rows shadowing them slip through.
        """
        for category in self:
            if not category.code:
                continue
            domain = [
                ("code", "=", category.code),
                ("id", "!=", category.id),
            ]
            if category.company_id:
                domain += [("company_id", "in", [False, category.company_id.id])]
            # A shared row is visible to every company, so it may collide with
            # nothing.
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
        """Return the shared categories plus ``company``'s own."""
        return self.search(
            ["|", ("company_id", "=", False), ("company_id", "=", company.id)]
        )
