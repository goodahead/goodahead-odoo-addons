# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

# lib/finvoice is the pure-Python reader (no ERP import, ADR-0004); models/ is
# the source that feeds it uploads and mail and hands the result to the core.
# The guard keeps the library's tests runnable with plain unittest, where the
# package is imported without the ERP (see tests/test_finvoice_library.py).
try:
    import odoo.models  # noqa: F401
except ImportError:  # pragma: no cover - outside the ERP
    pass
else:
    from . import models
