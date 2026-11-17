# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
# Dependency-free; declares which core import pipeline this build runs.
from . import core_profile
from . import prompt_profile
from . import res_company
from . import res_config_settings
from . import expense_category
from . import account_edi_common
from . import account_edi_ubl
# Before `res_partner`, which declares a One2many onto it.
from . import partner_identifier
from . import res_partner
from . import product_product
# Before `account_move`, which declares a One2many onto each of them.
from . import vendor_candidate
from . import vendor_data_suggestion
from . import account_move
from . import account_journal
from . import ai_invoice_import
