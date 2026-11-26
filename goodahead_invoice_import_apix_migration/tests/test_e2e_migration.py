# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""US-06 end to end through the migration tool's screen (test-plan
"Coverage" e2e-through-UI rows): connect by hand, shadow, switch to live,
recover the possibly lost packages, switch back.

APIX is replaced at its boundary by the receiving add-on's `FakeApix`;
everything else is the real chain. Runs with whatever migration sources are
installed (none, or e.g. the community adapter): the company has no earlier
connection, so detection finds nothing. AC-11d, AC-17, AC-18 (this side),
AC-19b. Invented data only.
"""

from odoo.exceptions import UserError
from odoo.tests import Form, new_test_user, tagged

from odoo.addons.goodahead_invoice_import_apix.tests.apix_e2e_common import (
    ALL_APIX_JOBS,
    TRANSFER_A,
    ApixE2ECase,
    make_zip,
    simple_finvoice,
)

#: Every APIX job except the mark: lets a flow look at the moment between a
#: bill's commit and APIX hearing of it.
NO_MARK_JOBS = tuple(prefix for prefix in ALL_APIX_JOBS if prefix != "apix-mark:")


@tagged("post_install", "-at_install", "apix_e2e")
class TestApixMigrationEndToEnd(ApixE2ECase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = new_test_user(
            cls.env,
            login="mig_e2e_admin",
            email="mig.e2e.admin@example.test",
            groups="base.group_user,account.group_account_manager",
            company_id=cls.company.id,
            company_ids=[(6, 0, cls.company.ids)],
        )
        cls.accountant = new_test_user(
            cls.env,
            login="mig_e2e_accountant",
            email="mig.e2e.accountant@example.test",
            groups="base.group_user,base.group_partner_manager,account.group_account_invoice",
            company_id=cls.company.id,
            company_ids=[(6, 0, cls.company.ids)],
        )

    def _package(self, storage_id, number, status="UNRECEIVED"):
        body = make_zip(("invoice.xml", simple_finvoice(number)))
        return self.fake.add_package(TRANSFER_A, storage_id, body, number, status=status)

    def _migration_form(self):
        return Form(self.env["goodahead.apix.migration"].with_user(self.admin))

    def test_us06_migration_cycle_through_the_tool(self):
        self.connection._apix_set_receiving_state("off")
        # Imported before by whatever received until now.
        self.Move.create(
            {
                "move_type": "in_invoice",
                "company_id": self.company.id,
                "partner_id": self.vendor.id,
                "ref": "M-OLD",
                "invoice_date": "2026-11-15",
                "invoice_line_ids": [
                    (0, 0, {"name": "Example line", "quantity": 1, "price_unit": 100.0})
                ],
            }
        )
        self._package("P-NEW", "M-NEW")
        self._package("P-OLD", "M-OLD", status="RECEIVED")
        self._package("P-LOST1", "M-LOST1", status="RECEIVED")
        self._package("P-LOST2", "M-LOST2", status="RECEIVED")
        bills_before = len(self._bills())

        # Step 1: no earlier connection for the company — connect by hand.
        form = self._migration_form()
        self.assertEqual(form.step, "detect")
        self.assertFalse(form.source_found)
        action = form.save().action_connect_manually()
        self.assertEqual(action["res_model"], self.connection._name)
        self.assertEqual(action["res_id"], self.connection.id)
        if self.connection.receiving_via_migration:
            # With a migration source installed the radio is read-only: the
            # migration tool changes receiving.
            self.connection.with_user(self.admin)._apix_set_receiving_state("shadow")
        else:
            with Form(self.connection.with_user(self.admin)) as connection_form:
                connection_form.receiving_state = "shadow"
        self.assertEqual(self.connection.receiving_state, "shadow")

        # Step 2: the shadow run classifies, never bills, never marks.
        self._scheduler_cycle()
        self.assertEqual(len(self._bills()), bills_before)
        self.assertFalse(self.fake.marked)
        self.assertFalse(self.fake.downloads(True))
        form = self._migration_form()
        self.assertEqual(form.step, "shadow")
        self.assertEqual(form.count_would_import, 1)
        self.assertEqual(form.count_imported_before, 1)
        self.assertEqual(form.count_would_be_possibly_lost, 2)
        self.assertEqual(form.count_would_fail, 0)
        self.assertEqual(form.count_unaccounted, 0)

        # Step 3: switch to live.
        wizard = form.save()
        wizard.action_switch_to_live()
        self.assertEqual(self.connection.receiving_state, "live")
        self._perform(NO_MARK_JOBS)
        new, old = self._record("P-NEW"), self._record("P-OLD")
        lost1, lost2 = self._record("P-LOST1"), self._record("P-LOST2")
        self.assertEqual(new.state, "imported", new.error_message or "")
        self.assertEqual(old.state, "imported", "recognised, not imported again")
        self.assertEqual(old.move_id.ref, "M-OLD")
        self.assertTrue(old.apix_mark_confirmed)
        self.assertEqual((lost1.state, lost2.state), ("possibly_lost", "possibly_lost"))
        self.assertEqual(len(self._bills()), bills_before + 1)

        # The Accountant imports one possibly lost package and closes the other.
        lost1.with_user(self.accountant).action_import_possibly_lost()
        dialog = Form(
            self.env["goodahead.invoice.import.close"]
            .with_user(self.accountant)
            .with_context(default_import_ids=[(6, 0, lost2.ids)])
        )
        dialog.reason = "Paid by hand before the switch"
        dialog.save().action_confirm()
        self._perform(NO_MARK_JOBS)
        lost1.invalidate_recordset()
        self.assertEqual(lost1.state, "imported", lost1.error_message or "")
        self.assertEqual(lost1.move_id.ref, "M-LOST1")
        self.assertEqual(lost2.state, "closed")
        self.assertEqual(len(self._bills()), bills_before + 2)

        # Switch back is refused while marks are pending.
        form = Form(wizard)
        self.assertEqual(form.step, "live")
        self.assertEqual(form.unconfirmed_count, 3)
        with self.assertRaisesRegex(UserError, "3 records not yet confirmed"):
            wizard.action_switch_back()
        self.assertEqual(self.connection.receiving_state, "live")

        # The marks go through; then switching back is allowed.
        self._perform(("apix-mark:",))
        self.assertEqual(sorted(self.fake.marked), ["P-LOST1", "P-LOST2", "P-NEW"])
        wizard.action_switch_back()
        form = Form(wizard)
        self.assertEqual(form.step, "switch_back")
        self.assertEqual(form.unconfirmed_count, 0)
        self.assertFalse(wizard.unsettled_ids, "nothing left for the earlier connector")
        wizard.action_confirm_switch_back()
        self.assertEqual(self.connection.receiving_state, "off")
        self.assertEqual(wizard.step, "detect")

        # Off: the Scheduler fetches nothing for the company any more.
        listings = len([c for c in self.fake.calls if c[0] == "list_packages"])
        self.Connection._cron_apix_run()
        self.assertFalse(self._pending(("apix-run:",)))
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "list_packages"]), listings)
        self.assertEqual(len(self._bills()), bills_before + 2)
