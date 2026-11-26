# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Receiving state and the per-company Scheduler run.

AC-10, AC-11, AC-12b, AC-15, AC-16b, AC-27. No network:
`ApixClient.list_packages` is patched per test. Invented data only.
"""

from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from odoo.addons.goodahead_apix.lib.apix_client import (
    ApixClient,
    ApixEmptyAnswer,
    ApixRejected,
    ApixUnexpected,
    ApixUnreachable,
)
from odoo.addons.goodahead_invoice_import_apix.lib import apix_listing
from odoo.addons.queue_job.job import Job

TRANSFER_A = "aaaaaaaa-0000-4000-8000-000000000001"
TRANSFER_B = "bbbbbbbb-0000-4000-8000-000000000002"


def package(storage_id, status="UNRECEIVED", **extra):
    group = {
        "StorageID": storage_id,
        "StorageKey": f"key-{storage_id}",
        "StorageStatus": status,
        "DocumentID": f"INV-{storage_id}",
        "SenderName": "Example Supplier Oy",
        "SenderVAT": "FI00000019",
        "AmountToPay": "124.00",
        "DocumentDate": "20261120",
    }
    group.update(extra)
    return group


@tagged("post_install", "-at_install")
class TestApixReceivingRun(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.Connection = cls.env["goodahead.apix.connection"]
        cls.Import = cls.env["goodahead.invoice.import"]
        cls.company_a = cls.env["res.company"].create({"name": "Receiving Test A Oy"})
        cls.company_b = cls.env["res.company"].create({"name": "Receiving Test B Oy"})
        cls.admin = new_test_user(
            cls.env,
            login="t10_admin",
            email="t10.admin@example.test",
            groups="base.group_user,account.group_account_manager",
            company_id=cls.company_a.id,
            company_ids=[(6, 0, (cls.company_a | cls.company_b).ids)],
        )
        cls.accountant_a = new_test_user(
            cls.env,
            login="t10_accountant_a",
            email="t10.accountant@example.test",
            groups="base.group_user,account.group_account_invoice",
            company_id=cls.company_a.id,
            company_ids=[(6, 0, cls.company_a.ids)],
        )
        cls.conn_a = cls._connection(cls.company_a, TRANSFER_A, "0000001-9")
        cls.conn_b = cls._connection(cls.company_b, TRANSFER_B, "0000002-7")

    @classmethod
    def _connection(cls, company, transfer_id, business_id):
        connection = cls.Connection.create(
            {
                "company_id": company.id,
                "environment": "test",
                "transfer_id": transfer_id,
                "transfer_key": f"secret-{transfer_id}",
                "business_id": business_id,
            }
        )
        connection.write({"status": "working"})
        connection._apix_set_receiving_state("live")
        return connection

    # -- helpers -----------------------------------------------------------

    def _patch_listing(self, by_transfer):
        """Patch `list_packages`: ``by_transfer`` maps a transfer id to a
        list of groups or an exception to raise."""

        def fake(client, transfer_id, transfer_key):
            answer = by_transfer[transfer_id]
            if isinstance(answer, Exception):
                raise answer
            return answer

        return patch.object(ApixClient, "list_packages", autospec=True, side_effect=fake)

    def _run(self, company):
        return self.Connection._apix_run(company.id)

    def _records(self, company):
        return self.Import.search([("company_id", "=", company.id), ("source", "=", "apix")])

    def _jobs(self, identity_key):
        return self.env["queue.job"].sudo().search([("identity_key", "=", identity_key)])

    def _run_jobs(self):
        return self.env["queue.job"].sudo().search([("identity_key", "=like", "apix-run:%")])

    def _package_jobs(self, records):
        keys = [f"apix-import:{record.id}" for record in records]
        return self.env["queue.job"].sudo().search([("identity_key", "in", keys)])

    def _todos(self, connection, key=None):
        return connection._apix_open_todos(key)

    # -- AC-10 -------------------------------------------------------------

    def test_ac10_rejected_stops_fetching_with_one_todo(self):
        rejected = ApixRejected("ListInvoiceZIPs: invalid digest (StatusCode 4701)", status_code="4701")
        with self._patch_listing({TRANSFER_A: rejected, TRANSFER_B: []}) as listing:
            self.assertEqual(self._run(self.company_a), "rejected")
            self.assertEqual(self.conn_a.status, "rejected")
            self.assertEqual(self.conn_a.last_run_state, "failed")
            self.assertEqual(len(self._todos(self.conn_a, "rejected")), 1)

            # Fetching stopped: the Scheduler enqueues nothing for A, and a
            # run that was already queued does not call APIX.
            self.Connection._cron_apix_run()
            self.assertFalse(self._jobs(f"apix-run:{self.company_a.id}"))
            self.assertEqual(self._run(self.company_a), "no working connection")
            self.assertEqual(listing.call_count, 1)

            # The Administrator's check succeeds, the next run is rejected
            # again: still one open to-do, not a new one per run.
            self.conn_a.write({"status": "working"})
            self.assertEqual(self._run(self.company_a), "rejected")
        self.assertEqual(len(self._todos(self.conn_a, "rejected")), 1)
        self.assertEqual(len(self._todos(self.conn_a)), 1)
        self.assertFalse(self._records(self.company_a))

    def test_ac10_empty_or_unreadable_answer_is_a_failed_run(self):
        for error in (
            ApixEmptyAnswer("ListInvoiceZIPs: APIX answer is not a valid Response (empty body)"),
            ApixUnexpected("ListInvoiceZIPs: APIX answered HTTP 502", http_status=502),
        ):
            with self._patch_listing({TRANSFER_A: error}):
                self.assertEqual(self._run(self.company_a), "failed")
        # Never "no invoices": the run is failed, with the reason, and the
        # connection still works (retried on the next run).
        self.assertEqual(self.conn_a.last_run_state, "failed")
        self.assertIn("apix.unexpected", self.conn_a.last_run_message)
        self.assertEqual(self.conn_a.run_failure_count, 2)
        self.assertEqual(self.conn_a.status, "working")
        self.assertEqual(len(self._todos(self.conn_a, "run-failed")), 1)
        self.assertEqual(len(self._todos(self.conn_a)), 1)

    def test_ac10_valid_empty_listing_is_success_without_todo(self):
        with self._patch_listing({TRANSFER_A: []}):
            self.assertEqual(self._run(self.company_a), "success")
        self.assertEqual(self.conn_a.last_run_state, "success")
        self.assertFalse(self._todos(self.conn_a))
        self.assertFalse(self._records(self.company_a))

    def test_ac10_unreachable_is_retried_on_the_next_run(self):
        with self._patch_listing({TRANSFER_A: ApixUnreachable("ListInvoiceZIPs: timeout")}):
            self.assertEqual(self._run(self.company_a), "failed")
        self.assertEqual(len(self._todos(self.conn_a, "run-failed")), 1)
        # Still working, so the next schedule enqueues the run again.
        self.Connection._cron_apix_run()
        self.assertEqual(len(self._jobs(f"apix-run:{self.company_a.id}")), 1)

        with self._patch_listing({TRANSFER_A: [package("S-1")]}):
            self.assertEqual(self._run(self.company_a), "success")
        self.assertEqual(self.conn_a.last_run_state, "success")
        self.assertEqual(self.conn_a.run_failure_count, 0)
        self.assertFalse(self._todos(self.conn_a), "the to-do closes when the condition clears")
        self.assertEqual(len(self._records(self.company_a)), 1)

    # -- AC-11 -------------------------------------------------------------

    def test_ac11_listing_cap_opens_todo_and_processes_all(self):
        groups = [package(f"CAP-{n:03d}") for n in range(apix_listing.LISTING_CAP)]
        with self._patch_listing({TRANSFER_A: groups}):
            self.assertEqual(self._run(self.company_a), "capped")
            self.assertEqual(self._run(self.company_a), "capped")
        records = self._records(self.company_a)
        self.assertEqual(len(records), apix_listing.LISTING_CAP)
        self.assertEqual(len(self._package_jobs(records)), apix_listing.LISTING_CAP)
        self.assertEqual(len(self._todos(self.conn_a, "listing-cap")), 1)

        with self._patch_listing({TRANSFER_A: groups[:3]}):
            self.assertEqual(self._run(self.company_a), "success")
        self.assertFalse(self._todos(self.conn_a, "listing-cap"))

    # -- AC-12b ------------------------------------------------------------

    def test_ac12b_fetch_now_runs_one_company_following_its_state(self):
        conn_a = self.conn_a.with_user(self.admin)
        run_a = f"apix-run:{self.company_a.id}"

        self.conn_a._apix_set_receiving_state("off")
        action = conn_a.action_fetch_now()
        self.assertEqual(action["params"]["type"], "warning")
        self.assertFalse(self._run_jobs())

        self.conn_a._apix_set_receiving_state("shadow")
        conn_a.action_fetch_now()
        self.assertEqual(self._run_jobs().mapped("identity_key"), [run_a])
        job = self._jobs(run_a)
        self.assertEqual(job.channel, "root.apix")
        self.assertEqual(job.method_name, "_apix_run")

        # The queued job runs the same run: shadow lists, enqueues nothing.
        with self._patch_listing({TRANSFER_A: [package("S-1")]}):
            self.assertEqual(Job.load(self.env, job.uuid).perform(), "success")
        records = self._records(self.company_a)
        self.assertEqual(len(records), 1)
        self.assertFalse(self._package_jobs(records))

        self.conn_a._apix_set_receiving_state("live")
        conn_a.action_fetch_now()
        self.assertEqual(len(self._jobs(run_a)), 1, "one run per company at a time")
        with self._patch_listing({TRANSFER_A: [package("S-1")]}):
            self._run(self.company_a)
        self.assertEqual(len(self._package_jobs(records)), 1)
        self.assertFalse(self._records(self.company_b))

    def test_ac12b_fetch_now_is_for_administrators(self):
        with self.assertRaises(UserError):
            self.conn_a.with_user(self.accountant_a).action_fetch_now()
        self.conn_a.write({"status": "unchecked"})
        with self.assertRaises(UserError):
            self.conn_a.with_user(self.admin).action_fetch_now()

    def test_receiving_state_edited_by_administrators_only(self):
        self.conn_a.with_user(self.admin)._apix_set_receiving_state("shadow")
        self.assertEqual(self.conn_a.receiving_state, "shadow")
        with self.assertRaises(AccessError):
            self.conn_a.with_user(self.accountant_a)._apix_set_receiving_state("live")
        self.assertEqual(self.conn_a.with_user(self.accountant_a).receiving_state, "shadow")

    # -- AC-15 + AC-27 -----------------------------------------------------

    def test_ac15_ac27_one_failing_company_does_not_stop_the_other(self):
        self.Connection._cron_apix_run()
        self.assertEqual(
            sorted(self._run_jobs().mapped("identity_key")),
            sorted([f"apix-run:{self.company_a.id}", f"apix-run:{self.company_b.id}"]),
        )
        listing = {
            TRANSFER_A: [package("A-1"), package("SHARED")],
            TRANSFER_B: ApixRejected("ListInvoiceZIPs: rejected (StatusCode 4701)"),
        }
        with self._patch_listing(listing):
            self.assertEqual(self._run(self.company_b), "rejected")
            self.assertEqual(self._run(self.company_a), "success")
        self.assertEqual(sorted(self._records(self.company_a).mapped("apix_storage_id")), ["A-1", "SHARED"])
        self.assertFalse(self._records(self.company_b))
        self.assertEqual(self.conn_a.last_run_state, "success")
        self.assertEqual(self.conn_b.last_run_state, "failed")
        self.assertFalse(self._todos(self.conn_a))
        self.assertEqual(len(self._todos(self.conn_b)), 1)

        # The other way round, and the same storage id in both companies is
        # two records, each in its own company.
        self.conn_b.write({"status": "working"})
        listing = {
            TRANSFER_A: ApixUnreachable("ListInvoiceZIPs: timeout"),
            TRANSFER_B: [package("B-1"), package("SHARED")],
        }
        with self._patch_listing(listing):
            self.assertEqual(self._run(self.company_a), "failed")
            self.assertEqual(self._run(self.company_b), "success")
        records_b = self._records(self.company_b)
        self.assertEqual(sorted(records_b.mapped("apix_storage_id")), ["B-1", "SHARED"])
        self.assertEqual(records_b.company_id, self.company_b)
        self.assertEqual(self._records(self.company_a).company_id, self.company_a)

        # The Accountant allowed only in A sees A's packages only.
        visible = self.Import.with_user(self.accountant_a).search([("source", "=", "apix")])
        self.assertEqual(visible, self._records(self.company_a))

    # -- AC-16b ------------------------------------------------------------

    def test_ac16b_off_fetches_nothing_and_stays_working(self):
        self.conn_a._apix_set_receiving_state("off")
        with self._patch_listing({TRANSFER_A: [package("S-1")], TRANSFER_B: []}) as listing:
            self.Connection._cron_apix_run()
            self.assertFalse(self._jobs(f"apix-run:{self.company_a.id}"))
            self.assertEqual(len(self._jobs(f"apix-run:{self.company_b.id}")), 1)
            self.assertEqual(self._run(self.company_a), "receiving off")
            listing.assert_not_called()
        self.assertEqual(self.conn_a.status, "working")
        self.assertFalse(self._records(self.company_a))

    # -- Upsert, NEW, shadow ----------------------------------------------

    def test_upsert_one_record_per_storage_id(self):
        listing = [package("S-1", AmountToPay="10.00"), package("S-1", AmountToPay="20.00")]
        with self._patch_listing({TRANSFER_A: listing}):
            self._run(self.company_a)
        record = self._records(self.company_a)
        self.assertEqual(len(record), 1)
        self.assertEqual(record.state, "waiting")
        self.assertEqual(record.apix_listing_data["AmountToPay"], "20.00")
        self.assertEqual(str(record.apix_document_date), "2026-11-20")

        with self._patch_listing({TRANSFER_A: [package("S-1", AmountToPay="30.00")]}):
            self._run(self.company_a)
        self.assertEqual(self._records(self.company_a), record)
        self.assertEqual(record.apix_listing_data["AmountToPay"], "30.00")
        self.assertEqual(len(self._package_jobs(record)), 1, "identity key: one queued job per record")
        self.assertEqual(record.queue_job_id, self._package_jobs(record))

    def test_unique_storage_id_per_company(self):
        vals = {"name": "x", "source": "apix", "apix_storage_id": "S-9", "company_id": self.company_a.id}
        self.Import.create(vals)
        self.Import.create(dict(vals, company_id=self.company_b.id))
        self.Import.create({"name": "no storage id"})
        self.Import.create({"name": "no storage id either"})
        with mute_logger("odoo.sql_db"), self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self.Import.create(vals)

    def test_overlapping_runs_create_each_record_once(self):
        """A run that lists a package another (overlapping) run created
        after this one looked for existing records: ON CONFLICT DO NOTHING,
        the run goes on."""
        Import = self.Import.sudo()
        first = Import.create(
            {
                "name": "listed by the other run",
                "source": "apix",
                "state": "waiting",
                "apix_storage_id": "S-OVER",
                "company_id": self.company_a.id,
            }
        )
        vals_list = [
            {
                "name": name,
                "source": "apix",
                "state": "waiting",
                "apix_storage_id": storage_id,
                "company_id": self.company_a.id,
            }
            for name, storage_id in (("again", "S-OVER"), ("fresh", "S-FRESH"))
        ]
        created = self.conn_a.sudo()._apix_create_listed_records(vals_list)
        self.assertEqual(created.mapped("apix_storage_id"), ["S-FRESH"])
        records = self._records(self.company_a)
        self.assertEqual(sorted(records.mapped("apix_storage_id")), ["S-FRESH", "S-OVER"])
        self.assertIn(first, records)

    def test_new_package_is_waiting_but_not_fetched(self):
        with self._patch_listing({TRANSFER_A: [package("S-NEW", "NEW")]}):
            self._run(self.company_a)
        record = self._records(self.company_a)
        self.assertEqual(record.state, "waiting")
        self.assertEqual(record.apix_status, "NEW")
        self.assertFalse(self._package_jobs(record))

        with self._patch_listing({TRANSFER_A: [package("S-NEW", "UNRECEIVED")]}):
            self._run(self.company_a)
        self.assertEqual(len(self._package_jobs(record)), 1)

    def test_live_unknown_received_is_recognised_not_imported_deleted_skipped(self):
        # T15: a received package unknown to the inbox is
        # possibly lost until its recognition job has looked for its bill;
        # it is never imported automatically.
        listing = [package("S-RCV", "RECEIVED"), package("S-DEL", "DELETED"), package("S-OK")]
        with self._patch_listing({TRANSFER_A: listing}):
            self._run(self.company_a)
        records = self._records(self.company_a)
        self.assertEqual(sorted(records.mapped("apix_storage_id")), ["S-OK", "S-RCV"])
        received = records.filtered(lambda r: r.apix_storage_id == "S-RCV")
        self.assertEqual(received.state, "possibly_lost")
        self.assertFalse(self._package_jobs(received))
        self.assertEqual(len(self._jobs(f"apix-recognise:{received.id}")), 1)
        self.assertEqual(received.display_state, "processing")
        self.assertEqual(len(self._package_jobs(records - received)), 1)

    def test_shadow_lists_and_enqueues_nothing(self):
        self.conn_a._apix_set_receiving_state("shadow")
        listing = [package("S-1"), package("S-RCV", "RECEIVED")]
        with self._patch_listing({TRANSFER_A: listing}):
            self.assertEqual(self._run(self.company_a), "success")
        records = self._records(self.company_a)
        self.assertEqual(sorted(records.mapped("apix_storage_id")), ["S-1", "S-RCV"])
        self.assertFalse(any(records.mapped("shadow_outcome")))
        self.assertFalse(self._package_jobs(records))
        # T15: one classification job per package instead (no bill, no mark).
        shadow_jobs = self.env["queue.job"].sudo().search(
            [("identity_key", "in", [f"apix-shadow:{r.id}" for r in records])]
        )
        self.assertEqual(len(shadow_jobs), 2)

    # -- Secrets -----------------------------------------------------------

    def test_storage_key_for_administrators_only(self):
        with self._patch_listing({TRANSFER_A: [package("S-1")]}):
            self._run(self.company_a)
        record = self._records(self.company_a)
        self.assertEqual(record.with_user(self.admin).apix_storage_key, "key-S-1")
        self.assertNotIn("StorageKey", record.apix_listing_data)
        as_accountant = record.with_user(self.accountant_a)
        self.assertEqual(as_accountant.apix_storage_id, "S-1")
        self.assertEqual(as_accountant.apix_status, "UNRECEIVED")
        with self.assertRaises(AccessError):
            as_accountant.apix_storage_key  # noqa: B018
        self.assertFalse(self.Import._fields["apix_storage_key"].exportable)
        copy = record.copy({"apix_storage_id": False})
        self.assertFalse(copy.apix_storage_key)
