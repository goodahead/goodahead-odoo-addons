# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

# First, and dependency-free: it declares which core pipeline this build runs,
# which everything below either hooks or refuses on.
from . import core_profile
from . import res_config_settings
from . import expense_category
from . import account_edi_common
from . import account_edi_ubl
# Before `res_partner`: that model declares a One2many onto this one and its
# create/write hooks call it.
from . import partner_identifier
from . import res_partner
from . import res_partner_bank
# The vendor-aware supplierinfo match is CORE's, and this module only decides
# WHICH OF CORE'S STEPS RUN; on the legacy core that decision is
# `product.product._get_product_domain_search_order`, which this file overrides.
from . import product_product
# Before `account_move`: that model declares a One2many onto each of them.
from . import vendor_candidate
from . import vendor_data_suggestion
from . import account_move
from . import goodahead_invoice_import
from . import einvoice_attention
