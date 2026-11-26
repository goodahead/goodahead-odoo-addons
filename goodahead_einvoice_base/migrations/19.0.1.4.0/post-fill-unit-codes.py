# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Post-migrate of goodahead_einvoice_base 19.0.1.4.0 (send ADR-0013;
promoted from the feature's staged migration 04).

19.0.1.4.0 adds ``uom.uom.rec20_code``. A fresh install fills it through the
``post_init_hook``; a database that already has the add-on gets the same
fill here, after the column exists. Never over a code already set.
"""

from odoo import SUPERUSER_ID, api
from odoo.addons.goodahead_einvoice_base.hooks import fill_unit_codes


def migrate(cr, version):
    if not version:
        return
    fill_unit_codes(api.Environment(cr, SUPERUSER_ID, {}))
