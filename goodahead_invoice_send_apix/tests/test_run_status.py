# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Live per-invoice status of the mass wizard's send now, retry and the
polling widget (send T35; AC-11; contract §3.2 ``action_retry``, §3.3
``get_run_status``, §3.4 the widget; SCR-06 sending states).

Jobs run here one at a time as the job runner runs them; ``FakeApixSendClient``
answers SendInvoiceZIP (no network). Two companies, invented data only.
"""

import time
import unittest

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from freezegun import freeze_time
from odoo.addons.queue_job.job import Job
from odoo.addons.web.tests.test_js import unit_test_error_checker
from odoo.exceptions import UserError
from odoo.tests import HttpCase, new_test_user, tagged

from .common import BATCH_ID, FakeApixSendClient, SendApixCommon

ATTEMPT = "goodahead.invoice.send.attempt"
WIZARD = "goodahead.invoice.send.wizard"
LINE_KEYS = {
    "line_id", "move_id", "name", "company", "channel", "state", "code", "message",
    "batch_id", "warnings", "updated", "can_retry",
}


@tagged("post_install", "-at_install")
class TestRunStatus(SendApixCommon):
    def _wizard(self, moves, user=None, companies=None):
        user = user or self.accountant
        companies = companies or (self.company | self.company_b)
        return self.env[WIZARD].with_user(user).with_context(
            active_model="account.move", active_ids=moves.ids,
            allowed_company_ids=companies.ids,
        ).create({})

    def _line(self, wizard, move):
        return wizard.line_ids.filtered(lambda line: line.move_id == move)

    def _row(self, status, move):
        return next(row for row in status["lines"] if row["move_id"] == move.id)

    def _perform(self, attempt):
        """Run the attempt's job as the runner does."""
        record = attempt.sudo().queue_job_id
        job = Job.load(self.env, record.uuid)
        job.set_started()
        job.store()
        result = job.perform()
        job.set_done(result=result)
        job.store()
        self._after_commit()
        attempt.invalidate_recordset()
        return result

    # -- get_run_status ----------------------------------------------------------

    def test_status_shape_and_outcomes(self):
        """AC-11: each row's status as it is sent — accepted with the batch
        id, refused with the reason; done counts the ended rows."""
        first, second = self._make_invoice(), self._make_invoice()
        wizard = self._wizard(first | second)
        wizard.action_send_now()
        status = wizard.get_run_status()
        self.assertEqual(status["run_uuid"], wizard.run_uuid)
        self.assertEqual((status["total"], status["done"]), (2, 0))
        self.assertFalse(status["waiting_for_worker"])
        self.assertEqual(len(status["lines"]), 2)
        for row in status["lines"]:
            self.assertEqual(set(row), LINE_KEYS)
            self.assertEqual(row["state"], "queued")
            self.assertEqual(row["company"], self.company.name)
            self.assertEqual(row["channel"], "einvoice")

        FakeApixSendClient("accepted").patch(self)
        self._perform(self._line(wizard, first).attempt_id)
        status = wizard.get_run_status()
        row = self._row(status, first)
        self.assertEqual((row["state"], row["batch_id"]), ("accepted", BATCH_ID))
        self.assertEqual(row["name"], first.name)
        self.assertFalse(row["can_retry"])
        self.assertTrue(row["updated"])
        self.assertEqual((status["total"], status["done"]), (2, 1))

        FakeApixSendClient("refused").patch(self)
        self._perform(self._line(wizard, second).attempt_id)
        status = wizard.get_run_status()
        row = self._row(status, second)
        self.assertEqual((row["state"], row["code"]), ("refused", "send.refused"))
        self.assertTrue(row["message"])
        self.assertNotIn("[send.refused]", row["message"], "the code is its own key")
        self.assertTrue(row["can_retry"])
        self.assertEqual(status["done"], 2)

    def test_review_wizard_has_no_run(self):
        wizard = self._wizard(self._make_invoice())
        status = wizard.get_run_status()
        self.assertEqual((status["run_uuid"], status["total"], status["lines"]), (False, 0, []))

    def test_invoice_started_elsewhere_is_reported_and_done(self):
        """SCR-06 sending — in progress conflict: the row is shown with its
        holder and does not keep the run open."""
        first, second = self._make_invoice(), self._make_invoice()
        wizard = self._wizard(first | second)
        self.env[ATTEMPT]._ledger_claim(first, "einvoice", "button", self.accountant)
        wizard.action_send_now()
        status = wizard.get_run_status()
        row = self._row(status, first)
        self.assertEqual(row["state"], "in_progress")
        self.assertIn(self.accountant.name, row["message"])
        self.assertEqual((status["total"], status["done"]), (2, 1))

    def test_only_attempts_the_user_may_read(self):
        """Record rules: an attempt of another company under the same run
        never reaches the status."""
        user_a = new_test_user(
            self.env, login="zz_run_a_only", groups="account.group_account_invoice",
            company_id=self.company.id, company_ids=[(6, 0, self.company.ids)],
        )
        move_a = self._make_invoice()
        wizard = self._wizard(move_a, user=user_a, companies=self.company)
        wizard.action_send_now()
        move_b = self._make_invoice(company=self.company_b)
        self.env[ATTEMPT]._ledger_record(move_b, "email", "sent", "erp_send", self.accountant,
                                         {"run_uuid": wizard.run_uuid})
        status = wizard.get_run_status()
        self.assertEqual([row["move_id"] for row in status["lines"]], [move_a.id])
        self.assertEqual(status["total"], 1)

    # -- waiting for the worker (SAD §11) ----------------------------------------------------------

    def test_waiting_for_worker_after_30_seconds(self):
        moves = self._make_invoice() | self._make_invoice()
        with freeze_time("2026-11-25 10:00:00"):
            wizard = self._wizard(moves)
            wizard.action_send_now()
        with freeze_time("2026-11-25 10:00:29"):
            self.assertFalse(wizard.get_run_status()["waiting_for_worker"])
        with freeze_time("2026-11-25 10:00:31"):
            self.assertTrue(wizard.get_run_status()["waiting_for_worker"])

    def test_a_send_that_started_resets_the_wait(self):
        first, second = self._make_invoice(), self._make_invoice()
        with freeze_time("2026-11-25 10:00:00"):
            wizard = self._wizard(first | second)
            wizard.action_send_now()
        FakeApixSendClient("accepted").patch(self)
        with freeze_time("2026-11-25 10:00:20"):
            self._perform(self._line(wizard, first).attempt_id)
        with freeze_time("2026-11-25 10:00:45"):
            self.assertFalse(wizard.get_run_status()["waiting_for_worker"], "25 s since a start")
        with freeze_time("2026-11-25 10:00:51"):
            self.assertTrue(wizard.get_run_status()["waiting_for_worker"])

    def test_nothing_queued_is_not_waiting(self):
        move = self._make_invoice()
        with freeze_time("2026-11-25 10:00:00"):
            wizard = self._wizard(move)
            wizard.action_send_now()
        FakeApixSendClient("accepted").patch(self)
        self._perform(self._line(wizard, move).attempt_id)
        with freeze_time("2026-11-25 11:00:00"):
            self.assertFalse(wizard.get_run_status()["waiting_for_worker"])

    # -- retry ---------------------------------------------------------------------

    def test_retry_queues_again_under_the_same_run(self):
        """AC-11: fix and retry without leaving the wizard."""
        first, second = self._make_invoice(), self._make_invoice()
        wizard = self._wizard(first | second)
        wizard.action_send_now()
        line = self._line(wizard, first)
        refused = line.attempt_id
        FakeApixSendClient("refused").patch(self)
        self._perform(refused)
        line.action_retry()
        retried = line.attempt_id
        self.assertNotEqual(retried, refused)
        self.assertEqual((retried.state, retried.run_uuid, retried.origin),
                         ("queued", wizard.run_uuid, "mass_now"))
        self.assertEqual(retried.sudo().queue_job_id.priority, 10)
        status = wizard.get_run_status()
        self.assertEqual(status["total"], 2, "one row per invoice, its latest attempt")
        self.assertEqual(self._row(status, first)["state"], "queued")
        FakeApixSendClient("accepted").patch(self)
        self._perform(retried)
        self.assertEqual(self._row(wizard.get_run_status(), first)["state"], "accepted")

    def test_retry_only_a_refused_or_failed_row(self):
        move = self._make_invoice()
        wizard = self._wizard(move)
        wizard.action_send_now()
        with self.assertRaises(UserError):
            self._line(wizard, move).action_retry()
        self.assertEqual(len(self.env[ATTEMPT].search([("move_id", "=", move.id)])), 1)

    def test_retry_rechecks_first(self):
        move = self._make_invoice()
        wizard = self._wizard(move)
        wizard.action_send_now()
        FakeApixSendClient("refused").patch(self)
        self._perform(self._line(wizard, move).attempt_id)
        move.partner_id.edicode = False
        move.invalidate_recordset()
        with self.assertRaises(UserError):
            self._line(wizard, move).action_retry()
        self.assertFalse(self.env[ATTEMPT].search([("move_id", "=", move.id),
                                                   ("state", "=", "queued")]))

    # -- timing: spec §6 Mass send now ≤ 2 s ---------------------------------------------------------

    def test_twenty_invoices_status_visible_within_two_seconds(self):
        """The widget polls every second; each poll must answer well within
        the second left, so an outcome shows ≤ 2 s after APIX answered."""
        moves = self.env["account.move"]
        for _index in range(20):
            moves |= self._make_invoice()
        wizard = self._wizard(moves)
        wizard.action_send_now()
        FakeApixSendClient("accepted").patch(self)
        slowest = 0.0
        for line in wizard.line_ids:
            self._perform(line.attempt_id)
            started = time.monotonic()
            status = wizard.get_run_status()
            slowest = max(slowest, time.monotonic() - started)
            self.assertEqual(self._row(status, line.move_id)["state"], "accepted")
        self.assertEqual(status["done"], 20)
        self.assertLess(slowest, 1.0, f"a status poll took {slowest:.3f} s")


@tagged("post_install", "-at_install")
class TestRunStatusWidget(HttpCase):
    """The widget's unit tests (poll every second, stop when done or closed,
    three failures → status unavailable), run in the browser."""

    def test_run_status_widget(self):
        self.browser_js(
            "/web/tests?headless&loglevel=2&preset=desktop&timeout=60000"
            "&filter=ApixSendRunStatus",
            "",
            "",
            login="admin",
            timeout=900,
            success_signal="[HOOT] Test suite succeeded",
            error_checker=unit_test_error_checker,
        )
