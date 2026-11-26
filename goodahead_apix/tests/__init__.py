# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from . import test_apix_client
from . import test_apix_client_send

# The connection tests need the ORM; the client tests must also run with plain
# `python -m unittest discover` (no Odoo), where this package is `tests`.
if __name__.startswith("odoo.addons."):
    from . import test_channel_ownership
    from . import test_connection
    from . import test_connection_trouble
    from . import test_copy_safety
    from . import test_menu
