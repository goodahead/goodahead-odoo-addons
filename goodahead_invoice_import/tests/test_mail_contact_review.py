# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Anybody can send mail: a mailed document's contact details wait for a person.

Whatever reads the document, a record that arrived by email imports under a
policy whose ``contact_details_need_review`` is set; an upload keeps its
reader's own policy.
"""

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_import.lib.import_source import (
    AI_POLICY,
    DEFAULT_POLICY,
    STRICT_POLICY,
)


@tagged("post_install", "-at_install")
class TestMailContactReview(TransactionCase):
    def _record(self, source):
        return self.env["goodahead.invoice.import"].create(
            {"name": "zz.xml", "company_id": self.env.company.id, "source": source}
        )

    def test_the_shipped_policies(self):
        self.assertFalse(DEFAULT_POLICY.contact_details_need_review)
        self.assertFalse(STRICT_POLICY.contact_details_need_review)
        self.assertTrue(AI_POLICY.contact_details_need_review)

    def test_a_mailed_record_needs_review_whatever_its_reader(self):
        policy = self._record("email")._import_reader_policy()
        self.assertTrue(policy.contact_details_need_review)
        # Matching itself is the reader's, unchanged.
        reader_policy = self._record("upload")._import_reader_policy()
        self.assertEqual(policy.assign_by, reader_policy.assign_by)
        self.assertEqual(policy.bank_account_lookup_only, reader_policy.bank_account_lookup_only)
