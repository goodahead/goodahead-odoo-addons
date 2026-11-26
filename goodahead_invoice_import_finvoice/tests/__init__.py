# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from . import test_finvoice_library

try:  # the source tests need the ERP; the library tests run without it
    import odoo.tests  # noqa: F401
except ImportError:  # pragma: no cover
    pass
else:
    from . import test_finvoice_intake_limits
    from . import test_finvoice_source
    from . import test_source_dispatch
