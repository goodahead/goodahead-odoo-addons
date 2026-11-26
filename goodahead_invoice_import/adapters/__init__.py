# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Code that turns invoice data into the ERP's own formats and therefore
imports the ERP: ``lib/`` stays pure Python, this package does
not have to."""

from . import ubl_generator
