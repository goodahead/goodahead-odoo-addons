# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Sending with the ledger on its own cursor, as outside tests.

Outside the test runner the ledger commits each step on a cursor of its
own, while the request (or job) runs in a REPEATABLE READ transaction whose
snapshot was taken before that commit: an attempt the ledger has just
inserted does not exist for the caller, and one it changed still reads as
before. Under the test runner the ledger normally writes through a savepoint
of the test cursor, which hides this. Here the ledger opens real cursors
(``ledger.FORCE_OWN_CURSOR``) on committed invented data, and each door
answers its outcome without a ``MissingError``: the button (accepted,
refused, failed), Send & Print, a background job and the mass wizard's send
now. APIX is never called (``FakeApixSendClient``). The committed data is
removed afterwards.
"""

import unittest
import uuid

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo import SUPERUSER_ID, api  # noqa: E402
from odoo.tests import TransactionCase, tagged  # noqa: E402
from unittest.mock import patch  # noqa: E402

from odoo.addons.queue_job.job import Job  # noqa: E402

from ..models import ledger  # noqa: E402
from ..models.goodahead_invoice_send_attempt import CannotCancel  # noqa: E402
from .common import BATCH_ID, DOCUMENT_ID, FakeApixSendClient  # noqa: E402

ATTEMPT = "goodahead.invoice.send.attempt"
ADMIN_UID = 2
PDF = ("invoice.pdf", b"%PDF-1.4 ZZ invented")
COMPANY_FIELDS = ["company_registry", "vat", "street", "zip", "city", "country_id",
                  "account_fiscal_country_id"]


@tagged("post_install", "-at_install")
class TestLedgerOwnCursor(TransactionCase):
    """Each door on committed data, the ledger on cursors of its own."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.registry_ = cls.registry
        cls.addClassCleanup(cls._remove_committed_data)
        cls.created = {}
        with cls.registry_.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            cls._commit_data(env)

    @classmethod
    def _commit_data(cls, env):
        company = env.ref("base.main_company")
        cls.company_id = company.id
        cls.saved_company = {
            name: value.id if hasattr(value, "id") else value
            for name, value in ((name, company[name]) for name in COMPANY_FIELDS)
        }
        cls.saved_edicode = (company.partner_id.edicode, company.partner_id.einvoice_operator_id.id)
        finland = env.ref("base.fi")
        fiscal_country = company.account_fiscal_country_id
        company.write({
            "company_registry": "0000009-2", "vat": "FI00000092", "street": "Esimerkkikatu 9",
            "zip": "00100", "city": "Helsinki", "country_id": finland.id,
        })
        company.account_fiscal_country_id = fiscal_country
        operator = env["res.partner.operator.einvoice"].create(
            {"name": "ZZ Own Cursor Operator", "identifier": "ZZOWNOP"})
        cls.created["operator"] = operator.id
        company.partner_id.write({"edicode": "003700000092", "einvoice_operator_id": operator.id})
        bank = env["res.bank"].create({"name": "ZZ Own Cursor Bank", "bic": "ZZOCFIHH"})
        account = env["res.partner.bank"].create({
            "partner_id": company.partner_id.id, "acc_number": "FI51 1234 5600 0000 51",
            "bank_id": bank.id, "allow_out_payment": True,
        })
        cls.created.update(bank=bank.id, bank_account=account.id)
        Connection = env["goodahead.apix.connection"]
        connection = Connection.search([("company_id", "=", company.id)])
        if connection:
            cls.saved_connection = connection.read(
                ["environment", "transfer_id", "transfer_key", "status", "sending_enabled",
                 "business_id", "send_paused_until"])[0]
        else:
            connection = Connection.create({"company_id": company.id, "business_id": "0000009-2"})
            cls.created["connection"] = connection.id
        connection.write({
            "environment": "test", "transfer_id": "ZZ-TRANSFER-OWN", "transfer_key": "zz-key-own",
            "business_id": "0000009-2", "status": "working", "sending_enabled": True,
            "send_paused_until": False,
        })
        customer = env["res.partner"].create({
            "name": "ZZ Own Cursor Customer Oy", "is_company": True,
            "company_registry": "0000010-6", "street": "Asiakkaantie 2", "zip": "00200",
            "city": "Espoo", "country_id": finland.id, "email": "zz.own@example.com",
            "edicode": "003700000106", "einvoice_operator_id": operator.id,
        })
        cls.created["customer"] = customer.id
        journal = env["account.journal"].search(
            [("company_id", "=", company.id), ("type", "=", "sale")], limit=1)
        tax = env["account.tax"].search([
            ("company_id", "=", company.id), ("type_tax_use", "=", "sale"),
            ("amount_type", "=", "percent"), ("amount", ">", 0),
        ], limit=1)
        moves = env["account.move"].with_company(company).create([{
            "move_type": "out_invoice",
            "partner_id": customer.id,
            "journal_id": journal.id,
            "invoice_date": "2026-11-25",
            "partner_bank_id": account.id,
            "transmit_method": "einvoice",
            "invoice_line_ids": [(0, 0, {
                "name": f"ZZ own cursor service {index}", "quantity": 1, "price_unit": 100.0,
                "tax_ids": [(6, 0, tax.ids)],
            })],
        } for index in range(8)])
        moves.action_post()
        cls.move_ids = moves.ids
        not_ready = [move.name for move in moves if not move.send_ready]
        assert not not_ready, f"invented invoices not ready to send: {moves[0].send_readiness}"

    @classmethod
    def _remove_committed_data(cls):
        with cls.registry_.cursor() as cr:
            moves = list(getattr(cls, "move_ids", []))
            cr.execute(f"SELECT id, queue_job_id FROM {ATTEMPT.replace('.', '_')}"
                       " WHERE move_id = ANY(%s)", [moves])
            rows = cr.fetchall()
            attempt_ids = [row[0] for row in rows]
            job_ids = [row[1] for row in rows if row[1]]
            cr.execute("DELETE FROM ir_attachment WHERE res_model = %s AND res_id = ANY(%s)",
                       [ATTEMPT, attempt_ids])
            cr.execute("DELETE FROM goodahead_invoice_send_attempt WHERE id = ANY(%s)",
                       [attempt_ids])
            cr.execute("DELETE FROM queue_job WHERE id = ANY(%s)", [job_ids])
            cr.execute("DELETE FROM goodahead_invoice_send_wizard_line WHERE move_id = ANY(%s)",
                       [moves])
            cr.execute("DELETE FROM account_move_send_wizard WHERE move_id = ANY(%s)", [moves])
            for model in ("account.move", "res.partner"):
                ids = moves if model == "account.move" else [cls.created.get("customer", 0)]
                cr.execute("DELETE FROM ir_attachment WHERE res_model = %s AND res_id = ANY(%s)",
                           [model, ids])
                cr.execute("DELETE FROM mail_message WHERE model = %s AND res_id = ANY(%s)",
                           [model, ids])
                cr.execute("DELETE FROM mail_followers WHERE res_model = %s AND res_id = ANY(%s)",
                           [model, ids])
            cr.execute("DELETE FROM account_move WHERE id = ANY(%s)", [moves])
            cr.execute("DELETE FROM res_partner WHERE id = %s", [cls.created.get("customer", 0)])
            cr.execute("DELETE FROM res_partner_bank WHERE id = %s",
                       [cls.created.get("bank_account", 0)])
            cr.execute("DELETE FROM res_bank WHERE id = %s", [cls.created.get("bank", 0)])
            env = api.Environment(cr, SUPERUSER_ID, {})
            if cls.created.get("connection"):
                cr.execute("DELETE FROM goodahead_apix_connection WHERE id = %s",
                           [cls.created["connection"]])
            elif getattr(cls, "saved_connection", None):
                saved = dict(cls.saved_connection)
                connection = env["goodahead.apix.connection"].browse(saved.pop("id"))
                connection.write(saved)
            if hasattr(cls, "saved_company"):
                company = env["res.company"].browse(cls.company_id)
                company.write(cls.saved_company)
                edicode, operator_id = cls.saved_edicode
                company.partner_id.write({"edicode": edicode, "einvoice_operator_id": operator_id})
            env.flush_all()
            cr.execute("DELETE FROM res_partner_operator_einvoice WHERE id = %s",
                       [cls.created.get("operator", 0)])

    def setUp(self):
        super().setUp()
        patcher = patch.object(ledger, "FORCE_OWN_CURSOR", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- helpers --------------------------------------------------------------------------

    def _move_id(self, index):
        return self.move_ids[index]

    def _request(self, function):
        """Run ``function(env)`` as a request of the administrator does: its
        own REPEATABLE READ transaction, the snapshot taken by a first read
        before any ledger step, committed at the end."""
        with self.registry_.cursor() as cr:
            env = api.Environment(cr, ADMIN_UID, {"allowed_company_ids": [self.company_id]})
            env["account.move"].search_count([])  # the snapshot starts here
            return function(env)

    def _committed(self, move_id):
        """The attempts of ``move_id`` as committed: ``[(state, origin)]``."""
        with self.registry_.cursor() as cr:
            cr.execute("SELECT state, origin, apix_batch_id FROM goodahead_invoice_send_attempt"
                       " WHERE move_id = %s ORDER BY id", [move_id])
            return cr.fetchall()

    def _fake(self, mode, **values):
        fake = FakeApixSendClient(mode, **values)
        fake.patch(self)
        return fake

    def _perform_job(self, attempt_id):
        """Run the attempt's job as the job runner does: a transaction of its
        own that loads the job first, then sends, then commits."""
        with self.registry_.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            env.cr.execute("SELECT uuid FROM queue_job WHERE id = (SELECT queue_job_id FROM"
                           " goodahead_invoice_send_attempt WHERE id = %s)", [attempt_id])
            job = Job.load(env, env.cr.fetchone()[0])
            job.set_started()
            job.store()
            result = job.perform()
            job.set_done(result=result)
            job.store()
            return result

    # -- the button (single send) ---------------------------------------------------------

    def test_single_send_accepted(self):
        fake = self._fake("accepted")
        move_id = self._move_id(0)

        def send(env):
            move = env["account.move"].browse(move_id)
            move.name  # noqa: B018 - read before the claim, as the form's request has
            result = move._apix_send_now("button", pdf=PDF)
            notice = move._apix_outcome_notice(result)
            return result.outcome, result.batch_id, result.document_id, result.credits, notice

        outcome, batch, document, credits, (kind, message) = self._request(send)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(outcome, "accepted")
        self.assertEqual((batch, document, credits), (BATCH_ID, DOCUMENT_ID, 1.0))
        self.assertEqual(kind, "success")
        self.assertIn(BATCH_ID, message)
        self.assertEqual(self._committed(move_id), [("accepted", "button", BATCH_ID)])

    def test_single_send_button_action_accepted(self):
        self._fake("accepted")
        move_id = self._move_id(1)

        def press(env):
            return env["account.move"].browse(move_id).action_apix_send()

        action = self._request(press)
        self.assertEqual(action["params"]["type"], "success")
        self.assertIn(BATCH_ID, action["params"]["message"])
        self.assertEqual(self._committed(move_id), [("accepted", "button", BATCH_ID)])

    def test_single_send_refused(self):
        self._fake("refused")
        move_id = self._move_id(2)

        def send(env):
            result = env["account.move"].browse(move_id)._apix_send_now("button", pdf=PDF)
            return result.outcome, result.attempt.reason

        outcome, reason = self._request(send)
        self.assertEqual(outcome, "refused")
        self.assertIn("ZZ validation text", reason)
        self.assertEqual([row[0] for row in self._committed(move_id)], ["refused"])

    def test_single_send_failed(self):
        self._fake("unreachable")
        move_id = self._move_id(3)

        def send(env):
            result = env["account.move"].browse(move_id)._apix_send_now("button", pdf=PDF)
            return result.outcome, result.code

        outcome, code = self._request(send)
        self.assertEqual(outcome, "failed")
        self.assertEqual(code, "send.unreachable")
        self.assertEqual([row[0] for row in self._committed(move_id)], ["failed"])

    # -- Send & Print ---------------------------------------------------------------------

    def test_send_and_print_single(self):
        self._fake("accepted")
        move_id = self._move_id(4)

        def press(env):
            wizard = env["account.move.send.wizard"].with_context(
                active_model="account.move", active_ids=[move_id],
            ).create({"sending_methods": ["apix"]})
            return wizard.with_context(goodahead_person_send=True).action_send_and_print()

        action = self._request(press)
        self.assertEqual(action["params"]["type"], "success")
        self.assertIn(BATCH_ID, action["params"]["message"])
        self.assertEqual(self._committed(move_id), [("accepted", "send_print", BATCH_ID)])

    # -- background job -------------------------------------------------------------------

    def test_background_job_send(self):
        self._fake("accepted")
        move_id = self._move_id(5)

        def queue(env):
            result = env["account.move"].browse(move_id)._apix_queue(
                str(uuid.uuid4()), "mass_background", 20)
            return result["queued"].id

        attempt_id = self._request(queue)
        self.assertEqual(self._committed(move_id), [("queued", "mass_background", None)])
        result = self._perform_job(attempt_id)
        self.assertTrue(result.startswith("accepted"), result)
        self.assertEqual(self._committed(move_id), [("accepted", "mass_background", BATCH_ID)])

    # -- the mass wizard's send now ------------------------------------------------------

    def test_mass_wizard_send_now(self):
        self._fake("accepted")
        move_id = self._move_id(6)

        def send_now(env):
            wizard = env["goodahead.invoice.send.wizard"].with_context(
                active_model="account.move", active_ids=[move_id],
            ).create({})
            wizard.action_send_now()
            return wizard.id, wizard.run_uuid

        wizard_id, run_uuid = self._request(send_now)
        self.assertTrue(run_uuid)
        attempt_id = self._committed_attempt_id(move_id)
        with self.registry_.cursor() as cr:
            # the widget's poll: its transaction began before the job's commit
            env = api.Environment(cr, ADMIN_UID, {"allowed_company_ids": [self.company_id]})
            wizard = env["goodahead.invoice.send.wizard"].browse(wizard_id)
            before = wizard.get_run_status()
            self.assertEqual([line["state"] for line in before["lines"]], ["queued"])
            result = self._perform_job(attempt_id)
            after = wizard.get_run_status()
        self.assertTrue(result.startswith("accepted"), result)
        self.assertEqual([line["state"] for line in after["lines"]], ["accepted"])
        self.assertEqual(after["done"], 1)

    def _committed_attempt_id(self, move_id):
        with self.registry_.cursor() as cr:
            cr.execute("SELECT id FROM goodahead_invoice_send_attempt WHERE move_id = %s",
                       [move_id])
            return cr.fetchone()[0]

    # -- cancel ---------------------------------------------------------------------------

    def test_cannot_cancel_reports_the_state_committed_now(self):
        self._fake("accepted")
        move_id = self._move_id(7)

        def queue(env):
            result = env["account.move"].browse(move_id)._apix_queue(
                str(uuid.uuid4()), "mass_background", 20)
            return result["queued"].id

        attempt_id = self._request(queue)
        with self.registry_.cursor() as cr:
            env = api.Environment(cr, ADMIN_UID, {"allowed_company_ids": [self.company_id]})
            attempt = env[ATTEMPT].browse(attempt_id)
            self.assertEqual(attempt.state, "queued")  # the snapshot starts here
            self._perform_job(attempt_id)  # the job sends meanwhile
            with self.assertRaises(CannotCancel) as caught:
                attempt.action_cancel()
        self.assertIn("Accepted by APIX", str(caught.exception))
