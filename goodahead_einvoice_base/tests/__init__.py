# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from . import test_finvoice_reader
from . import test_finvoice_references
from . import test_finvoice_rows
from . import test_finvoice_vat
from . import test_finvoice_writer
from . import test_finvoice_writer_content
from . import test_finvoice_writer_credit_print
from . import test_units

# The attention bubble tests need the ERP; the library tests also run with
# plain `python -m unittest discover` (no ERP).
if __name__.startswith("odoo.addons."):
    from . import test_buyer_reference_field
    from . import test_einvoice_attention
    from . import test_unit_codes
    from . import test_unit_codes_screen
