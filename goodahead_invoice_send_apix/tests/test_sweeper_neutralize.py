# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The every-minute sweeper and the sending neutralization (send T33;
AC-41, AC-59; contract §1.4 ``_ledger_sweep``, §12; events.md
``apix.send.sweep.v1``; sad.md Flow E).

The clock is the ``now`` the sweeper is given; start dates are set by SQL.
Two companies, invented data only.
"""

import unittest
import uuid
from datetime import timedelta

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo import fields
from odoo.modules.neutralize import get_neutralization_queries, neutralize_database
from odoo.tests import tagged

from ..models.ledger import CANCELLED_BY_NEUTRALIZATION, JOB_LOST, RAN_TOO_LONG
from .common import SendApixCommon

ATTEMPT = "goodahead.invoice.send.attempt"


@tagged("post_install", "-at_install")
class TestSweeperNeutralize(SendApixCommon):
    def setUp(self):
        super().setUp()
        self.Attempt = self.env[ATTEMPT]
        self.now = fields.Datetime.now()

    def _running(self, minutes_ago, company=None):
        move = self._make_invoice(company=company)
        attempt = self._row(self.Attempt._ledger_claim(move, "einvoice", "button",
                                                       self.accountant))
        self.env.cr.execute(
            "UPDATE goodahead_invoice_send_attempt SET start_date = %s WHERE id = %s",
            (self.now - timedelta(minutes=minutes_ago), attempt.id),
        )
        attempt.invalidate_recordset()
        return attempt

    def _queued(self, company=None):
        move = self._make_invoice(company=company)
        return move.with_user(self.accountant)._apix_queue(
            str(uuid.uuid4()), "mass_background", 20
        )["queued"]

    def _job(self, attempt):
        return attempt.sudo().queue_job_id

    def _code(self, attempt):
        attempt.invalidate_recordset()
        return (attempt.reason or "").split("]")[0].lstrip("[")

    # -- the 10-minute rule ------------------------------------------------------------

    def test_running_eleven_minutes_is_failed_and_released(self):
        attempt = self._running(11)
        self.assertEqual(self.Attempt._ledger_sweep(self.now), 1)
        attempt.invalidate_recordset()
        self.assertEqual(attempt.state, "failed")
        self.assertEqual(self._code(attempt), RAN_TOO_LONG)
        self.assertIn("may already have reached APIX", attempt.reason)
        self.assertTrue(attempt.needs_attention)
        self.assertTrue(attempt.end_date)
        move = attempt.move_id
        move.invalidate_recordset()
        self.assertFalse(move.send_in_progress, "sendable again without waiting")

    def test_running_nine_minutes_is_left_alone(self):
        attempt = self._running(9)
        self.assertEqual(self.Attempt._ledger_sweep(self.now), 0)
        attempt.invalidate_recordset()
        self.assertEqual(attempt.state, "running")

    def test_overlapping_runs_fail_each_attempt_once(self):
        attempt = self._running(15)
        self.assertEqual(self.Attempt._ledger_sweep(self.now), 1)
        self.assertEqual(self.Attempt._ledger_sweep(self.now), 0)
        self.assertEqual(self.Attempt.search_count([("move_id", "=", attempt.move_id.id)]), 1)

    def test_both_companies_are_swept(self):
        a = self._running(12)
        b = self._running(12, company=self.company_b)
        self.assertEqual(self.Attempt._ledger_sweep(self.now), 2)
        self.assertEqual({self._code(a), self._code(b)}, {RAN_TOO_LONG})

    # -- lost jobs (Flow E) ----------------------------------------------------------------------

    def test_queued_with_an_ended_job_is_job_lost(self):
        for job_state in ("failed", "cancelled", "done"):
            with self.subTest(job_state=job_state):
                attempt = self._queued()
                self._job(attempt).write({"state": job_state})
                self.assertEqual(self.Attempt._ledger_sweep(self.now), 1)
                attempt.invalidate_recordset()
                self.assertEqual(attempt.state, "failed")
                self.assertEqual(self._code(attempt), JOB_LOST)
                self.assertIn("nothing was sent", attempt.reason)

    def test_queued_whose_job_row_is_gone_is_job_lost(self):
        attempt = self._queued()
        self._job(attempt).unlink()
        self.assertEqual(self.Attempt._ledger_sweep(self.now), 1)
        self.assertEqual(self._code(attempt), JOB_LOST)

    def test_queued_with_a_waiting_job_is_left_alone(self):
        attempt = self._queued()
        self.env.cr.execute(
            "UPDATE goodahead_invoice_send_attempt SET queued_date = %s WHERE id = %s",
            (self.now - timedelta(hours=3), attempt.id),
        )
        for job_state in ("pending", "enqueued", "started"):
            with self.subTest(job_state=job_state):
                self._job(attempt).write({"state": job_state})
                self.assertEqual(self.Attempt._ledger_sweep(self.now), 0)
                attempt.invalidate_recordset()
                self.assertEqual(attempt.state, "queued")

    def test_cron_runs_every_minute(self):
        cron = self.env.ref("goodahead_invoice_send_apix.ir_cron_send_sweep").sudo()
        self.assertEqual((cron.interval_number, cron.interval_type), (1, "minutes"))
        self.assertEqual(cron.model_id.model, ATTEMPT)
        self.assertTrue(cron.active)
        # the scheduler runs it on a cursor of its own (not the test's): run
        # its code here
        self.assertEqual(cron.code.strip(), "model._ledger_sweep()")
        attempt = self._running(20)
        cron.ir_actions_server_id.with_context(active_test=False).run()
        attempt.invalidate_recordset()
        self.assertEqual(attempt.state, "failed")

    # -- neutralization --------------------------------------------------------------------

    def test_neutralize_script_is_shipped(self):
        queries = list(get_neutralization_queries(["goodahead_invoice_send_apix"]))
        self.assertEqual(len(queries), 1)
        self.assertIn("sending_enabled", queries[0])

    def test_neutralization_switches_sending_off_and_cancels_queued_sends(self):
        queued_a = self._queued()
        queued_b = self._queued(company=self.company_b)
        running = self._running(2)
        self.env.flush_all()
        neutralize_database(self.env.cr)
        self.env.invalidate_all()
        Connection = self.env["goodahead.apix.connection"].sudo()
        connections = Connection.search([("company_id", "in", (self.company | self.company_b).ids)])
        self.assertEqual(connections.mapped("sending_enabled"), [False, False])
        for attempt in (queued_a | queued_b).sudo():
            with self.subTest(attempt=attempt.id):
                self.assertEqual(attempt.state, "cancelled")
                self.assertEqual(self._code(attempt), CANCELLED_BY_NEUTRALIZATION)
                self.assertTrue(attempt.end_date)
                self.assertFalse(attempt.needs_attention)
                self.assertEqual(self._job(attempt).state, "cancelled")
        self.assertEqual(running.state, "running", "the sweeper fails running sends")
        # the copy can send only after an Administrator switches sending on again
        self.assertFalse(Connection._apix_sending_enabled(self.company))
