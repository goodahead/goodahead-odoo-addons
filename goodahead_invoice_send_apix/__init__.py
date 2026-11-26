# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from . import lib

# lib/ is pure Python (no ERP import, receiving ADR-0004); its tests also run
# with plain unittest, where this package is imported without the ERP. The
# ERP layers load only inside the ERP.
if __name__.startswith("odoo.addons."):
    from . import models
    from . import adapters
    from . import wizard
    from .hooks import post_init_hook
