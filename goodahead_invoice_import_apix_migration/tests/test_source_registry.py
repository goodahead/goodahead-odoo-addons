# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The migration-source registry and its dispatch, with test-only sources: what a source adapter
can rely on, and what the tool refuses from one."""

from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.goodahead_apix.lib.apix_client import ApixClient

from .common import FAKE, TRANSFER_B, MigrationCase, finvoice, make_zip

OTHER = "other"
REGISTRY_LOGGER = "odoo.addons.goodahead_invoice_import_apix_migration.models.apix_connection"

CREDENTIALS = {
    "environment": "production",
    "transfer_id": TRANSFER_B,
    "transfer_key": f"secret-{TRANSFER_B}",
    "business_id": "0000007-8",
}
COMPANY_INFO = {"groups": [{"y-tunnus": "0000007-8", "Name": "Registry Oy"}]}


@tagged("post_install", "-at_install")
class TestMigrationSourceRegistry(MigrationCase):
    def _company(self, name="Registry Oy", registry=False):
        company = self.env["res.company"].create({"name": name, "company_registry": registry})
        self.admin.company_ids = [(4, company.id)]
        return company

    # -- registry and dispatch ----------------------------------------------------

    def test_no_source_installed(self):
        self._fake_sources()
        self.assertEqual(self.Connection._migration_sources(), [])
        self.connection.invalidate_recordset(["receiving_via_migration"])
        self.assertFalse(self.connection.receiving_via_migration, "the radio is editable")
        self.assertFalse(self.Connection._migration_problem())
        self.assertFalse(self.Connection._migration_find_bill(self.company, "P-1"))
        self.assertFalse(self._wizard().source_code)
        # Switching needs nothing to pause.
        self.connection._migration_switch_to_live()
        self.assertEqual(self.connection.receiving_state, "live")

    def test_a_migrated_connection_goes_through_the_tool_even_without_sources(self):
        self._fake_sources()
        self.connection.sudo().migrated_from = FAKE
        self.connection.invalidate_recordset(["receiving_via_migration", "migrated_from_label"])
        self.assertTrue(self.connection.receiving_via_migration)
        self.assertEqual(self.connection.migrated_from_label, FAKE, "the code, unlabelled")
        with self.assertRaisesRegex(UserError, "migration tool"):
            self.connection.with_user(self.admin).write({"receiving_state": "live"})

    def test_dispatch_by_code(self):
        Connection = self.Connection
        self.assertEqual(Connection._migration_sources(), [FAKE])
        self.assertIs(
            Connection._migration_source_method(FAKE, "pause"), self.sources[FAKE]["pause"]
        )
        self.assertIsNone(
            Connection._migration_source_method(FAKE, "find_bill"), "find_bill is optional"
        )
        with self.assertRaisesRegex(UserError, "not installed"):
            Connection._migration_source_method("missing", "detect")
        # A source that registers its code without its methods is refused.
        self._patch_connection("_migration_sources", return_value=[FAKE, "broken"])
        for method in ("detect", "status", "pause", "resume"):
            with self.subTest(method=method), self.assertRaisesRegex(UserError, "incomplete"):
                Connection._migration_source_method("broken", method)

    def test_detect_is_normalised_and_never_carries_more_than_the_credentials(self):
        company = self._company(registry="0000008-6")
        detect = self.sources[FAKE]["detect"]
        detect.return_value = {"environment": "staging", "transfer_id": TRANSFER_B}
        self.assertEqual(
            self.Connection._migration_source_detect(FAKE, company),
            {
                "environment": "test",
                "transfer_id": TRANSFER_B,
                "transfer_key": False,
                "business_id": "0000008-6",
            },
        )
        # 🔴 A source that hands over anything else (a password) is refused.
        detect.return_value = dict(CREDENTIALS, password="ZZ-never")
        with mute_logger(REGISTRY_LOGGER), self.assertRaisesRegex(
            UserError, "more than the transfer credentials"
        ):
            self.Connection._migration_carry_over(company, FAKE)
        self.assertFalse(self.Connection.search([("company_id", "=", company.id)]))
        with mute_logger(REGISTRY_LOGGER):
            found, problems = self.Connection._migration_detect_all(company)
        self.assertEqual(found, {})
        self.assertIn(FAKE, problems)

    def test_several_sources_the_admin_picks_one(self):
        company = self._company()
        sources = self._fake_sources(
            **{
                FAKE: {
                    "detect": dict(CREDENTIALS, transfer_id="ffffffff-0000-4000-8000-0000000000aa")
                },
                OTHER: {"detect": CREDENTIALS},
            }
        )
        with self.assertRaisesRegex(UserError, "choose the one"):
            self.Connection._migration_carry_over(company)
        wizard = self._wizard(company=company)
        self.assertEqual(wizard.source_count, 2)
        wizard.source_code = OTHER
        self.assertEqual(wizard.source_business_id, "0000007-8")
        with patch.object(ApixClient, "company_info", autospec=True, return_value=COMPANY_INFO):
            wizard.action_carry_over()
        connection = self.Connection.search([("company_id", "=", company.id)])
        self.assertEqual(connection.migrated_from, OTHER)
        self.assertEqual(connection.transfer_id, TRANSFER_B)
        self.assertEqual(connection.receiving_state, "shadow")
        # A second wizard starts from the source the company was migrated from.
        self.assertEqual(self._wizard(company=company).source_code, OTHER)
        self.assertTrue(sources[FAKE]["detect"].called)

    def test_every_source_is_paused_and_resumed_and_a_mismatch_blocks_the_switch(self):
        sources = self._fake_sources(**{FAKE: {}, OTHER: {}})
        sources[OTHER]["status"].return_value = ("mismatch", "The other connector changed.")
        with self.assertRaisesRegex(UserError, "The other connector changed."):
            self.connection._migration_switch_to_live()
        self.assertFalse(sources[FAKE]["pause"].called, "nothing paused before every check")
        self.assertEqual(self._wizard().source_message, "The other connector changed.")

        sources[OTHER]["status"].return_value = ("ok", False)
        self.connection._migration_switch_to_live()
        for code in (FAKE, OTHER):
            sources[code]["pause"].assert_called_once_with(self.company)
        self.connection._migration_switch_back()
        for code in (FAKE, OTHER):
            sources[code]["resume"].assert_called_once_with(self.company)

    # -- find_bill: the exact link comes first ------------------------------------

    def test_find_bill_is_preferred_over_the_checksum_and_needs_no_download(self):
        if not self.journal:
            self.skipTest("no purchase journal on this database")
        # The checksum would find this bill ...
        by_checksum = self._bill("BY-CHECKSUM", attachment=finvoice("M-EXACT"))
        # ... but the earlier connector recorded the package on this one.
        recorded = self._bill("RECORDED")
        asked = []

        def find_bill(company, storage_id):
            asked.append((company, storage_id))
            return recorded if storage_id == "P-EXACT" else self.env["account.move"]

        sources = self._fake_sources(**{FAKE: {"find_bill": find_bill}})
        self._package("P-EXACT", make_zip(("invoice.xml", finvoice("M-EXACT"))), "RECEIVED")
        record = self._run_and_classify()
        self.assertEqual(record.shadow_outcome, "imported_before")
        self.assertEqual(record.shadow_vendor_id, self.vendor)
        self.assertIn((self.company, "P-EXACT"), asked)
        self.assertNotIn("P-EXACT", [call[0] for call in self.download_calls], "no download")

        # Live: the recognition links the recorded bill, not the checksum one.
        self.assertTrue(sources[FAKE]["find_bill"].called)
        self.connection._migration_switch_to_live()
        self.assertEqual(self.Connection._apix_run(self.company.id), "success")
        record.invalidate_recordset()
        self.assertEqual(record.state, "possibly_lost")
        for job in self._jobs("apix-recognise:", record):
            self._attempt(job)
        record.invalidate_recordset()
        self.assertEqual(record.state, "imported")
        self.assertEqual(record.move_id, recorded)
        self.assertNotEqual(record.move_id, by_checksum)
        self.assertTrue(record.apix_mark_confirmed)
        self.assertNotIn("P-EXACT", [call[0] for call in self.download_calls], "no download")
        self.assertIn("earlier APIX connector recorded", record.message_ids[:1].body)

    def test_find_bill_answers_that_are_no_live_bill_are_ignored(self):
        if not self.journal:
            self.skipTest("no purchase journal on this database")
        entry = self.env["account.move"].create({"move_type": "entry", "ref": "NOT-A-BILL"})
        cancelled = self._bill("CANCELLED")
        cancelled.button_cancel()
        answers = iter([entry, cancelled])
        self._fake_sources(**{FAKE: {"find_bill": lambda company, storage_id: next(answers)}})
        self._bill("M-FALLBACK")
        self._package("P-FALLBACK", make_zip(("invoice.xml", finvoice("M-FALLBACK"))), "RECEIVED")
        record = self._run_and_classify()
        # Neither answer counts: the duplicate rule finds the bill instead.
        self.assertEqual(record.shadow_outcome, "imported_before")
        self.assertIn("P-FALLBACK", [call[0] for call in self.download_calls])
        self.assertEqual(record._apix_earlier_bill(), self.env["account.move"])

    def test_find_bill_asks_the_source_the_company_was_migrated_from(self):
        seen = []

        def asked(code):
            def find_bill(company, storage_id):
                seen.append(code)
                return self.env["account.move"]

            return find_bill

        self._fake_sources(**{FAKE: {"find_bill": asked(FAKE)}, OTHER: {"find_bill": asked(OTHER)}})
        self.Connection._migration_find_bill(self.company, "P-1")
        self.assertEqual(seen, [FAKE, OTHER], "unknown origin: every source, in order")
        seen.clear()
        self.connection.sudo().migrated_from = OTHER
        self.Connection._migration_find_bill(self.company, "P-1")
        self.assertEqual(seen, [OTHER])
