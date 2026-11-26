# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The developer view of each attempt and "Regenerate and check" (send T38;
AC-35, AC-36, AC-49; contract §1.1 ``action_apix_regenerate_check``; SCR-13).

``FakeApixSendClient`` stands in for SendInvoiceZIP: no network. Developer
mode is the debug flag of the request (patched). Invented data only.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from lxml import etree
from odoo.addons.goodahead_einvoice_base.lib import finvoice
from odoo.exceptions import AccessError
from odoo.tests import new_test_user, tagged

from .common import FakeApixSendClient, SendApixCommon

ATTEMPT = "goodahead.invoice.send.attempt"
REGENERATE = "goodahead.invoice.send.regenerate"
RES_USERS_REQUEST = "odoo.addons.base.models.res_users.request"
DEBUG = SimpleNamespace(session=SimpleNamespace(debug="1"))


@tagged("post_install", "-at_install")
class TestDeveloperView(SendApixCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.developer = new_test_user(
            cls.env, login="zz_send_dev_view", groups="account.group_account_invoice",
            company_id=cls.company.id, company_ids=[(6, 0, (cls.company | cls.company_b).ids)],
        )

    def _counts(self):
        return {
            "attempts": self.env[ATTEMPT].sudo().search_count([]),
            "attachments": self.env["ir.attachment"].sudo().search_count([]),
            "jobs": self.env["queue.job"].sudo().search_count([]),
        }

    def _form_arch(self, model, user, view_id=False):
        views = self.env[model].with_user(user).get_views([(view_id, "form")])
        return etree.fromstring(views["views"]["form"]["arch"])

    def _shown(self, nodes):
        """Nodes the client shows (19.0 keeps developer-mode nodes in the
        arch and makes them invisible outside developer mode)."""
        return [node for node in nodes if node.get("invisible") != "1"]

    # -- SCR-13: the Technical page ---------------------------------------------------------------

    def test_technical_view_only_in_developer_mode(self):
        """AC-35, AC-36: the developer view is a view of its own, opened from
        a developer-mode button; the ordinary attempt form never names a
        developer field, so it opens for everyone."""
        attempt = self._row(self.env[ATTEMPT]._ledger_record(
            self._make_invoice(), "email", "sent", "erp_send", self.accountant, {}))
        with patch(RES_USERS_REQUEST, DEBUG):
            action = attempt.with_user(self.developer).action_open_technical()
            view_id = action["views"][0][0]
            page = self._form_arch(ATTEMPT, self.developer, view_id).xpath("//page[@name='technical']")
            self.assertTrue(page)
            for name in ("message_file", "package_file", "exchange_log", "schema_result",
                         "schema_errors", "units_without_code", "queue_job_label"):
                with self.subTest(field=name):
                    self.assertTrue(page[0].xpath(f".//field[@name='{name}']"))
            for model, button in ((ATTEMPT, "action_open_technical"),
                                  ("account.move", "action_apix_regenerate_check")):
                nodes = self._form_arch(model, self.developer).xpath(f"//button[@name='{button}']")
                self.assertTrue(self._shown(nodes), button)
            # the developer view reads the developer fields
            attempt.with_user(self.developer).read(["exchange_log", "message_file"])
        with self.assertRaises(AccessError):
            attempt.with_user(self.accountant).action_open_technical()
        for model, button in ((ATTEMPT, "action_open_technical"),
                              ("account.move", "action_apix_regenerate_check")):
            nodes = self._form_arch(model, self.accountant).xpath(f"//button[@name='{button}']")
            self.assertFalse(self._shown(nodes), button)
        ordinary = self._form_arch(ATTEMPT, self.accountant)
        for name in ("message_attachment_id", "package_attachment_id", "exchange_log",
                     "schema_errors", "queue_job_id", "message_file"):
            self.assertFalse(ordinary.xpath(f"//field[@name='{name}']"), name)
        # the ordinary form opens without developer mode
        attempt.with_user(self.accountant).web_read({"state": {}, "reason": {}})

    def test_sent_attempt_keeps_files_and_masked_exchange(self):
        """AC-35, sad.md §8 Secrets: no transfer id, key or digest stored."""
        FakeApixSendClient("accepted").patch(self)
        move = self._make_invoice()
        result = move.with_user(self.accountant)._apix_send_now("button")
        with patch(RES_USERS_REQUEST, DEBUG):
            attempt = self._row(result).with_user(self.developer)
            self.assertTrue(attempt.message_attachment_id.sudo().raw.startswith(b"<?xml"))
            self.assertTrue(attempt.package_attachment_id)
            self.assertEqual(attempt.schema_result, "valid")
            log = attempt.exchange_log
        self.assertIn("TraID=***", log)
        self.assertNotIn("ZZ-TRANSFER-003700000011", log)
        self.assertNotIn("zz-key-003700000011", log)

    # -- Regenerate and check -------------------------------------------------------------

    def test_regenerate_valid_creates_and_sends_nothing(self):
        fake = FakeApixSendClient("accepted").patch(self)
        move = self._make_invoice()
        before = self._counts()
        with patch(RES_USERS_REQUEST, DEBUG):
            result = move.with_user(self.developer)._apix_regenerate_check()
        self.assertEqual(result["schema_result"], "valid")
        self.assertTrue(result["message"].startswith(b"<?xml"))
        self.assertEqual(result["problems"], [])
        self.assertEqual(self._counts(), before)
        self.assertFalse(fake.calls)
        move.invalidate_recordset()
        self.assertFalse(move.already_sent)

    def test_regenerate_dialog_offers_the_message_as_a_download(self):
        move = self._make_invoice()
        with patch(RES_USERS_REQUEST, DEBUG):
            action = move.with_user(self.developer).action_apix_regenerate_check()
            self.assertEqual((action["res_model"], action["target"]), (REGENERATE, "new"))
            dialog = self.env[REGENERATE].with_user(self.developer).browse(action["res_id"])
            self.assertEqual(dialog.schema_result, "valid")
            self.assertTrue(dialog.message_file)
            self.assertTrue(dialog.message_filename.endswith(".xml"))
        self.assertFalse(self.env["ir.attachment"].sudo().search(
            [("res_model", "=", REGENERATE)]), "the download is no attachment")

    def test_regenerate_invalid_lists_the_problems_in_plain_words(self):
        move = self._make_invoice()
        problem = {"path": "Finvoice/BuyerPartyDetails/BuyerOrganisationName",
                   "message": "ZZ element missing"}
        before = self._counts()
        with patch.object(finvoice, "validate_schema", lambda message: [problem]), \
                patch(RES_USERS_REQUEST, DEBUG):
            result = move.with_user(self.developer)._apix_regenerate_check()
        self.assertEqual(result["schema_result"], "invalid")
        self.assertEqual(len(result["problems"]), 1)
        self.assertIn("ZZ element missing", result["problems"][0])
        self.assertIn(finvoice.explain(problem), result["problems"][0])
        self.assertEqual(self._counts(), before)

    def test_regenerate_names_units_without_a_code(self):
        """AC-49."""
        move = self._make_invoice(lines=[{
            "name": "ZZ pallet rent", "quantity": 2, "price_unit": 10.0,
            "product_uom_id": self.unit_without_code.id,
        }])
        with patch(RES_USERS_REQUEST, DEBUG):
            result = move.with_user(self.developer)._apix_regenerate_check()
        self.assertEqual(result["units_without_code"], ["ZZ pallet"])
        self.assertEqual(result["schema_result"], "valid", "an unknown unit never blocks")

    def test_regenerate_needs_developer_mode(self):
        move = self._make_invoice()
        with self.assertRaises(AccessError):
            move.with_user(self.accountant).action_apix_regenerate_check()
