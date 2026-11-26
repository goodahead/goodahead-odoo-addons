# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Locate the Finvoice library, which lives in ``goodahead_einvoice_base``.

Relative to the add-ons namespace inside the ERP (``odoo.addons``); as a
top-level package when the pure tests run with plain unittest from the
add-ons folder (``-t goodahead``), where a relative import cannot leave this
add-on. Neither way imports the ERP itself.
"""

try:
    from ....goodahead_einvoice_base.lib import finvoice
except ImportError:  # plain unittest: the add-ons folder is the import root
    from goodahead_einvoice_base.lib import finvoice

__all__ = ["finvoice"]
