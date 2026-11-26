# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The two hooks another add-on (the migration tool) answers:

* `_apix_receiving_via_migration` — when True, receiving goes live and back
  only through `_apix_set_receiving_state`;
* `_apix_bill_from_earlier_receiver` — a bill an earlier connector recorded
  for the package is recognised first, without downloading it.

The hooks are patched, so the tests hold whatever else is installed.
Invented data only.
"""

from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_apix.lib.apix_client import ApixClient, ApixError

from ..models import apix_recognition

TRANSFER = "f0f0f0f0-0000-4000-8000-000000000013"


@tagged("post_install", "-at_install")
class TestReceivingHooks(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.Connection = cls.env["goodahead.apix.connection"]
        cls.connection = cls.Connection.search([("company_id", "=", cls.company.id)], limit=1)
        vals = {"transfer_id": TRANSFER, "transfer_key": f"secret-{TRANSFER}", "status": "working"}
        if cls.connection:
            cls.connection.write(vals)
        else:
            cls.connection = cls.Connection.create(
                dict(vals, company_id=cls.company.id, environment="test", business_id="0000006-0")
            )
        cls.connection._apix_set_receiving_state("off")
        cls.vendor = cls.env["res.partner"].create({"name": "Example Hook Oy", "is_company": True})

    def setUp(self):
        super().setUp()
        patcher = patch.object(
            ApixClient, "download", autospec=True, side_effect=AssertionError("no download")
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _via_migration(self, value):
        patcher = patch.object(
            type(self.Connection), "_apix_receiving_via_migration", return_value=value
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _earlier_bill(self, move):
        patcher = patch.object(
            type(self.env["goodahead.invoice.import"]),
            "_apix_bill_from_earlier_receiver",
            return_value=move,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _bill(self):
        journal = self.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", self.company.id)], limit=1
        )
        if not journal:
            self.skipTest("no purchase journal on this database")
        return self.env["account.move"].create(
            {
                "move_type": "in_invoice",
                "company_id": self.company.id,
                "partner_id": self.vendor.id,
                "ref": "HOOK-1",
                "invoice_date": "2026-11-15",
                "invoice_line_ids": [
                    (0, 0, {"name": "Example line", "quantity": 1, "price_unit": 10.0})
                ],
            }
        )

    def _record(self, storage_id="P-HOOK"):
        return (
            self.env["goodahead.invoice.import"]
            .sudo()
            .create(
                {
                    "name": "Hook package",
                    "company_id": self.company.id,
                    "source": "apix",
                    "state": "waiting",
                    "apix_storage_id": storage_id,
                    "apix_status": "RECEIVED",
                }
            )
        )

    # -- _apix_receiving_via_migration ---------------------------------------

    def test_via_migration_only_the_guarded_path_goes_live_or_back(self):
        self._via_migration(True)
        self.connection.invalidate_recordset(["receiving_via_migration"])
        self.assertTrue(self.connection.receiving_via_migration)
        with self.assertRaisesRegex(UserError, "migration tool"):
            self.connection.write({"receiving_state": "live"})
        self.connection.write({"receiving_state": "shadow"})
        self.connection._apix_set_receiving_state("live")
        self.assertEqual(self.connection.receiving_state, "live")
        with self.assertRaisesRegex(UserError, "migration tool"):
            self.connection.write({"receiving_state": "off"})
        self.connection._apix_set_receiving_state("off")
        self.assertEqual(self.connection.receiving_state, "off")

        company = self.env["res.company"].create({"name": "Hook Create Oy"})
        with self.assertRaisesRegex(UserError, "migration tool"):
            self.Connection.create(
                {
                    "company_id": company.id,
                    "environment": "test",
                    "business_id": "0000009-4",
                    "receiving_state": "live",
                }
            )

    def test_without_migration_the_radio_goes_live(self):
        self._via_migration(False)
        self.connection.invalidate_recordset(["receiving_via_migration"])
        self.assertFalse(self.connection.receiving_via_migration)
        self.connection.write({"receiving_state": "live"})
        self.assertEqual(self.connection.receiving_state, "live")

    # -- _apix_bill_from_earlier_receiver ------------------------------------

    def test_earlier_bill_is_recognised_first_without_a_download(self):
        bill = self._bill()
        self._earlier_bill(bill)
        record = self._record()
        recognition = record._apix_recognise()
        self.assertEqual(recognition.kind, apix_recognition.MATCH_EARLIER)
        self.assertEqual(recognition.move, bill)
        self.assertEqual(recognition.vendor, self.vendor)
        self.assertEqual(
            apix_recognition.shadow_outcome_for(recognition, "RECEIVED"), "imported_before"
        )
        record.write({"state": "possibly_lost"})
        self.assertEqual(record._apix_recognise_received(), "OK: recognised")
        self.assertEqual(record.state, "imported")
        self.assertEqual(record.move_id, bill)
        self.assertTrue(record.apix_mark_confirmed, "APIX shows it received already")

    def test_an_earlier_answer_that_is_no_live_bill_is_ignored(self):
        bill = self._bill()
        bill.button_cancel()
        self._earlier_bill(bill)
        record = self._record()
        self.assertFalse(record._apix_earlier_bill())
        # The generic matching runs instead: it needs the package first.
        with self.assertRaisesRegex(ApixError, "download key"):
            record._apix_recognise()

    # -- _apix_after_package_settled -------------------------------------------

    def _settled_calls(self):
        calls = []
        patcher = patch.object(
            type(self.env["goodahead.invoice.import"]),
            "_apix_after_package_settled",
            autospec=True,
            side_effect=lambda records: calls.append(records.ids),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_settled_hook_hears_of_a_close_and_a_recognition(self):
        calls = self._settled_calls()
        closed = self._record()
        closed.action_close_without_bill("Handled by hand")
        self.assertEqual(calls, [closed.ids])

        bill = self._bill()
        self._earlier_bill(bill)
        recognised = self._record("P-HOOK-2")
        recognised.write({"state": "possibly_lost"})
        self.assertEqual(recognised._apix_recognise_received(), "OK: recognised")
        self.assertEqual(calls, [closed.ids, recognised.ids])

    def test_settled_hook_not_called_for_other_sources(self):
        calls = self._settled_calls()
        other = (
            self.env["goodahead.invoice.import"]
            .sudo()
            .create({"name": "Mail", "company_id": self.company.id, "state": "waiting"})
        )
        other.action_close_without_bill("Test message")
        self.assertEqual(calls, [])
