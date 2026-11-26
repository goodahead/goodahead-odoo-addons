# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Shared set-up of the migration tool's tests.

Runs without any real earlier connector: `MigrationSourceMixin` registers
test-only migration sources on the connection model by patching
``_migration_sources`` and adding ``_migration_<code>_<method>`` mocks, so
the tests behave the same whether or not an adapter (e.g. the community one)
is installed. No network: `ApixClient` calls are patched. Invented data only
(Business IDs 00000NN-C, @example.test).
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, new_test_user

from odoo.addons.goodahead_apix.lib.apix_client import ApixClient
from odoo.addons.goodahead_invoice_import_apix.tests.apix_e2e_common import (
    make_zip,
    simple_finvoice,
)
from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.job import Job

TRANSFER = "dddddddd-0000-4000-8000-000000000015"
TRANSFER_B = "eeeeeeee-0000-4000-8000-000000000015"

#: The test-only migration source most tests use.
FAKE = "fake"

__all__ = ["FAKE", "TRANSFER", "TRANSFER_B", "MigrationCase", "group", "make_zip"]


def group(storage_id, status="UNRECEIVED"):
    return {
        "StorageID": storage_id,
        "StorageKey": f"key-{storage_id}",
        "StorageStatus": status,
        "DocumentID": f"INV-{storage_id}",
        "SenderName": "Example Supplier Oy",
        "DocumentName": "invoice.xml",
        "DocumentDate": "20261115",
    }


def finvoice(number):
    """The simple 3.0 fixture (seller VAT FI00000035, Business ID 0000003-5)
    under another invoice number."""
    return simple_finvoice(number)


class MigrationSourceMixin:
    """Test-only migration sources."""

    def _patch_connection(self, name, **kwargs):
        patcher = patch.object(type(self.env["goodahead.apix.connection"]), name, **kwargs)
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def _fake_sources(self, **sources):
        """Register ``sources`` (``code -> spec``) as the installed migration
        sources, replacing whatever is installed. A spec may give ``detect``
        (the dict to return, default None), ``status`` (default
        ``("ok", False)``) and ``find_bill`` (a function ``(company,
        storage_id) -> account.move``; omitted: the source has none).
        Returns ``{code: {method: mock}}``."""
        self._patch_connection("_migration_sources", return_value=list(sources))
        mocks = {}
        for code, spec in sources.items():
            methods = {
                "detect": self._patch_connection(
                    f"_migration_{code}_detect", create=True, return_value=spec.get("detect")
                ),
                "status": self._patch_connection(
                    f"_migration_{code}_status",
                    create=True,
                    return_value=spec.get("status", ("ok", False)),
                ),
                "pause": self._patch_connection(
                    f"_migration_{code}_pause", create=True, return_value=True
                ),
                "resume": self._patch_connection(
                    f"_migration_{code}_resume", create=True, return_value=True
                ),
            }
            if "find_bill" in spec:
                methods["find_bill"] = self._patch_connection(
                    f"_migration_{code}_find_bill", create=True, side_effect=spec["find_bill"]
                )
            mocks[code] = methods
        return mocks


