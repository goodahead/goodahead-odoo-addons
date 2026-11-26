# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

# Pure Python: nothing here imports the ERP. The UBL generator,
# which reads constants from the ERP's UBL add-on, is in `adapters/`.
from . import bank_numbers
from . import pdf_currency
from . import invoice_data
from . import import_record
from . import import_source
