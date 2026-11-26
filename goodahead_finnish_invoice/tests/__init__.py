# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from . import test_fi_address
from . import test_fi_bank_barcode
from . import test_release

# The report test needs the ERP; the library tests also run with plain
# `python -m unittest discover` (no ERP), where this package is `tests`.
if __name__.startswith("odoo.addons."):
    from . import test_report
