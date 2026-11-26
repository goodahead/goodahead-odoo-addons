# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""What APIX adds to the inbox and the import record: listing columns before download, the APIX / shadow / ageing /
mark-unconfirmed filters, the mark-unconfirmed note for administrators, and
never the storage key. Invented data only."""

import json
from datetime import timedelta

from odoo import fields
from odoo.tests import TransactionCase, new_test_user, tagged
from odoo.tools.safe_eval import safe_eval

from odoo.addons.goodahead_invoice_import.tests.test_inbox_screens import (
    ScreenHelpers,
    invoice_data,
)
from odoo.addons.goodahead_invoice_import_apix.lib import apix_window


@tagged("post_install", "-at_install")
class TestApixInboxScreens(ScreenHelpers, TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.Import = cls.env["goodahead.invoice.import"]
        cls.company = cls.env.company
        cls.admin = new_test_user(
            cls.env,
            login="t14_apix_admin",
            groups="base.group_user,account.group_account_manager",
        )
        cls.billing = new_test_user(
            cls.env,
            login="t14_apix_billing",
            groups="base.group_user,account.group_account_invoice",
        )

    def _apix(self, storage_id, **vals):
        listing = {
            "StorageID": storage_id,
            "DocumentID": f"INV-{storage_id}",
            "SenderName": "ZZ Listed Supplier Oy",
            "AmountToPay": "124.00",
            "DocumentDate": "20261120",
        }
        listing.update(vals.pop("listing", {}))
        values = {
            "name": f"{storage_id}.zip",
            "source": "apix",
            "company_id": self.company.id,
            "apix_storage_id": storage_id,
            "apix_listing_data": listing,
            "apix_status": "UNRECEIVED",
            "apix_document_date": fields.Date.to_date("2026-11-20"),
        }
        values.update(vals)
        return self.Import.create(values)

    # -- SCR-01 columns: the listing until the package is read ----------

    def test_listed_package_shows_the_listing_until_it_is_read(self):
        record = self._apix("ZZ-T14-L1")
        self.assertEqual(record.seller_name, "ZZ Listed Supplier Oy")
        self.assertEqual(record.invoice_number, "INV-ZZ-T14-L1")
        self.assertAlmostEqual(record.amount_total, 124.0)
        self.assertEqual(record.invoice_date, fields.Date.to_date("2026-11-20"))
        record.extraction_data = json.dumps(invoice_data(number="ZZ-READ-1"))
        self.assertEqual(record.invoice_number, "ZZ-READ-1")
        self.assertEqual(record.seller_name, "ZZ Inbox Seller Oy")

    def test_list_has_shadow_outcome_only_with_the_shadow_filter(self):
        arch = self._arch("list")
        shadow = arch.xpath("//list/field[@name='shadow_outcome']")[0]
        bill = arch.xpath("//list/field[@name='move_id']")[0]
        context = {"apix_shadow_results": 1}
        self.assertTrue(safe_true(shadow.get("column_invisible"), {}))
        self.assertFalse(safe_true(shadow.get("column_invisible"), context))
        self.assertTrue(safe_true(bill.get("column_invisible"), context))
        shadow_filter = self._arch("search").xpath("//filter[@name='shadow_results']")[0]
        self.assertIn("apix_shadow_results", shadow_filter.get("context"))
        todo_button = arch.xpath("//list/header/button[@name='action_open_apix_todos']")
        self.assertTrue(todo_button)

    # -- SCR-01 filters ----------------------------------------------------------

    def test_apix_filters_return_the_right_records(self):
        today = fields.Date.context_today(self.env.user)
        old = today - timedelta(days=90)
        fresh = today
        ageing = self._apix("ZZ-T14-A1", apix_document_date=old)
        ageing_settled = self._apix(
            "ZZ-T14-A2", apix_document_date=old, state="closed", close_reason="ZZ test"
        )
        recent = self._apix("ZZ-T14-A3", apix_document_date=fresh)
        shadow = self._apix("ZZ-T14-S1", shadow_outcome="would_import")
        unconfirmed = self._apix("ZZ-T14-M1", state="closed", close_reason="ZZ handled")
        confirmed = self._apix(
            "ZZ-T14-M2", state="closed", close_reason="ZZ handled", apix_mark_confirmed=True
        )
        upload = self.Import.create({"name": "zz-upload.pdf", "state": "failed"})
        ours = ageing | ageing_settled | recent | shadow | unconfirmed | confirmed | upload

        def found(name):
            return self.Import.search([("id", "in", ours.ids)] + self._filter_domain(name))

        self.assertEqual(found("source_apix"), ours - upload)
        self.assertEqual(found("shadow_results"), shadow)
        self.assertEqual(found("apix_mark_unconfirmed"), ageing_settled | unconfirmed)
        self.assertIn(ageing, found("apix_ageing"))
        self.assertNotIn(ageing_settled, found("apix_ageing"))
        self.assertNotIn(recent, found("apix_ageing"))
        self.assertNotIn(upload, found("apix_ageing"))

    def test_ageing_filter_agrees_with_the_window_helper(self):
        """The filter's domain is the same arithmetic as `apix_window`."""
        today = fields.Date.context_today(self.env.user)
        dates = [today - timedelta(days=days) for days in range(0, 100, 3)]
        records = self.Import
        for index, day in enumerate(dates):
            records |= self._apix(f"ZZ-T14-W{index}", apix_document_date=day)
        found = self.Import.search(
            [("id", "in", records.ids)] + self._filter_domain("apix_ageing")
        )
        expected = records.filtered(
            lambda r: apix_window.is_ageing(r.apix_document_date, today)
        )
        self.assertEqual(found, expected)

    # -- SCR-02: APIX fields, the note, never the key -------------------------------

    def test_form_never_shows_the_storage_key(self):
        for user in (self.admin, self.billing):
            arch = self._arch("form", user=user)
            self.assertFalse(arch.xpath("//field[@name='apix_storage_key']"))
            self.assertTrue(arch.xpath("//field[@name='apix_storage_id']"))
            self.assertTrue(arch.xpath("//field[@name='apix_status']"))

    def test_mark_unconfirmed_note_for_administrators(self):
        unconfirmed = self._apix("ZZ-T14-N1", state="closed", close_reason="ZZ handled")
        confirmed = self._apix(
            "ZZ-T14-N2", state="closed", close_reason="ZZ handled", apix_mark_confirmed=True
        )
        waiting = self._apix("ZZ-T14-N3")
        banner_xpath = "//div[@name='banner_apix_mark_unconfirmed']"
        admin_banner = self._arch("form", user=self.admin).xpath(banner_xpath)[0]
        self.assertTrue(self._is_visible(admin_banner, unconfirmed))
        self.assertFalse(self._is_visible(admin_banner, confirmed))
        self.assertFalse(self._is_visible(admin_banner, waiting))
        self.assertFalse(self._arch("form", user=self.billing).xpath(banner_xpath))

    def test_possibly_lost_apix_record_imports_through_the_package_job(self):
        record = self._apix("ZZ-T14-P1", state="possibly_lost")
        header, banners, _smart = self._form_state(record)
        self.assertEqual(header, {"action_import_possibly_lost", "action_open_close_dialog"})
        self.assertIn("banner_possibly_lost", banners)
        # Imports from APIX run only while the company receives live.
        Connection = self.env["goodahead.apix.connection"]
        connection = Connection.search([("company_id", "=", self.company.id)], limit=1)
        if not connection:
            connection = Connection.create(
                {"company_id": self.company.id, "environment": "test", "business_id": "0000006-0"}
            )
        connection._apix_set_receiving_state("live")
        record.action_import_possibly_lost()
        self.assertEqual(record.state, "waiting")
        self.assertTrue(record.queue_job_id)
        self.assertEqual(record.queue_job_id.channel, "root.apix")

    def test_todos_link_opens_the_connection_activities(self):
        action = self.Import.with_user(self.admin).action_open_apix_todos()
        self.assertEqual(action["res_model"], "mail.activity")
        self.assertIn(("res_model", "=", "goodahead.apix.connection"), action["domain"])

    def test_todos_button_works_as_the_web_client_calls_it(self):
        """A list header button is called with the selected ids (none here),
        as the web client's call_button does."""
        from odoo.service.model import call_kw

        action = call_kw(self.Import.with_user(self.admin), "action_open_apix_todos", [[]], {})
        self.assertEqual(action["res_model"], "mail.activity")
        self.assertIn(("res_model", "=", "goodahead.apix.connection"), action["domain"])


def safe_true(expression, context):
    return bool(safe_eval(expression or "False", {"context": context}))
