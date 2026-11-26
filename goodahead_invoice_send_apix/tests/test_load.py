# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Background sending at volume (send T39; spec §6 "Background sending",
"Isolation from other APIX work"; sad.md §7, §10 QG-1).

🔴 PRE-RELEASE ONLY: tagged ``pre_release`` and ``-standard``, so the normal
test run skips it; run it with ``--test-tags pre_release``.

1,000 invoices are queued at once through the mass wizard and every job is
run as the job runner runs it against a recorded APIX stub that takes 10 s
per call, scaled by ``GOODAHEAD_LOAD_TIME_FACTOR`` (default 0.001, i.e.
10 ms). The projected wall time with the channel capacity of 2
(``root.apix.send:2``) must stay within 3 h, scaled by the same factor for
the measured overhead; every invoice ends in exactly one terminal attempt.
Receiving keeps its slot by the channel topology (``root.apix.send`` under
``root.apix``), asserted here; the inbox age itself needs a staging run with
a real job runner (reported, not measured in-process). Also times the list
filters "Ready to send" / "Not ready" on the sent volume.
"""

import logging
import os
import time
import unittest

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo.tests import tagged

from odoo.addons.queue_job.job import Job

from .common import FakeApixSendClient, SendApixCommon

_logger = logging.getLogger(__name__)

VOLUME = 1000
APIX_SECONDS = 10.0
CAPACITY = 2
BUDGET_SECONDS = 3 * 3600
TIME_FACTOR = float(os.environ.get("GOODAHEAD_LOAD_TIME_FACTOR", "0.001"))
TERMINAL = {"accepted", "sent", "refused", "failed"}


class SlowStub(FakeApixSendClient):
    """The recorded APIX answer after ``APIX_SECONDS × TIME_FACTOR``."""

    def __call__(self, client, transfer_id, transfer_key, soft, ver, zip_bytes):
        time.sleep(APIX_SECONDS * TIME_FACTOR)
        return super().__call__(client, transfer_id, transfer_key, soft, ver, zip_bytes)


@tagged("-standard", "pre_release", "post_install", "-at_install")
class TestBackgroundLoad(SendApixCommon):
    def _vals_list(self):
        journal = self.env["account.journal"].search(
            [("company_id", "=", self.company.id), ("type", "=", "sale")], limit=1)
        tax = self.env["account.tax"].search([
            ("company_id", "=", self.company.id), ("type_tax_use", "=", "sale"),
            ("amount_type", "=", "percent"), ("amount", ">", 0),
        ], limit=1)
        return [{
            "move_type": "out_invoice",
            "partner_id": self.customer.id,
            "company_id": self.company.id,
            "journal_id": journal.id,
            "invoice_date": "2026-11-25",
            "partner_bank_id": self.bank_account.id,
            "invoice_line_ids": [(0, 0, {
                "name": f"ZZ load line {index}", "quantity": 1, "price_unit": 10.0,
                "tax_ids": [(6, 0, tax.ids)],
            })],
        } for index in range(VOLUME)]

    def test_list_filters_on_a_thousand_unsent_invoices(self):
        """Review R9: "Ready" / "Not ready" (worked out by the readiness
        rules) and the stored states (sent, in progress, failed) on 1,000
        posted invoices nobody sent yet; times are logged."""
        not_ready_customer = self._make_customer(
            "ZZ Load No Address Oy", business_id="0000004-3", invoice_sending_method="apix")
        vals_list = self._vals_list()
        for vals in vals_list[VOLUME // 2:]:
            vals["partner_id"] = not_ready_customer.id
        moves = self.env["account.move"].with_company(self.company).create(vals_list)
        moves.action_post()
        self.env["goodahead.invoice.send.attempt"]._ledger_record(
            moves[0], "email", "sent", "erp_send", self.accountant, {})
        Move = self.env["account.move"].with_user(self.accountant).with_context(
            allowed_company_ids=self.company.ids)
        expected = {"ready": VOLUME // 2 - 1, "not_ready": VOLUME // 2, "sent": 1,
                    "in_progress": 0, "failed": 0}
        for state, count in expected.items():
            self.env.invalidate_all()
            started = time.monotonic()
            found = Move.search([("send_state", "=", state), ("id", "in", moves.ids)])
            _logger.info("Filter %s over %s unsent invoices: %.2f s",
                         state, VOLUME, time.monotonic() - started)
            self.assertEqual(len(found), count, state)

    def test_channel_topology_keeps_a_slot_for_receiving(self):
        send = self.env.ref("goodahead_invoice_send_apix.channel_apix_send").sudo()
        self.assertEqual(send.complete_name, "root.apix.send")
        function = self.env.ref("goodahead_invoice_send_apix.job_function_apix_send_queued").sudo()
        self.assertEqual(function.channel_id, send)

    def test_a_thousand_background_sends(self):
        stub = SlowStub("accepted").patch(self)
        moves = self.env["account.move"].with_company(self.company).create(self._vals_list())
        moves.action_post()
        wizard = self.env["goodahead.invoice.send.wizard"].with_user(self.accountant).with_context(
            active_model="account.move", active_ids=moves.ids,
            allowed_company_ids=self.company.ids,
        ).create({})
        self.assertTrue(wizard.recommend_background)
        wizard.action_send_background()
        attempts = self.env["goodahead.invoice.send.attempt"].search(
            [("run_uuid", "=", wizard.run_uuid)], order="id")
        self.assertEqual(len(attempts), VOLUME)
        busy = 0.0
        for attempt in attempts:
            started = time.monotonic()
            job = Job.load(self.env, attempt.sudo().queue_job_id.uuid)
            job.set_started()
            job.store()
            job.set_done(result=job.perform())
            job.store()
            self._after_commit()
            busy += time.monotonic() - started
        attempts.invalidate_recordset()
        everything = self.env["goodahead.invoice.send.attempt"].search(
            [("move_id", "in", moves.ids)])
        self.assertEqual(len(everything), VOLUME, "no duplicated attempt")
        self.assertEqual(set(everything.mapped("state")) - TERMINAL, set(), "none left in progress")
        self.assertEqual(len(everything.move_id), VOLUME, "no invoice lost")
        self.assertEqual(len(stub.calls), VOLUME, "one APIX call per invoice")
        overhead = busy - VOLUME * APIX_SECONDS * TIME_FACTOR
        # the ERP's own work per send is real seconds; only the APIX wait is scaled
        projected = (overhead + VOLUME * APIX_SECONDS) / CAPACITY
        _logger.info(
            "Load: %s sends, busy %.1f s (time factor %s), overhead %.1f s; projected wall "
            "time at capacity %s with a 10 s APIX: %.0f s (budget %s s)",
            VOLUME, busy, TIME_FACTOR, overhead, CAPACITY, projected, BUDGET_SECONDS,
        )
        self.assertLessEqual(projected, BUDGET_SECONDS)

        Move = self.env["account.move"].with_user(self.accountant).with_context(
            allowed_company_ids=self.company.ids)
        for state in ("ready", "not_ready", "sent"):
            started = time.monotonic()
            Move.search([("send_state", "=", state), ("id", "in", moves.ids)])
            _logger.info("Filter %s over %s sent invoices: %.2f s",
                         state, VOLUME, time.monotonic() - started)
