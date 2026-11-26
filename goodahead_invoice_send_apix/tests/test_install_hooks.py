# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Install hook: the sent-before-install mark and the transmit-method
back-fill (send T20; AC-55, AC-62; ADR-0006, ADR-0009; promoted from the
feature's staged migrations 02 and 03).

The add-on is already installed when these tests run, so each test first puts
its invented documents back into the "before install" state with plain SQL
(no transmit method, no mark) and then runs the hook functions.
"""

import unittest

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo.modules.module import get_manifest  # noqa: E402
from odoo.tests import tagged  # noqa: E402

from odoo.addons.account.tests.common import AccountTestInvoicingCommon  # noqa: E402

from ..hooks import (  # noqa: E402
    backfill_transmit_method,
    mark_sent_before_install,
    post_init_hook,
)

ATTEMPT = "goodahead.invoice.send.attempt"


@tagged("post_install", "-at_install")
class TestInstallHooks(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.company_data["company"]
        Partner = cls.env["res.partner"]
        cls.operator = cls.env["res.partner.operator.einvoice"].create(
            {"name": "ZZ Operator", "identifier": "ZZOPER"}
        )
        cls.with_address = Partner.create({
            "name": "ZZ Address Customer Oy",
            "edicode": "003700000027",
            "einvoice_operator_id": cls.operator.id,
        })
        cls.letter = Partner.create({
            "name": "ZZ Letter Customer Oy",
            "invoice_sending_method": "apix_letter",
            "edicode": "003700000035",
        })
        cls.plain = Partner.create({"name": "ZZ Plain Customer Oy", "email": "zz@example.com"})
        cls.desk = Partner.create({
            "name": "ZZ Billing Desk", "type": "invoice", "parent_id": cls.with_address.id,
        })

    def _invoice(self, partner, move_type="out_invoice"):
        return self.init_invoice(move_type, partner=partner, amounts=[100.0], post=True)

    def _before_install(self, moves, sent=False):
        """Put ``moves`` back into the state they had before the add-on."""
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE account_move SET transmit_method = NULL, sent_before_install = FALSE,"
            " is_move_sent = %s WHERE id IN %s",
            (sent, tuple(moves.ids)),
        )
        moves.invalidate_recordset(["transmit_method", "sent_before_install", "is_move_sent"])

    def _counts(self):
        return (
            self.env[ATTEMPT].sudo().search_count([]),
            self.env["queue.job"].sudo().search_count([]),
            self.env["ir.attachment"].sudo().search_count([]),
        )

    # -- the manifest wires the hook ------------------------------------------------------

    def test_manifest_registers_the_hook(self):
        manifest = get_manifest("goodahead_invoice_send_apix")
        self.assertEqual(manifest.get("post_init_hook"), "post_init_hook")

    # -- sent before install ------------------------------------------------------

    def test_sent_customer_documents_are_marked(self):
        sent_invoice = self._invoice(self.plain)
        sent_refund = self._invoice(self.plain, "out_refund")
        unsent = self._invoice(self.plain)
        self._before_install(sent_invoice | sent_refund, sent=True)
        self._before_install(unsent, sent=False)
        mark_sent_before_install(self.env.cr)
        (sent_invoice | sent_refund | unsent).invalidate_recordset(["sent_before_install"])
        self.assertTrue(sent_invoice.sent_before_install)
        self.assertTrue(sent_refund.sent_before_install)
        self.assertFalse(unsent.sent_before_install)

    def test_vendor_bills_marked_sent_are_untouched(self):
        bill = self._invoice(self.plain, "in_invoice")
        self.env.flush_all()
        self.env.cr.execute("UPDATE account_move SET is_move_sent = TRUE WHERE id = %s", (bill.id,))
        mark_sent_before_install(self.env.cr)
        bill.invalidate_recordset(["sent_before_install"])
        self.assertFalse(bill.sent_before_install)

    # -- transmit-method back-fill (AC-61 rule) ----------------------------------------------

    def test_backfill_follows_the_creation_rule(self):
        address = self._invoice(self.with_address)
        letter = self._invoice(self.letter)
        plain = self._invoice(self.plain)
        desk = self._invoice(self.desk)
        refund = self._invoice(self.with_address, "out_refund")
        moves = address | letter | plain | desk | refund
        self._before_install(moves)
        backfill_transmit_method(self.env.cr)
        moves.invalidate_recordset(["transmit_method"])
        self.assertEqual(address.transmit_method, "einvoice")
        self.assertEqual(letter.transmit_method, "letter", "set channel wins over the address")
        self.assertEqual(plain.transmit_method, "email")
        self.assertEqual(desk.transmit_method, "einvoice", "commercial customer's address")
        self.assertEqual(refund.transmit_method, "einvoice")

    def test_backfill_reads_the_channel_of_the_invoice_company(self):
        other = self.setup_other_company(name="ZZ Other Company Oy")["company"]
        self.plain.with_company(other).invoice_sending_method = "apix"
        here = self._invoice(self.plain)
        self._before_install(here)
        backfill_transmit_method(self.env.cr)
        here.invalidate_recordset(["transmit_method"])
        self.assertEqual(here.transmit_method, "email")

    def test_backfill_keeps_an_existing_method(self):
        move = self._invoice(self.with_address)
        self._before_install(move)
        self.env.cr.execute(
            "UPDATE account_move SET transmit_method = 'letter' WHERE id = %s", (move.id,)
        )
        backfill_transmit_method(self.env.cr)
        move.invalidate_recordset(["transmit_method"])
        self.assertEqual(move.transmit_method, "letter")

    def test_backfill_leaves_vendor_bills_without_a_method(self):
        bill = self._invoice(self.with_address, "in_invoice")
        backfill_transmit_method(self.env.cr)
        bill.invalidate_recordset(["transmit_method"])
        self.assertFalse(bill.transmit_method)

    # -- nothing else happens ------------------------------------------------------

    def test_hook_produces_no_attempt_job_or_attachment(self):
        moves = self.env["account.move"]
        for i in range(30):
            partner = (self.with_address, self.letter, self.plain)[i % 3]
            moves |= self._invoice(partner, "out_invoice" if i % 4 else "out_refund")
        self._before_install(moves, sent=True)
        before = self._counts()
        post_init_hook(self.env)
        moves.invalidate_recordset(["transmit_method", "sent_before_install"])
        self.assertEqual(self._counts(), before)
        self.assertTrue(all(moves.mapped("transmit_method")))
        self.assertTrue(all(moves.mapped("sent_before_install")))
