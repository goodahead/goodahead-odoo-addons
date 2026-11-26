# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Moved to ``goodahead_einvoice_base/lib/finvoice/parser.py``.

This module replaces itself with the base's, so
``from ...lib.finvoice.parser import X`` keeps giving the very same objects.
"""

import sys

from ._base import finvoice as _finvoice

sys.modules[__name__] = _finvoice.parser
