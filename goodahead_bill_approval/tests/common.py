# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Shared harness: a draft vendor bill and four users with different approval levels.

Uses the existing company, journals and chart rather than `AccountTestInvoicingCommon`,
whose own company setup can fail on multi-company databases with a shared dropship route.

A `TransactionCase` runs as superuser and the per-level guards skip `env.su`, so every
level assertion goes through `with_user(...)`. The `_post` gate has no `env.su` bypass
and holds in these tests as it does in production.
"""

from odoo import Command, fields
from odoo.tests import TransactionCase

GROUP_1 = 'goodahead_bill_approval.group_bill_approval_1'
GROUP_2 = 'goodahead_bill_approval.group_bill_approval_2'


class BillApprovalCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.AM = cls.env['account.move']

        Journal = cls.env['account.journal']
        cls.purchase_journal = Journal.search([
            ('type', '=', 'purchase'), ('company_id', '=', cls.company.id),
        ], limit=1)
        cls.sale_journal = Journal.search([
            ('type', '=', 'sale'), ('company_id', '=', cls.company.id),
        ], limit=1)
        cls.misc_journal = Journal.search([
            ('type', '=', 'general'), ('company_id', '=', cls.company.id),
        ], limit=1)
        assert cls.purchase_journal and cls.sale_journal and cls.misc_journal, \
            "this database must already carry a purchase, a sale and a misc journal"

        Account = cls.env['account.account']
        cls.expense_account = Account.search([
            ('account_type', '=', 'expense'), ('company_ids', 'in', cls.company.id),
        ], limit=1)
        cls.income_account = Account.search([
            ('account_type', '=', 'income'), ('company_ids', 'in', cls.company.id),
        ], limit=1)
        assert cls.expense_account and cls.income_account

        cls.vendor = cls.env['res.partner'].create({
            'name': 'ZZ BA Vendor',
            'is_company': True,
        })
        cls.customer = cls.env['res.partner'].create({
            'name': 'ZZ BA Customer',
            'is_company': True,
        })

        base_groups = [
            Command.link(cls.env.ref('base.group_user').id),
            Command.link(cls.env.ref('account.group_account_invoice').id),
            Command.link(cls.env.ref('account.group_account_user').id),
        ]
        cls.user_none = cls.env['res.users'].create({
            'name': 'BA no level',
            'login': 'ba_none',
            'email': 'ba_none@example.com',
            'company_id': cls.company.id,
            'company_ids': [Command.set([cls.company.id])],
            'groups_id': base_groups,
        })
        cls.user_reviewer = cls.env['res.users'].create({
            'name': 'BA bookkeeper',
            'login': 'ba_reviewer',
            'email': 'ba_reviewer@example.com',
            'company_id': cls.company.id,
            'company_ids': [Command.set([cls.company.id])],
            'groups_id': base_groups + [Command.link(cls.env.ref(GROUP_1).id)],
        })
        cls.user_director = cls.env['res.users'].create({
            'name': 'BA financial director',
            'login': 'ba_director',
            'email': 'ba_director@example.com',
            'company_id': cls.company.id,
            'company_ids': [Command.set([cls.company.id])],
            'groups_id': base_groups + [Command.link(cls.env.ref(GROUP_2).id)],
        })
        cls.user_both = cls.env['res.users'].create({
            'name': 'BA both levels',
            'login': 'ba_both',
            'email': 'ba_both@example.com',
            'company_id': cls.company.id,
            'company_ids': [Command.set([cls.company.id])],
            'groups_id': base_groups + [
                Command.link(cls.env.ref(GROUP_1).id),
                Command.link(cls.env.ref(GROUP_2).id),
            ],
        })

    # --------------------------------------------------------------- factories

    @classmethod
    def _create_invoice(cls, move_type='in_invoice', ref=None, price_unit=100.0,
                        partner=None):
        """A postable one-line invoice of any type, with an explicit account."""
        purchase = move_type in ('in_invoice', 'in_refund', 'in_receipt')
        return cls.AM.create({
            'move_type': move_type,
            'journal_id': (cls.purchase_journal if purchase else cls.sale_journal).id,
            'partner_id': (partner or (cls.vendor if purchase else cls.customer)).id,
            'invoice_date': fields.Date.to_date('2026-10-01'),
            'date': fields.Date.to_date('2026-10-01'),
            'ref': ref,
            'invoice_line_ids': [Command.create({
                'name': 'BA line',
                'quantity': 1.0,
                'price_unit': price_unit,
                'account_id': (cls.expense_account if purchase
                               else cls.income_account).id,
                'tax_ids': [Command.set([])],
            })],
        })

    @classmethod
    def _bill(cls, move_type='in_invoice', **kwargs):
        return cls._create_invoice(move_type=move_type, **kwargs)

    @classmethod
    def _approve_fully(cls, move):
        """Grant both approvals directly, where approval is only a precondition.

        This relies on `cls.env` being superuser: the ORM guard refuses non-su writes
        of approval fields (see `test_orm_guard.py`).
        """
        move.write({
            'approval_1_uid': cls.env.user.id,
            'approval_1_date': fields.Datetime.now(),
            'approval_2_uid': cls.env.user.id,
            'approval_2_date': fields.Datetime.now(),
        })
        return move
