# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Background sends: one prioritised job per invoice, run in the invoice's
company (send T30; AC-12, AC-15, AC-16, AC-53, AC-58; contract §1.1
``_apix_queue`` / ``_apix_send_queued``, events.md ``apix.send.requested.v1``,
sad.md Flow D).

Jobs are stored by the queue and run here one at a time, as the job runner
runs them (``Job.load(...).perform()``). ``FakeApixSendClient`` stands in
for SendInvoiceZIP: no network. Two companies, invented data only.
"""

import unittest
import uuid
from unittest.mock import patch

import psycopg2.errors

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo.addons.queue_job.job import Job

from ..models.account_move_queue import (
    CANNOT_CANCEL,
    COMPANY_PAUSED,
    NOT_READY_SINCE_QUEUED,
    SENDING_OFF_SINCE_QUEUED,
)
from ..models.account_move_send import CHANNEL_UNSUPPORTED, UNEXPECTED, UNREACHABLE
from .common import FakeApixSendClient, SendApixCommon
from odoo.tests import tagged

ATTEMPT = "goodahead.invoice.send.attempt"


class PerCompanyFake(FakeApixSendClient):
    """Answers by the calling company's transfer id: ``modes`` maps a
    transfer id to a ``FakeApixSendClient`` mode (default accepted)."""

    def __init__(self, modes):
        super().__init__("accepted")
        self.modes = modes

    def __call__(self, client, transfer_id, transfer_key, soft, ver, zip_bytes):
        self.mode = self.modes.get(transfer_id, "accepted")
        return super().__call__(client, transfer_id, transfer_key, soft, ver, zip_bytes)


@tagged("post_install", "-at_install")
class TestSendJobs(SendApixCommon):
    def setUp(self):
        super().setUp()
        self.connection = self.env["goodahead.apix.connection"]._apix_for_company(self.company)
        self.connection_b = self.env["goodahead.apix.connection"]._apix_for_company(self.company_b)

    def _as(self, moves, company=None):
        """``moves`` as the Accountant, active company ``company`` (A)."""
        company = company or self.company
        return moves.with_user(self.accountant).with_context(
            allowed_company_ids=[company.id] + [c.id for c in self.company | self.company_b
                                                if c != company]
        )

    def _queue(self, moves, origin="mass_background", priority=20, company=None, run=None):
        return self._as(moves, company)._apix_queue(run or str(uuid.uuid4()), origin, priority)

    def _job(self, attempt):
        return attempt.sudo().queue_job_id

    def _perform(self, attempt, commit=True):
        """Run the attempt's job as the runner does, then what its commit
        runs after it (``commit=False``: not yet)."""
        record = self._job(attempt)
        job = Job.load(self.env, record.uuid)
        job.set_started()
        job.store()
        result = job.perform()
        job.set_done(result=result)
        job.store()
        if commit:
            self._after_commit()
        attempt.invalidate_recordset()
        return result

    def _reason_code(self, attempt):
        return (attempt.reason or "").split("]")[0].lstrip("[")

    # -- queuing (AC-12, Flow C) -------------------------------------------------------------

    def test_queue_inserts_one_queued_attempt_and_one_job_per_invoice(self):
        moves = self._make_invoice() | self._make_invoice() | self._make_invoice(
            transmit_method="letter")
        run = str(uuid.uuid4())
        result = self._queue(moves, origin="mass_background", priority=20, run=run)
        queued = result["queued"]
        self.assertEqual(len(queued), 3, "the wizard tells how many were queued")
        self.assertEqual(result["in_progress"], {})
        self.assertEqual(set(queued.mapped("state")), {"queued"})
        self.assertEqual(set(queued.mapped("run_uuid")), {run})
        self.assertEqual(set(queued.mapped("origin")), {"mass_background"})
        self.assertEqual(queued.mapped("requested_by_id"), self.accountant)
        self.assertTrue(all(queued.mapped("queued_date")))
        self.assertEqual(sorted(queued.mapped("channel")), ["einvoice", "einvoice", "letter"])
        for attempt in queued:
            job = self._job(attempt)
            self.assertEqual(job.identity_key, f"apix-send:{attempt.id}")
            self.assertEqual(job.priority, 20)
            self.assertEqual(job.channel, "root.apix.send")
            self.assertEqual(job.state, "pending")
            self.assertEqual(job.method_name, "_apix_send_queued")
        moves.invalidate_recordset()
        self.assertTrue(all(moves.mapped("send_in_progress")))

    def test_send_now_jumps_ahead(self):
        result = self._queue(self._make_invoice(), origin="mass_now", priority=10)
        self.assertEqual(self._job(result["queued"]).priority, 10)
        self.assertEqual(result["queued"].origin, "mass_now")

    def test_same_invoice_queued_twice_is_in_progress(self):
        """Edge case: the second insert hits the in-progress rule; only that
        invoice is affected."""
        first, second = self._make_invoice(), self._make_invoice()
        self._queue(first)
        result = self._queue(first | second)
        self.assertEqual(result["queued"].move_id, second)
        self.assertIn(first.id, result["in_progress"])
        self.assertEqual(result["in_progress"][first.id].user_name, self.accountant.name)
        self.assertEqual(self.env[ATTEMPT].search_count([("move_id", "=", first.id)]), 1)

    def _serialization_failure(self, *args, **kwargs):
        raise psycopg2.errors.SerializationFailure("could not serialize access")

    def test_serialization_failure_without_a_holder_is_retried_not_in_progress(self):
        """Review F1, AC-08: a concurrent update that is not another send of
        the invoice is raised for the ERP to retry, never shown as "a send
        is in progress"."""
        move = self._make_invoice()
        with patch.object(type(self.env[ATTEMPT]), "_ledger_insert_row",
                          self._serialization_failure), \
                self.assertRaises(psycopg2.errors.SerializationFailure):
            self._queue(move)
        self.assertFalse(self.env[ATTEMPT].search([("move_id", "=", move.id)]))

    def test_serialization_failure_with_a_holder_is_in_progress(self):
        move = self._make_invoice()
        self.env[ATTEMPT]._ledger_claim(move, "einvoice", "button", self.accountant)
        with patch.object(type(self.env[ATTEMPT]), "_ledger_insert_row",
                          self._serialization_failure):
            result = self._queue(move)
        self.assertFalse(result["queued"])
        holder = result["in_progress"][move.id]
        self.assertEqual(holder.user_name, self.accountant.name)
        self.assertFalse(holder.busy)

    def test_no_retry_pattern_registered(self):
        function = self.env.ref("goodahead_invoice_send_apix.job_function_apix_send_queued").sudo()
        self.assertFalse(function.retry_pattern)
        self.assertEqual(function.channel, "root.apix.send")
        channel = self.env.ref("goodahead_invoice_send_apix.channel_apix_send").sudo()
        self.assertEqual(channel.parent_id, self.env.ref("goodahead_apix.channel_apix"))

    # -- the job (Flow D) -----------------------------------------------------------------------

    def test_job_sends_and_records_the_outcome(self):
        fake = FakeApixSendClient("accepted").patch(self)
        move = self._make_invoice()
        attempt = self._queue(move)["queued"]
        self._perform(attempt)
        self.assertEqual(attempt.state, "accepted")
        self.assertTrue(attempt.counts_as_sent)
        self.assertTrue(attempt.start_date)
        self.assertEqual(len(fake.calls), 1)
        move.invalidate_recordset()
        self.assertFalse(move.send_in_progress)

    def test_redelivered_job_does_nothing(self):
        fake = FakeApixSendClient("accepted").patch(self)
        attempt = self._queue(self._make_invoice())["queued"]
        self._perform(attempt)
        job = Job.load(self.env, self._job(attempt).uuid)
        job.perform()
        self.assertEqual(len(fake.calls), 1, "no second send")
        self.assertEqual(self.env[ATTEMPT].search_count([("move_id", "=", attempt.move_id.id)]), 1)

    def test_error_after_the_claim_is_recorded_and_never_retried(self):
        fake = FakeApixSendClient("accepted").patch(self)
        attempt = self._queue(self._make_invoice())["queued"]
        with patch.object(type(self.env["account.move"]), "_apix_finvoice_values",
                          side_effect=RuntimeError("ZZ boom")):
            self._perform(attempt)  # does not raise: the job ends normally
        self.assertEqual(attempt.state, "failed")
        self.assertEqual(self._reason_code(attempt), UNEXPECTED)
        self.assertFalse(fake.calls)

    def test_switched_off_since_queuing_fails_without_a_call(self):
        fake = FakeApixSendClient("accepted").patch(self)
        attempt = self._queue(self._make_invoice())["queued"]
        self.connection.sudo().write({"sending_enabled": False})
        self._perform(attempt)
        self.assertEqual(attempt.state, "failed")
        self.assertEqual(self._reason_code(attempt), SENDING_OFF_SINCE_QUEUED)
        self.assertFalse(fake.calls)

    def test_not_ready_since_queuing_names_the_items(self):
        fake = FakeApixSendClient("accepted").patch(self)
        customer = self._make_customer("ZZ Leaving Oy", "003700000027", "0000005-1")
        attempt = self._queue(self._make_invoice(customer))["queued"]
        customer.write({"edicode": False})
        self._perform(attempt)
        self.assertEqual(attempt.state, "failed")
        self.assertEqual(self._reason_code(attempt), NOT_READY_SINCE_QUEUED)
        self.assertIn("e-invoice address", attempt.reason)
        self.assertFalse(fake.calls)

    def test_channel_the_job_cannot_send(self):
        move = self._make_invoice()
        attempt = self.env[ATTEMPT]._ledger().create({
            "move_id": move.id, "channel": "unknown", "state": "queued",
            "origin": "mass_background", "requested_by_id": self.accountant.id,
            "queued_date": "2026-11-25 10:00:00",
        })
        job = move.with_company(move.company_id).with_delay()._apix_send_queued(attempt)
        attempt._ledger().write({"queue_job_id": job.db_record().id})
        self._perform(attempt)
        self.assertEqual(attempt.state, "failed")
        self.assertEqual(self._reason_code(attempt), CHANNEL_UNSUPPORTED)

    def test_email_attempt_goes_through_the_erp_mail(self):
        """AC-16: email through the ERP's email sending, recorded as sent."""
        fake = FakeApixSendClient("accepted").patch(self)
        move = self._make_invoice(transmit_method="email")
        attempt = self._queue(move)["queued"]
        self.assertEqual(attempt.channel, "email")
        messages_before = len(move.message_ids)
        self._perform(attempt)
        self.assertEqual(attempt.state, "sent", attempt.reason)
        self.assertTrue(attempt.counts_as_sent)
        self.assertFalse(fake.calls)
        move.invalidate_recordset()
        self.assertGreater(len(move.message_ids), messages_before)

    def test_email_job_is_sent_only_once_the_job_commits(self):
        """Review R5: the job's email counts once the job's transaction
        commits; a rolled-back job ends its attempt failed, nothing sent."""
        sent = self._queue(self._make_invoice(transmit_method="email"))["queued"]
        self._perform(sent, commit=False)
        self.assertEqual(sent.state, "running", "not sent before the job commits")
        self._after_commit()
        sent.invalidate_recordset()
        self.assertEqual(sent.state, "sent")
        undone = self._queue(self._make_invoice(transmit_method="email"))["queued"]
        self._perform(undone, commit=False)
        self._after_rollback()
        undone.invalidate_recordset()
        self.assertEqual(undone.state, "failed")
        self.assertTrue(undone.reason.startswith("[send.email_failed]"), undone.reason)
        self.assertFalse(undone.counts_as_sent)

    # -- two companies ------------------------------------------------------------

    def test_jobs_run_in_the_invoice_company_whoever_queued(self):
        """AC-53: queued from company B's session, A's invoice runs in A
        with A's connection."""
        fake = FakeApixSendClient("accepted").patch(self)
        move_a = self._make_invoice()
        attempt = self._queue(move_a, company=self.company_b)["queued"]
        job = self._job(attempt)
        self.assertEqual(job.company_id, self.company)
        self._perform(attempt)
        self.assertEqual(attempt.state, "accepted")
        self.assertEqual(fake.calls[0]["transfer_id"], self.connection.transfer_id)

    def test_failing_company_fails_alone(self):
        """AC-53: A's connection fails, its next invoice fails fast as paused
        (no call); B's invoice is sent normally."""
        fake = PerCompanyFake({self.connection.transfer_id: "unreachable"}).patch(self)
        a1, a2 = self._make_invoice(), self._make_invoice()
        b1 = self._make_invoice(company=self.company_b)
        queued = self._queue(a1 | a2 | b1)["queued"]
        by_move = {attempt.move_id: attempt for attempt in queued}
        for move in (a1, a2, b1):
            self._perform(by_move[move])
        self.assertEqual(self._reason_code(by_move[a1]), UNREACHABLE)
        self.assertEqual(by_move[a2].state, "failed")
        self.assertEqual(self._reason_code(by_move[a2]), COMPANY_PAUSED)
        self.assertIn("not working", by_move[a2].reason)
        self.assertEqual(by_move[b1].state, "accepted")
        self.assertEqual([call["transfer_id"] for call in fake.calls],
                         [self.connection.transfer_id, self.connection_b.transfer_id])

    def test_company_with_sending_off_einvoice_fails_email_goes_out(self):
        """AC-15: company B switched off: its e-invoice fails as off, its
        email goes out; company A is unaffected."""
        fake = FakeApixSendClient("accepted").patch(self)
        b_einvoice = self._make_invoice(company=self.company_b)
        b_email = self._make_invoice(company=self.company_b, transmit_method="email")
        a_einvoice = self._make_invoice()
        queued = self._queue(b_einvoice | b_email | a_einvoice)["queued"]
        self.connection_b.sudo().write({"sending_enabled": False})
        by_move = {attempt.move_id: attempt for attempt in queued}
        for attempt in queued:
            self._perform(attempt)
        self.assertEqual(self._reason_code(by_move[b_einvoice]), SENDING_OFF_SINCE_QUEUED)
        self.assertEqual(by_move[b_email].state, "sent")
        self.assertEqual(by_move[a_einvoice].state, "accepted")
        self.assertEqual(len(fake.calls), 1)

    # -- cancel ---------------------------------------------------------------------------

    def test_cancel_a_queued_send(self):
        fake = FakeApixSendClient("accepted").patch(self)
        move = self._make_invoice()
        attempt = self._queue(move)["queued"]
        action = attempt.with_user(self.accountant).action_cancel()
        self.assertEqual(action["tag"], "soft_reload")
        attempt.invalidate_recordset()
        self.assertEqual(attempt.state, "cancelled")
        self.assertEqual(attempt.cancelled_by_id, self.accountant)
        self.assertTrue(attempt.end_date)
        self.assertEqual(self._job(attempt).state, "cancelled")
        move.invalidate_recordset()
        self.assertFalse(move.send_in_progress)
        # free at once: transmit method change and a new send
        self._as(move).write({"transmit_method": "letter"})
        self._perform(attempt)
        self.assertFalse(fake.calls, "the cancelled send does not take place")
        self.assertEqual(attempt.state, "cancelled")

    def test_cancel_after_start_is_refused(self):
        attempt = self._queue(self._make_invoice())["queued"]
        self.env[ATTEMPT]._ledger_start(attempt)
        with self.assertRaises(Exception) as caught:
            attempt.with_user(self.accountant).action_cancel()
        self.assertEqual(getattr(caught.exception, "code", None), CANNOT_CANCEL)
        self.assertIn("Sending", str(caught.exception))
        attempt.invalidate_recordset()
        self.assertEqual(attempt.state, "running")
