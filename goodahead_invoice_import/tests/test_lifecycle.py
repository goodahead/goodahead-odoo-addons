# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The import record's lifecycle (core/source split; ADR-0008, contracts §1).

Invented data only. Queue jobs are rows written for the purpose; nothing here
runs a job.
"""

import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import Form, TransactionCase, new_test_user, tagged

from odoo.addons.goodahead_invoice_import import hooks
from odoo.addons.goodahead_invoice_import.lib import import_record as lifecycle

from .common import ensure_purchase_journal

STORED = ["waiting", "imported", "duplicate", "failed", "possibly_lost", "closed"]


@tagged("post_install", "-at_install")
class TestImportLifecycle(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.Import = cls.env["goodahead.invoice.import"]
        cls.billing_user = new_test_user(
            cls.env,
            login="t3_billing",
            groups="base.group_user,account.group_account_invoice",
        )

    def _new(self, **vals):
        return self.Import.create({"name": "invented.pdf", **vals})

    def _job(self, state, **dates):
        """A queue_job row for the import model, as the job runner leaves it."""
        columns = ["uuid", "state", "model_name"] + list(dates)
        values = [str(uuid.uuid4()), state, self.Import._name] + list(dates.values())
        self.env.cr.execute(
            "INSERT INTO queue_job ({}) VALUES ({}) RETURNING id".format(
                ", ".join(columns), ", ".join(["%s"] * len(columns))
            ),
            values,
        )
        return self.env["queue.job"].sudo().browse(self.env.cr.fetchone()[0])

    def _link(self, record, job):
        record.queue_job_id = job.id
        self.env.invalidate_all()

    # -- the values -----------------------------------------------------------

    def test_stored_states_and_default(self):
        field = self.Import._fields["state"]
        self.assertEqual([key for key, _label in field.selection], STORED)
        self.assertTrue(field.required)
        self.assertEqual(
            dict(field.selection)["closed"], "Closed without bill"
        )
        record = self._new()
        self.assertEqual(record.state, "waiting")
        self.assertEqual(record.display_state, "waiting")
        self.assertEqual(record.source, "upload")
        display = [key for key, _label in self.Import._fields["display_state"].selection]
        self.assertEqual(display, STORED + ["processing"])

    def test_legacy_state_mapping_function(self):
        self.assertEqual(lifecycle.map_legacy_state("draft"), "waiting")
        self.assertEqual(lifecycle.map_legacy_state("processing"), "waiting")
        self.assertEqual(lifecycle.map_legacy_state("done"), "imported")
        self.assertEqual(lifecycle.map_legacy_state("failed"), "failed")
        for state in STORED:
            self.assertEqual(lifecycle.map_legacy_state(state), state)

    def test_map_states_rewrites_old_rows(self):
        records = {old: self._new(name=f"legacy-{old}.pdf") for old in ("draft", "processing", "done", "failed")}
        for old, record in records.items():
            self.env.cr.execute(
                "UPDATE goodahead_invoice_import SET state = %s WHERE id = %s", (old, record.id)
            )
        counts = hooks.map_states(self.env.cr)
        self.assertEqual(counts, {"draft": 1, "processing": 1, "done": 1})
        self.env.invalidate_all()
        self.assertEqual(
            {old: rec.state for old, rec in records.items()},
            {"draft": "waiting", "processing": "waiting", "done": "imported", "failed": "failed"},
        )
        self.assertEqual(hooks.map_states(self.env.cr), {}, "a second run changes nothing")

    # -- processing derived from the job -----------------------------

    def test_display_state_follows_the_job(self):
        record = self._new(state="failed", error_message="invented")
        expected = {
            "pending": "processing",
            "enqueued": "processing",
            "started": "processing",
            "wait_dependencies": "failed",
            "done": "failed",
            "failed": "failed",
            "cancelled": "failed",
        }
        for job_state, shown in expected.items():
            with self.subTest(job_state=job_state):
                self._link(record, self._job(job_state))
                self.assertEqual(record.display_state, shown)
                self.assertEqual(record.state, "failed", "the stored state never changes")
        self._link(record, self.env["queue.job"])
        self.assertEqual(record.display_state, "failed")

    def test_display_state_is_searchable(self):
        busy = self._new(state="waiting")
        self._link(busy, self._job("started"))
        idle = self._new(state="waiting")
        finished = self._new(state="failed")
        self._link(finished, self._job("done"))
        ids = (busy | idle | finished).ids
        search = self.Import.search
        self.assertEqual(search([("id", "in", ids), ("display_state", "=", "processing")]), busy)
        self.assertEqual(search([("id", "in", ids), ("display_state", "=", "waiting")]), idle)
        self.assertEqual(search([("id", "in", ids), ("display_state", "=", "failed")]), finished)
        self.assertEqual(
            search([("id", "in", ids), ("display_state", "!=", "processing")]), idle | finished
        )

    def test_processing_longer_than_30_minutes_is_flagged(self):
        now = fields.Datetime.now()
        old = self._new()
        self._link(old, self._job("started", date_started=now - timedelta(minutes=31)))
        recent = self._new()
        self._link(recent, self._job("started", date_started=now - timedelta(minutes=5)))
        queued_long = self._new()
        self._link(queued_long, self._job("pending", date_created=now - timedelta(hours=2)))
        done_long = self._new(state="imported")
        self._link(done_long, self._job("done", date_started=now - timedelta(hours=2)))
        self.assertTrue(old.processing_stale)
        self.assertFalse(recent.processing_stale)
        self.assertTrue(queued_long.processing_stale)
        self.assertFalse(done_long.processing_stale)
        ids = (old | recent | queued_long | done_long).ids
        self.assertEqual(
            self.Import.search([("id", "in", ids), ("processing_stale", "=", True)]),
            old | queued_long,
        )

    def test_billing_user_sees_processing_without_job_access(self):
        record = self._new()
        self._link(record, self._job("started"))
        as_user = record.with_user(self.billing_user)
        self.assertEqual(as_user.display_state, "processing")
        self.assertEqual(
            self.Import.with_user(self.billing_user).search(
                [("id", "=", record.id), ("display_state", "=", "processing")]
            ),
            record,
        )

    def test_enqueue_links_the_job_under_the_new_identity_key(self):
        record = self._new(state="failed", error_message="invented", failure_kind="data")
        record.action_retry()
        self.assertEqual(record.state, "waiting")
        self.assertFalse(record.error_message)
        self.assertFalse(record.failure_kind)
        job = record.queue_job_id
        self.assertTrue(job, "the retry's job is linked")
        self.assertEqual(job.sudo().identity_key, f"invoice-import:{record.id}")
        self.assertEqual(record.display_state, "processing")
        # The same key again links the job already queued, not a second one.
        record._enqueue_import("again")
        self.assertEqual(record.queue_job_id, job)
        self.assertEqual(
            self.env["queue.job"].sudo().search_count(
                [("identity_key", "=", f"invoice-import:{record.id}")]
            ),
            1,
        )
        self.assertTrue(lifecycle.is_import_identity_key(f"ai_invoice_import:{record.id}", record.id))
        self.assertTrue(lifecycle.is_import_identity_key(f"invoice-import:{record.id}", record.id))
        self.assertFalse(lifecycle.is_import_identity_key(f"invoice-import:{record.id + 1}", record.id))

    def test_retry_from_failed_or_from_waiting_without_an_active_job(self):
        for state in ("imported", "duplicate", "possibly_lost"):
            with self.subTest(state=state), self.assertRaises(UserError):
                self._new(state=state).action_retry()
        closed = self._new(state="closed", close_reason="test message")
        with self.assertRaises(UserError):
            closed.action_retry()
        # Waiting with no job, or with a finished one: retry imports it.
        for job_state in (None, "done", "failed", "cancelled"):
            with self.subTest(job_state=job_state):
                record = self._new(state="waiting")
                if job_state:
                    self._link(record, self._job(job_state))
                old_job = record.queue_job_id
                record.action_retry()
                self.assertTrue(record.queue_job_id)
                self.assertNotEqual(record.queue_job_id, old_job)
                self.assertEqual(record.display_state, "processing")
        # While a job is queued or running: refused, nothing enqueued.
        for job_state in ("pending", "enqueued", "started"):
            for state in ("waiting", "failed"):
                with self.subTest(job_state=job_state, state=state):
                    record = self._new(state=state)
                    job = self._job(job_state)
                    self._link(record, job)
                    with patch.object(type(self.Import), "with_delay") as delay, self.assertRaises(UserError):
                        record.action_retry()
                    delay.assert_not_called()
                    self.assertEqual(record.queue_job_id, job)
                    self.assertEqual(record.state, state)

    # -- _fail ---------------------------------------------------------------

    def test_fail_new_and_legacy_signatures(self):
        record = self._new()
        record._fail("import.totals_mismatch", "Totals don't add up", kind="interrupted")
        self.assertEqual(record.state, "failed")
        self.assertEqual(record.error_message, "Totals don't add up")
        self.assertEqual(record.failure_kind, "interrupted")
        legacy = self._new()
        legacy._fail("Claude API key is not configured", extra_vals={"extraction_data": "{}"})
        self.assertEqual(legacy.state, "failed")
        self.assertEqual(legacy.error_message, "Claude API key is not configured")
        self.assertEqual(legacy.failure_kind, "data")
        self.assertEqual(legacy.extraction_data, "{}")

    # -- close / reopen / override ----------------------------------------------

    def test_close_without_bill(self):
        for state in ("waiting", "failed", "duplicate", "possibly_lost"):
            with self.subTest(state=state):
                record = self._new(state=state)
                with patch.object(type(self.Import), "_on_settled", autospec=True) as settled:
                    record.action_close_without_bill("  test message from the vendor  ")
                settled.assert_called_once()
                self.assertEqual(settled.call_args.args[0], record)
                self.assertEqual(record.state, "closed")
                self.assertEqual(record.display_state, "closed")
                self.assertEqual(record.close_reason, "test message from the vendor")
                self.assertEqual(record.closed_by_id, self.env.user)
                self.assertTrue(record.closed_date)

    def test_close_requires_a_reason_and_an_unsettled_record(self):
        record = self._new(state="failed")
        for reason in ("", "   ", None):
            with self.subTest(reason=reason), self.assertRaises(UserError):
                record.action_close_without_bill(reason)
        self.assertEqual(record.state, "failed")
        for state in ("imported", "closed"):
            settled = self._new(state=state, close_reason="earlier" if state == "closed" else False)
            with self.subTest(state=state), self.assertRaises(UserError):
                settled.action_close_without_bill("again")
        with self.assertRaises(ValidationError):
            self._new(state="closed")

    def test_close_refused_while_processing(self):
        record = self._new()
        self._link(record, self._job("started"))
        with self.assertRaises(UserError):
            record.action_close_without_bill("handled by hand")
        self.assertEqual(record.state, "waiting")

    def test_on_settled_is_a_noop_in_the_core(self):
        self.assertIsNone(self._new()._on_settled())

    def test_reopen(self):
        record = self._new(state="failed")
        record.action_close_without_bill("closed by mistake")
        record.action_reopen()
        self.assertEqual(record.state, "waiting")
        self.assertFalse(record.close_reason)
        self.assertFalse(record.closed_by_id)
        self.assertFalse(record.closed_date)
        self.assertFalse(record.queue_job_id, "reopen enqueues nothing; the import runs on request")
        self.assertEqual(record.display_state, "waiting")
        self.assertIn("closed by mistake", " ".join(record.message_ids.mapped("body")))
        # ... and the request is Retry, which imports it from what it kept.
        record.action_retry()
        self.assertTrue(record.queue_job_id)
        self.assertEqual(record.display_state, "processing")
        with self.assertRaises(UserError):
            self._new(state="failed").action_reopen()
        bill = self.env["account.move"].create({"move_type": "in_invoice"})
        with_bill = self._new(state="closed", close_reason="handled by hand", move_id=bill.id)
        with self.assertRaises(UserError):
            with_bill.action_reopen()

    def test_override_duplicate(self):
        record = self._new(state="duplicate")
        record.action_override_duplicate()
        self.assertTrue(record.duplicate_override)
        self.assertEqual(record.state, "waiting")
        self.assertTrue(record.queue_job_id)
        for state in ("waiting", "failed", "imported"):
            with self.subTest(state=state), self.assertRaises(UserError):
                self._new(state=state).action_override_duplicate()

    def test_a_closed_record_is_not_imported_by_a_stray_job(self):
        record = self._new(state="closed", close_reason="test message")
        self.assertEqual(record._process_import(), "OK: already processed")
        self.assertEqual(record.state, "closed")

    def test_billing_user_closes_reopens_and_overrides(self):
        record = self._new(state="failed")
        Close = self.env["goodahead.invoice.import.close"].with_user(self.billing_user)
        action = record.with_user(self.billing_user).action_open_close_dialog()
        self.assertEqual(action["res_model"], "goodahead.invoice.import.close")
        with Form(Close.with_context(**action["context"])) as dialog:
            dialog.reason = "test message"
        dialog.record.action_confirm()
        self.assertEqual(record.state, "closed")
        self.assertEqual(record.closed_by_id, self.billing_user)
        record.with_user(self.billing_user).action_reopen()
        self.assertEqual(record.state, "waiting")
        self.assertFalse(record.queue_job_id)
        record.with_user(self.billing_user).action_retry()
        self.assertTrue(record.queue_job_id)
        duplicate = self._new(state="duplicate")
        duplicate.with_user(self.billing_user).action_override_duplicate()
        self.assertTrue(duplicate.duplicate_override)

    def test_the_dialog_requires_a_reason(self):
        record = self._new(state="failed")
        Close = self.env["goodahead.invoice.import.close"].with_context(
            default_import_ids=[(6, 0, record.ids)]
        )
        dialog = Form(Close)
        with self.assertRaises(AssertionError):
            dialog.save()
        blank = Close.create({"import_ids": [(6, 0, record.ids)], "reason": "   "})
        with self.assertRaises(UserError):
            blank.action_confirm()
        self.assertEqual(record.state, "failed")

    # -- structured sources need no PDF ---------------------------------------

    def test_import_without_a_pdf(self):
        ensure_purchase_journal(self.env)
        record = self._new(
            state="failed",
            extraction_data=json.dumps({"invoice_number": "inv-0042", "seller": {"vat": "FI00000001"}}),
        )
        self.assertFalse(record.pdf_attachment_id)
        Move = type(self.env["account.move"])
        with patch.object(Move, "_ai_generate_ubl_from_extracted", return_value=b"<Invoice/>"), patch.object(
            Move, "_ai_decode_ubl_into_move", return_value=None
        ), patch.object(Move, "_ai_post_process", return_value=None), patch.object(
            type(self.Import), "_import_before_reimport", return_value=None
        ):
            record.action_reimport_from_json()
        self.assertEqual(record.state, "imported", record.error_message or "")
        self.assertTrue(record.move_id)
        self.assertEqual(record.invoice_number_normalized, "INV0042")
        self.assertEqual(record.seller_vat, "FI00000001")


@tagged("post_install", "-at_install")
class TestBackfillAndNormalisation(TransactionCase):
    def test_normalize_invoice_number(self):
        cases = {
            "inv-001": "INV001",
            "INV 001": "INV001",
            " inv/001 ": "INV001",
            "A.B_C:D#1": "ABCD1",
            "2026-11/23": "20261123",
            "Äb-1": "B1",
            "": "",
            "---": "",
            None: "",
            False: "",
            12345: "12345",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(lifecycle.normalize_invoice_number(value), expected)

    def test_backfill_values(self):
        data = json.dumps(
            {"invoice_number": "a-77", "seller": {"vat": " FI12345678 ", "business_id": "1234567-8"}}
        )
        self.assertEqual(
            lifecycle.backfill_values("vendor@example.com", data),
            {
                "source": "email",
                "invoice_number_normalized": "A77",
                "seller_vat": "FI12345678",
                "seller_business_id": "1234567-8",
            },
        )
        empty = {
            "source": "upload",
            "invoice_number_normalized": False,
            "seller_vat": False,
            "seller_business_id": False,
        }
        for broken in (False, "", "not json", "[1, 2]", json.dumps({"seller": "text"})):
            with self.subTest(extraction=broken):
                self.assertEqual(lifecycle.backfill_values(False, broken), empty)

    def test_backfill_fills_old_records_without_touching_write_date(self):
        Import = self.env["goodahead.invoice.import"]
        with_data = Import.create(
            {
                "name": "with-data.pdf",
                "source_email": "vendor@example.com",
                "extraction_data": json.dumps(
                    {"invoice_number": "INV 10023", "seller": {"vat": "FI00000002", "business_id": "0000000-2"}}
                ),
            }
        )
        without_data = Import.create({"name": "without-data.pdf"})
        records = with_data | without_data
        # The stored inbox columns are computed at flush; flush now so
        # the raw UPDATE below is the last write, as on a real old row.
        self.env.flush_all()
        # As a record imported before the core/source split looks once the columns exist.
        self.env.cr.execute(
            "UPDATE goodahead_invoice_import SET source = 'upload', invoice_number_normalized = NULL, "
            "seller_vat = NULL, seller_business_id = NULL, write_date = '2025-01-01 00:00:00' "
            "WHERE id IN %s",
            (tuple(records.ids),),
        )
        self.env.invalidate_all()
        self.assertGreaterEqual(hooks.backfill(self.env), 2)
        self.assertEqual(with_data.source, "email")
        self.assertEqual(with_data.invoice_number_normalized, "INV10023")
        self.assertEqual(with_data.seller_vat, "FI00000002")
        self.assertEqual(with_data.seller_business_id, "0000000-2")
        self.assertEqual(without_data.source, "upload")
        self.assertFalse(without_data.invoice_number_normalized)
        self.assertFalse(without_data.seller_vat)
        self.assertEqual(
            set(records.mapped("write_date")), {fields.Datetime.to_datetime("2025-01-01 00:00:00")}
        )
