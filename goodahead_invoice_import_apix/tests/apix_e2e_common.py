# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Shared pieces of the end-to-end, load and secret-safety suites.

* `FakeApix` — APIX replaced at its boundary (test-plan "Integration
  strategy"): it holds per-account packages built from the Finvoice add-on's
  invented fixtures and implements the three client operations the add-ons
  call (`list_packages`, `download`, `company_info`), keeping APIX's state:
  a download with ``mark_received=True`` turns the package RECEIVED and is
  recorded in `marked`.
* `ApixE2ECase` — one company with a purchase journal, a 25.5 % purchase tax,
  the vendor of the 3.0 fixtures and a working, live APIX connection; a job
  runner that performs the queued jobs explicitly, as the job runner would.

Invented data only: Business IDs 0000NNN-C, @example.test, made-up keys.
"""

import io
import os
import time
import zipfile
from unittest.mock import patch

from odoo import fields
from odoo.tests import TransactionCase

from odoo.addons.goodahead_apix.lib.apix_client import (
    ApixClient,
    ApixEmptyAnswer,
    ApixRejected,
    ApixUnreachable,
)
from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.job import Job

FINVOICE_DATA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "goodahead_invoice_import_finvoice",
    "tests",
    "data",
)

TRANSFER_A = "abababab-0000-4000-8000-000000000017"
TRANSFER_B = "cdcdcdcd-0000-4000-8000-000000000017"

#: Every queued job of the APIX receiving side, by identity-key prefix.
ALL_APIX_JOBS = (
    "apix-run:",
    "apix-import:",
    "apix-mark:",
    "apix-shadow:",
    "apix-recognise:",
    "apix-daily:",
)


def finvoice_fixture(name):
    with open(os.path.join(FINVOICE_DATA, name), "rb") as handle:
        return handle.read()


def simple_finvoice(number="10045", vat="FI00000035", business_id="0000003-5"):
    """The simple 3.0 fixture (360.00 + 91.80 VAT = 451.80 EUR) under another
    invoice number and, optionally, another seller."""
    raw = finvoice_fixture("fi30_simple_invoice.xml")
    raw = raw.replace(
        b"<InvoiceNumber>10045</InvoiceNumber>",
        f"<InvoiceNumber>{number}</InvoiceNumber>".encode(),
    )
    return raw.replace(b"FI00000035", vat.encode()).replace(b"0000003-5", business_id.encode())


def make_zip(*files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files:
            archive.writestr(name, content)
    return buffer.getvalue()


def percentile(values, pct):
    """Nearest-rank percentile (no interpolation): the smallest value with
    at least ``pct`` % of the values at or below it."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = max(1, -(-len(ordered) * pct // 100))
    return ordered[int(rank) - 1]


class FakeApix:
    """APIX at its boundary: accounts, their packages and APIX's state."""

    def __init__(self):
        self.accounts = {}
        self.packages = {}
        self.calls = []
        self.marked = []
        #: storage id -> number of mark-received downloads that fail first
        self.mark_failures = {}

    # -- set-up ----------------------------------------------------------------

    def add_account(self, transfer_id, transfer_key, business_id, name, rejected=False):
        self.accounts[transfer_id] = {
            "key": transfer_key,
            "business_id": business_id,
            "name": name,
            "rejected": rejected,
        }

    def add_package(
        self,
        transfer_id,
        storage_id,
        body,
        number,
        status="UNRECEIVED",
        sender="Example Supplier Oy",
        document_date=None,
        amount="451.80",
        storage_key=None,
    ):
        document_date = document_date or fields.Date.today()
        group = {
            "StorageID": storage_id,
            "StorageKey": storage_key or f"key-{storage_id}",
            "DocumentID": number,
            "SenderName": sender,
            "SenderVAT": "FI00000035",
            "AmountToPay": amount,
            "Currency": "EUR",
            "DocumentName": "invoice.xml",
            "DocumentDate": document_date.strftime("%Y%m%d"),
        }
        self.packages[storage_id] = {
            "transfer_id": transfer_id,
            "group": group,
            "body": body,
            "status": status,
        }
        return group

    def status(self, storage_id):
        return self.packages[storage_id]["status"]

    # -- the client operations ------------------------------------------------

    def _check_account(self, operation, transfer_id, transfer_key):
        account = self.accounts.get(transfer_id)
        if not account or account["rejected"] or account["key"] != transfer_key:
            raise ApixRejected(
                f"{operation}: Authentication failed (StatusCode 4701)", status_code="4701"
            )
        return account

    def list_packages(self, transfer_id, transfer_key):
        self.calls.append(("list_packages", transfer_id))
        self._check_account("ListInvoiceZIPs", transfer_id, transfer_key)
        return [
            dict(package["group"], StorageStatus=package["status"])
            for package in self.packages.values()
            if package["transfer_id"] == transfer_id
        ]

    def download(self, storage_id, storage_key, mark_received):
        self.calls.append(("download", storage_id, mark_received))
        package = self.packages.get(storage_id)
        if not package or package["group"]["StorageKey"] != storage_key:
            raise ApixEmptyAnswer("Download: APIX returned nothing (wrong storage id or key)")
        if mark_received:
            if self.mark_failures.get(storage_id):
                self.mark_failures[storage_id] -= 1
                raise ApixUnreachable("Download: APIX could not be reached (ConnectionError)")
            package["status"] = "RECEIVED"
            self.marked.append(storage_id)
        return package["body"]

    def company_info(self, transfer_id, transfer_key):
        self.calls.append(("company_info", transfer_id))
        account = self._check_account("RetrieveCompanyInformation", transfer_id, transfer_key)
        return {
            "status": "OK",
            "status_code": "5000",
            "free_text": "",
            "free_texts": {},
            "groups": [{"y-tunnus": account["business_id"], "Name": account["name"]}],
        }

    def downloads(self, mark_received=None):
        return [
            call[1]
            for call in self.calls
            if call[0] == "download" and (mark_received is None or call[2] is mark_received)
        ]

    def install(self, testcase):
        """Patch `ApixClient` for the test: every call goes to this fake."""
        for name in ("list_packages", "download", "company_info"):
            method = getattr(self, name)
            patcher = patch.object(
                ApixClient,
                name,
                autospec=True,
                side_effect=lambda client, *args, _method=method, **kwargs: _method(
                    *args, **kwargs
                ),
            )
            patcher.start()
            testcase.addCleanup(patcher.stop)
        return self


class ApixJobRunnerMixin:
    """Perform queued APIX jobs explicitly, one try at a time, as the job
    runner does: a retryable error postpones the job and keeps its count."""

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

    def _pending(self, prefixes=ALL_APIX_JOBS):
        domain = [("state", "=", "pending")]
        domain += ["|"] * (len(prefixes) - 1)
        domain += [("identity_key", "=like", f"{prefix}%") for prefix in prefixes]
        return self.env["queue.job"].sudo().search(domain, order="id")

    def _perform(self, prefixes=ALL_APIX_JOBS, timings=None, max_rounds=40, between=None):
        """Perform pending jobs whose identity key starts with one of
        ``prefixes`` until none is left; jobs enqueued meanwhile are
        performed too. ``timings`` (a list) collects
        ``(identity_key, seconds, result)`` per try. ``between(tries)`` is
        called before each try, outside the timing. Returns the tries."""
        tries = 0
        for _round in range(max_rounds):
            jobs = self._pending(prefixes)
            if not jobs:
                return tries
            for job in jobs:
                if between:
                    between(tries)
                start = time.perf_counter()
                result = self._attempt(job)
                elapsed = time.perf_counter() - start
                tries += 1
                if timings is not None:
                    timings.append((job.identity_key, elapsed, result))
        self.fail(f"jobs still pending after {max_rounds} rounds: {self._pending(prefixes)}")
        return tries

    def _jobs(self, prefix, records):
        keys = [f"{prefix}{record.id}" for record in records]
        return self.env["queue.job"].sudo().search([("identity_key", "in", keys)], order="id")


class ApixE2ECase(ApixJobRunnerMixin, TransactionCase):
    """One live company with the fixtures' vendor and APIX faked."""

    #: The load suite switches chatter tracking off; the others keep it on,
    #: so tracking values are part of what they check.
    disable_tracking = False

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if cls.disable_tracking:
            cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.Import = cls.env["goodahead.invoice.import"]
        cls.Connection = cls.env["goodahead.apix.connection"]
        cls.Move = cls.env["account.move"]
        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", cls.company.id)], limit=1
        )
        if not cls.journal:
            return
        cls.env.ref("base.EUR").active = True
        cls.tax_25 = cls._purchase_tax(cls.company, 25.5)
        cls.transfer_key = f"secret-{TRANSFER_A}"
        cls.connection = cls._live_connection(
            cls.company, TRANSFER_A, cls.transfer_key, "0000006-0"
        )
        # The seller of the 3.0 fixtures: Business ID 0000003-5, VAT FI00000035.
        cls.vendor = cls.env["res.partner"].create(
            {
                "name": "Example Supplier Oy",
                "is_company": True,
                "vat": "FI00000035",
                "company_registry": "0000003-5",
                "country_id": cls.env.ref("base.fi").id,
            }
        )

    @classmethod
    def _purchase_tax(cls, company, amount, name=None):
        Tax = cls.env["account.tax"]
        tax = Tax.search(
            [
                ("type_tax_use", "=", "purchase"),
                ("amount_type", "=", "percent"),
                ("amount", "=", amount),
                ("company_id", "=", company.id),
            ],
            limit=1,
        )
        return tax or Tax.create(
            {
                "name": name or f"Example {amount}% Purchase",
                "type_tax_use": "purchase",
                "amount_type": "percent",
                "amount": amount,
                "company_id": company.id,
            }
        )

    @classmethod
    def _live_connection(cls, company, transfer_id, transfer_key, business_id):
        connection = cls.Connection.search([("company_id", "=", company.id)], limit=1)
        vals = {
            "environment": "test",
            "transfer_id": transfer_id,
            "transfer_key": transfer_key,
            "business_id": business_id,
        }
        if connection:
            connection.write(vals)
        else:
            connection = cls.Connection.create(dict(vals, company_id=company.id))
        connection.write({"status": "working"})
        connection._apix_set_receiving_state("live")
        return connection

    def setUp(self):
        super().setUp()
        if not self.journal:
            self.skipTest("no purchase journal on this database")
        self.fake = FakeApix().install(self)
        self.fake.add_account(TRANSFER_A, self.transfer_key, "0000006-0", self.company.name)

    # -- helpers -----------------------------------------------------------------

    def _record(self, storage_id, company=None):
        return self.Import.sudo().search(
            [
                ("company_id", "=", (company or self.company).id),
                ("apix_storage_id", "=", storage_id),
            ]
        )

    def _apix_records(self, company=None):
        return self.Import.sudo().search(
            [("company_id", "=", (company or self.company).id), ("source", "=", "apix")]
        )

    def _bills(self, company=None, ref=None):
        domain = [
            ("company_id", "=", (company or self.company).id),
            ("move_type", "in", ("in_invoice", "in_refund")),
        ]
        if ref:
            domain.append(("ref", "=", ref))
        return self.Move.sudo().search(domain)

    def _scheduler_cycle(self, prefixes=ALL_APIX_JOBS, timings=None):
        """The Scheduler fires (`_cron_apix_run`), then every resulting job
        is performed."""
        self.Connection._cron_apix_run()
        return self._perform(prefixes, timings=timings)

    def _chatter(self, record):
        return "\n".join(str(body) for body in record.sudo().message_ids.mapped("body"))
