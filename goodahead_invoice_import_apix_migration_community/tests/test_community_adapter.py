# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The community migration source: AC-18 and AC-19,
community side.

Runs only where the community connector is installed (this add-on depends on
it). No network: the community's `ListInvoiceZIPs` and `Download` are patched
on the backend model, and `ApixClient` for this add-on's own calls. Invented
data only (Business IDs 0000NNN-C, @example.test).
"""

from unittest.mock import patch

from lxml import etree

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_apix.lib.apix_client import ApixClient

from ..models.apix_connection import community_mismatches

TRANSFER_A = "aaaaaaaa-0000-4000-8000-000000000016"
TRANSFER_B = "bbbbbbbb-0000-4000-8000-000000000016"
WEB_PASSWORD = "ZZ-web-password-never-copied"


def community_listing(*storage_ids):
    """A list2 answer as the community connector parses it."""
    root = etree.Element("Response")
    content = etree.SubElement(root, "Content")
    for storage_id in storage_ids:
        group = etree.SubElement(content, "Group")
        for kind, value in (
            ("StorageID", storage_id),
            ("StorageKey", f"key-{storage_id}"),
            ("StorageStatus", "UNRECEIVED"),
            ("DocumentID", f"INV-{storage_id}"),
            ("SenderName", "Example Supplier Oy"),
        ):
            etree.SubElement(group, "Value", type=kind).text = value
    return root


@tagged("post_install", "-at_install")
class TestCommunityBridge(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.Connection = cls.env["goodahead.apix.connection"]
        cls.Backend = cls.env["apix.backend"]
        cls.company_a = cls.env["res.company"].create(
            {"name": "Bridge Test A Oy", "company_registry": "0000001-9"}
        )
        cls.company_b = cls.env["res.company"].create(
            {"name": "Bridge Test B Oy", "company_registry": "0000002-7"}
        )
        cls.backend_a = cls._backend(cls.company_a, TRANSFER_A)
        cls.backend_b = cls._backend(cls.company_b, TRANSFER_B)
        cls.conn_a = cls._connection(cls.company_a, TRANSFER_A, "0000001-9")
        cls.conn_b = cls._connection(cls.company_b, TRANSFER_B, "0000002-7")

    @classmethod
    def _backend(cls, company, transfer_id):
        return cls.Backend.create(
            {
                "name": company.name,
                "company_id": company.id,
                "username": f"{company.id}@example.test",
                "password": WEB_PASSWORD,
                "environment": "test",
                "transfer_id": transfer_id,
                "transfer_key": f"secret-{transfer_id}",
                "state": "confirmed",
            }
        )

    @classmethod
    def _connection(cls, company, transfer_id, business_id):
        connection = cls.Connection.create(
            {
                "company_id": company.id,
                "environment": "test",
                "transfer_id": transfer_id,
                "transfer_key": f"secret-{transfer_id}",
                "business_id": business_id,
            }
        )
        connection.write({"status": "working"})
        connection._apix_set_receiving_state("shadow")
        return connection

    def setUp(self):
        super().setUp()
        backend_class = type(self.Backend)
        self.listing = patch.object(
            backend_class, "ListInvoiceZIPs", autospec=True, return_value=community_listing("C-1")
        ).start()
        self.download = patch.object(backend_class, "Download", autospec=True).start()
        for name in ("list_packages", "download"):
            patch.object(
                ApixClient, name, autospec=True, side_effect=AssertionError("no APIX call")
            ).start()
        self.addCleanup(patch.stopall)

    def _community_jobs(self, backend=None, method=None, states=("pending",)):
        jobs = (
            self.env["queue.job"]
            .sudo()
            .search([("model_name", "=", "apix.backend"), ("state", "in", list(states))])
        )
        if method:
            jobs = jobs.filtered(lambda j: j.method_name == method)
        if backend:
            jobs = jobs.filtered(lambda j: j.records == backend)
        return jobs

    # -- install and credentials ------------------------------------------------

    def test_adapter_installed_registered_and_matching(self):
        modules = self.env["ir.module.module"].search(
            [
                (
                    "name",
                    "in",
                    [
                        "goodahead_invoice_import_apix_migration",
                        "goodahead_invoice_import_apix_migration_community",
                    ],
                )
            ]
        )
        self.assertEqual(set(modules.mapped("state")), {"installed"}, "auto-installed, tool too")
        self.assertIn("community", self.Connection._migration_sources())
        self.assertEqual(
            self.Connection._migration_source_label("community"), "Community APIX modules"
        )
        self.assertEqual(self.Connection._migration_source_status("community"), ("ok", False))
        self.assertFalse(self.Connection._migration_problem())
        self.assertIsNone(
            self.Connection._migration_source_method("community", "find_bill"),
            "the community modules keep no package-to-bill record",
        )

    def test_with_the_community_modules_only_the_tool_goes_live(self):
        """The receiving add-on's guard: a company the community modules may
        still receive for goes live through the migration tool only."""
        self.assertTrue(self.conn_a.receiving_via_migration)
        with self.assertRaisesRegex(UserError, "migration tool"):
            self.conn_a.write({"receiving_state": "live"})
        self.assertEqual(self.conn_a.receiving_state, "shadow")

    def test_adapter_mismatch_when_the_community_methods_change(self):
        class Renamed:
            def list_invoices(self, refresh=False):
                pass

        problems = community_mismatches(Renamed)
        self.assertTrue(any(p.startswith("list_invoices") for p in problems))
        self.assertTrue(any(p.startswith("download_invoice") for p in problems))
        self.assertFalse(community_mismatches(self.Connection._community_backend_class()))

    def test_credentials_detected_without_the_password(self):
        detected = self.Connection._migration_source_detect("community", self.company_a)
        self.assertEqual(
            detected,
            {
                "environment": "test",
                "transfer_id": TRANSFER_A,
                "transfer_key": f"secret-{TRANSFER_A}",
                "business_id": "0000001-9",
            },
        )
        self.assertNotIn(WEB_PASSWORD, repr(detected))
        read = type(self.Backend).read
        with patch.object(type(self.Backend), "read", autospec=True, side_effect=read) as spy:
            self.Connection._migration_source_detect("community", self.company_a)
        for call in spy.call_args_list:
            fields = call.args[1] if len(call.args) > 1 else call.kwargs.get("fields")
            self.assertNotIn("password", fields or [])
            self.assertNotIn("username", fields or [])

    def test_carry_over_through_the_tool_records_the_source(self):
        company = self.env["res.company"].create(
            {"name": "Bridge Test C Oy", "company_registry": "0000004-3"}
        )
        transfer_c = "cccccccc-0000-4000-8000-000000000016"
        self._backend(company, transfer_c)
        info = {"groups": [{"y-tunnus": "0000004-3", "Name": "Bridge Test C Oy"}]}
        with patch.object(ApixClient, "company_info", autospec=True, return_value=info):
            connection = self.Connection._migration_carry_over(company)
        self.assertEqual(connection.migrated_from, "community")
        self.assertEqual(connection.migrated_from_label, "Community APIX modules")
        self.assertEqual(connection.transfer_id, transfer_c)
        self.assertEqual(connection.receiving_state, "shadow")
        self.assertNotIn(WEB_PASSWORD, repr(connection.read()))

    # -- pause: listing skipped, re-fetch refused, queued jobs cancelled ---

    def test_live_company_skipped_in_the_community_listing(self):
        self.conn_a._apix_set_receiving_state("live")
        self.assertEqual(
            self.backend_a.list_invoices(), "SKIPPED: received by the APIX receiving add-on"
        )
        self.assertEqual(self.listing.call_count, 0)
        self.backend_b.list_invoices()
        self.assertEqual(self.listing.call_count, 1)
        self.assertEqual(len(self._community_jobs(self.backend_b, "download_invoice")), 1)
        self.assertFalse(self._community_jobs(self.backend_a))

        # The recurring community fetch: only the other company is listed.
        self.Backend.action_cron_einvoice_fetch()
        listed = self._community_jobs(method="list_invoices")
        self.assertIn(self.backend_b, listed.mapped("records"))
        self.assertNotIn(self.backend_a, listed.mapped("records"))
        self.assertTrue(self.backend_a.goodahead_receiving_live)
        self.assertFalse(self.backend_b.goodahead_receiving_live)

    def test_refetch_refused_for_a_live_company(self):
        self.conn_a._apix_set_receiving_state("live")
        with self.assertRaisesRegex(UserError, "Re-fetching is disabled"):
            self.backend_a.action_einvoice_refetch()
        self.backend_b.action_einvoice_refetch()
        self.assertEqual(len(self._community_jobs(self.backend_b, "list_invoices")), 1)
        # A download that slipped through does not import for the live company.
        self.assertEqual(
            self.backend_a.download_invoice("C-9", "key-C-9"),
            "SKIPPED: received by the APIX receiving add-on",
        )
        self.download.assert_not_called()

    def test_pause_cancels_queued_community_downloads_of_that_company_only(self):
        self.backend_a.with_delay().download_invoice("C-A1", "key-C-A1")
        self.backend_a.with_delay().list_invoices(refetch=False)
        self.backend_b.with_delay().download_invoice("C-B1", "key-C-B1")
        self.Connection._migration_community_pause(self.company_a)
        self.assertFalse(self._community_jobs(self.backend_a))
        self.assertEqual(len(self._community_jobs(self.backend_a, states=("cancelled",))), 2)
        self.assertEqual(len(self._community_jobs(self.backend_b, "download_invoice")), 1)

    # -- through the migration tool -------------------------------

    def test_switch_to_live_and_back(self):
        self.backend_a.with_delay().download_invoice("C-A2", "key-C-A2")
        self.conn_a._migration_switch_to_live()
        self.assertEqual(self.conn_a.receiving_state, "live")
        self.assertFalse(
            self._community_jobs(self.backend_a), "queued community download cancelled"
        )
        self.backend_a.list_invoices()
        self.assertEqual(self.listing.call_count, 0, "community listing paused")
        with self.assertRaises(UserError):
            self.backend_a.action_einvoice_refetch()

        self.conn_a._migration_switch_back()
        self.assertEqual(self.conn_a.receiving_state, "off")
        resumed = self._community_jobs(self.backend_a, "list_invoices")
        self.assertEqual(len(resumed), 1, "the community modules fetch again")
        self.assertEqual(self.backend_a.transfer_id, TRANSFER_A, "with the same credentials")
        self.backend_a.list_invoices()
        self.assertEqual(self.listing.call_count, 1)
        self.assertEqual(len(self._community_jobs(self.backend_a, "download_invoice")), 1)