class MigrationCase(MigrationSourceMixin, TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.Import = cls.env["goodahead.invoice.import"]
        cls.Connection = cls.env["goodahead.apix.connection"]
        cls.Wizard = cls.env["goodahead.apix.migration"]
        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", cls.company.id)], limit=1
        )
        cls.admin = new_test_user(
            cls.env,
            login="mig_admin",
            email="mig.admin@example.test",
            groups="base.group_user,account.group_account_manager",
            company_id=cls.company.id,
            company_ids=[(6, 0, cls.company.ids)],
        )
        cls.accountant = new_test_user(
            cls.env,
            login="mig_accountant",
            email="mig.accountant@example.test",
            groups="base.group_user,account.group_account_invoice",
            company_id=cls.company.id,
            company_ids=[(6, 0, cls.company.ids)],
        )
        cls.connection = cls.Connection.search([("company_id", "=", cls.company.id)], limit=1)
        vals = {
            "transfer_id": TRANSFER,
            "transfer_key": f"secret-{TRANSFER}",
            "status": "working",
            "receiving_state": "shadow",
        }
        if cls.connection:
            cls.connection.write(vals)
        else:
            cls.connection = cls.Connection.create(
                dict(vals, company_id=cls.company.id, environment="test", business_id="0000006-0")
            )
        cls.vendor = cls.env["res.partner"].create(
            {
                "name": "Example Supplier Oy",
                "is_company": True,
                "vat": "FI00000035",
                "company_registry": "0000003-5",
                "country_id": cls.env.ref("base.fi").id,
            }
        )

    def setUp(self):
        super().setUp()
        self.packages = {}
        self.listing = []
        self.download_calls = []

        def fake_download(client, storage_id, storage_key, mark_received):
            self.download_calls.append((storage_id, mark_received))
            return self.packages[storage_id]

        def fake_listing(client, transfer_id, transfer_key):
            return list(self.listing)

        for name, side_effect in (("download", fake_download), ("list_packages", fake_listing)):
            patcher = patch.object(ApixClient, name, autospec=True, side_effect=side_effect)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._assert_never_marked)
        # One earlier connector is installed; it has nothing for the
        # company until a test says so.
        self.sources = self._fake_sources(**{FAKE: {}})

    def _assert_never_marked(self):
        marked = [call for call in self.download_calls if call[1] is not False]
        self.assertFalse(marked, "the migration never downloads with mark_received=True")

    # -- helpers -------------------------------------------------------------

    def _bill(self, ref, partner=None, attachment=None, company=None):
        move = self.env["account.move"].create(
            {
                "move_type": "in_invoice",
                "company_id": (company or self.company).id,
                "partner_id": (partner or self.vendor).id,
                "ref": ref,
                "invoice_date": "2026-11-15",
                "invoice_line_ids": [
                    (0, 0, {"name": "Example line", "quantity": 1, "price_unit": 100.0})
                ],
            }
        )
        if attachment:
            self.env["ir.attachment"].create(
                {
                    "name": "invoice.xml",
                    "raw": attachment,
                    "res_model": "account.move",
                    "res_id": move.id,
                }
            )
        return move

    def _package(self, storage_id, body, status="UNRECEIVED"):
        self.packages[storage_id] = body
        self.listing.append(group(storage_id, status))

    def _record(self, storage_id):
        return self.Import.search(
            [("company_id", "=", self.company.id), ("apix_storage_id", "=", storage_id)]
        )

    def _jobs(self, prefix, records):
        keys = [f"{prefix}{record.id}" for record in records]
        return self.env["queue.job"].sudo().search([("identity_key", "in", keys)])

    def _attempt(self, job_record):
        job = Job.load(self.env, job_record.uuid)
        job.set_started()
        job.store()
        try:
            result = job.perform()
        except RetryableJobError as error:
            job.postpone(result=str(error), seconds=error.seconds)
            job.set_pending(reset_retry=False)
            job.store()
            return error
        job.set_done(result=result)
        job.store()
        return result

    def _run_and_classify(self):
        self.assertEqual(self.Connection._apix_run(self.company.id), "success")
        records = self.Import.search(
            [("company_id", "=", self.company.id), ("source", "=", "apix")]
        )
        for job in self._jobs("apix-shadow:", records):
            self._attempt(job)
        records.invalidate_recordset()
        return records

    def _shadow_fixture(self):
        """One package per shadow outcome."""
        if not self.journal:
            self.skipTest("no purchase journal on this database")
        # (1) a bill carrying the package's Finvoice file, booked under another
        # reference: only the attachment checksum recognises it.
        self._bill("EARLIER-ATT", attachment=finvoice("M-ATT"))
        self._package("P-ATT", make_zip(("invoice.xml", finvoice("M-ATT"))))
        # (2) bills with the same vendor and invoice number.
        self._bill("M-DUP")
        self._package("P-DUP", make_zip(("invoice.xml", finvoice("M-DUP"))))
        self._bill("M-DUPR")
        self._package("P-DUPR", make_zip(("invoice.xml", finvoice("M-DUPR"))), "RECEIVED")
        # (3) received at APIX, no bill here; (4) new; (5) not a package.
        self._package("P-LOST", make_zip(("invoice.xml", finvoice("M-LOST"))), "RECEIVED")
        self._package("P-NEW", make_zip(("invoice.xml", finvoice("M-NEW"))))
        self._package("P-BAD", b"this is not an invoice package")

    def _wizard(self, user=None, company=None, **values):
        company = company or self.company
        env = self.env(
            user=(user or self.admin).id,
            context=dict(
                self.env.context, allowed_company_ids=company.ids, default_company_id=company.id
            ),
        )
        return env["goodahead.apix.migration"].create(values)
