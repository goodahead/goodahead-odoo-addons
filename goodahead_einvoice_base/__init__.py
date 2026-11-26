# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

# lib/finvoice is the pure-Python Finvoice library (no ERP import, receiving
# ADR-0004), shared by receiving and sending. Its tests also run with plain
# unittest from the add-ons folder, where this package is imported without the
# ERP: the ERP layers (the attention bubble, unit codes) load only inside the
# ERP.
if __name__.startswith("odoo.addons."):
    from . import controllers
    from . import models
    from .hooks import post_init_hook
