# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Nothing happens until someone sends, at data-migration volume (send T39;
AC-55, US-18; spec §6 "Nothing until send"; sad.md §10 QG-1).

Posted customer documents (invoices and credit notes; 200 in every run,
1,000 in the pre-release run) in a company with sending on and ready
customers produce 0 attempts, attachments, jobs and APIX client calls. The
same documents time the list filters "Ready to send"
and "Not ready", whose readiness is worked out in Python (wave 10 note).
Invented data only; every APIX client method is patched to record a call.
"""

import logging
import time
import unittest
from unittest.mock import patch

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo.tests import tagged

from odoo.addons.goodahead_apix.lib.apix_client import ApixClient

from .common import SendApixCommon

_logger = logging.getLogger(__name__)

#: The standard run (every gate) and the pre-release run (data-migration volume).
VOLUME = 200
RELEASE_VOLUME = 1000
#: One in ten documents is a credit note.
CREDIT_NOTE_SHARE = 10
#: Created and posted in chunks, as a data migration does.
CHUNK = 100
#: The list filters on the documents, readiness in Python (seconds).
FILTER_BUDGET = 30.0
APIX_CALLS = ("send_invoice_zip", "address_query", "delivery_method")


@tagged("post_install", "-at_install")
class TestNothingUntilSend(SendApixCommon):
    volume = VOLUME

    def _counts(self):
        return {
            "attempts": self.env["goodahead.invoice.send.attempt"].sudo().search_count([]),
            "attachments": self.env["ir.attachment"].sudo().search_count([]),
            "jobs": self.env["queue.job"].sudo().search_count([]),
        }

    def _record_apix_calls(self):
        calls = []
        for name in APIX_CALLS:
            def record(*args, _name=name, **kwargs):
                calls.append(_name)
                raise AssertionError(f"APIX {_name} called while creating or posting")
            patcher = patch.object(ApixClient, name, record)
            patcher.start()
            self.addCleanup(patcher.stop)
        return calls

    def _vals_list(self):
        journal = self.env["account.journal"].search(
            [("company_id", "=", self.company.id), ("type", "=", "sale")], limit=1)
        tax = self.env["account.tax"].search([
            ("company_id", "=", self.company.id), ("type_tax_use", "=", "sale"),
            ("amount_type", "=", "percent"), ("amount", ">", 0),
        ], limit=1)
        return [
            {
                "move_type": "out_refund" if index % CREDIT_NOTE_SHARE == 0 else "out_invoice",
                "partner_id": self.customer.id,
                "company_id": self.company.id,
                "journal_id": journal.id,
                "invoice_date": "2026-11-25",
                "partner_bank_id": self.bank_account.id,
                "invoice_line_ids": [(0, 0, {
                    "name": f"ZZ migrated line {index}", "quantity": 1,
                    "price_unit": 10.0 + index, "tax_ids": [(6, 0, tax.ids)],
                })],
            }
            for index in range(self.volume)
        ]

    def test_a_thousand_posted_documents_produce_nothing(self):
        if "goodahead_community_sending_stopped" in self.env["res.company"]._fields:
            # with the community modules installed, the company is in the state
            # the migration leaves it in: their own posting-time e-invoice
            # documents are theirs, not this add-on's
            self.company.sudo().goodahead_community_sending_stopped = True
        calls = self._record_apix_calls()
        before = self._counts()
        started = time.monotonic()
        moves = self.env["account.move"]
        vals_list = self._vals_list()
        for start in range(0, self.volume, CHUNK):
            chunk = self.env["account.move"].with_company(self.company).create(
                vals_list[start:start + CHUNK])
            chunk.action_post()
            moves |= chunk
            _logger.info("AC-55: %s posted after %.1f s", len(moves), time.monotonic() - started)
        _logger.info("AC-55: %s documents created and posted in %.1f s",
                     len(moves), time.monotonic() - started)
        self.env.flush_all()
        self.assertEqual(set(moves.mapped("state")), {"posted"})
        self.assertEqual(len(moves.filtered(lambda m: m.move_type == "out_refund")),
                         self.volume // CREDIT_NOTE_SHARE)
        self.assertEqual(self._counts(), before, "no attempt, attachment or job")
        self.assertEqual(calls, [], "no APIX call or lookup")
        # the transmit method is the only thing stored
        self.assertEqual(set(moves.mapped("transmit_method")), {"einvoice"})

        Move = self.env["account.move"].with_user(self.accountant).with_context(
            allowed_company_ids=self.company.ids)
        timings = {}
        for state in ("ready", "not_ready"):
            started = time.monotonic()
            found = Move.search([("send_state", "=", state), ("id", "in", moves.ids)])
            timings[state] = time.monotonic() - started
            if state == "ready":
                self.assertEqual(len(found), self.volume)
        _logger.info("List filters over %s documents: ready %.2f s, not ready %.2f s",
                     self.volume, timings["ready"], timings["not_ready"])
        self.assertLess(max(timings.values()), FILTER_BUDGET, timings)
        self.assertEqual(self._counts(), before, "filtering creates nothing either")
        self.assertEqual(calls, [])


@tagged("-standard", "pre_release", "post_install", "-at_install")
class TestNothingUntilSendAtMigrationVolume(TestNothingUntilSend):
    """The same at data-migration volume (spec §6: >= 1,000 documents). Run on
    its own (``--test-tags pre_release/goodahead_invoice_send_apix``): after
    the rest of the suite has run in the same process, posting in the test
    cursor slows down far beyond the add-on's own work."""

    volume = RELEASE_VOLUME
    #: the test method is the parent's (the ERP's loader skips inherited ones)
    allow_inherited_tests_method = True
