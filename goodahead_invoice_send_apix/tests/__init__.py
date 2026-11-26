# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from . import test_lib_purity
from . import test_package_masking
from . import test_readiness_rules

# The model tests need the ERP; the library tests also run with plain
# `python -m unittest discover` (no ERP).
if __name__.startswith("odoo.addons."):
    from . import test_attempt_model
    from . import test_buyer_reference_default
    from . import test_company_isolation
    from . import test_concurrency
    from . import test_delivery_tab
    from . import test_developer_view
    from . import test_einvoice_lookup
    from . import test_failed_sends
    from . import test_finvoice_content_load
    from . import test_finvoice_content_round_trip
    from . import test_finvoice_content_upgrade
    from . import test_finvoice_values
    from . import test_finvoice_values_references
    from . import test_finvoice_values_rows
    from . import test_finvoice_values_vat
    from . import test_fix_business_id
    from . import test_fix_wizard
    from . import test_install_hooks
    from . import test_install_warnings
    from . import test_invoice_form
    from . import test_ledger
    from . import test_ledger_own_cursor
    from . import test_load
    from . import test_mass_wizard
    from . import test_nothing_until_send
    from . import test_readiness_checks
    from . import test_run_status
    from . import test_secrets
    from . import test_send_and_print
    from . import test_send_and_print_batch
    from . import test_send_jobs
    from . import test_sending_switch
    from . import test_single_send
    from . import test_sweeper_neutralize
    from . import test_tax_vat_settings
    from . import test_tax_views
    from . import test_transmit_method
