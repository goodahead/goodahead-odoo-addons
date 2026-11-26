# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""No secret leaves the connection: after
sends through every door and outcome, neither the run's log, nor the stored
exchange, the attempt files, the chatter or the job records hold the
transfer id, the transfer key or a request digest. Invented credentials;
no network.
"""

import logging
import re
import unittest

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo.tests import tagged

from odoo.addons.queue_job.job import Job

from .common import FakeApixSendClient, SendApixCommon

TRANSFER_ID = "ZZ-TRANSFER-003700000011"
TRANSFER_KEY = "zz-key-003700000011"
DIGEST = re.compile(r"\b[0-9a-f]{64}\b")


@tagged("post_install", "-at_install")
class TestSecrets(SendApixCommon):
    def _secrets_in(self, text):
        text = text or ""
        found = [secret for secret in (TRANSFER_ID, TRANSFER_KEY) if secret in text]
        # a SHA-256 of the zip is allowed in the masked body line only
        digests = [d for d in DIGEST.findall(text) if f"sha256 {d}" not in text
                   and f"sha256={d}" not in text]
        return found + digests

    def _send_everything(self):
        moves = self.env["account.move"]
        for mode, status in (("accepted", None), ("warning", None), ("refused", None),
                             ("err", "4001"), ("no_answer", None), ("unreachable", None)):
            FakeApixSendClient(mode, status).patch(self)
            move = self._make_invoice()
            move.with_user(self.accountant)._apix_send_now("button")
            moves |= move
            self.env["goodahead.apix.connection"]._apix_for_company(self.company).sudo()\
                ._apix_close_send_circuit()
        FakeApixSendClient("accepted").patch(self)
        queued = self._make_invoice()
        result = queued.with_user(self.accountant)._apix_queue("zz-run", "mass_background", 20)
        job = Job.load(self.env, result["queued"].sudo().queue_job_id.uuid)
        job.set_started()
        job.store()
        job.set_done(result=job.perform())
        job.store()
        self._after_commit()
        return moves | queued

    def test_no_secret_in_logs_history_files_or_chatter(self):
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger(__name__).info("secret scan starts")
            moves = self._send_everything()
        self.assertEqual(self._secrets_in("\n".join(logs.output)), [], "the run's log")
        attempts = self.env["goodahead.invoice.send.attempt"].sudo().search(
            [("move_id", "in", moves.ids)])
        self.assertGreaterEqual(len(attempts), 7)
        for attempt in attempts:
            with self.subTest(attempt=attempt.id, state=attempt.state):
                for field in ("exchange_log", "reason", "warning_text", "schema_errors"):
                    self.assertEqual(self._secrets_in(attempt[field]), [], field)
                for attachment in attempt.message_attachment_id | attempt.package_attachment_id:
                    raw = attachment.raw or b""
                    self.assertNotIn(TRANSFER_ID.encode(), raw)
                    self.assertNotIn(TRANSFER_KEY.encode(), raw)
                if attempt.exchange_log:
                    self.assertIn("TraID=***", attempt.exchange_log)
        bodies = "\n".join(moves.message_ids.mapped(lambda m: str(m.body)))
        self.assertEqual(self._secrets_in(bodies), [], "chatter")
        jobs = attempts.queue_job_id
        self.assertTrue(jobs)
        for job in jobs:
            text = " ".join(str(job[name] or "") for name in ("name", "result", "exc_info"))
            self.assertEqual(self._secrets_in(text), [], "job record")
