# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""A neutralized database copy never receives from APIX.

`data/neutralize.sql` runs when a production copy is neutralized for a test
or staging environment. Receiving must be off afterwards on every company,
or the copy would mark packages received on the live account. Invented data.
"""

from odoo.modules.neutralize import neutralize_database
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestApixNeutralize(TransactionCase):
    def test_neutralize_turns_receiving_off(self):
        Connection = self.env["goodahead.apix.connection"]
        connections = Connection
        for index, state in enumerate(("live", "shadow", "off"), start=1):
            company = self.env["res.company"].create({"name": f"Neutralize Test {index} Oy"})
            connection = Connection.create(
                {
                    "company_id": company.id,
                    "environment": "test",
                    "transfer_id": f"cccccccc-0000-4000-8000-00000000000{index}",
                    "transfer_key": f"secret-{index}",
                    "business_id": f"000000{index}-0",
                }
            )
            connection.write({"status": "working"})
            connection._apix_set_receiving_state(state)
            connections |= connection
        self.env.flush_all()

        neutralize_database(self.env.cr)
        self.env.invalidate_all()

        self.assertEqual(set(connections.mapped("receiving_state")), {"off"})

    def test_neutralize_forgets_the_package_storage_keys(self):
        """A StorageKey alone downloads a package or marks it received: a
        copy must not keep the live account's keys."""
        record = self.env["goodahead.invoice.import"].sudo().create(
            {
                "name": "Neutralize Test package",
                "company_id": self.env.company.id,
                "source": "apix",
                "apix_storage_id": "S-NEUTRAL-1",
                "apix_storage_key": "key-S-NEUTRAL-1",
            }
        )
        self.env.flush_all()

        neutralize_database(self.env.cr)
        self.env.invalidate_all()

        self.assertFalse(record.apix_storage_key)
        self.assertEqual(record.apix_storage_id, "S-NEUTRAL-1", "the id stays, for the history")
