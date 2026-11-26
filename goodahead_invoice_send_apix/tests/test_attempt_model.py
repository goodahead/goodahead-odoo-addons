# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The delivery history: one immutable attempt per try to deliver an invoice
(send T13; AC-29, AC-32, AC-34, AC-54; ADR-0006, data-model.md "Aggregate:
delivery history").

Users read the attempts of their allowed companies only and change nothing;
rows are written through the ledger alone. Every name here is invented.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo.addons.account.tests.common import AccountTestInvoicingCommon  # noqa: E402
from odoo.exceptions import AccessError, ValidationError  # noqa: E402
from odoo.tests import new_test_user, tagged  # noqa: E402
from odoo.tools import mute_logger  # noqa: E402
from psycopg2 import IntegrityError  # noqa: E402

from ..models.goodahead_invoice_send_attempt import HISTORY_READONLY  # noqa: E402

MODEL = "goodahead.invoice.send.attempt"
TABLE = "goodahead_invoice_send_attempt"
RES_USERS_REQUEST = "odoo.addons.base.models.res_users.request"
DEVELOPER_FIELDS = (
    "message_attachment_id",
    "package_attachment_id",
    "exchange_log",
    "schema_errors",
    "queue_job_id",
)


@tagged("post_install", "-at_install")
class TestSendAttemptModel(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.company_data["company"]
        cls.company_b = cls.setup_other_company(name="ZZ Second Company Oy")["company"]
        cls.move_a = cls.init_invoice("out_invoice", amounts=[100.0], company=cls.company_a)
        cls.move_b = cls.init_invoice("out_invoice", amounts=[200.0], company=cls.company_b)
        cls.accountant = new_test_user(
            cls.env,
            login="zz_send_accountant",
            groups="account.group_account_invoice",
            company_id=cls.company_a.id,
            company_ids=[(6, 0, cls.company_a.ids)],
        )
        cls.developer = new_test_user(
            cls.env,
            login="zz_send_developer",
            groups="account.group_account_invoice,base.group_no_one",
            company_id=cls.company_a.id,
            company_ids=[(6, 0, cls.company_a.ids)],
        )
        cls.Attempt = cls.env[MODEL]

    def _attempt(self, move, **values):
        vals = {
            "move_id": move.id,
            "channel": "einvoice",
            "state": "accepted",
            "origin": "button",
            "requested_by_id": self.env.user.id,
        }
        vals.update(values)
        # Back in the test's own environment: the ledger's marker must not
        # ride along into the checks below.
        return self.Attempt.browse(self.Attempt._ledger().create(vals).id)

    # -- schema ---------------------------------------------------------------

    def test_indexes_exist(self):
        self.env.cr.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s", [TABLE])
        indexes = dict(self.env.cr.fetchall())
        for key in (
            "one_in_progress",
            "migration_once",
            "running_since_idx",
            "queued_idx",
            "attention_idx",
            "sent_idx",
            "run_idx",
        ):
            with self.subTest(index=key):
                names = [name for name in indexes if name.endswith(key)]
                self.assertEqual(len(names), 1, sorted(indexes))
        unique = [name for name, ddl in indexes.items() if "UNIQUE" in ddl]
        self.assertTrue(any(name.endswith("one_in_progress") for name in unique))
        self.assertTrue(any(name.endswith("migration_once") for name in unique))

    def test_company_follows_the_invoice(self):
        self.assertEqual(self._attempt(self.move_b).company_id, self.company_b)

    def test_defaults(self):
        attempt = self.Attempt._ledger().create(
            {"move_id": self.move_a.id, "channel": "einvoice", "origin": "button",
             "requested_by_id": self.env.user.id}
        )
        self.assertEqual(attempt.state, "queued")
        self.assertEqual(attempt.schema_result, "not_checked")
        self.assertFalse(attempt.counts_as_sent)
        self.assertFalse(attempt.is_latest)
        self.assertFalse(attempt.needs_attention)

    def test_selections(self):
        fields_ = self.Attempt._fields
        self.assertEqual(
            [key for key, _label in fields_["state"].selection],
            ["queued", "running", "accepted", "sent", "refused", "failed", "cancelled", "downloaded"],
        )
        self.assertEqual(
            [key for key, _label in fields_["channel"].selection],
            ["einvoice", "letter", "email", "download", "unknown"],
        )
        self.assertEqual(
            [key for key, _label in fields_["origin"].selection],
            ["button", "send_print", "mass_now", "mass_background", "erp_send", "migration",
             "late_answer"],
        )

    # -- one send at a time (AC-08 index) --------------------------------------

    def test_two_in_progress_attempts_for_one_invoice_raise(self):
        self._attempt(self.move_a, state="queued")
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"), self.env.cr.savepoint():
            self._attempt(self.move_a, state="running")

    def test_ended_attempts_do_not_block_a_new_one(self):
        self._attempt(self.move_a, state="accepted")
        self._attempt(self.move_a, state="failed")
        self.assertTrue(self._attempt(self.move_a, state="queued"))

    def test_in_progress_on_two_invoices_is_fine(self):
        self._attempt(self.move_a, state="running")
        self.assertTrue(self._attempt(self.move_b, state="running"))

    def test_carried_over_once_per_invoice_and_source(self):
        values = {"state": "sent", "origin": "migration", "channel": "unknown",
                  "migration_source": "zz_source"}
        self._attempt(self.move_a, **values)
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"), self.env.cr.savepoint():
            self._attempt(self.move_a, **values)

    def test_handled_reason_required_with_handled_by(self):
        with self.assertRaises(ValidationError):
            self._attempt(self.move_a, state="failed", handled_by_id=self.env.user.id)

    # -- the history is the record --------------------------------------

    def test_user_cannot_create(self):
        with self.assertRaises(AccessError) as caught:
            self.Attempt.with_user(self.accountant).create(
                {"move_id": self.move_a.id, "channel": "einvoice", "origin": "button",
                 "requested_by_id": self.accountant.id}
            )
        self.assertEqual(caught.exception.code, HISTORY_READONLY)

    def test_user_cannot_change_an_attempt(self):
        attempt = self._attempt(self.move_a, state="failed", reason="ZZ reason")
        for vals in ({"state": "accepted"}, {"reason": "edited"}, {"handled_reason": "ZZ"}):
            with self.subTest(vals=vals):
                with self.assertRaises(AccessError) as caught:
                    attempt.with_user(self.accountant).write(vals)
                self.assertEqual(caught.exception.code, HISTORY_READONLY)
        self.assertEqual(attempt.state, "failed")
        self.assertEqual(attempt.reason, "ZZ reason")

    def test_not_even_an_administrator_changes_or_removes_it(self):
        attempt = self._attempt(self.move_a)
        with self.assertRaises(AccessError):
            attempt.write({"state": "failed"})
        with self.assertRaises(AccessError):
            attempt.unlink()

    def test_user_cannot_remove_an_attempt(self):
        attempt = self._attempt(self.move_a)
        with self.assertRaises(AccessError) as caught:
            attempt.with_user(self.accountant).unlink()
        self.assertEqual(caught.exception.code, HISTORY_READONLY)
        self.assertTrue(attempt.exists())

    def test_ledger_writes(self):
        attempt = self._attempt(self.move_a, state="running")
        attempt._ledger().write({"state": "accepted", "counts_as_sent": True})
        self.assertEqual(attempt.state, "accepted")

    def test_deleting_a_draft_invoice_removes_its_history(self):
        draft = self.init_invoice("out_invoice", amounts=[10.0], company=self.company_a)
        attempt = self._attempt(draft, state="failed")
        draft.unlink()
        self.assertFalse(attempt.exists())

    def test_access_rights_are_read_only(self):
        Attempt = self.Attempt.with_user(self.accountant)
        self.assertTrue(Attempt.has_access("read"))
        for operation in ("write", "create", "unlink"):
            with self.subTest(operation=operation):
                self.assertFalse(Attempt.has_access(operation))

    # -- developer fields (AC-35 read model) -------------------------------------

    def test_developer_fields_need_developer_mode(self):
        attempt = self._attempt(self.move_a, exchange_log="PUT ***", schema_errors="ZZ")
        for name in DEVELOPER_FIELDS:
            with self.subTest(field=name):
                self.assertEqual(self.Attempt._fields[name].groups, "base.group_no_one")
        with self.assertRaises(AccessError):
            attempt.with_user(self.accountant).read(["exchange_log"])
        # The developer group counts only in developer (debug) mode; in 19.0
        # every internal user is in it, so developer mode is what opens these.
        with self.assertRaises(AccessError):
            attempt.with_user(self.developer).read(["exchange_log"])
        with patch(RES_USERS_REQUEST, SimpleNamespace(session=SimpleNamespace(debug="1"))):
            row = attempt.with_user(self.developer).read(["exchange_log"])[0]
            self.assertEqual(row["exchange_log"], "PUT ***")
        # Everything else of the row is readable.
        row = attempt.with_user(self.accountant).read(["state", "channel", "reason"])[0]
        self.assertEqual(row["state"], "accepted")

    # -- other companies stay hidden ------------------------------

    def test_accountant_sees_only_allowed_companies(self):
        mine = self._attempt(self.move_a, state="failed")
        other = self._attempt(self.move_b, state="failed")
        found = self.Attempt.with_user(self.accountant).search([("id", "in", (mine | other).ids)])
        self.assertEqual(found, mine)
        with self.assertRaises(AccessError):
            other.with_user(self.accountant).read(["state"])

    def test_two_company_user_sees_both(self):
        both = new_test_user(
            self.env,
            login="zz_send_both",
            groups="account.group_account_invoice",
            company_id=self.company_a.id,
            company_ids=[(6, 0, (self.company_a | self.company_b).ids)],
        )
        attempts = self._attempt(self.move_a) | self._attempt(self.move_b)
        found = (
            self.Attempt.with_user(both)
            .with_context(allowed_company_ids=(self.company_a | self.company_b).ids)
            .search([("id", "in", attempts.ids)])
        )
        self.assertEqual(found, attempts)
