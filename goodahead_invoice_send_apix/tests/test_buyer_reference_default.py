# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The buyer reference starts with the customer's standing value (finvoice
content T10; AC-12, AC-13). Every name and value is invented."""

import unittest

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import Form, tagged

from .common import SendApixCommon

FIELD = "einvoice_buyer_reference"
STANDING = "einvoice_standing_buyer_reference"


@tagged("post_install", "-at_install")
class TestBuyerReferenceDefault(SendApixCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.with_reference = cls._make_customer("ZZ Standing Oy", **{STANDING: "Cost centre 42"})
        cls.contact = cls.env["res.partner"].create({
            "name": "ZZ Invoice Desk", "type": "invoice", "parent_id": cls.with_reference.id,
        })
        cls.own_contact = cls.env["res.partner"].create({
            "name": "ZZ Own Desk", "type": "invoice", "parent_id": cls.with_reference.id,
            STANDING: "Test User",
        })
        cls.without_reference = cls._make_customer("ZZ Plain Oy")
        cls.other_reference = cls._make_customer("ZZ Other Oy", **{STANDING: "Other ref"})

    def _create(self, partner, move_type="out_invoice", **vals):
        return self.env["account.move"].create({
            "move_type": move_type,
            "partner_id": partner.id,
            "invoice_date": "2026-11-25",
            "invoice_line_ids": [Command.create({
                "name": "ZZ line", "quantity": 1, "price_unit": 100.0, "tax_ids": False,
            })],
            **vals,
        })

    # -- the standing value ------------------------------------------------

    def test_standing_value_from_contact_else_commercial(self):
        self.assertEqual(self.own_contact._einvoice_standing_buyer_reference(), "Test User")
        self.assertEqual(self.contact._einvoice_standing_buyer_reference(), "Cost centre 42")
        self.assertIsNone(self.without_reference._einvoice_standing_buyer_reference())

    def test_standing_value_over_70_refused(self):
        with self.assertRaisesRegex(ValidationError, "70 characters"):
            self.without_reference.write({STANDING: "S" * 71})
        self.without_reference.write({STANDING: "S" * 70})

    def test_standing_value_is_tracked(self):
        self.assertTrue(self.env["res.partner"]._fields[STANDING].tracking)

    def test_standing_value_follows_the_contact_rights(self):
        """review F7 (spec §6.1 changed): the standing value is customer
        data, written under the ERP's contact rights only - a billing user
        without them is refused and sets the buyer reference on the invoice;
        a user who may edit contacts sets it."""
        partner = self.without_reference.with_user(self.accountant)
        self.assertFalse(self.accountant.has_group("base.group_partner_manager"))
        with self.assertRaises(AccessError):
            partner.write({STANDING: "Edited by billing"})
        self.assertFalse(self.without_reference[STANDING])
        move = self._create(self.without_reference).with_user(self.accountant)
        move.write({FIELD: "Set on the invoice"})
        self.assertEqual(move[FIELD], "Set on the invoice")
        self.accountant.group_ids += self.env.ref("base.group_partner_manager")
        partner.write({STANDING: "Edited by a contact editor"})
        self.assertEqual(self.without_reference[STANDING], "Edited by a contact editor")

    # -- creation ------------------------------------------------------------

    def test_create_without_a_value_takes_the_standing_one(self):
        self.assertEqual(self._create(self.with_reference)[FIELD], "Cost centre 42")
        self.assertEqual(self._create(self.contact)[FIELD], "Cost centre 42")
        self.assertEqual(self._create(self.own_contact)[FIELD], "Test User")
        self.assertEqual(self._create(self.with_reference, "out_refund")[FIELD],
                         "Cost centre 42")

    def test_create_for_a_customer_without_one_stays_empty(self):
        self.assertFalse(self._create(self.without_reference)[FIELD])

    def test_explicit_value_or_explicit_empty_is_kept(self):
        self.assertEqual(self._create(self.with_reference, **{FIELD: "Typed"})[FIELD], "Typed")
        self.assertFalse(self._create(self.with_reference, **{FIELD: False})[FIELD])

    def test_vendor_bill_never_takes_the_standing_value(self):
        self.assertFalse(self._create(self.with_reference, "in_invoice")[FIELD])

    def test_form_create_starts_with_the_standing_value_and_keeps_a_change(self):
        form = Form(self.env["account.move"].with_context(default_move_type="out_invoice"))
        form.partner_id = self.with_reference
        self.assertEqual(form[FIELD], "Cost centre 42")
        form[FIELD] = "Changed before posting"
        with form.invoice_line_ids.new() as line:
            line.name = "ZZ line"
            line.price_unit = 10.0
        move = form.save()
        self.assertEqual(move[FIELD], "Changed before posting")

    # -- change of customer ------------------------------------------------------

    def test_customer_change_replaces_the_previous_standing_value(self):
        form = Form(self.env["account.move"].with_context(default_move_type="out_invoice"))
        form.partner_id = self.with_reference
        form.partner_id = self.other_reference
        self.assertEqual(form[FIELD], "Other ref")
        form.partner_id = self.without_reference
        self.assertFalse(form[FIELD])

    def test_customer_change_keeps_a_typed_value(self):
        form = Form(self.env["account.move"].with_context(default_move_type="out_invoice"))
        form.partner_id = self.with_reference
        form[FIELD] = "Typed by hand"
        form.partner_id = self.other_reference
        self.assertEqual(form[FIELD], "Typed by hand")

    def test_customer_change_on_a_saved_draft(self):
        move = self._create(self.with_reference)
        with Form(move) as form:
            form.partner_id = self.other_reference
        self.assertEqual(move[FIELD], "Other ref")
        move.write({FIELD: "Typed"})
        with Form(move) as form:
            form.partner_id = self.with_reference
        self.assertEqual(move[FIELD], "Typed")

    # -- reversal ------------------------------------------------------------------

    def test_reversal_copies_the_reversed_invoice_value(self):
        move = self._create(self.with_reference, **{FIELD: "Invoice value"})
        move.action_post()
        self.with_reference.write({STANDING: "Changed since"})
        reversal = move._reverse_moves()
        self.assertEqual(reversal.move_type, "out_refund")
        self.assertEqual(reversal[FIELD], "Invoice value")

    def test_reversal_of_an_invoice_without_one_stays_empty(self):
        move = self._create(self.with_reference, **{FIELD: False})
        move.action_post()
        self.assertFalse(move._reverse_moves()[FIELD])

    def test_posted_invoice_keeps_its_value(self):
        move = self._create(self.with_reference)
        move.action_post()
        self.with_reference.write({STANDING: "Changed since"})
        self.assertEqual(move[FIELD], "Cost centre 42")
