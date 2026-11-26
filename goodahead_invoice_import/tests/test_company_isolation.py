# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Company isolation of the vendor data the imports produce.

A vendor-data suggestion (an invoice's bank account, VAT, phone...) and a
vendor candidate belong to one company's bill; a partner identifier is as
readable as its partner. Invented data only (``ZZ`` names, made-up numbers).
"""

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, new_test_user, tagged

from .common import ensure_purchase_journal


@tagged("post_install", "-at_install")
class TestCompanyIsolation(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company_a = cls.env.company
        cls.company_b = cls.env["res.company"].create({"name": "ZZ Isolation B Oy"})
        cls.journal_a = ensure_purchase_journal(cls.env, cls.company_a)
        cls.journal_b = cls.env["account.journal"].create(
            {
                "name": "ZZ B Purchases",
                "code": "ZZBP",
                "type": "purchase",
                "company_id": cls.company_b.id,
            }
        )
        cls.vendor = cls.env["res.partner"].create(
            {"name": "ZZ Shared Vendor Oy", "is_company": True}
        )
        Move = cls.env["account.move"]
        cls.bill_a = Move.with_company(cls.company_a).create(
            {"move_type": "in_invoice", "journal_id": cls.journal_a.id}
        )
        cls.bill_b = Move.with_company(cls.company_b).create(
            {"move_type": "in_invoice", "journal_id": cls.journal_b.id}
        )
        cls.user_a = new_test_user(
            cls.env,
            login="zz_isolation_a",
            groups="base.group_user,account.group_account_invoice",
            company_id=cls.company_a.id,
            company_ids=[(6, 0, cls.company_a.ids)],
        )

    def _suggestions(self):
        Suggestion = self.env["goodahead.vendor.data.suggestion"]
        return Suggestion.create(
            [
                {
                    "move_id": bill.id,
                    "partner_id": self.vendor.id,
                    "field_name": "bank_account",
                    "invoice_value": iban,
                    "is_sensitive": True,
                }
                for bill, iban in (
                    (self.bill_a, "FI1612345600000002"),
                    (self.bill_b, "FI5912345600000004"),
                )
            ]
        )

    def test_suggestion_carries_the_bills_company_and_stays_in_it(self):
        own, foreign = self._suggestions()
        self.assertEqual(own.company_id, self.company_a)
        self.assertEqual(foreign.company_id, self.company_b)
        Suggestion = self.env["goodahead.vendor.data.suggestion"].with_user(self.user_a)
        visible = Suggestion.search([("id", "in", (own | foreign).ids)])
        self.assertEqual(visible, own)
        with self.assertRaises(AccessError):
            foreign.with_user(self.user_a).read(["invoice_value"])

    def test_candidate_carries_the_bills_company_and_stays_in_it(self):
        Candidate = self.env["goodahead.vendor.candidate"]
        own = Candidate._build_for_move(self.bill_a, [self._candidate()])
        foreign = Candidate._build_for_move(self.bill_b, [self._candidate()])
        self.assertEqual(own.company_id, self.company_a)
        self.assertEqual(foreign.company_id, self.company_b)
        visible = Candidate.with_user(self.user_a).search([("id", "in", (own | foreign).ids)])
        self.assertEqual(visible, own)
        with self.assertRaises(AccessError):
            foreign.with_user(self.user_a).read(["reason", "bill_count"])

    def _candidate(self):
        return {
            "partner": self.vendor,
            "reason": "same phone",
            "bill_count": 3,
            "draft_bill_count": 0,
            "archived": False,
        }

    def test_company_follows_the_bill(self):
        own, _foreign = self._suggestions()
        own.move_id = self.bill_b
        self.assertEqual(own.company_id, self.company_b)

    def test_partner_identifier_readable_only_with_its_partner(self):
        own_partner = self.env["res.partner"].create(
            {"name": "ZZ A Only Oy", "is_company": True, "company_id": self.company_a.id}
        )
        foreign_partner = self.env["res.partner"].create(
            {"name": "ZZ B Only Oy", "is_company": True, "company_id": self.company_b.id}
        )
        Identifier = self.env["goodahead.partner.identifier"]
        rows = Identifier.create(
            [
                {"partner_id": partner.id, "kind": "domain", "value": value}
                for partner, value in (
                    (self.vendor, "zz-shared.example"),
                    (own_partner, "zz-a-only.example"),
                    (foreign_partner, "zz-b-only.example"),
                )
            ]
        )
        shared, own, foreign = rows
        visible = Identifier.with_user(self.user_a).search([("id", "in", rows.ids)])
        self.assertEqual(visible, shared | own)
        with self.assertRaises(AccessError):
            foreign.with_user(self.user_a).read(["value"])

    def test_import_job_runs_in_the_records_company(self):
        """A job queued by a user whose active company is another one still
        runs in the company of the import record (multi-company)."""
        user = new_test_user(
            self.env,
            login="zz_isolation_ab",
            groups="base.group_user,account.group_account_manager",
            company_id=self.company_a.id,
            company_ids=[(6, 0, (self.company_a | self.company_b).ids)],
        )
        record = self.env["goodahead.invoice.import"].create(
            {"name": "zz-invented.xml", "company_id": self.company_b.id}
        )
        as_user = record.with_user(user).with_context(
            allowed_company_ids=[self.company_a.id, self.company_b.id]
        )
        self.assertEqual(as_user.env.company, self.company_a)
        as_user._enqueue_import("ZZ import")
        job = record.queue_job_id
        self.assertTrue(job)
        self.assertEqual(job.company_id, self.company_b)
        self.assertEqual(job.records.env.company, self.company_b)
