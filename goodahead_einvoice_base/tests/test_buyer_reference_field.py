# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The buyer reference on invoices and bills (finvoice content T07; AC-13,
AC-24). Every name and value is invented."""

import unittest

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo import Command
from odoo.exceptions import ValidationError
from odoo.tests import Form, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

FIELD = "einvoice_buyer_reference"


@tagged("post_install", "-at_install")
class TestBuyerReferenceField(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.customer = cls.env["res.partner"].create({"name": "ZZ Buyer Oy"})

    def _invoice(self, move_type="out_invoice", **vals):
        return self.env["account.move"].create({
            "move_type": move_type,
            "partner_id": self.customer.id,
            "invoice_date": "2026-11-25",
            "invoice_line_ids": [Command.create({
                "name": "ZZ line", "quantity": 1, "price_unit": 10.0, "tax_ids": False,
            })],
            **vals,
        })

    def test_field_is_a_copied_tracked_char_without_silent_cut(self):
        field = self.env["account.move"]._fields[FIELD]
        self.assertEqual(field.type, "char")
        # No ORM size: the ORM would cut a longer value silently (AC-13 refuses it).
        self.assertIsNone(field.size)
        self.assertTrue(field.copy)
        self.assertTrue(field.tracking)
        self.assertFalse(field.compute)
        self.assertFalse(field.default)

    def test_empty_on_a_new_invoice_without_a_value(self):
        self.assertFalse(self._invoice()[FIELD])

    def test_70_characters_fit(self):
        move = self._invoice(**{FIELD: "R" * 70})
        self.assertEqual(move[FIELD], "R" * 70)

    def test_71_characters_are_refused_with_the_maximum(self):
        with self.assertRaisesRegex(ValidationError, "70 characters"):
            self._invoice(**{FIELD: "R" * 71})
        move = self._invoice()
        with self.assertRaisesRegex(ValidationError, "70 characters"):
            move.write({FIELD: "R" * 71})

    def test_kept_on_bills_too(self):
        bill = self._invoice("in_invoice", **{FIELD: "Test User"})
        self.assertEqual(bill[FIELD], "Test User")

    def test_copied_with_the_invoice(self):
        move = self._invoice(**{FIELD: "Cost centre 42"})
        self.assertEqual(move.copy()[FIELD], "Cost centre 42")

    def test_read_only_once_posted_in_the_form(self):
        move = self._invoice(**{FIELD: "Before posting"})
        with Form(move) as form:
            form[FIELD] = "Changed in draft"
        self.assertEqual(move[FIELD], "Changed in draft")
        move.action_post()
        with Form(move) as form, self.assertRaises(AssertionError):
            form[FIELD] = "After posting"

    def test_shown_on_invoices_and_bills(self):
        arch = self.env["account.move"].get_view(
            self.env.ref("account.view_move_form").id, "form")["arch"]
        self.assertIn(f'name="{FIELD}"', arch)

    def test_column_added_empty_for_existing_moves(self):
        # AC-24: no row of account_move carries a value nobody entered.
        self.env.cr.execute(
            f"SELECT count(*) FROM account_move WHERE {FIELD} IS NOT NULL AND {FIELD} != ''"
        )
        self.assertEqual(self.env.cr.fetchone()[0], 0)
