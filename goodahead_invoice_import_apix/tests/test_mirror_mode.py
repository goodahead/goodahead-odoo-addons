# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Mirror mode: a test or staging copy receives next to production.

Every listed package, received ones too, becomes a draft bill; nothing is
ever marked received at APIX, not even after receiving goes live. No
network: `ApixClient.list_packages` and `ApixClient.download` are patched;
fixtures are the Finvoice add-on's invented ones.
"""

from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_apix.lib.apix_client import ApixClient
from odoo.addons.queue_job.job import Job

from .test_package_import import finvoice_fixture, listing_group, make_zip

TRANSFER = "cccccccc-0000-4000-8000-000000000031"


@tagged("post_install", "-at_install")
class TestApixMirrorMode(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.Import = cls.env["goodahead.invoice.import"]
        cls.Connection = cls.env["goodahead.apix.connection"]
        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", cls.company.id)], limit=1
        )
        if not cls.journal:
            return
        cls.env.ref("base.EUR").active = True
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
                    "name": "Example 25.5% Purchase",
                    "type_tax_use": "purchase",
                    "amount_type": "percent",
                    "amount": 25.5,
                    "company_id": cls.company.id,
                }
            )
        cls.connection = cls.Connection.search([("company_id", "=", cls.company.id)], limit=1)
        if not cls.connection:
            cls.connection = cls.Connection.create(
                {
                    "company_id": cls.company.id,
                    "environment": "test",
                    "transfer_id": TRANSFER,
                    "transfer_key": f"secret-{TRANSFER}",
                    "business_id": "0000006-0",
                }
            )
        cls.connection.write(
            {"transfer_id": TRANSFER, "transfer_key": f"secret-{TRANSFER}", "status": "working"}
        )
        cls.connection._apix_set_receiving_state("off")
        cls.vendor = cls.env["res.partner"].create(
            {
                "name": "Example Supplier Oy",
                "is_company": True,
                "vat": "FI00000035",
                "company_registry": "0000003-5",
                "country_id": cls.env.ref("base.fi").id,
            }
        )

    def setUp(self):
        super().setUp()
        if not self.journal:
            self.skipTest("no purchase journal on this database")
        self.packages = {}
        self.download_calls = []

        def fake_download(client, storage_id, storage_key, mark_received):
            self.download_calls.append((storage_id, mark_received))
            return self.packages[storage_id]

        patcher = patch.object(ApixClient, "download", autospec=True, side_effect=fake_download)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._assert_never_marked)

    def _assert_never_marked(self):
        marked = [call for call in self.download_calls if call[1] is not False]
        self.assertFalse(marked, "mirror mode never downloads with mark_received=True")

    # -- helpers ---------------------------------------------------------------

    def _group(self, storage_id, status, body):
        self.packages[storage_id] = body
        group = listing_group(storage_id)
        group["StorageStatus"] = status
        return group

    def _run(self, groups):
        with patch.object(ApixClient, "list_packages", autospec=True, return_value=groups):
            return self.Connection._apix_run(self.company.id)

    def _jobs(self, prefix, record):
        return self.env["queue.job"].sudo().search([("identity_key", "=", f"{prefix}{record.id}")])

    def _perform(self, job_record):
        job = Job.load(self.env, job_record.uuid)
        job.set_started()
        job.store()
        result = job.perform()
        job.set_done(result=result)
        job.store()
        return result

    def _invoice(self, number):
        return make_zip(
            (
                "invoice.xml",
                finvoice_fixture("fi30_simple_invoice.xml").replace(
                    b">10045<", f">{number}<".encode()
                ),
            )
        )

    # -- tests -----------------------------------------------------------------

    def test_mirror_bills_received_and_unreceived_packages_without_marking(self):
        self.connection.action_start_mirror()
        self.assertEqual(self.connection.receiving_state, "mirror")
        groups = [
            self._group("S-MIR-NEW", "UNRECEIVED", self._invoice("M-1")),
            # Production received this one already: mirror bills it anyway.
            self._group("S-MIR-OLD", "RECEIVED", self._invoice("M-2")),
        ]
        self.assertEqual(self._run(groups), "success")
        records = self.Import.search([("apix_storage_id", "in", ["S-MIR-NEW", "S-MIR-OLD"])])
        self.assertEqual(len(records), 2)
        for record in records:
            job = self._jobs("apix-import:", record)
            self.assertTrue(job, f"{record.apix_storage_id} is queued for import, not recognition")
            self._perform(job)
        records.invalidate_recordset()
        for record in records:
            with self.subTest(storage_id=record.apix_storage_id):
                self.assertEqual(record.state, "imported", record.error_message or "")
                self.assertEqual(record.move_id.state, "draft")
                self.assertTrue(record.apix_mirrored)
                self.assertFalse(record.apix_mark_confirmed)
                self.assertFalse(self._jobs("apix-mark:", record), "no mark-received job")
        self.assertEqual(
            sorted(self.download_calls),
            [("S-MIR-NEW", False), ("S-MIR-OLD", False)],
        )

    def test_mirrored_bills_are_never_marked_after_going_live(self):
        self.connection.action_start_mirror()
        self._run([self._group("S-MIR-LIVE", "UNRECEIVED", self._invoice("M-3"))])
        record = self.Import.search([("apix_storage_id", "=", "S-MIR-LIVE")])
        self._perform(self._jobs("apix-import:", record))
        record.invalidate_recordset()
        self.assertTrue(record.apix_mirrored)

        self.connection._apix_set_receiving_state("live")
        self.assertFalse(self.Import._apix_unconfirmed_marks(self.company))
        self.assertEqual(record._apix_mark_received(), "SKIPPED: mirrored")
        listing = {"S-MIR-LIVE": listing_group("S-MIR-LIVE")}
        differences = self.connection._apix_reconciliation_differences(listing)
        self.assertFalse(
            [d for d in differences if d[1] == record], "a mirrored bill is no difference"
        )

    def test_mark_job_skips_in_mirror_mode(self):
        self.connection.action_start_mirror()
        self._run([self._group("S-MIR-MARK", "UNRECEIVED", self._invoice("M-4"))])
        record = self.Import.search([("apix_storage_id", "=", "S-MIR-MARK")])
        record.sudo().write({"state": "closed", "close_reason": "Booked by hand"})
        self.assertEqual(record._apix_mark_received(), "SKIPPED: mirror")

    def test_mirror_is_not_started_from_live_and_stops_to_off(self):
        self.connection._apix_set_receiving_state("live")
        with self.assertRaisesRegex(UserError, "test or staging copy"):
            self.connection.action_start_mirror()
        self.assertEqual(self.connection.receiving_state, "live")

        self.connection._apix_set_receiving_state("shadow")
        self.connection.action_start_mirror()
        self.assertEqual(self.connection.receiving_state, "mirror")
        self.connection.action_stop_mirror()
        self.assertEqual(self.connection.receiving_state, "off")

    def test_job_title_and_result_name_the_bill(self):
        """The job list is searchable by the references known before the
        import (title) and by the bill made (result)."""
        self.connection.action_start_mirror()
        self._run([self._group("S-MIR-JOB", "UNRECEIVED", self._invoice("M-5"))])
        record = self.Import.search([("apix_storage_id", "=", "S-MIR-JOB")])
        job = self._jobs("apix-import:", record)
        self.assertIn("Example Supplier Oy", job.name)
        self.assertIn("No. INV-S-MIR-JOB", job.name)
        self.assertIn("2026-11-15", job.name)
        self.assertIn("S-MIR-JOB", job.name)
        result = self._perform(job)
        self.assertIn("Example Supplier Oy", result)
        self.assertIn("No. M-5", result)

    def test_no_daily_checks_in_mirror_mode(self):
        self.connection.action_start_mirror()
        self.assertEqual(self.Connection._apix_daily_checks(self.company.id), "receiving off")
