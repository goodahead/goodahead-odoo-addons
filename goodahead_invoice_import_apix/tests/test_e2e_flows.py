# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""End-to-end flows across the add-ons (test-plan "Coverage" e2e and
e2e-through-UI rows, "Edge cases").

APIX is replaced at its boundary by `FakeApix`; everything else is the real
chain: Scheduler -> run job -> package jobs -> bills -> mark-received jobs ->
APIX shows received -> daily checks. Queued jobs are performed explicitly.
The UI flow (US-04 recovery) is driven through `odoo.tests.Form` on the
screens a user works with — no browser tours. US-06 through the migration
tool is in `goodahead_invoice_import_apix_migration` (test_e2e_migration).

AC-01, AC-03, AC-08, AC-11c, AC-15, AC-22, AC-27. Invented data only.
"""

import json
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from odoo.tests import Form, new_test_user, tagged

from odoo.addons.queue_job.job import Job

from .apix_e2e_common import (
    ALL_APIX_JOBS,
    TRANSFER_A,
    TRANSFER_B,
    ApixE2ECase,
    finvoice_fixture,
    make_zip,
    simple_finvoice,
)
from .test_package_import import TINY_PDF

#: Every APIX job except the mark: lets a flow look at the moment between a
#: bill's commit and APIX hearing of it.
NO_MARK_JOBS = tuple(prefix for prefix in ALL_APIX_JOBS if prefix != "apix-mark:")


@tagged("post_install", "-at_install", "apix_e2e")
class TestApixEndToEnd(ApixE2ECase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = new_test_user(
            cls.env,
            login="t17_admin",
            email="t17.admin@example.test",
            groups="base.group_user,account.group_account_manager",
            company_id=cls.company.id,
            company_ids=[(6, 0, cls.company.ids)],
        )
        cls.accountant = new_test_user(
            cls.env,
            login="t17_accountant",
            email="t17.accountant@example.test",
            groups="base.group_user,base.group_partner_manager,account.group_account_invoice",
            company_id=cls.company.id,
            company_ids=[(6, 0, cls.company.ids)],
        )

    def _package(self, storage_id, number, raw=None, status="UNRECEIVED", transfer_id=None, **kw):
        body = make_zip(("invoice.xml", raw or simple_finvoice(number)))
        return self.fake.add_package(
            transfer_id or TRANSFER_A,
            storage_id,
            body,
            number,
            status=status,
            **kw,
        )

    def _daily_checks(self):
        """The daily Scheduler: enqueue and perform the daily checks."""
        self.Connection._cron_apix_daily_checks()
        timings = []
        self._perform(("apix-daily:",), timings=timings)
        return [result for _key, _seconds, result in timings]

    # -- AC-01: the full live cycle ------------------------------------------

    def test_ac01_full_live_cycle(self):
        group = self._package("E-1", "E2E-1")
        group["ImageName"] = "invoice-E2E-1.pdf"
        self.fake.packages["E-1"]["body"] = make_zip(
            ("invoice.xml", simple_finvoice("E2E-1")), ("invoice-E2E-1.pdf", TINY_PDF)
        )
        self._package("E-2", "E2E-2")
        self._package("E-3", "E2E-3")

        timings = []
        self._scheduler_cycle(timings=timings)
        keys = [key for key, _seconds, _result in timings]
        self.assertEqual(keys[0], f"apix-run:{self.company.id}", "the run comes first")
        self.assertEqual(sum(key.startswith("apix-import:") for key in keys), 3)
        self.assertEqual(sum(key.startswith("apix-mark:") for key in keys), 3)

        records = self._apix_records()
        self.assertEqual(len(records), 3)
        for record in records:
            number = record.apix_listing_data["DocumentID"]
            with self.subTest(number=number):
                self.assertEqual(record.state, "imported", record.error_message or "")
                move = record.move_id
                self.assertEqual(move.state, "draft")
                self.assertEqual(move.move_type, "in_invoice")
                self.assertEqual(move.company_id, self.company)
                self.assertEqual(move.partner_id, self.vendor, "matched by VAT number")
                self.assertEqual(move.ref, number)
                self.assertEqual(str(move.invoice_date), "2026-11-15")
                self.assertEqual(str(move.invoice_date_due), "2026-11-29")
                self.assertEqual(move.payment_reference, "10001235")
                lines = move.invoice_line_ids.filtered(lambda line: line.display_type == "product")
                self.assertEqual(len(lines), 2)
                self.assertEqual(lines.tax_ids, self.tax_25)
                self.assertAlmostEqual(move.amount_untaxed, 360.00, places=2)
                self.assertAlmostEqual(move.amount_tax, 91.80, places=2)
                self.assertAlmostEqual(move.amount_total, 451.80, places=2)
                # APIX shows it received (ADR-0005: only after the bill).
                self.assertTrue(record.apix_mark_confirmed)
                self.assertEqual(self.fake.status(record.apix_storage_id), "RECEIVED")
        image_record = self._record("E-1")
        self.assertEqual(
            image_record.move_id.message_main_attachment_id.name, "invoice-E2E-1.pdf"
        )
        self.assertEqual(
            self._record("E-2").move_id.message_main_attachment_id.name, "invoice.xml"
        )
        # Every package downloaded once to import, once to mark; never marked first.
        self.assertEqual(sorted(self.fake.downloads(False)), ["E-1", "E-2", "E-3"])
        self.assertEqual(sorted(self.fake.marked), ["E-1", "E-2", "E-3"])
        for storage_id in ("E-1", "E-2", "E-3"):
            calls = [c for c in self.fake.calls if c[0] == "download" and c[1] == storage_id]
            self.assertEqual([c[2] for c in calls], [False, True])
        self.assertEqual(self.connection.last_run_state, "success")

        # The inbox: nothing left to do for the Accountant.
        unsettled = self.Import.with_user(self.accountant).search(
            [("source", "=", "apix"), ("state", "not in", ("imported", "closed"))]
        )
        self.assertFalse(unsettled)

        # The daily check is clean and opens no to-do.
        self.assertEqual(self._daily_checks(), ["ageing: 0; reconciliation: 0"])
        self.assertFalse(self.connection._apix_open_todos())

        # The next Scheduler run imports nothing again.
        downloads = len(self.fake.downloads())
        self._scheduler_cycle()
        self.assertEqual(len(self.fake.downloads()), downloads)
        self.assertEqual(len(self._bills()), 3)

    # -- AC-22: the same invoice by email, then through APIX ---------------------

    def _mail_finvoice(self, raw, name):
        message = MIMEMultipart()
        message["From"] = "laskutus@example.test"
        message["To"] = "bills@example.test"
        message["Subject"] = f"Lasku {name}"
        message["Message-Id"] = f"<t17-{name}@example.test>"
        message.attach(MIMEText("Liitteenä lasku.", "plain", "utf-8"))
        part = MIMEText(raw.decode("utf-8"), "xml", "utf-8")
        part.add_header("Content-Disposition", "attachment", filename=name)
        message.attach(part)
        before = self.Import.search([]).ids
        self.env["mail.thread"].message_process("goodahead.invoice.import", message.as_bytes())
        return self.Import.search([("id", "not in", before)], order="id")

    def test_ac22_email_first_then_apix_is_a_duplicate_marked_only_when_closed(self):
        raw = simple_finvoice("DUP-77")
        by_mail = self._mail_finvoice(raw, "DUP-77.xml")
        self.assertEqual(len(by_mail), 1)
        self.assertEqual(by_mail.source, "email")
        Job.load(self.env, by_mail.queue_job_id.uuid).perform()
        by_mail.invalidate_recordset()
        self.assertEqual(by_mail.state, "imported", by_mail.error_message or "")
        bill = by_mail.move_id

        self._package("D-1", "DUP-77", raw=raw)
        self._scheduler_cycle()
        record = self._record("D-1")
        self.assertEqual(record.state, "duplicate")
        self.assertEqual(record.duplicate_of_move_id, bill)
        self.assertFalse(record.move_id)
        self.assertEqual(len(self._bills(ref="DUP-77")), 1, "no second bill")
        self.assertFalse(self._jobs("apix-mark:", record), "a duplicate is not marked")
        self.assertFalse(self.fake.marked)
        self.assertEqual(self.fake.status("D-1"), "UNRECEIVED")

        # The Accountant confirms it is the same invoice: the close dialog.
        dialog = Form(
            self.env["goodahead.invoice.import.close"]
            .with_user(self.accountant)
            .with_context(default_import_ids=[(6, 0, record.ids)])
        )
        self.assertTrue(dialog.is_apix, "the dialog says APIX will be told")
        dialog.reason = "Same invoice as the one that came by email"
        dialog.save().action_confirm()
        record.invalidate_recordset()
        self.assertEqual(record.state, "closed")
        self.assertEqual(record.closed_by_id, self.accountant)
        self._perform()
        record.invalidate_recordset()
        self.assertTrue(record.apix_mark_confirmed)
        self.assertEqual(self.fake.marked, ["D-1"])
        self.assertEqual(len(self._bills(ref="DUP-77")), 1)

    # -- AC-03: a crash between the bill and the mark ----------------------------

    def test_ac03_mark_lost_after_commit_is_recovered_by_the_daily_check(self):
        self._package("C-1", "CRASH-1")
        self.fake.mark_failures["C-1"] = 5
        timings = []
        self._scheduler_cycle(timings=timings)
        record = self._record("C-1")
        self.assertEqual(record.state, "imported", record.error_message or "")
        mark_tries = [result for key, _s, result in timings if key.startswith("apix-mark:")]
        self.assertEqual(len(mark_tries), 5, "5 tries (events.md)")
        self.assertEqual(mark_tries[-1], "FAILED: apix.unreachable")
        mark_job = self._jobs("apix-mark:", record)
        self.assertEqual(mark_job.state, "done", "given up quietly")
        self.assertFalse(record.apix_mark_confirmed, "unconfirmed")
        self.assertEqual(self.fake.status("C-1"), "UNRECEIVED")

        # The next run neither imports again nor loses the mark.
        self._scheduler_cycle()
        self.assertEqual(self.fake.downloads(False), ["C-1"], "never downloaded to import again")
        self.assertEqual(len(self._bills(ref="CRASH-1")), 1)
        self.assertIn(record, self.Import._apix_unconfirmed_marks(self.company))

        # The daily reconciliation finds it, sends the mark again, opens a to-do.
        self.Connection._cron_apix_daily_checks()
        timings = []
        self._perform(("apix-daily:",), timings=timings)
        self.assertEqual(timings[0][2], "ageing: 0; reconciliation: 1")
        self.assertEqual(len(self.connection._apix_open_todos("reconciliation")), 1)
        again = self._jobs("apix-mark:", record).filtered(lambda j: j.state == "pending")
        self.assertEqual(len(again), 1, "re-enqueued")
        self._perform(("apix-mark:",))
        record.invalidate_recordset()
        self.assertTrue(record.apix_mark_confirmed, "confirmed")
        self.assertEqual(self.fake.status("C-1"), "RECEIVED")
        self.assertEqual(self.fake.marked, ["C-1"])

        # The next comparison is clean and closes the to-do.
        self.assertEqual(
            self.Connection._apix_daily_checks(self.company.id), "ageing: 0; reconciliation: 0"
        )
        self.assertFalse(self.connection._apix_open_todos("reconciliation"))

    # -- AC-27: two companies, one rejected ----------------------------------------

    def test_ac27_two_companies_one_rejected(self):
        company_b = self.env["res.company"].create({"name": "E2E Rejected Oy"})
        conn_b = self._live_connection(company_b, TRANSFER_B, f"secret-{TRANSFER_B}", "0000007-8")
        self.fake.add_account(
            TRANSFER_B, f"secret-{TRANSFER_B}", "0000007-8", company_b.name, rejected=True
        )
        self._package("A-1", "TWO-1")
        self._package("A-2", "TWO-2")
        # B's packages exist at APIX but its credentials are rejected.
        self._package("B-1", "TWO-B1", transfer_id=TRANSFER_B)

        timings = []
        self._scheduler_cycle(timings=timings)
        runs = {key: result for key, _s, result in timings if key.startswith("apix-run:")}
        self.assertEqual(
            runs, {f"apix-run:{self.company.id}": "success", f"apix-run:{company_b.id}": "rejected"}
        )
        records_a = self._apix_records()
        self.assertEqual(sorted(records_a.mapped("state")), ["imported", "imported"])
        self.assertTrue(all(records_a.mapped("apix_mark_confirmed")))
        self.assertEqual(len(self._bills()), 2)
        self.assertFalse(self.Import.sudo().search([("company_id", "=", company_b.id)]))
        self.assertFalse(self._bills(company=company_b))
        self.assertEqual(self.connection.last_run_state, "success")
        self.assertEqual(conn_b.last_run_state, "failed")
        self.assertEqual(conn_b.status, "rejected")
        self.assertEqual(len(conn_b._apix_open_todos("rejected")), 1)
        self.assertFalse(self.connection._apix_open_todos())
        self.assertNotIn("B-1", self.fake.downloads())

        # Fetching stays stopped for B until a check succeeds; A goes on.
        self.Connection._cron_apix_run()
        pending = self._pending(("apix-run:",)).mapped("identity_key")
        self.assertEqual(pending, [f"apix-run:{self.company.id}"])

    # -- AC-15: two companies, each its own bills and vendors ----------------------

    def test_ac15_two_companies_bills_and_vendors_stay_in_their_company(self):
        company_b = self.env["res.company"].create(
            {"name": "E2E Second Oy", "currency_id": self.company.currency_id.id}
        )
        self.env.user.company_ids = [(4, company_b.id)]
        try:
            self.env["account.chart.template"].try_loading(
                self.company.chart_template, company=company_b, install_demo=False
            )
        except Exception as error:  # noqa: BLE001 - depends on the chart available
            self.skipTest(f"no chart of accounts for a second company: {error}")
        if not self.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", company_b.id)], limit=1
        ):
            self.skipTest("no purchase journal for the second company")
        self._purchase_tax(company_b, 25.5)
        self._live_connection(company_b, TRANSFER_B, f"secret-{TRANSFER_B}", "0000007-8")
        self.fake.add_account(TRANSFER_B, f"secret-{TRANSFER_B}", "0000007-8", company_b.name)

        # A seller known as a private vendor in each company (same VAT number).
        seller = {"vat": "FI00000043", "business_id": "0000004-3"}
        vendor_a = self.env["res.partner"].create(
            {
                "name": "Example Private Seller Oy (A)",
                "is_company": True,
                "vat": seller["vat"],
                "company_registry": seller["business_id"],
                "company_id": self.company.id,
            }
        )
        vendor_b = self.env["res.partner"].create(
            {
                "name": "Example Private Seller Oy (B)",
                "is_company": True,
                "vat": seller["vat"],
                "company_registry": seller["business_id"],
                "company_id": company_b.id,
            }
        )
        self._package("S-A", "PRIV-A", raw=simple_finvoice("PRIV-A", **seller))
        self._package(
            "S-B", "PRIV-B", raw=simple_finvoice("PRIV-B", **seller), transfer_id=TRANSFER_B
        )
        self._scheduler_cycle()

        record_a, record_b = self._record("S-A"), self._record("S-B", company=company_b)
        self.assertEqual(record_a.state, "imported", record_a.error_message or "")
        self.assertEqual(record_b.state, "imported", record_b.error_message or "")
        self.assertEqual(record_a.move_id.company_id, self.company)
        self.assertEqual(record_b.move_id.company_id, company_b)
        self.assertEqual(record_a.move_id.partner_id, vendor_a)
        self.assertEqual(record_b.move_id.partner_id, vendor_b, "never the other company's vendor")
        self.assertTrue(record_a.apix_mark_confirmed and record_b.apix_mark_confirmed)

        # The Accountant allowed only in the first company sees its side only.
        imports = self.Import.with_user(self.accountant).search([("source", "=", "apix")])
        self.assertEqual(imports, record_a)
        bills = self.Move.with_user(self.accountant).search(
            [("move_type", "=", "in_invoice"), ("ref", "in", ["PRIV-A", "PRIV-B"])]
        )
        self.assertEqual(bills, record_a.move_id)

    # -- US-04 through the record form: failed -> edit -> retry -> bill ----------

    def test_us04_recovery_through_the_inbox_and_the_record_form(self):
        raw = simple_finvoice("REC-1").replace(
            b">451,80</InvoiceTotalVatIncludedAmount>", b">461,80</InvoiceTotalVatIncludedAmount>"
        )
        self._package("R-1", "REC-1", raw=raw)
        self._scheduler_cycle()

        # The Accountant opens the inbox (its action, unsettled by default).
        action = self.env["ir.actions.act_window"].with_user(self.accountant)._for_xml_id(
            "goodahead_invoice_import.action_ai_invoice_import"
        )
        self.assertEqual(action["res_model"], "goodahead.invoice.import")
        Inbox = self.env["goodahead.invoice.import"].with_user(self.accountant)
        failed = Inbox.search(
            list(action.get("domain") or []) + [("state", "=", "failed"), ("source", "=", "apix")]
        )
        self.assertEqual(failed.apix_storage_id, "R-1")
        self.assertEqual(failed.failure_kind, "data")
        # The amount to pay (451.80) and the stated total (461.80) disagree:
        # what is owed is unclear, so this still fails. Rows that do
        # not make a total are a draft with the banner instead.
        self.assertIn("451.80", failed.error_message, "a reason naming both")
        self.assertIn("461.80", failed.error_message, "a reason naming both")
        self.assertFalse(failed.move_id)
        self.assertFalse(self.fake.marked, "AC-03: a failed import is never marked")
        self.assertEqual(self.fake.status("R-1"), "UNRECEIVED", "the package stays ready")

        # The record form: the data is editable while failed; fix the total.
        with Form(failed) as form:
            data = json.loads(form.extraction_data)
            self.assertAlmostEqual(data["totals"]["tax_inclusive"], 461.80)
            data["totals"]["tax_inclusive"] = 451.80
            form.extraction_data = json.dumps(data, indent=2)
        failed.action_reimport_from_json()
        failed.invalidate_recordset()
        self.assertEqual(failed.state, "imported", failed.error_message or "")
        self.assertAlmostEqual(failed.move_id.amount_total, 451.80, places=2)
        self.assertEqual(failed.move_id.partner_id, self.vendor)
        self.assertEqual(self.fake.downloads(False), ["R-1"], "no new download")

        self._perform()
        failed.invalidate_recordset()
        self.assertTrue(failed.apix_mark_confirmed)
        self.assertEqual(self.fake.marked, ["R-1"])


@tagged("post_install", "-at_install", "apix_e2e")
class TestApixEdgeCasesEndToEnd(ApixE2ECase):
    """test-plan "Edge cases" through the whole APIX chain (run -> package job
    -> bill -> mark). The Finvoice library's own tests cover the mapping; here
    the bill is checked."""

    def _import_via_apix(self, storage_id, raw, number):
        self.fake.add_package(
            TRANSFER_A, storage_id, make_zip(("invoice.xml", raw)), number
        )
        self._scheduler_cycle()
        record = self._record(storage_id)
        self.assertEqual(record.state, "imported", record.error_message or "")
        self.assertTrue(record.apix_mark_confirmed)
        return record, record.move_id

    @staticmethod
    def _product_lines(move):
        return move.invoice_line_ids.filtered(lambda line: line.display_type == "product")

    def test_sub_rows_are_not_booked(self):
        vendor = self.env["res.partner"].create(
            {
                "name": "Example Hardware Oy",
                "is_company": True,
                "vat": "FI00000027",
                "company_registry": "0000002-7",
            }
        )
        record, move = self._import_via_apix(
            "EC-SUB", finvoice_fixture("fi30_subrows_freetext.xml"), "H-778"
        )
        self.assertEqual(move.partner_id, vendor)
        lines = self._product_lines(move)
        self.assertEqual(len(lines), 3, "the SubInvoiceRow summary is not a line")
        self.assertFalse(lines.filtered(lambda line: "Välisumma" in (line.name or "")))
        self.assertAlmostEqual(move.amount_untaxed, 25.00, places=2)
        self.assertAlmostEqual(move.amount_total, 31.38, places=2)
        data = json.loads(record.extraction_data)
        self.assertTrue(any("SubInvoiceRow" in w for w in data["source_meta"]["warnings"]))

    def test_rounding_row_becomes_a_rounding_line(self):
        _record, move = self._import_via_apix(
            "EC-ROUND", finvoice_fixture("fi30_rounding.xml"), "10047"
        )
        lines = self._product_lines(move)
        rounding = lines.filtered(lambda line: line.name == "Rounding")
        self.assertEqual(len(rounding), 1)
        self.assertAlmostEqual(rounding.price_subtotal, 0.01, places=2)
        self.assertAlmostEqual(move.amount_total, 100.40, places=2, msg="validation passes")

    def test_multi_rate_vat_gives_one_tax_per_rate(self):
        taxes = {rate: self._purchase_tax(self.company, rate) for rate in (10.0, 14.0, 0.0)}
        taxes[25.5] = self.tax_25
        _record, move = self._import_via_apix(
            "EC-MULTI", finvoice_fixture("fi30_multi_rate.xml"), "10046"
        )
        lines = self._product_lines(move)
        self.assertEqual(len(lines), 5)
        rates = sorted(lines.mapped(lambda line: line.tax_ids.amount))
        self.assertEqual(rates, [0.0, 0.0, 10.0, 14.0, 25.5])
        tax_lines = move.line_ids.filtered(lambda line: line.display_type == "tax")
        by_rate = {
            line.tax_line_id.amount: abs(line.balance) for line in tax_lines if line.tax_line_id
        }
        self.assertAlmostEqual(by_rate[10.0], 4.00, places=2)
        self.assertAlmostEqual(by_rate[14.0], 4.20, places=2)
        self.assertAlmostEqual(by_rate[25.5], 25.50, places=2)
        self.assertAlmostEqual(move.amount_untaxed, 190.00, places=2)
        self.assertAlmostEqual(move.amount_tax, 33.70, places=2)
        self.assertAlmostEqual(move.amount_total, 223.70, places=2)

    def test_non_eur_invoice_takes_the_currency_of_the_amounts(self):
        sek = self.env.ref("base.SEK")
        sek.active = True
        self.assertNotEqual(self.company.currency_id, sek)
        raw = finvoice_fixture("fi30_non_eur.xml").replace(
            b'AmountCurrencyIdentifier="USD"', b'AmountCurrencyIdentifier="SEK"'
        )
        _record, move = self._import_via_apix("EC-SEK", raw, "EX-2026-77")
        self.assertEqual(move.currency_id, sek)
        self.assertAlmostEqual(move.amount_untaxed, 200.00, places=2)
        self.assertAlmostEqual(move.amount_total, 251.00, places=2)
