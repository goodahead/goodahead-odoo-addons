# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The `root.apix` job channel belongs to the connection (send T8; AC-12,
AC-53; staged migration 01).

Receiving and sending share `root.apix` (sending hangs `root.apix.send` under
it), so the channel record and its external id live in `goodahead_apix`. The
pre-migrate script moves the external id from receiving on upgrade.
"""

import importlib.util
import os
import unittest

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the client tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo.tests import TransactionCase, tagged  # noqa: E402

ADDON = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ADDON, "migrations", "19.0.1.4.0", "pre-move-apix-channel.py")
RECEIVING = "goodahead_invoice_import_apix"


def _load_script(path):
    spec = importlib.util.spec_from_file_location("pre_move_apix_channel", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@tagged("post_install", "-at_install")
class TestApixChannelOwnership(TransactionCase):
    def _owner_rows(self):
        self.env.cr.execute(
            "SELECT module FROM ir_model_data WHERE name = 'channel_apix' "
            "AND model = 'queue.job.channel' ORDER BY module"
        )
        return [row[0] for row in self.env.cr.fetchall()]

    def test_the_connection_owns_root_apix(self):
        channel = self.env.ref("goodahead_apix.channel_apix")
        self.assertEqual(channel.complete_name, "root.apix")
        self.assertEqual(channel.parent_id, self.env.ref("queue_job.channel_root"))
        self.assertEqual(self._owner_rows(), ["goodahead_apix"])
        self.assertEqual(
            self.env["queue.job.channel"].search_count([("complete_name", "=", "root.apix")]), 1
        )

    def test_receiving_jobs_run_on_the_shared_channel(self):
        if not self.env.ref(f"{RECEIVING}.job_function_apix_run", raise_if_not_found=False):
            self.skipTest("APIX receiving is not installed")
        channel = self.env.ref("goodahead_apix.channel_apix")
        for name in (
            "job_function_apix_run",
            "job_function_apix_import_package",
            "job_function_apix_mark_received",
            "job_function_apix_shadow_classify",
            "job_function_apix_recognise_received",
            "job_function_apix_daily_checks",
        ):
            with self.subTest(job_function=name):
                function = self.env.ref(f"{RECEIVING}.{name}")
                self.assertEqual(function.channel_id, channel)
                self.assertEqual(function.channel, "root.apix")

    def test_pre_migrate_moves_the_external_id_from_receiving(self):
        script = _load_script(SCRIPT)
        cr = self.env.cr
        # A database from before the move: receiving owns the external id.
        cr.execute(
            "UPDATE ir_model_data SET module = %s WHERE module = 'goodahead_apix' "
            "AND name = 'channel_apix'",
            [RECEIVING],
        )
        script.migrate(cr, "19.0.1.3.0")
        self.assertEqual(self._owner_rows(), ["goodahead_apix"])
        # Idempotent: already owned by the connection, nothing to move.
        script.migrate(cr, "19.0.1.3.0")
        self.assertEqual(self._owner_rows(), ["goodahead_apix"])

    def test_pre_migrate_does_nothing_on_a_fresh_install(self):
        script = _load_script(SCRIPT)
        cr = self.env.cr
        cr.execute(
            "UPDATE ir_model_data SET module = %s WHERE module = 'goodahead_apix' "
            "AND name = 'channel_apix'",
            [RECEIVING],
        )
        script.migrate(cr, None)
        self.assertEqual(self._owner_rows(), [RECEIVING])

    def test_pre_migrate_never_creates_a_second_owner(self):
        script = _load_script(SCRIPT)
        cr = self.env.cr
        channel = self.env.ref("goodahead_apix.channel_apix")
        cr.execute(
            "INSERT INTO ir_model_data (module, name, model, res_id, noupdate) "
            "VALUES (%s, 'channel_apix', 'queue.job.channel', %s, false)",
            [RECEIVING, channel.id],
        )
        script.migrate(cr, "19.0.1.3.0")
        self.assertEqual(self._owner_rows(), ["goodahead_apix", RECEIVING])

    def test_receiving_side_guard_moves_the_external_id_too(self):
        # Upgrading receiving alone (the connection not upgraded) must not
        # lose the channel: receiving's own pre-migrate moves it as well.
        path = os.path.join(
            os.path.dirname(ADDON), RECEIVING, "migrations", "19.0.1.8.0", "pre-move-apix-channel.py"
        )
        if not os.path.isdir(os.path.join(os.path.dirname(ADDON), RECEIVING)):
            self.skipTest("APIX receiving is not next to this add-on")
        self.assertTrue(os.path.isfile(path), f"missing receiving pre-migrate {path}")
        script = _load_script(path)
        cr = self.env.cr
        cr.execute(
            "UPDATE ir_model_data SET module = %s WHERE module = 'goodahead_apix' "
            "AND name = 'channel_apix'",
            [RECEIVING],
        )
        script.migrate(cr, "19.0.1.7.2")
        self.assertEqual(self._owner_rows(), ["goodahead_apix"])
