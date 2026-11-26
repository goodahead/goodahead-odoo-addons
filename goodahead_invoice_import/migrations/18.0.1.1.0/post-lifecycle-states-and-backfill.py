# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""19.0.1.1.0 — the import record's new lifecycle (T3, ADR-0008).

For a database that installed this core at 19.0.1.0.0, before the lifecycle
changed. The rename path (upgrading the AI add-on from before the split into a core and sources) does
the same work in the core's install hooks and never runs this script, because
a module being installed runs no migrations.

* states: draft and processing -> waiting, done -> imported (ADR-0002);
* the new columns ``source``, ``invoice_number_normalized``, ``seller_vat``,
  ``seller_business_id`` filled from what the records already hold.
"""

from odoo import SUPERUSER_ID, api

from odoo.addons.goodahead_invoice_import import hooks


def migrate(cr, version):
    if not version:
        return
    hooks.map_states(cr)
    env = api.Environment(cr, SUPERUSER_ID, {})
    hooks.backfill(env)
