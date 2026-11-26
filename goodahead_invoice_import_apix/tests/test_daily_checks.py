# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The daily ageing and reconciliation checks.

AC-11b (ageing), spec §6 "Listing window" and "Loss / duplication"
(reconciliation). No network: `ApixClient.list_packages` is patched per test,
and "today" is passed to the check. Invented data only.
"""

from datetime import date
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_apix.lib.apix_client import ApixClient, ApixUnreachable

TRANSFER_A = "aaaaaaaa-0000-4000-8000-000000000013"
TRANSFER_B = "bbbbbbbb-0000-4000-8000-000000000013"

#: A package dated 2026-09-15 leaves the listing on 2026-11-01; the warning
#: is due from 2026-10-25.
DATED = "20260915"
EIGHT_BEFORE = date(2026, 10, 24)
SEVEN_BEFORE = date(2026, 10, 25)
SIX_BEFORE = date(2026, 10, 26)


def group(storage_id, status="UNRECEIVED", document_date=DATED, seller="Example Supplier Oy"):
    return {
        "StorageID": storage_id,
        "StorageKey": f"key-{storage_id}",
        "StorageStatus": status,
        "DocumentID": f"INV-{storage_id}",
        "SenderName": seller,
        "DocumentDate": document_date,
    }


@tagged("post_install", "-at_install")
class TestApixDailyChecks(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.Connection = cls.env["goodahead.apix.connection"]
        cls.Import = cls.env["goodahead.invoice.import"].sudo()
        cls.company_a = cls.env["res.company"].create({"name": "Daily Checks A Oy"})
        cls.company_b = cls.env["res.company"].create({"name": "Daily Checks B Oy"})
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

    def setUp(self):
        super().setUp()
        self.listings = {TRANSFER_A: [], TRANSFER_B: []}

        def fake(client, transfer_id, transfer_key):
            answer = self.listings[transfer_id]
            if isinstance(answer, Exception):
                raise answer
            return answer

        patcher = patch.object(ApixClient, "list_packages", autospec=True, side_effect=fake)
        self.list_packages = patcher.start()
        self.addCleanup(patcher.stop)

    # -- helpers -------------------------------------------------------------

    def _record(self, company, storage_id, state="waiting", status="UNRECEIVED", **vals):
        listing = group(storage_id, status=status, seller=vals.pop("seller", "Example Supplier Oy"))
        if "document_date" in vals:
            listing["DocumentDate"] = vals.pop("document_date")
        record_vals = dict(
            self.Import._apix_listing_vals(listing),
            name=f"Example Supplier Oy — {storage_id}",
            company_id=company.id,
            source="apix",
            state=state,
            apix_storage_id=storage_id,
        )
        if state == "closed":
            record_vals["close_reason"] = "Invented: no bill needed"
        record_vals.update(vals)
        return self.Import.create(record_vals)

    def _check(self, company, today=SEVEN_BEFORE):
        return self.Connection._apix_daily_checks(company.id, today=today)

    def _todos(self, connection, key):
        return connection._apix_open_todos(key)

    def _note(self, connection, key):
        return str(self._todos(connection, key).note or "")

    def _mark_jobs(self, record):
        return self.env["queue.job"].sudo().search([("identity_key", "=", f"apix-mark:{record.id}")])

    # -- trigger -------------------------------------------------------------

    def test_cron_enqueues_one_job_per_receiving_company_per_day(self):
        self.conn_b._apix_set_receiving_state("shadow")
        company_c = self.env["res.company"].create({"name": "Daily Checks C Oy"})
        self._connection(
            company_c, "cccccccc-0000-4000-8000-000000000013", "0000004-3"
        )._apix_set_receiving_state("off")
        self.Connection._cron_apix_daily_checks()
        self.Connection._cron_apix_daily_checks()
        jobs = self.env["queue.job"].sudo().search([("identity_key", "=like", "apix-daily:%")])
        keys = set(jobs.mapped("identity_key"))
        today = jobs[:1].identity_key.rsplit(":", 1)[-1] if jobs else ""
        self.assertEqual(
            keys,
            {f"apix-daily:{self.company_a.id}:{today}", f"apix-daily:{self.company_b.id}:{today}"},
        )
        self.assertEqual(len(jobs), 2)
        self.assertEqual(set(jobs.mapped("channel")), {"root.apix"})
        self.assertEqual(set(jobs.mapped("method_name")), {"_apix_daily_checks"})

    def test_ageing_runs_without_a_working_connection(self):
        """A rejected connection stops the listing, not the ageing: packages
        leave APIX's listing whatever the connection's status."""
        self.conn_a.write({"status": "rejected"})
        self._record(self.company_a, "S-AGE-REJ", seller="Rejected Times Oy")
        self.Connection._cron_apix_daily_checks()
        jobs = self.env["queue.job"].sudo().search(
            [("identity_key", "=like", f"apix-daily:{self.company_a.id}:%")]
        )
        self.assertEqual(len(jobs), 1, "enqueued although the connection is rejected")
        result = self._check(self.company_a)
        self.assertEqual(result, "ageing: 1; reconciliation: skipped (no working connection)")
        self.assertIn("Rejected Times Oy", self._note(self.conn_a, "ageing"))
        self.assertFalse(self.list_packages.called, "no listing without a working connection")

    def test_receiving_off_does_nothing(self):
        self.conn_a._apix_set_receiving_state("off")
        self._record(self.company_a, "S-OFF-1")
        self.assertEqual(self._check(self.company_a), "receiving off")
        self.assertFalse(self.list_packages.called)

    # -- ageing -----------------------------------------------------

    def test_ageing_boundaries(self):
        record = self._record(self.company_a, "S-AGE-1", seller="Ageing Supplier Oy")
        self.listings[TRANSFER_A] = [group("S-AGE-1")]

        self._check(self.company_a, today=EIGHT_BEFORE)
        self.assertFalse(self._todos(self.conn_a, "ageing"))

        self._check(self.company_a, today=SEVEN_BEFORE)
        self.assertEqual(len(self._todos(self.conn_a, "ageing")), 1)
        note = self._note(self.conn_a, "ageing")
        self.assertIn("Ageing Supplier Oy", note)
        self.assertIn("INV-S-AGE-1", note)
        self.assertIn("40 days old", note)
        self.assertIn("leaves the listing in 7 days", note)

        self._check(self.company_a, today=SIX_BEFORE)
        todo = self._todos(self.conn_a, "ageing")
        self.assertEqual(len(todo), 1, "one open to-do per condition")
        self.assertIn("41 days old", str(todo.note))

        # Settled -> the to-do is closed.
        record.write({"state": "closed", "close_reason": "Invented: paid elsewhere"})
        self._check(self.company_a, today=SIX_BEFORE)
        self.assertFalse(self._todos(self.conn_a, "ageing"))

    def test_ageing_year_rollover(self):
        self._record(self.company_a, "S-AGE-DEC", document_date="20261231")
        self.listings[TRANSFER_A] = [group("S-AGE-DEC", document_date="20261231")]
        self._check(self.company_a, today=date(2027, 1, 24))
        self.assertFalse(self._todos(self.conn_a, "ageing"))
        self._check(self.company_a, today=date(2027, 1, 25))
        self.assertEqual(len(self._todos(self.conn_a, "ageing")), 1)

    def test_ageing_covers_every_unsettled_state_only(self):
        for index, state in enumerate(["waiting", "failed", "duplicate", "possibly_lost"]):
            self._record(self.company_a, f"S-UNS-{index}", state=state)
        self._record(self.company_a, "S-SET-1", state="closed", apix_mark_confirmed=True)
        self.listings[TRANSFER_A] = [group(f"S-UNS-{i}") for i in range(4)] + [
            group("S-SET-1", status="RECEIVED")
        ]
        self._check(self.company_a)
        note = self._note(self.conn_a, "ageing")
        for index in range(4):
            self.assertIn(f"INV-S-UNS-{index}", note)
        self.assertNotIn("INV-S-SET-1", note)

    def test_ageing_falls_back_to_create_date(self):
        record = self._record(self.company_a, "S-NODATE", apix_document_date=False)
        self.listings[TRANSFER_A] = [group("S-NODATE")]
        created = record.create_date.date()
        # 62 days after creation the package has left the listing in any month.
        self._check(self.company_a, today=date.fromordinal(created.toordinal() + 62))
        self.assertIn("INV-S-NODATE", self._note(self.conn_a, "ageing"))

    # -- reconciliation ------------------------------------------------------

    def test_reconciliation_settled_but_not_received_reenqueues_mark(self):
        unconfirmed = self._record(self.company_a, "S-REC-A1", state="closed")
        confirmed_stale = self._record(
            self.company_a, "S-REC-A2", state="closed", apix_mark_confirmed=True
        )
        fine = self._record(self.company_a, "S-REC-OK", state="closed", apix_mark_confirmed=True)
        self.listings[TRANSFER_A] = [
            group("S-REC-A1", status="RECEIVED"),
            group("S-REC-A2", status="UNRECEIVED"),
            group("S-REC-OK", status="RECEIVED"),
        ]
        self._check(self.company_a)
        todo = self._todos(self.conn_a, "reconciliation")
        self.assertEqual(len(todo), 1)
        note = str(todo.note)
        self.assertIn("S-REC-A1", note)
        self.assertIn("S-REC-A2", note)
        self.assertNotIn("S-REC-OK", note)
        self.assertEqual(len(self._mark_jobs(unconfirmed)), 1)
        self.assertEqual(len(self._mark_jobs(confirmed_stale)), 1)
        self.assertFalse(confirmed_stale.apix_mark_confirmed)
        self.assertFalse(self._mark_jobs(fine))

        # The mark goes through -> the next day is clean, the to-do closed.
        (unconfirmed | confirmed_stale).write({"apix_mark_confirmed": True})
        self.listings[TRANSFER_A] = [
            group(storage_id, status="RECEIVED") for storage_id in ("S-REC-A1", "S-REC-A2", "S-REC-OK")
        ]
        self._check(self.company_a, today=SIX_BEFORE)
        self.assertFalse(self._todos(self.conn_a, "reconciliation"))

    def test_reconciliation_shadow_reports_but_never_marks(self):
        self.conn_a._apix_set_receiving_state("shadow")
        record = self._record(self.company_a, "S-SHADOW", state="closed")
        self.listings[TRANSFER_A] = [group("S-SHADOW", status="UNRECEIVED")]
        self._check(self.company_a)
        self.assertIn("S-SHADOW", self._note(self.conn_a, "reconciliation"))
        self.assertFalse(self._mark_jobs(record))

    def test_reconciliation_received_at_apix_but_unsettled_or_unknown(self):
        self._record(self.company_a, "S-REC-B1", state="failed")
        self.listings[TRANSFER_A] = [
            group("S-REC-B1", status="RECEIVED"),
            group("S-REC-B2", status="RECEIVED"),
        ]
        self._check(self.company_a)
        note = self._note(self.conn_a, "reconciliation")
        self.assertIn("S-REC-B1", note)
        self.assertIn("received at APIX, but failed here", note)
        self.assertIn("S-REC-B2", note)
        self.assertIn("unknown to the import inbox", note)
        # T15 creates possibly-lost records; the check only reports.
        self.assertFalse(self.Import.search([("apix_storage_id", "=", "S-REC-B2")]))

    def test_reconciliation_unknown_received_not_reported_in_shadow(self):
        self.conn_a._apix_set_receiving_state("shadow")
        self.listings[TRANSFER_A] = [group("S-REC-B3", status="RECEIVED")]
        self._check(self.company_a)
        self.assertFalse(self._todos(self.conn_a, "reconciliation"))

    def test_reconciliation_gone_from_listing(self):
        self._record(self.company_a, "S-REC-C1", state="waiting")
        self._record(self.company_a, "S-REC-C2", state="closed", apix_mark_confirmed=True)
        self.listings[TRANSFER_A] = []
        self._check(self.company_a)
        note = self._note(self.conn_a, "reconciliation")
        self.assertIn("S-REC-C1", note)
        self.assertIn("gone from the APIX listing", note)
        self.assertNotIn("S-REC-C2", note)

    def test_reconciliation_clean_opens_nothing(self):
        self._record(self.company_a, "S-CLEAN-1", state="waiting")
        self._record(self.company_a, "S-CLEAN-2", state="closed", apix_mark_confirmed=True)
        self.listings[TRANSFER_A] = [
            group("S-CLEAN-1", status="UNRECEIVED"),
            group("S-CLEAN-2", status="RECEIVED"),
        ]
        self.assertEqual(self._check(self.company_a, today=EIGHT_BEFORE), "ageing: 0; reconciliation: 0")
        self.assertFalse(self._todos(self.conn_a, "reconciliation"))

    def test_listing_failure_skips_reconciliation_quietly(self):
        self._record(self.company_a, "S-FAIL-1", state="waiting")
        self.listings[TRANSFER_A] = []
        self._check(self.company_a)
        self.assertEqual(len(self._todos(self.conn_a, "reconciliation")), 1)

        self.listings[TRANSFER_A] = ApixUnreachable("Invented: connection refused")
        result = self._check(self.company_a, today=SIX_BEFORE)
        self.assertIn("reconciliation: skipped", result)
        # The open reconciliation to-do stays; no failure to-do is added.
        self.assertEqual(len(self._todos(self.conn_a, "reconciliation")), 1)
        self.assertFalse(self._todos(self.conn_a, "run-failed"))
        # Ageing still ran.
        self.assertEqual(len(self._todos(self.conn_a, "ageing")), 1)

    # -- company isolation ---------------------------------------------------

    def test_company_isolation(self):
        self._record(self.company_b, "S-B-ONLY", state="waiting", seller="Other Supplier Oy")
        self._record(self.company_a, "S-A-ONLY", state="waiting")
        self.listings[TRANSFER_A] = [group("S-A-ONLY")]
        self.listings[TRANSFER_B] = []
        self._check(self.company_a)
        self.assertNotIn("S-B-ONLY", self._note(self.conn_a, "ageing"))
        self.assertFalse(self._todos(self.conn_a, "reconciliation"))
        self.assertFalse(self._todos(self.conn_b, "ageing"))
        self.assertFalse(self._todos(self.conn_b, "reconciliation"))
        for call in self.list_packages.call_args_list:
            self.assertEqual(call.args[1], TRANSFER_A)

        self._check(self.company_b)
        self.assertIn("S-B-ONLY", self._note(self.conn_b, "reconciliation"))
        self.assertIn("Other Supplier Oy", self._note(self.conn_b, "ageing"))
        self.assertNotIn("S-A-ONLY", self._note(self.conn_b, "ageing"))
