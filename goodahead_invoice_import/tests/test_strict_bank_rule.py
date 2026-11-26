# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The strict bank-account rule, in the core (core/source split; spec AC-06,
AC-07).

Under a matching policy with ``bank_account_lookup_only`` (STRICT_POLICY) the
decoder may look up a bank account the vendor already holds but never creates
one; an account registered to another partner is not attached, not suggested,
and the chatter names its owner; a new account nobody holds becomes a
sensitive (never pre-selected) suggestion. The default policy — the AI import —
is unchanged.

A structured in-test source hands the core hand-built invoice data, as in
`test_source_interface.py`. Invented data only: `ZZ …` companies, IBANs with
bank code 123456.
"""

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_import.lib.import_source import (
    DEFAULT_POLICY,
    STRICT_POLICY,
)

from . import test_source_interface as source_interface_tests
from .common import ensure_purchase_journal

HELD_IBAN = "FI3912345600000008"
PRINTED_IBAN = "FI1612345600000002"


@tagged("post_install", "-at_install")
class TestStrictBankRule(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.fi = cls.env.ref("base.fi")
        cls.journal = ensure_purchase_journal(cls.env, cls.company)
        if not cls.env["account.tax"].search(
            [
                ("type_tax_use", "=", "purchase"),
                ("amount_type", "=", "percent"),
                ("amount", "=", 25.5),
                ("company_id", "=", cls.company.id),
            ],
            limit=1,
        ):
            cls.env["account.tax"].create(
                {
                    "name": "ZZ T11b 25.5% Purchase",
                    "type_tax_use": "purchase",
                    "amount_type": "percent",
                    "amount": 25.5,
                    "company_id": cls.company.id,
                }
            )
        cls.vendor = cls.env["res.partner"].create(
            {
                "name": "ZZ Bank Rule Vendor Oy",
                "is_company": True,
                "vat": "FI90000001",
                "country_id": cls.fi.id,
            }
        )

    # The same hand-built invoice data as the source-interface tests.
    _data = source_interface_tests.TestSourceInterface._data

    def _import(self, policy=STRICT_POLICY, **seller):
        data = self._data(number="ZZ-T11B-1", iban=PRINTED_IBAN, **seller)
        record, result = source_interface_tests._StructuredSource(self.env, policy).receive(self.company, data)
        self.assertEqual(result.outcome, "imported", record.error_message or "")
        self.assertEqual(result.move.partner_id, self.vendor)
        return result.move

    def _banks(self, number):
        return self.env["res.partner.bank"].with_context(active_test=False).search(
            [("acc_number", "=", number)]
        )

    def _bank_suggestions(self, move):
        return self.env["goodahead.vendor.data.suggestion"].search(
            [("move_id", "=", move.id), ("field_name", "=", "bank_account")]
        )

    def _chatter(self, move):
        return " ".join(str(body) for body in move.message_ids.mapped("body"))

    # -- the policy ----------------------------------------------------------------

    def test_policy_flag(self):
        self.assertTrue(STRICT_POLICY.bank_account_lookup_only)
        self.assertFalse(DEFAULT_POLICY.bank_account_lookup_only)
        self.assertTrue(DEFAULT_POLICY.is_legacy, "the AI path is unchanged")

    def test_lookup_only_never_creates_but_finds(self):
        Bank = self.env["res.partner.bank"]
        strict = Bank.with_context(goodahead_matching_policy=STRICT_POLICY)
        found = strict._find_or_create_bank_account(PRINTED_IBAN, self.vendor, self.company)
        self.assertFalse(found)
        self.assertFalse(self._banks(PRINTED_IBAN), "nothing created")
        held = Bank.create({"acc_number": HELD_IBAN, "partner_id": self.vendor.id})
        found = strict._find_or_create_bank_account(HELD_IBAN, self.vendor, self.company)
        self.assertEqual(found, held)
        # Without the policy, the ERP's own behaviour: created on the vendor.
        created = Bank._find_or_create_bank_account(PRINTED_IBAN, self.vendor, self.company)
        self.assertEqual(created.partner_id, self.vendor)

    # -- AC-06 ---------------------------------------------------------------------

    def test_ac06_new_iban_is_a_sensitive_suggestion_and_never_created(self):
        held = self.env["res.partner.bank"].create(
            {"acc_number": HELD_IBAN, "partner_id": self.vendor.id}
        )
        move = self._import()
        self.assertFalse(self._banks(PRINTED_IBAN), "no bank account was created")
        self.assertEqual(self.vendor.bank_ids, held, "the vendor's accounts are unchanged")
        self.assertNotEqual(move.partner_bank_id.acc_number, PRINTED_IBAN)
        suggestion = self._bank_suggestions(move)
        self.assertEqual(len(suggestion), 1)
        self.assertTrue(suggestion.is_sensitive, "never pre-selected")
        self.assertEqual(suggestion.state, "pending")
        self.assertEqual(suggestion.invoice_value, PRINTED_IBAN)

    def test_ac06_an_iban_the_vendor_holds_is_used(self):
        held = self.env["res.partner.bank"].create(
            {"acc_number": PRINTED_IBAN, "partner_id": self.vendor.id}
        )
        move = self._import()
        self.assertEqual(move.partner_bank_id, held)
        self.assertEqual(self._banks(PRINTED_IBAN), held, "no second row")
        self.assertFalse(self._bank_suggestions(move))

    # -- AC-07 ---------------------------------------------------------------------

    def test_ac07_iban_of_another_partner_is_not_attached_nor_suggested(self):
        owner = self.env["res.partner"].create(
            {"name": "ZZ Factoring Oy", "is_company": True}
        )
        foreign = self.env["res.partner.bank"].create(
            {"acc_number": PRINTED_IBAN, "partner_id": owner.id}
        )
        move = self._import()
        self.assertNotEqual(move.partner_bank_id, foreign)
        self.assertFalse(move.partner_bank_id)
        self.assertEqual(self._banks(PRINTED_IBAN), foreign, "no row on the vendor")
        self.assertEqual(foreign.partner_id, owner)
        self.assertFalse(self._bank_suggestions(move), "a foreign IBAN is never offered")
        chatter = self._chatter(move)
        self.assertIn("ZZ Factoring Oy", chatter)
        self.assertIn("registered to", chatter)

    def test_ac07_rule_is_not_applied_under_the_default_policy(self):
        owner = self.env["res.partner"].create(
            {"name": "ZZ Default Owner Oy", "is_company": True}
        )
        self.env["res.partner.bank"].create(
            {"acc_number": PRINTED_IBAN, "partner_id": owner.id}
        )
        move = self._import(policy=DEFAULT_POLICY)
        self.assertNotIn("registered to", self._chatter(move))
