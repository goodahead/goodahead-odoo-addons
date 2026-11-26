# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The migration tool.

AC-11d, AC-17, AC-18 (this side), AC-19 (this side), AC-19b. The earlier
connector is a test-only migration source (`common.MigrationSourceMixin`):
its methods are mocks, checked for being called. No network.
"""

from unittest.mock import patch

from psycopg2.errors import LockNotAvailable

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.goodahead_apix.lib.apix_client import ApixClient

from .common import FAKE, TRANSFER_B, MigrationCase, finvoice, group, make_zip


@tagged("post_install", "-at_install")
class TestApixMigrationTool(MigrationCase):
    # -- step 1: detect ---------------------------------------------------------

    def test_detect_nothing_lets_the_admin_connect_manually(self):
        company = self.env["res.company"].create({"name": "Migration Detect Oy"})
        self.admin.company_ids = [(4, company.id)]
        self.assertEqual(self.Connection._migration_detect_all(company), ({}, {}))
        wizard = self._wizard(company=company)
        self.assertEqual(wizard.step, "detect")
        self.assertFalse(wizard.source_found)
        with self.assertRaises(UserError):
            wizard.action_carry_over()
        action = wizard.action_connect_manually()
        self.assertEqual(action["res_model"], "goodahead.apix.connection")
        self.assertEqual(action["context"]["default_company_id"], company.id)

    def test_carry_over_from_the_earlier_connector(self):
        company = self.env["res.company"].create({"name": "Migration Carry Oy"})
        self.admin.company_ids = [(4, company.id)]
        detect = self.sources[FAKE]["detect"]
        detect.return_value = {
            "environment": "production",
            "transfer_id": TRANSFER_B,
            "transfer_key": f"secret-{TRANSFER_B}",
            "business_id": "0000007-8",
        }
        info = {"groups": [{"y-tunnus": "0000007-8", "Name": "Migration Carry Oy"}]}
        with patch.object(ApixClient, "company_info", autospec=True, return_value=info):
            wizard = self._wizard(company=company)
            self.assertEqual(wizard.source_code, FAKE, "the one source is preselected")
            self.assertEqual(wizard.source_count, 1)
            self.assertTrue(wizard.source_found)
            self.assertEqual(wizard.source_environment, "production")
            self.assertEqual(wizard.source_business_id, "0000007-8")
            # 🔴 the screen never holds a secret
            self.assertFalse({"transfer_key", "transfer_id", "password"} & set(wizard._fields))
            action = wizard.action_carry_over()
        self.assertEqual(detect.call_args.args[-1], company)
        self.assertEqual(wizard.step, "shadow")
        self.assertEqual(action["res_model"], "goodahead.apix.migration")
        connection = self.Connection.search([("company_id", "=", company.id)])
        self.assertEqual(connection.migrated_from, FAKE)
        self.assertEqual(connection.receiving_state, "shadow")
        self.assertEqual(connection.status, "working")
        self.assertEqual(connection.environment, "production")
        self.assertEqual(connection.transfer_id, TRANSFER_B)
        self.assertEqual(connection.transfer_key, f"secret-{TRANSFER_B}")
        self.assertEqual(connection.business_id, "0000007-8")
        self.assertTrue(connection.receiving_state_since)

    def test_administrators_only(self):
        with self.assertRaises(UserError):
            self.Connection.with_user(self.accountant)._migration_check_admin()
        with self.assertRaises(UserError):
            self.connection.with_user(self.accountant)._migration_switch_to_live()

    # -- step 3: shadow --------------------------------------------------

    def test_ac17_shadow_classifies_every_package_without_bills_or_marks(self):
        self._shadow_fixture()
        moves_before = self.env["account.move"].search_count([])
        records = self._run_and_classify()
        outcomes = {r.apix_storage_id: r.shadow_outcome for r in records}
        self.assertEqual(
            outcomes,
            {
                "P-ATT": "imported_before",
                "P-DUP": "would_duplicate",
                "P-DUPR": "imported_before",
                "P-LOST": "would_be_possibly_lost",
                "P-NEW": "would_import",
                "P-BAD": "would_fail",
            },
        )
        self.assertEqual(self.env["account.move"].search_count([]), moves_before, "no bill")
        self.assertFalse(records.filtered("move_id"))
        self.assertEqual(set(records.mapped("state")), {"waiting"})
        self.assertFalse(self._jobs("apix-import:", records), "nothing queued for import")
        self.assertFalse(self._jobs("apix-mark:", records), "nothing marked")
        self.assertTrue(self.download_calls)
        self.assertFalse(records.mapped("finvoice_attachment_id"), "shadow keeps nothing")

        # AC-17: the matched vendor and why a package would fail.
        vendors = {r.apix_storage_id: (r.shadow_vendor_id, r.shadow_vendor_match) for r in records}
        for storage_id in ("P-ATT", "P-DUP", "P-DUPR", "P-LOST", "P-NEW"):
            self.assertEqual(vendors[storage_id], (self.vendor, "matched"), storage_id)
        bad = records.filtered(lambda r: r.apix_storage_id == "P-BAD")
        self.assertFalse(bad.shadow_vendor_id)
        self.assertIn("neither a ZIP file nor a Finvoice message", bad.shadow_reason)
        self.assertFalse((records - bad).filtered("shadow_reason"), "a reason for failures only")
        shadow_list = self.env["goodahead.invoice.import"].with_context(apix_shadow_results=1)
        arch = shadow_list.get_view(view_type="list")["arch"]
        for name in ("shadow_vendor_id", "shadow_vendor_match", "shadow_reason"):
            self.assertIn(f'name="{name}"', arch)

        counts = self.connection._migration_shadow_counts()
        self.assertEqual(counts["imported_before"], 2)
        self.assertEqual(counts["unaccounted"], 0)
        wizard = self._wizard()
        self.assertEqual(wizard.step, "shadow")
        self.assertEqual(wizard.count_would_fail, 1)
        self.assertFalse(wizard.shadow_ready, "a shadow run of less than a week")
        action = wizard.action_open_shadow_results()
        self.assertEqual(action["context"]["search_default_shadow_results"], 1)

        # The next run classifies nothing again, unless APIX's status changed:
        # the earlier connector received P-NEW meanwhile.
        self.download_calls.clear()
        self.listing = [
            g if g["StorageID"] != "P-NEW" else group("P-NEW", "RECEIVED") for g in self.listing
        ]
        self.assertEqual(self.Connection._apix_run(self.company.id), "success")
        new = self._record("P-NEW")
        self.assertFalse(new.shadow_outcome)
        self.assertEqual(
            len(self._jobs("apix-shadow:", new).filtered(lambda j: j.state == "pending")), 1
        )

    def test_ac17_ambiguous_vendor_falls_back_to_the_seller_identifiers(self):
        if not self.journal:
            self.skipTest("no purchase journal on this database")
        # The earlier connector booked the bill on a second vendor record with
        # the same VAT number: the vendor is ambiguous, the identifiers find it.
        twin = self.vendor.copy({"name": "Example Supplier Oy (old)", "vat": "FI00000035"})
        self._bill("M-TWIN", partner=twin)
        self._package("P-TWIN", make_zip(("invoice.xml", finvoice("M-TWIN"))), "RECEIVED")
        records = self._run_and_classify()
        self.assertEqual(records.shadow_outcome, "imported_before")
        self.assertEqual(records.shadow_vendor_match, "ambiguous", "two vendors share the VAT")
        self.assertFalse(records.shadow_vendor_id)

    def test_package_job_never_imports_in_shadow(self):
        self._package("P-SHADOW", make_zip(("invoice.xml", finvoice("M-SH"))))
        self.Connection._apix_run(self.company.id)
        record = self._record("P-SHADOW")
        self.assertEqual(record._apix_import_package(), "SKIPPED: receiving shadow")
        self.assertFalse(record.move_id)
        self.assertNotIn("P-SHADOW", [call[0] for call in self.download_calls])

    # -- step 4: switch to live ---------------------------

    def test_ac18_ac11d_switch_to_live(self):
        self._shadow_fixture()
        self._run_and_classify()
        pause = self.sources[FAKE]["pause"]
        wizard = self._wizard()
        wizard.action_switch_to_live()
        pause.assert_called_once()
        self.assertEqual(pause.call_args.args[-1], self.company)
        self.assertFalse(self.sources[FAKE]["resume"].called)
        self.assertEqual(self.connection.receiving_state, "live")
        self.assertEqual(wizard.step, "live")
        records = self.Import.search(
            [("company_id", "=", self.company.id), ("source", "=", "apix")]
        )
        self.assertFalse(any(records.mapped("shadow_outcome")), "shadow results cleared")
        self.assertFalse(any(records.mapped("shadow_vendor_id")), "shadow vendors cleared")
        self.assertFalse(any(records.mapped("shadow_vendor_match")))
        self.assertFalse(any(records.mapped("shadow_reason")), "shadow reasons cleared")
        self.assertTrue(
            self.env["queue.job"]
            .sudo()
            .search([("identity_key", "=", f"apix-run:{self.company.id}")]),
            "a live run re-evaluates the listing",
        )
        # Nothing is imported or recognised on the shadow run's statuses.
        self.assertFalse(self._jobs("apix-import:", records), "no import before a fresh run")
        self.assertFalse(self._jobs("apix-recognise:", records))
        self.assertEqual(set(records.mapped("state")), {"waiting"})

        # The earlier connector received P-NEW before it was paused: the
        # fresh run sees it, so it is recognised, never imported a second time.
        self.listing = [
            g if g["StorageID"] != "P-NEW" else group("P-NEW", "RECEIVED") for g in self.listing
        ]
        self.assertEqual(self.Connection._apix_run(self.company.id), "success")
        records.invalidate_recordset()
        received = records.filtered(lambda r: r.apix_status == "RECEIVED")
        self.assertIn(self._record("P-NEW"), received)
        waiting = records - received
        self.assertEqual(set(received.mapped("state")), {"possibly_lost"})
        self.assertEqual(set(waiting.mapped("state")), {"waiting"})
        self.assertEqual(
            len(self._jobs("apix-import:", waiting)), len(waiting), "imported normally"
        )
        self.assertFalse(self._jobs("apix-import:", received), "never imported automatically")

        for job in self._jobs("apix-recognise:", received):
            self._attempt(job)
        recognised, lost = self._record("P-DUPR"), self._record("P-LOST")
        self.assertEqual(recognised.state, "imported")
        self.assertEqual(recognised.move_id.ref, "M-DUPR")
        self.assertTrue(recognised.apix_mark_confirmed)
        self.assertFalse(self._jobs("apix-mark:", recognised), "APIX already shows it received")
        self.assertEqual(lost.state, "possibly_lost")
        self.assertFalse(lost.move_id)
        self.assertEqual(self._record("P-NEW").state, "possibly_lost", "no bill of it here")

        # An Accountant imports the possibly lost one: the package job.
        lost.with_user(self.accountant).action_import_possibly_lost()
        self.assertEqual(lost.state, "waiting")
        self.assertEqual(lost.queue_job_id.identity_key, f"apix-import:{lost.id}")

    def test_switch_to_live_refused_without_working_connection_or_with_source_mismatch(self):
        pause = self.sources[FAKE]["pause"]
        wizard = self._wizard()
        self.connection.write({"status": "unchecked"})
        with self.assertRaises(UserError):
            wizard.action_switch_to_live()
        self.connection.write({"status": "working"})
        self.sources[FAKE]["status"].return_value = ("mismatch", False)
        with self.assertRaisesRegex(UserError, "not the version this migration tool supports"):
            wizard.action_switch_to_live()
        self.assertFalse(pause.called)
        self.assertEqual(self.connection.receiving_state, "shadow")

    # -- switch back ---------------------------------------------

    def test_ac19b_switch_back_refused_with_unconfirmed_marks_then_allowed(self):
        self.connection._apix_set_receiving_state("live")
        resume = self.sources[FAKE]["resume"]
        closed = self.Import.sudo().create(
            {
                "name": "Closed ZZ",
                "company_id": self.company.id,
                "source": "apix",
                "state": "closed",
                "close_reason": "ZZ handled by hand",
                "apix_storage_id": "P-CLOSED",
            }
        )
        waiting = self.Import.sudo().create(
            {
                "name": "Waiting ZZ",
                "company_id": self.company.id,
                "source": "apix",
                "state": "waiting",
                "apix_storage_id": "P-WAIT",
                "apix_status": "UNRECEIVED",
            }
        )
        waiting._apix_enqueue_package_import()
        wizard = self._wizard()
        self.assertEqual(wizard.step, "live")
        self.assertEqual(wizard.unconfirmed_count, 1)
        with self.assertRaisesRegex(UserError, "1 records not yet confirmed"):
            wizard.action_switch_back()
        self.assertEqual(self.connection.receiving_state, "live")

        closed.apix_mark_confirmed = True
        wizard.action_switch_back()
        self.assertEqual(wizard.step, "switch_back")
        self.assertIn(waiting, wizard.unsettled_ids)
        self.assertNotIn(closed, wizard.unsettled_ids)
        self.assertFalse(resume.called, "nothing changes before the confirmation")

        wizard.action_confirm_switch_back()
        self.assertEqual(self.connection.receiving_state, "off")
        resume.assert_called_once()
        self.assertEqual(resume.call_args.args[-1], self.company)
        self.assertEqual(self._jobs("apix-import:", waiting).state, "cancelled")
        self.assertEqual(waiting._apix_import_package(), "SKIPPED: receiving off")

    # -- the receiving radio cannot bypass the tool ------------------------------

    def _apix_record(self, storage_id, **vals):
        return self.Import.sudo().create(
            dict(
                {
                    "name": f"Example {storage_id}",
                    "company_id": self.company.id,
                    "source": "apix",
                    "state": "waiting",
                    "apix_storage_id": storage_id,
                    "apix_status": "UNRECEIVED",
                },
                **vals,
            )
        )

    def test_leaving_live_by_hand_is_gated_like_the_switch_back(self):
        self.connection._apix_set_receiving_state("live")
        connection = self.connection.with_user(self.admin)

        def leave(state):
            # The form's radio when it is editable; with a migration source
            # installed it is read-only and the tool's path is the same gate.
            if connection.receiving_via_migration:
                return connection._apix_set_receiving_state(state)
            return connection.write({"receiving_state": state})

        closed = self._apix_record("P-GATE", state="closed", close_reason="Handled by hand")
        for state in ("off", "shadow"):
            with self.subTest(state=state), self.assertRaisesRegex(UserError, "not yet confirmed"):
                leave(state)
        closed.apix_mark_confirmed = True

        # A package job of the company is running: refused too.
        waiting = self._apix_record("P-RUNNING")
        waiting._apix_enqueue_package_import()
        self._jobs("apix-import:", waiting).write({"state": "started"})
        with self.assertRaisesRegex(UserError, "being imported"):
            leave("off")
        with self.assertRaisesRegex(UserError, "being imported"):
            self.connection._migration_check_switch_back()
        self._jobs("apix-import:", waiting).write({"state": "done"})

        leave("off")
        self.assertEqual(self.connection.receiving_state, "off")

    def test_with_a_migration_source_only_the_tool_goes_live_or_back(self):
        self.assertTrue(self.connection.receiving_via_migration)
        connection = self.connection.with_user(self.admin)
        with self.assertRaisesRegex(UserError, "migration tool"):
            connection.write({"receiving_state": "live"})
        self.assertEqual(self.connection.receiving_state, "shadow")
        resume = self.sources[FAKE]["resume"]
        connection._migration_switch_to_live()
        self.assertEqual(self.connection.receiving_state, "live")
        with self.assertRaisesRegex(UserError, "migration tool"):
            connection.write({"receiving_state": "off"})
        connection._migration_switch_back()
        self.assertEqual(self.connection.receiving_state, "off")
        resume.assert_called_once()
        # Off and shadow stay on the form.
        connection.write({"receiving_state": "shadow"})
        self.assertEqual(self.connection.receiving_state, "shadow")

        arch = self.env["goodahead.apix.connection"].get_view(view_type="form")["arch"]
        self.assertIn('readonly="receiving_via_migration"', arch)
        self.assertIn('name="migrated_from_label"', arch)

    def test_switch_back_refused_while_a_package_job_holds_the_connection(self):
        self.connection._apix_set_receiving_state("live")
        cursor_class = type(self.env.cr)
        execute = cursor_class.execute

        def locked_execute(cr, query, *args, **kwargs):
            text = str(query)
            if "FROM goodahead_apix_connection WHERE" in text and "FOR UPDATE NOWAIT" in text:
                raise LockNotAvailable("could not obtain lock on row")
            return execute(cr, query, *args, **kwargs)

        resume = self.sources[FAKE]["resume"]
        with patch.object(cursor_class, "execute", locked_execute), self.assertRaisesRegex(
            UserError, "being imported"
        ):
            self.connection._migration_switch_back()
        self.assertEqual(self.connection.receiving_state, "live")
        self.assertFalse(resume.called)

    # -- company isolation (security) --------------------------------------------

    def test_the_tool_acts_on_the_users_companies_only(self):
        other = self.env["res.company"].create({"name": "Migration Other Oy"})
        other_connection = self.Connection.create(
            {
                "company_id": other.id,
                "environment": "test",
                "transfer_id": TRANSFER_B,
                "transfer_key": f"secret-{TRANSFER_B}",
                "business_id": "0000007-8",
            }
        )
        other_connection.write({"status": "working"})
        other_connection._apix_set_receiving_state("shadow")
        detect = self.sources[FAKE]["detect"]
        self.assertNotIn(other, self.admin.company_ids)

        Connection = self.Connection.with_user(self.admin)
        with self.assertRaises(AccessError):
            Connection._migration_carry_over(other)
        self.assertFalse(detect.called, "refused before anything else ran")
        foreign = other_connection.with_user(self.admin)
        for method in (
            "_migration_switch_to_live",
            "_migration_check_switch_back",
            "_migration_switch_back",
            "_migration_shadow_counts",
            "_migration_unsettled",
            "_migration_unconfirmed",
            "_migration_cancel_package_jobs",
        ):
            with self.subTest(method=method), self.assertRaises(AccessError):
                getattr(foreign, method)()
        self.assertEqual(other_connection.receiving_state, "shadow")
        self.assertFalse(self.sources[FAKE]["pause"].called)

        # The other company's wizard is out of reach.
        wizard = self.Wizard.sudo().create({"company_id": other.id})
        with self.assertRaises(AccessError):
            wizard.with_user(self.admin).read(["step"])
