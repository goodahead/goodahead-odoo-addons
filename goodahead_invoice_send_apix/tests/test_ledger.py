# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The delivery ledger: claim, start, finish, record and cancel in
transactions of their own.

Two kinds of tests:

- ``TestLedger`` runs inside the test's transaction: under the test runner
  the ledger writes through a savepoint of the test cursor, so the tests stay
  isolated (contract §1.4 rules, state machine, flags, late answers).
- ``TestLedgerRealDatabase`` makes the ledger open real cursors of its own
  (``ledger.FORCE_OWN_CURSOR``) on committed invented data, and races two
  connections: one claim wins, a start and a cancel resolve to one winner, a
  caller holding the invoice row cannot deadlock the ledger, and a caller's
  rollback keeps what the ledger recorded. Its data is removed afterwards.

Every name here is invented.
"""

import threading
import time

import psycopg2.errors
import unittest
from datetime import timedelta
from unittest.mock import patch

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo import SUPERUSER_ID, api, fields  # noqa: E402
from odoo.modules.registry import DummyRLock, Registry  # noqa: E402
from odoo.tests import TransactionCase, new_test_user, tagged  # noqa: E402

from odoo.addons.account.tests.common import AccountTestInvoicingCommon  # noqa: E402

from ..models import ledger  # noqa: E402
from ..models.ledger import Holder  # noqa: E402

MODEL = "goodahead.invoice.send.attempt"
TABLE = "goodahead_invoice_send_attempt"


@tagged("post_install", "-at_install")
class TestLedger(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.move = cls.init_invoice("out_invoice", amounts=[100.0], post=True)
        cls.other_move = cls.init_invoice("out_invoice", amounts=[50.0], post=True)
        cls.user_a = new_test_user(cls.env, login="zz_ledger_a", groups="account.group_account_invoice")
        cls.user_b = new_test_user(cls.env, login="zz_ledger_b", groups="account.group_account_invoice")
        cls.Attempt = cls.env[MODEL]

    def _read(self, attempt):
        """The attempt (a record or the ledger's ``AttemptView``) read fresh
        in the test's transaction (the ledger writes through its savepoint)."""
        row = self.Attempt.browse(attempt.id)
        row.invalidate_recordset()
        return row

    def _queued(self, move=None, user=None, run_uuid=False):
        """A queued attempt as the send doors create it (in the caller's
        transaction, with its job - T30)."""
        created = self.Attempt._ledger().create({
            "move_id": (move or self.move).id,
            "channel": "einvoice",
            "state": "queued",
            "origin": "mass_background",
            "requested_by_id": (user or self.user_a).id,
            "queued_date": fields.Datetime.now(),
            "run_uuid": run_uuid,
        })
        return self.Attempt.browse(created.id)

    # -- claim -------------------------------------------------------------

    def test_claim_lock_timeout_answers_in_progress(self):
        """Review R8: when the claim waits past the ledger's lock timeout
        (another transaction holds the invoice), it answers "in progress",
        never a raw database error."""
        def locked(*args, **kwargs):
            raise psycopg2.errors.LockNotAvailable("canceling statement due to lock timeout")

        with patch.object(type(self.Attempt), "_ledger_insert_running", locked):
            holder = self.Attempt._ledger_claim(self.move, "einvoice", "button", self.user_a)
        self.assertIsInstance(holder, Holder)
        self.assertEqual(holder.code, "send.in_progress")
        self.assertIn("in progress", holder.message(self.env))
        self.assertFalse(self.Attempt.search([("move_id", "=", self.move.id)]))

    def test_claim_inserts_a_running_attempt(self):
        attempt = self.Attempt._ledger_claim(self.move, "einvoice", "button", self.user_a)
        self.assertNotIsInstance(attempt, Holder)
        attempt = self._read(attempt)
        self.assertEqual(attempt.state, "running")
        self.assertEqual(attempt.move_id, self.move)
        self.assertEqual(attempt.requested_by_id, self.user_a)
        self.assertTrue(attempt.start_date)
        self.assertFalse(attempt.is_latest)

    def test_second_claim_gets_the_holder(self):
        first = self.Attempt._ledger_claim(self.move, "einvoice", "button", self.user_a)
        holder = self.Attempt._ledger_claim(self.move, "email", "send_print", self.user_b)
        self.assertIsInstance(holder, Holder)
        self.assertEqual(holder.attempt_id, first.id)
        self.assertEqual(holder.user_id, self.user_a.id)
        self.assertEqual(holder.user_name, self.user_a.name)
        self.assertEqual(holder.since, self._read(first).start_date)
        self.assertEqual(holder.code, "send.in_progress")
        self.assertIn(self.user_a.name, holder.message())
        # only one attempt in progress, and the other invoice is free
        in_progress = self.Attempt.search([("move_id", "=", self.move.id),
                                           ("state", "in", ("queued", "running"))])
        self.assertEqual(len(in_progress), 1)
        other = self.Attempt._ledger_claim(self.other_move, "einvoice", "button", self.user_b)
        self.assertNotIsInstance(other, Holder)

    def test_queued_send_holds_the_invoice(self):
        queued = self._queued(user=self.user_b)
        holder = self.Attempt._ledger_claim(self.move, "einvoice", "button", self.user_a)
        self.assertIsInstance(holder, Holder)
        self.assertEqual(holder.attempt_id, queued.id)
        self.assertEqual(holder.user_id, self.user_b.id)
        self.assertEqual(holder.state, "queued")

    def test_claim_fails_a_send_running_for_more_than_ten_minutes(self):
        hung = self.Attempt._ledger_claim(self.move, "einvoice", "button", self.user_a)
        old = fields.Datetime.now() - timedelta(minutes=11)
        self.env.cr.execute(f"UPDATE {TABLE} SET start_date = %s WHERE id = %s", [old, hung.id])
        attempt = self.Attempt._ledger_claim(self.move, "einvoice", "button", self.user_b)
        self.assertNotIsInstance(attempt, Holder)
        hung = self._read(hung)
        self.assertEqual(hung.state, "failed")
        self.assertIn("[send.ran_too_long]", hung.reason)
        self.assertIn("may already have reached APIX", hung.reason)
        self.assertTrue(hung.end_date)
        self.assertTrue(hung.is_latest)
        self.assertTrue(hung.needs_attention)
        self.assertEqual(self._read(attempt).state, "running")

    def test_claim_keeps_a_send_running_for_less_than_ten_minutes(self):
        running = self.Attempt._ledger_claim(self.move, "einvoice", "button", self.user_a)
        recent = fields.Datetime.now() - timedelta(minutes=9)
        self.env.cr.execute(f"UPDATE {TABLE} SET start_date = %s WHERE id = %s", [recent, running.id])
        holder = self.Attempt._ledger_claim(self.move, "einvoice", "button", self.user_b)
        self.assertIsInstance(holder, Holder)
        self.assertEqual(self._read(running).state, "running")

    def test_ended_send_releases_the_invoice(self):
        for state in ("sent", "refused", "failed"):
            with self.subTest(state=state):
                attempt = self.Attempt._ledger_claim(self.move, "einvoice", "button", self.user_a)
                self.Attempt._ledger_finish(attempt, state, {"reason": f"[zz] {state}"})
                again = self.Attempt._ledger_claim(self.move, "einvoice", "button", self.user_a)
                self.assertNotIsInstance(again, Holder)
                self.Attempt._ledger_finish(again, "sent", {})

    # -- start ------------------------------------------------------

    def test_start_moves_a_queued_attempt_to_running(self):
        queued = self._queued()
        self.assertTrue(self.Attempt._ledger_start(queued))
        queued = self._read(queued)
        self.assertEqual(queued.state, "running")
        self.assertTrue(queued.start_date)
        self.assertFalse(self.Attempt._ledger_start(queued), "already running: do nothing")

    def test_start_of_a_cancelled_attempt_does_nothing(self):
        queued = self._queued()
        self.assertTrue(self.Attempt._ledger_cancel(queued, self.user_b))
        self.assertFalse(self.Attempt._ledger_start(queued))
        self.assertEqual(self._read(queued).state, "cancelled")

    # -- finish ----------------------------------------------------------------------

    def test_finish_sets_the_outcome_and_the_flags(self):
        attempt = self.Attempt._ledger_claim(self.move, "einvoice", "button", self.user_a)
        values = {
            "reason": "[send.refused] ZZ refused",
            "apix_status_code": "4001",
            "apix_batch_id": "ZZ-BATCH",
            "apix_document_id": "ZZ-DOC",
            "apix_credits": 1.5,
            "schema_result": "valid",
            "units_without_code": "ZZ erä",
            "warning_text": "ZZ warning",
        }
        self.Attempt._ledger_finish(attempt, "refused", values)
        attempt = self._read(attempt)
        self.assertEqual(attempt.state, "refused")
        self.assertTrue(attempt.end_date)
        self.assertFalse(attempt.counts_as_sent)
        self.assertTrue(attempt.is_latest)
        self.assertTrue(attempt.needs_attention)
        for key, value in values.items():
            self.assertEqual(attempt[key], value, key)

    def test_finish_moves_latest_and_attention(self):
        first = self.Attempt._ledger_claim(self.move, "einvoice", "button", self.user_a)
        self.Attempt._ledger_finish(first, "failed", {"reason": "[send.unreachable] zz"})
        self.assertTrue(self._read(first).needs_attention)
        second = self.Attempt._ledger_claim(self.move, "einvoice", "button", self.user_a)
        self.Attempt._ledger_finish(second, "accepted", {"apix_document_id": "ZZ-2"})
        first, second = self._read(first), self._read(second)
        self.assertFalse(first.is_latest)
        self.assertFalse(first.needs_attention)
        self.assertTrue(second.is_latest)
        self.assertFalse(second.needs_attention)
        self.assertTrue(second.counts_as_sent)
        latest = self.Attempt.search([("move_id", "=", self.move.id), ("is_latest", "=", True)])
        self.assertEqual(latest, second)

    def test_finish_counts_sent_and_accepted_only(self):
        expected = {"accepted": True, "sent": True, "refused": False, "failed": False,
                    "downloaded": False}
        for state, counts in expected.items():
            with self.subTest(state=state):
                attempt = self.Attempt._ledger_claim(self.move, "einvoice", "button", self.user_a)
                self.Attempt._ledger_finish(attempt, state, {})
                self.assertEqual(self._read(attempt).counts_as_sent, counts)

    def test_finish_refuses_a_state_that_is_not_an_outcome(self):
        attempt = self.Attempt._ledger_claim(self.move, "einvoice", "button", self.user_a)
        for state in ("queued", "running", "cancelled"):
            with self.subTest(state=state), self.assertRaises(ValueError):
                self.Attempt._ledger_finish(attempt, state, {})

    def test_finish_refuses_fields_it_does_not_own(self):
        attempt = self.Attempt._ledger_claim(self.move, "einvoice", "button", self.user_a)
        with self.assertRaises(ValueError):
            self.Attempt._ledger_finish(attempt, "sent", {"move_id": self.other_move.id})

    def test_late_answer_after_the_sweep_is_a_new_attempt(self):
        hung = self.Attempt._ledger_claim(self.move, "einvoice", "button", self.user_a)
        old = fields.Datetime.now() - timedelta(minutes=11)
        self.env.cr.execute(f"UPDATE {TABLE} SET start_date = %s WHERE id = %s", [old, hung.id])
        self.Attempt._ledger_claim(self.other_move, "einvoice", "button", self.user_a)  # unrelated
        newer = self.Attempt._ledger_claim(self.move, "einvoice", "button", self.user_b)
        self.Attempt._ledger_finish(newer, "failed", {"reason": "[zz] newer"})
        swept = self._read(hung)
        self.assertEqual(swept.state, "failed")
        before = swept.read(["state", "reason", "end_date"])

        late = self.Attempt._ledger_finish(hung, "accepted", {"apix_document_id": "ZZ-LATE"})
        self.assertNotEqual(late.id, hung.id)
        self.assertEqual(self._read(hung).read(["state", "reason", "end_date"]), before)
        late = self._read(late)
        self.assertEqual(late.origin, "late_answer")
        self.assertEqual(late.late_for_attempt_id, swept)
        self.assertEqual(late.state, "accepted")
        self.assertEqual(late.move_id, self.move)
        self.assertEqual(late.requested_by_id, self.user_a)
        self.assertEqual(late.apix_document_id, "ZZ-LATE")
        self.assertTrue(late.counts_as_sent)
        self.assertTrue(late.is_latest)
        self.assertFalse(self._read(newer).is_latest)

    # -- record ----------------------------------------------------------------------

    def test_record_an_email_send(self):
        attempt = self.Attempt._ledger_record(
            self.move, "email", "sent", "send_print", self.user_a, {}
        )
        attempt = self._read(attempt)
        self.assertEqual((attempt.state, attempt.channel, attempt.origin),
                         ("sent", "email", "send_print"))
        self.assertTrue(attempt.counts_as_sent)
        self.assertTrue(attempt.is_latest)
        self.assertTrue(attempt.start_date and attempt.end_date)

    def test_record_a_carried_over_send_once_per_source(self):
        when = fields.Datetime.now() - timedelta(days=30)
        values = {"migration_source": "zz_source", "end_date": when, "apix_document_id": "ZZ-OLD"}
        first = self.Attempt._ledger_record(self.move, "einvoice", "sent", "migration",
                                            self.user_a, values)
        again = self.Attempt._ledger_record(self.move, "einvoice", "sent", "migration",
                                            self.user_a, values)
        self.assertEqual(again.id, first.id)
        self.assertEqual(self._read(first).end_date, when)
        self.assertEqual(
            self.Attempt.search_count([("move_id", "=", self.move.id), ("origin", "=", "migration")]),
            1,
        )

    def test_record_refuses_an_in_progress_state(self):
        with self.assertRaises(ValueError):
            self.Attempt._ledger_record(self.move, "email", "running", "send_print", self.user_a, {})

    # -- cancel -------------------------------------------------------------

    def test_cancel_a_queued_send(self):
        queued = self._queued()
        self.assertTrue(self.Attempt._ledger_cancel(queued, self.user_b))
        queued = self._read(queued)
        self.assertEqual(queued.state, "cancelled")
        self.assertEqual(queued.cancelled_by_id, self.user_b)
        self.assertTrue(queued.end_date)
        self.assertIn("[send.cancelled]", queued.reason)
        self.assertFalse(queued.needs_attention)
        # the invoice is free again at once
        attempt = self.Attempt._ledger_claim(self.move, "einvoice", "button", self.user_a)
        self.assertNotIsInstance(attempt, Holder)

    def test_cancel_with_another_code(self):
        queued = self._queued()
        self.assertTrue(self.Attempt._ledger_cancel(
            queued, self.user_b, code="send.cancelled_by_neutralization"))
        self.assertIn("[send.cancelled_by_neutralization]", self._read(queued).reason)

    def test_cancel_of_a_running_send_does_nothing(self):
        running = self.Attempt._ledger_claim(self.move, "einvoice", "button", self.user_a)
        self.assertFalse(self.Attempt._ledger_cancel(running, self.user_b))
        self.assertEqual(self._read(running).state, "running")

    def test_cancel_cancels_the_job(self):
        queued = self._queued()
        # any stored job stands in for the send job
        delayed = self.env.user.sudo().with_delay(description="ZZ ledger job").exists()
        job = self.env["queue.job"].sudo().search([("uuid", "=", delayed.uuid)])
        self.assertEqual(job.state, "pending")
        queued._ledger().write({"queue_job_id": job.id})
        self.assertTrue(self.Attempt._ledger_cancel(queued, self.user_b))
        job.invalidate_recordset()
        self.assertEqual(job.state, "cancelled")

    # -- run status ------------------------------------------------------------------

    def test_run_status_reads_the_run(self):
        run = "zz-run-1"
        first = self._queued(run_uuid=run)
        second = self._queued(move=self.other_move, run_uuid=run)
        self.Attempt._ledger_start(first)
        self.Attempt._ledger_finish(first, "sent", {})
        rows = self.Attempt._ledger_run_status(run)
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(set(by_id), {first.id, second.id})
        self.assertEqual(by_id[first.id]["state"], "sent")
        self.assertEqual(by_id[second.id]["state"], "queued")
        self.assertEqual(by_id[second.id]["move_id"], self.other_move.id)
        self.assertEqual(self.Attempt._ledger_run_status("zz-no-such-run"), [])

    # -- the ledger never writes the invoice -----------------------------------------

    def test_ledger_never_writes_account_move(self):
        queries = []

        def spy(cr_self, query, params=None, log_exceptions=True):
            queries.append(query.code if hasattr(query, "code") else str(query))
            return real_execute(cr_self, query, params, log_exceptions)

        cursor_class = type(self.env.cr)
        real_execute = cursor_class.execute
        with patch.object(cursor_class, "execute", spy):
            attempt = self.Attempt._ledger_claim(self.move, "einvoice", "button", self.user_a)
            self.Attempt._ledger_finish(attempt, "sent", {})
            queued = self._queued(move=self.other_move)
            self.Attempt._ledger_cancel(queued, self.user_a)
            self.Attempt._ledger_record(self.move, "email", "sent", "send_print", self.user_a, {})
        writes = [q for q in queries if q.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE"))]
        self.assertTrue(writes)
        for query in writes:
            self.assertNotRegex(
                query, r'^\s*(UPDATE|INSERT INTO|DELETE FROM)\s+"?account_move"?[\s(]'
            )


@tagged("post_install", "-at_install")
class TestLedgerRealDatabase(TransactionCase):
    """Two connections at once, on committed data."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.registry_ = cls.registry
        with cls.registry_.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            company = env.ref("base.main_company")
            journal = env["account.journal"].create({
                "name": "ZZ Ledger Journal", "code": "ZZLDG", "type": "general",
                "company_id": company.id,
            })
            moves = env["account.move"].create([
                {"journal_id": journal.id, "move_type": "entry", "ref": f"ZZ ledger {i}"}
                for i in range(4)
            ])
            cls.journal_id = journal.id
            cls.move_ids = moves.ids
        cls.addClassCleanup(cls._remove_committed_data)

    @classmethod
    def _remove_committed_data(cls):
        with cls.registry_.cursor() as cr:
            cr.execute("DELETE FROM account_move WHERE id = ANY(%s)", [cls.move_ids])
            cr.execute("DELETE FROM account_journal WHERE id = %s", [cls.journal_id])

    def setUp(self):
        super().setUp()
        patcher = patch.object(ledger, "FORCE_OWN_CURSOR", True)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The test runner's thread holds the registry lock for the whole run;
        # the racing threads would wait on it forever (HttpCase does the same).
        lock_patcher = patch.object(Registry, "_lock", DummyRLock())
        lock_patcher.start()
        self.addCleanup(lock_patcher.stop)

    def _in_new_cursor(self, function):
        """Run ``function(env)`` on a committed connection of its own."""
        with self.registry_.cursor() as cr:
            return function(api.Environment(cr, SUPERUSER_ID, {}))

    def _attempts(self, move_id):
        def read(env):
            rows = env[MODEL].search_read([("move_id", "=", move_id)],
                                          ["state", "origin", "reason"], order="id")
            return rows
        return self._in_new_cursor(read)

    def _race(self, *functions):
        """Run each ``function(env)`` in its own thread and connection, all
        released at once; return their results in order."""
        barrier = threading.Barrier(len(functions))
        results = [None] * len(functions)
        errors = []

        def run(index, function):
            try:
                with self.registry_.cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    barrier.wait(timeout=10)
                    results[index] = function(env)
            except Exception as error:  # reported below, in the test thread
                errors.append(error)

        threads = [threading.Thread(target=run, args=(i, f)) for i, f in enumerate(functions)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse([t for t in threads if t.is_alive()], "a racing connection hung")
        self.assertFalse(errors, errors)
        return results

    def test_two_connections_racing_the_claim(self):
        move_id = self.move_ids[0]

        def claim(env):
            result = env[MODEL]._ledger_claim(env["account.move"].browse(move_id),
                                              "einvoice", "button", env.user)
            return result if isinstance(result, Holder) else result.id

        first, second = self._race(claim, claim)
        winners = [r for r in (first, second) if not isinstance(r, Holder)]
        holders = [r for r in (first, second) if isinstance(r, Holder)]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(holders), 1)
        self.assertEqual(holders[0].attempt_id, winners[0])
        self.assertEqual(holders[0].user_id, SUPERUSER_ID)
        self.assertTrue(holders[0].since)
        rows = self._attempts(move_id)
        self.assertEqual([row["state"] for row in rows], ["running"])

    def test_job_start_and_cancel_race(self):
        move_id = self.move_ids[1]

        def queue(env):
            return env[MODEL]._ledger().create({
                "move_id": move_id, "channel": "einvoice", "state": "queued",
                "origin": "mass_background", "requested_by_id": SUPERUSER_ID,
                "queued_date": fields.Datetime.now(),
            }).id

        attempt_id = self._in_new_cursor(queue)

        def start(env):
            return env[MODEL]._ledger_start(env[MODEL].browse(attempt_id))

        def cancel(env):
            return env[MODEL]._ledger_cancel(env[MODEL].browse(attempt_id), env.user)

        started, cancelled = self._race(start, cancel)
        self.assertNotEqual(started, cancelled, "exactly one of them wins")
        state = self._attempts(move_id)[0]["state"]
        self.assertEqual(state, "running" if started else "cancelled")

    def test_caller_holding_the_invoice_neither_deadlocks_nor_loses_the_outcome(self):
        move_id = self.move_ids[2]
        cr = self.registry_.cursor()
        try:
            env = api.Environment(cr, SUPERUSER_ID, {})
            move = env["account.move"].browse(move_id)
            # the caller's transaction writes the invoice and keeps it open
            move.write({"ref": "ZZ ledger caller wrote this"})
            env.flush_all()
            started = time.monotonic()
            attempt = env[MODEL]._ledger_claim(move, "einvoice", "button", env.user)
            self.assertNotIsInstance(attempt, Holder)
            env[MODEL]._ledger_finish(attempt, "accepted", {"apix_document_id": "ZZ-KEPT"})
            self.assertLess(time.monotonic() - started, 5, "the ledger waited on the caller")
            cr.rollback()  # the caller's transaction fails afterwards
        finally:
            cr.close()
        rows = self._attempts(move_id)
        self.assertEqual([row["state"] for row in rows], ["accepted"])
        ref = self._in_new_cursor(lambda env: env["account.move"].browse(move_id).ref)
        self.assertEqual(ref, "ZZ ledger 2", "the caller's own write was rolled back")

    def test_late_answer_from_another_connection(self):
        move_id = self.move_ids[3]

        def claim(env):
            return env[MODEL]._ledger_claim(env["account.move"].browse(move_id),
                                            "einvoice", "button", env.user).id

        hung_id = self._in_new_cursor(claim)

        def backdate(env):
            env.cr.execute(f"UPDATE {TABLE} SET start_date = %s WHERE id = %s",
                           [fields.Datetime.now() - timedelta(minutes=11), hung_id])

        self._in_new_cursor(backdate)
        self._in_new_cursor(claim)  # the next claim fails the hung send

        def finish(env):
            return env[MODEL]._ledger_finish(env[MODEL].browse(hung_id), "sent", {}).id

        late_id = self._in_new_cursor(finish)
        self.assertNotEqual(late_id, hung_id)
        rows = {row["id"]: row for row in self._in_new_cursor(
            lambda env: env[MODEL].search_read([("move_id", "=", move_id)],
                                               ["state", "origin", "late_for_attempt_id"]))}
        self.assertEqual(rows[hung_id]["state"], "failed")
        self.assertEqual(rows[late_id]["origin"], "late_answer")
        self.assertEqual(rows[late_id]["late_for_attempt_id"][0], hung_id)
