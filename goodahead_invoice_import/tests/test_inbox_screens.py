# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The inbox and import-record screens (core/source split; SCR-01, SCR-02, the
systray; spec AC-09, AC-11c, AC-11d, AC-22) and the T4 carry-over: the bill's
vendor buttons decide under the policy the import used.

Component level: the form's `invisible` expressions are evaluated against a
record in each state, the search view's filter domains are run, and the
actions are called. Invented data only (`ZZ …` names, made-up identifiers).
"""

import json
import re
import uuid
from unittest.mock import patch

from dateutil.relativedelta import relativedelta
from lxml import etree

from odoo import fields
from odoo.exceptions import UserError
from odoo.models import BaseModel
from odoo.tests import TransactionCase, new_test_user, tagged
from odoo.tools.safe_eval import safe_eval

from odoo.addons.goodahead_invoice_import.lib.import_source import (
    AI_POLICY,
    DEFAULT_POLICY,
    STRICT_POLICY,
    SourceReader,
)
from odoo.addons.goodahead_invoice_import.models.res_partner import (
    MATCHING_POLICY_CONTEXT_KEY,
)

from .common import ensure_purchase_journal

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def invoice_data(number="ZZ-T14-1", seller_name="ZZ Inbox Seller Oy", vat="FI90000014"):
    return {
        "type": "Invoice",
        "invoice_number": number,
        "issue_date": "2026-11-02",
        "currency": "EUR",
        "seller": {"name": seller_name, "vat": vat, "country_code": "FI"},
        "lines": [],
        "totals": {
            "line_extension": 100.0,
            "tax_exclusive": 100.0,
            "tax_inclusive": 125.5,
            "payable": 125.5,
        },
    }


class ScreenHelpers:
    """Evaluate the views the way the web client does, without a browser."""

    def _arch(self, view_type, model=None, user=None):
        Model = self.env[model or "goodahead.invoice.import"]
        if user:
            Model = Model.with_user(user)
        views = Model.get_views([(False, view_type)])
        return etree.fromstring(views["views"][view_type]["arch"])

    def _values(self, record, expression, context=None):
        values = {"context": context or {}, "True": True, "False": False, "None": None}
        for name in _NAME.findall(expression):
            if name in record._fields:
                value = record[name]
                if isinstance(value, BaseModel):
                    value = value.id or False
                values[name] = value
        return values

    def _is_visible(self, node, record, context=None):
        expression = node.get("invisible")
        if not expression:
            return True
        return not safe_eval(expression, self._values(record, expression, context))

    def _form_state(self, record):
        """(visible header buttons, visible banners, visible smart buttons)."""
        record.invalidate_recordset()
        arch = self._arch("form")
        header = {
            node.get("name")
            for node in arch.xpath("//header/button[@name]")
            if self._is_visible(node, record)
        }
        banners = {
            node.get("name")
            for node in arch.xpath("//div[starts-with(@name, 'banner_')]")
            if self._is_visible(node, record)
        }
        smart = {
            node.get("name")
            for node in arch.xpath("//div[@name='button_box']/button[@name]")
            if self._is_visible(node, record)
        }
        return header, banners, smart

    def _filter_domain(self, name):
        arch = self._arch("search")
        nodes = arch.xpath(f"//filter[@name='{name}']")
        self.assertTrue(nodes, f"filter {name} missing from the inbox search view")
        return safe_eval(
            nodes[0].get("domain"),
            {
                "context_today": lambda: fields.Date.context_today(self.env.user),
                "relativedelta": relativedelta,
            },
        )


@tagged("post_install", "-at_install")
class TestInboxScreens(ScreenHelpers, TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.Import = cls.env["goodahead.invoice.import"]
        cls.company = cls.env.company
        cls.other_company = cls.env["res.company"].create({"name": "ZZ Other Books Oy"})
        cls.accountant = new_test_user(
            cls.env,
            login="t14_accountant",
            groups="base.group_user,account.group_account_manager",
            company_id=cls.company.id,
            company_ids=[(6, 0, [cls.company.id, cls.other_company.id])],
        )
        cls.invoicing_only = new_test_user(
            cls.env,
            login="t14_invoicing",
            groups="base.group_user,account.group_account_invoice",
        )

    def _new(self, company=None, **vals):
        vals.setdefault("name", "zz-invented.xml")
        vals.setdefault("company_id", (company or self.company).id)
        return self.Import.create(vals)

    def _active_job(self, record):
        self.env.cr.execute(
            "INSERT INTO queue_job (uuid, state, model_name) VALUES (%s, %s, %s) RETURNING id",
            [str(uuid.uuid4()), "pending", self.Import._name],
        )
        record.queue_job_id = self.env.cr.fetchone()[0]
        self.env.invalidate_all()

    def _bill(self):
        journal = ensure_purchase_journal(self.env, self.company)
        return self.env["account.move"].create(
            {"move_type": "in_invoice", "journal_id": journal.id}
        )

    # -- SCR-02: banners and buttons per state ---------------------------------

    def test_failed_bad_data_offers_retry_reimport_and_close(self):
        record = self._new(
            state="failed",
            failure_kind="data",
            error_message="Totals don't add up",
            extraction_data=json.dumps(invoice_data()),
        )
        header, banners, smart = self._form_state(record)
        self.assertEqual(
            header, {"action_retry", "action_reimport_from_json", "action_open_close_dialog"}
        )
        self.assertEqual(banners, {"banner_failed_data"})
        self.assertFalse(smart)

    def test_failed_interrupted_offers_retry_and_close(self):
        """An interrupted import can be retried by hand (the retry goes to
        the record's keyed job, so it never doubles a queued one); the banner
        says what happened without promising an automatic retry."""
        record = self._new(state="failed", failure_kind="interrupted", error_message="APIX down")
        header, banners, _smart = self._form_state(record)
        self.assertEqual(header, {"action_retry", "action_open_close_dialog"})
        self.assertEqual(banners, {"banner_failed_interrupted"})
        banner = self._arch("form").xpath("//div[@name='banner_failed_interrupted']")[0]
        self.assertNotIn("automatically", "".join(banner.itertext()))
        with patch.object(type(self.Import), "_enqueue_import") as enqueue:
            record.action_retry()
        enqueue.assert_called_once()
        self.assertEqual(record.state, "waiting")
        self.assertFalse(record.failure_kind)

    def test_systray_list_title_is_translatable(self):
        # Since send T7 the bubble's "Show all" action comes from the server
        # (the shared bubble's failed_imports section), translated there.
        from odoo.tools.misc import file_open

        with file_open("goodahead_invoice_import/models/goodahead_invoice_import.py") as handle:
            source = handle.read()
        self.assertIn('"name": _("Incoming invoices needing attention")', source)
        self.assertIn('"label": _("Failed imports")', source)
        section = self.Import.with_user(self.accountant)._attention_failed_imports_section()
        self.assertEqual(section["action"]["name"], "Incoming invoices needing attention")

    def test_failed_interrupted_while_processing_offers_nothing_but_the_stale_note(self):
        record = self._new(state="failed", failure_kind="interrupted", error_message="APIX down")
        self._active_job(record)
        header, banners, _smart = self._form_state(record)
        self.assertNotIn("action_retry", header)
        self.assertNotIn("banner_failed_interrupted", banners)

    def test_duplicate_links_the_bill_and_offers_override_and_close(self):
        bill = self._bill()
        record = self._new(state="duplicate", duplicate_of_move_id=bill.id)
        header, banners, smart = self._form_state(record)
        self.assertEqual(header, {"action_override_duplicate", "action_open_close_dialog"})
        self.assertEqual(banners, {"banner_duplicate"})
        self.assertEqual(smart, {"action_open_duplicate_bill"})
        action = record.action_open_duplicate_bill()
        self.assertEqual((action["res_model"], action["res_id"]), ("account.move", bill.id))

    def test_possibly_lost_offers_import_and_close(self):
        record = self._new(state="possibly_lost")
        header, banners, _smart = self._form_state(record)
        self.assertEqual(header, {"action_import_possibly_lost", "action_open_close_dialog"})
        self.assertEqual(banners, {"banner_possibly_lost"})

    def test_closed_shows_reason_and_only_reopen(self):
        record = self._new(state="waiting")
        record.action_close_without_bill("ZZ test message")
        header, banners, _smart = self._form_state(record)
        self.assertEqual(header, {"action_reopen"})
        self.assertFalse(banners)
        closed_group = self._arch("form").xpath("//group[@name='group_closed']")[0]
        self.assertTrue(self._is_visible(closed_group, record))
        self.assertEqual(record.closed_by_id, self.env.user)
        self.assertTrue(record.closed_date)

    def test_imported_shows_the_bill_and_no_action(self):
        bill = self._bill()
        record = self._new(state="imported", move_id=bill.id)
        header, banners, smart = self._form_state(record)
        self.assertFalse(header)
        self.assertFalse(banners)
        self.assertEqual(smart, {"action_open_bill"})
        self.assertEqual(record.action_open_bill()["res_id"], bill.id)

    def test_processing_offers_nothing(self):
        for state in ("waiting", "failed", "duplicate", "possibly_lost"):
            record = self._new(state=state, failure_kind="data" if state == "failed" else False)
            self._active_job(record)
            self.assertEqual(record.display_state, "processing")
            header, _banners, _smart = self._form_state(record)
            self.assertFalse(header, state)

    # -- SCR-02: the actions behind the buttons --------------------------------

    def test_import_possibly_lost_enqueues_the_normal_import(self):
        record = self._new(state="possibly_lost")
        with patch.object(type(self.Import), "_enqueue_import") as enqueue:
            record.action_import_possibly_lost()
        self.assertEqual(record.state, "waiting")
        enqueue.assert_called_once()

    def test_import_possibly_lost_refuses_other_states(self):
        for state in ("waiting", "failed", "duplicate", "imported"):
            record = self._new(state=state)
            with self.assertRaises(UserError):
                record.action_import_possibly_lost()
            self.assertEqual(record.state, state)

    def test_import_possibly_lost_refuses_while_processing(self):
        record = self._new(state="possibly_lost")
        self._active_job(record)
        with self.assertRaises(UserError):
            record.action_import_possibly_lost()

    def test_reimport_of_structured_data_that_still_fails_is_refused(self):
        data = invoice_data()
        data["totals"]["payable"] = 999.0
        record = self._new(state="failed", failure_kind="data", extraction_data=json.dumps(data))
        structured = SourceReader("zz_structured", 10, STRICT_POLICY, structured=True)
        with patch.object(type(self.Import), "_import_reader_spec", lambda self: structured):
            with self.assertRaises(UserError) as caught:
                record.action_reimport_from_json()
        self.assertIn("still does not pass", str(caught.exception))
        self.assertEqual(record.state, "failed")
        self.assertFalse(record.move_id)

    # -- SCR-01: columns -------------------------------------------------------

    def test_inbox_columns_come_from_the_invoice_data(self):
        record = self._new(extraction_data=json.dumps(invoice_data()))
        self.assertEqual(record.seller_name, "ZZ Inbox Seller Oy")
        self.assertEqual(record.invoice_number, "ZZ-T14-1")
        self.assertEqual(record.invoice_date, fields.Date.to_date("2026-11-02"))
        self.assertAlmostEqual(record.amount_total, 125.5)
        self.assertEqual(record.currency_id.name, "EUR")
        # A hand edit of failed data moves the columns with it.
        record.extraction_data = json.dumps(invoice_data(number="ZZ-T14-2"))
        self.assertEqual(record.invoice_number, "ZZ-T14-2")
        unreadable = self._new(extraction_data="{not json")
        self.assertFalse(unreadable.seller_name)
        self.assertEqual(unreadable.amount_total, 0.0)

    def test_inbox_list_shows_the_columns(self):
        arch = self._arch("list")
        columns = {node.get("name") for node in arch.xpath("//list/field")}
        for name in (
            "display_state", "source", "reader", "seller_name", "invoice_number",
            "amount_total", "invoice_date", "company_id", "create_date",
        ):
            self.assertIn(name, columns)
        action = self.env.ref("goodahead_invoice_import.action_ai_invoice_import")
        self.assertEqual(action.name, "Incoming invoices")
        menu = self.env.ref("goodahead_invoice_import.menu_ai_invoice_import")
        self.assertEqual(menu.name, "Incoming invoices")
        self.assertEqual(menu.action, action)

    # -- SCR-01: filters -------------------------------------------------------

    def test_search_filters_return_the_right_records(self):
        records = {
            state: self._new(state=state, name=f"zz-{state}.xml")
            for state in ("waiting", "imported", "duplicate", "failed", "possibly_lost")
        }
        records["closed"] = self._new(state="waiting", name="zz-closed.xml")
        records["closed"].action_close_without_bill("ZZ handled by hand")
        stale = self._new(state="waiting", name="zz-stale.xml")
        self.env.cr.execute(
            "INSERT INTO queue_job (uuid, state, model_name, date_created) "
            "VALUES (%s, 'started', %s, now() at time zone 'utc' - interval '2 hours') RETURNING id",
            [str(uuid.uuid4()), self.Import._name],
        )
        stale.queue_job_id = self.env.cr.fetchone()[0]
        email = self._new(state="waiting", source="email", name="zz-email.xml")
        self.env.invalidate_all()
        ours = self.Import.browse([r.id for r in records.values()] + [stale.id, email.id])

        def found(name):
            return self.Import.search([("id", "in", ours.ids)] + self._filter_domain(name))

        self.assertEqual(
            found("unsettled"),
            records["waiting"] | records["duplicate"] | records["failed"]
            | records["possibly_lost"] | stale | email,
        )
        self.assertEqual(found("failed"), records["failed"])
        self.assertEqual(found("duplicate"), records["duplicate"])
        self.assertEqual(found("possibly_lost"), records["possibly_lost"])
        self.assertEqual(found("processing_too_long"), stale)
        self.assertEqual(found("closed"), records["closed"])
        self.assertEqual(found("source_email"), email)
        self.assertNotIn(email, found("source_upload"))

    def test_records_of_other_companies_are_not_listed(self):
        mine = self._new(state="failed")
        theirs = self._new(company=self.other_company, state="failed")
        user = new_test_user(
            self.env,
            login="t14_one_company",
            groups="base.group_user,account.group_account_manager",
        )
        visible = self.Import.with_user(user).search([("id", "in", (mine | theirs).ids)])
        self.assertEqual(visible, mine)

    # -- systray -------------------------------------------------------

    def test_systray_counts_failed_duplicate_and_possibly_lost_of_my_companies(self):
        baseline = (
            self.Import.with_user(self.accountant)
            .with_context(allowed_company_ids=[self.company.id])
            ._systray_attention_data()["count"]
        )
        for state in ("failed", "duplicate", "possibly_lost", "waiting", "imported"):
            self._new(state=state, extraction_data=json.dumps(invoice_data()))
        busy = self._new(state="failed")
        self._active_job(busy)  # shows processing: not counted
        self._new(company=self.other_company, state="failed")

        as_accountant = self.Import.with_user(self.accountant)
        one_company = as_accountant.with_context(allowed_company_ids=[self.company.id])
        data = one_company._systray_attention_data()
        self.assertEqual(data["count"], baseline + 3)
        self.assertTrue(any(item["name"] == "ZZ Inbox Seller Oy ZZ-T14-1" for item in data["imports"]))
        self.assertTrue(all(item["status"] for item in data["imports"]))

        both = as_accountant.with_context(
            allowed_company_ids=[self.company.id, self.other_company.id]
        )
        self.assertGreaterEqual(both._systray_attention_data()["count"], baseline + 4)

        self.assertEqual(
            self.Import.with_user(self.invoicing_only)._systray_attention_data(),
            {"count": 0, "imports": []},
        )

    # -- the shared e-invoicing attention bubble -------

    def _failed_imports_section(self, user, company_ids):
        Attention = self.env["goodahead.einvoice.attention"].with_user(user)
        data = Attention.with_context(allowed_company_ids=company_ids)._attention_data()
        return next((s for s in data["sections"] if s["key"] == "failed_imports"), None), data

    def test_failed_imports_section_in_the_shared_bubble(self):
        self._new(state="failed", extraction_data=json.dumps(invoice_data()), error_message="ZZ broken")
        section, data = self._failed_imports_section(self.accountant, [self.company.id])
        legacy = (
            self.Import.with_user(self.accountant)
            .with_context(allowed_company_ids=[self.company.id])
            ._systray_attention_data()
        )
        self.assertTrue(section)
        self.assertEqual(section["count"], legacy["count"])
        self.assertEqual(section["groups"], "account.group_account_user,account.group_account_manager")
        self.assertGreaterEqual(data["total"], section["count"])
        item = next(i for i in section["items"] if i["title"] == "ZZ Inbox Seller Oy ZZ-T14-1")
        self.assertEqual(set(item), {"id", "title", "subtitle", "badge", "date"})
        self.assertEqual(item["subtitle"], "ZZ broken")
        self.assertTrue(item["badge"])
        self.assertEqual(
            [i["id"] for i in section["items"]], [i["id"] for i in legacy["imports"]]
        )
        self.assertEqual(section["action"]["res_model"], "goodahead.invoice.import")
        self.assertEqual(
            section["action"]["domain"],
            [["display_state", "in", ["failed", "duplicate", "possibly_lost"]]],
        )
        self.assertEqual(section["item_action"]["res_model"], "goodahead.invoice.import")

    def test_failed_imports_section_counts_only_allowed_companies(self):
        # AC-34: an Accountant allowed only in the first company.
        one_company_accountant = new_test_user(
            self.env,
            login="t7_one_company_accountant",
            groups="base.group_user,account.group_account_manager",
            company_id=self.company.id,
            company_ids=[(6, 0, [self.company.id])],
        )
        before, _data = self._failed_imports_section(one_company_accountant, [self.company.id])
        before = before["count"] if before else 0
        mine = self._new(state="failed")
        self._new(company=self.other_company, state="failed")
        self._new(company=self.other_company, state="duplicate")
        section, _data = self._failed_imports_section(one_company_accountant, [self.company.id])
        self.assertEqual(section["count"], before + 1)
        self.assertIn(mine.id, [i["id"] for i in section["items"]])
        both, _data = self._failed_imports_section(
            self.accountant, [self.company.id, self.other_company.id]
        )
        self.assertGreaterEqual(both["count"], before + 3)

    def test_failed_imports_section_hidden_from_plain_invoicing(self):
        self._new(state="failed")
        section, data = self._failed_imports_section(self.invoicing_only, [self.company.id])
        self.assertIsNone(section)
        self.assertNotIn("failed_imports", [s["key"] for s in data["sections"]])

    # -- T4 carry-over: the vendor buttons use the import's policy -----------------

    def _shared_vat_vendors(self, vat):
        Partner = self.env["res.partner"]
        established = Partner.create(
            {"name": "ZZ Established Vendor Oy", "is_company": True, "vat": vat, "supplier_rank": 5}
        )
        other = Partner.create(
            {"name": "ZZ Sharing Vendor Oy", "is_company": True, "vat": vat}
        )
        return established, other

    def _imported_bill(self, seller_name, vat):
        bill = self._bill()
        self._new(
            state="imported",
            move_id=bill.id,
            extraction_data=json.dumps(invoice_data(seller_name=seller_name, vat=vat)),
        )
        return bill

    def test_create_vendor_on_a_strict_bill_never_links_an_identifier_sharer(self):
        vat = "FI90001414"
        established, other = self._shared_vat_vendors(vat)
        bill = self._imported_bill("ZZ Brand New Seller Oy", vat)
        with patch.object(
            type(self.Import), "_import_reader_policy", lambda self: STRICT_POLICY
        ):
            self.assertIs(bill._ai_import_policy(), STRICT_POLICY)
            self.assertEqual(
                bill._ai_with_import_policy().env.context.get(MATCHING_POLICY_CONTEXT_KEY),
                STRICT_POLICY,
            )
            bill.action_ai_create_vendor_from_extraction()
        self.assertTrue(bill.partner_id)
        self.assertNotIn(bill.partner_id, established | other)
        self.assertEqual(bill.partner_id.name, "ZZ Brand New Seller Oy")

    def test_create_vendor_on_a_strict_bill_links_a_unique_vat_match(self):
        vendor = self.env["res.partner"].create(
            {"name": "ZZ Only Vendor Oy", "is_company": True, "vat": "FI90001415"}
        )
        bill = self._imported_bill("ZZ Only Vendor Oy", "FI90001415")
        with patch.object(
            type(self.Import), "_import_reader_policy", lambda self: STRICT_POLICY
        ):
            bill.action_ai_create_vendor_from_extraction()
        self.assertEqual(bill.partner_id, vendor)

    def test_create_vendor_on_an_ai_bill_is_unchanged(self):
        vat = "FI90001416"
        established, other = self._shared_vat_vendors(vat)
        bill = self._imported_bill("ZZ Brand New Seller Oy", vat)
        # The default policy, or the AI reader's when that add-on is installed:
        # both match vendors exactly as before (the AI one only looks bank
        # accounts up, a security fix).
        self.assertIn(bill._ai_import_policy(), (DEFAULT_POLICY, AI_POLICY))
        self.assertEqual(bill._ai_with_import_policy(), bill)
        on_context = bill._ai_with_import_policy().env.context.get(MATCHING_POLICY_CONTEXT_KEY)
        self.assertIn(on_context, (None, False, AI_POLICY))
        self.assertTrue((on_context or DEFAULT_POLICY).is_legacy)
        expected = self.env["res.partner"]._ai_find_vendor(
            invoice_data(vat=vat)["seller"], company=bill.company_id
        )
        bill.action_ai_create_vendor_from_extraction()
        self.assertIn(bill.partner_id, established | other)
        self.assertEqual(bill.partner_id, expected)
