# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Load scenarios of the test-plan (spec §6 thresholds).

A timed integration test is enough: the load is generated inside the ERP,
APIX is the in-process `FakeApix`. Thresholds are asserted per unit of work
(test-plan edge case "slow test machine"), never on the suite's wall clock:

* listing: the run job (listing + one record per package + one package job
  each) <= 2 min;
* per package: p95 of the package jobs (download to draft bill) <= 30 s,
  measured with `time.perf_counter` around each performed job;
* no unit of work (run, package or mark job) > 5 min.

Opt-in: tagged `-standard` and `apix_load`, so a default run (and any run
by module, `/goodahead_invoice_import_apix`) leaves it out; the pre-release
gate runs it with `--test-tags apix_load`. The measured numbers are logged
(`T17 load:`).

ANALYZE caveat: the whole cycle runs in one uncommitted test transaction, so
autovacuum never refreshes the planner statistics as it does in production
after each committed job. `_refresh_statistics` runs ANALYZE every
`STATISTICS_EVERY` jobs inside the transaction instead; without it the
per-package time measures stale statistics, not the import. The numbers are
therefore indicative of a database whose statistics are kept current.
"""

import logging

from odoo.tests import tagged

from .apix_e2e_common import (
    TRANSFER_A,
    TRANSFER_B,
    ApixE2ECase,
    make_zip,
    percentile,
    simple_finvoice,
)

_logger = logging.getLogger(__name__)

PACKAGES = 500
LISTING_MAX_SECONDS = 120
PACKAGE_P95_MAX_SECONDS = 30
UNIT_MAX_SECONDS = 300

#: Planner statistics are refreshed every this many jobs (`_refresh_statistics`).
STATISTICS_EVERY = 100
STATISTICS_TABLES = (
    "account_move",
    "account_move_line",
    "account_partial_reconcile",
    "account_payment",
    "goodahead_invoice_import",
    "goodahead_vendor_data_suggestion",
    "ir_attachment",
    "mail_message",
    "queue_job",
    "res_partner",
)


@tagged("post_install", "-at_install", "-standard", "apix_load")
class TestApixLoad(ApixE2ECase):
    disable_tracking = True

    def _fill(self, transfer_id, prefix, count):
        for index in range(count):
            number = f"{prefix}-{index:04d}"
            self.fake.add_package(
                transfer_id,
                f"{prefix}-S{index:04d}",
                make_zip(("invoice.xml", simple_finvoice(number))),
                number,
            )

    def _refresh_statistics(self, tries=0):
        """What autovacuum's ANALYZE does in production as each job commits.

        Here the whole cycle is one uncommitted test transaction, so the
        planner never learns that the bill tables grew from a few rows to
        thousands; after earlier tests have left dead rows behind, it picks
        plans whose cost grows with every bill (Odoo's payment-state query
        climbed from 0.2 s to over 2.5 s per bill after a few hundred bills). ANALYZE
        inside the transaction counts its own rows, keeping the per-package
        time a measure of the import, not of stale statistics. Not timed."""
        if tries % STATISTICS_EVERY:
            return
        self.env.flush_all()
        for table in STATISTICS_TABLES:
            self.env.cr.execute(f"ANALYZE {table}")

    def _cycle(self):
        """One Scheduler cycle, each kind of job timed on its own."""
        self._refresh_statistics()
        self.Connection._cron_apix_run()
        timings = {"run": [], "package": [], "mark": []}
        self._perform(("apix-run:",), timings=timings["run"])
        self._perform(
            ("apix-import:",), timings=timings["package"], between=self._refresh_statistics
        )
        self._perform(("apix-mark:",), timings=timings["mark"], between=self._refresh_statistics)
        return timings

    def _check_healthy_company(self, timings, label):
        company_run = [t for t in timings["run"] if t[0] == f"apix-run:{self.company.id}"]
        self.assertEqual(len(company_run), 1)
        _key, listing_seconds, outcome = company_run[0]
        self.assertEqual(outcome, "capped", "500 packages is a full listing")
        self.assertEqual(len(self.connection._apix_open_todos("listing-cap")), 1)

        records = self._apix_records()
        self.assertEqual(len(records), PACKAGES, "every listed package has its record")
        package_keys = {key for key, _s, _r in timings["package"]}
        self.assertEqual(package_keys, {f"apix-import:{record.id}" for record in records})
        self.assertEqual(len(timings["package"]), PACKAGES, "one package job each, one try")
        self.assertEqual(set(records.mapped("state")), {"imported"})
        self.assertEqual(len(self._bills()), PACKAGES)
        self.assertTrue(all(records.mapped("apix_mark_confirmed")))
        self.assertEqual(len(self.fake.marked), PACKAGES)

        package_seconds = [seconds for _k, seconds, _r in timings["package"]]
        p95 = percentile(package_seconds, 95)
        slowest = max(seconds for kind in timings.values() for _k, seconds, _r in kind)
        total = sum(seconds for kind in timings.values() for _k, seconds, _r in kind)
        _logger.info(
            "T17 load: %s: listing (run job, %s packages) %.2f s; package job p50 %.3f s, "
            "p95 %.3f s, max %.3f s; mark job p95 %.3f s; slowest unit %.2f s; "
            "all jobs %.1f s",
            label,
            PACKAGES,
            listing_seconds,
            percentile(package_seconds, 50),
            p95,
            max(package_seconds),
            percentile([seconds for _k, seconds, _r in timings["mark"]], 95),
            slowest,
            total,
        )
        self.assertLessEqual(listing_seconds, LISTING_MAX_SECONDS, "listing <= 2 min")
        self.assertLessEqual(p95, PACKAGE_P95_MAX_SECONDS, "p95 download to draft bill <= 30 s")
        self.assertLessEqual(slowest, UNIT_MAX_SECONDS, "no unit of work > 5 min")

    def test_full_listing_of_500_packages(self):
        self._fill(TRANSFER_A, "LOAD", PACKAGES)
        timings = self._cycle()
        self._check_healthy_company(timings, "one company")

    def test_two_companies_one_rejected(self):
        company_b = self.env["res.company"].create({"name": "Load Rejected Oy"})
        conn_b = self._live_connection(company_b, TRANSFER_B, f"secret-{TRANSFER_B}", "0000007-8")
        self.fake.add_account(
            TRANSFER_B, f"secret-{TRANSFER_B}", "0000007-8", company_b.name, rejected=True
        )
        self._fill(TRANSFER_A, "LOAD2", PACKAGES)
        timings = self._cycle()
        runs = {key: outcome for key, _s, outcome in timings["run"]}
        self.assertEqual(runs[f"apix-run:{company_b.id}"], "rejected")
        self.assertEqual(conn_b.last_run_state, "failed")
        self.assertFalse(self.Import.sudo().search([("company_id", "=", company_b.id)]))
        self._check_healthy_company(timings, "two companies, one rejected")
