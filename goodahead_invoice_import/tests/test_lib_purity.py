# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""ADR-0004: ``lib/`` is pure Python. No module in it imports the ERP at
module level, so it can be read and tested without one; what needs the ERP
(the UBL generator) lives in ``adapters/``."""

import glob
import os
import re

from odoo.tests import BaseCase, tagged

ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ERP_IMPORT = re.compile(r"^(?:from|import)\s+(?:odoo|\.\.)", re.MULTILINE)


@tagged("post_install", "-at_install")
class TestLibPurity(BaseCase):
    def test_lib_imports_no_erp(self):
        offenders = []
        for path in sorted(glob.glob(os.path.join(ADDON_DIR, "lib", "*.py"))):
            with open(path, encoding="utf-8") as handle:
                if _ERP_IMPORT.search(handle.read()):
                    offenders.append(os.path.basename(path))
        self.assertEqual(offenders, [], "lib/ must not import the ERP or the add-on")

    def test_ubl_generator_is_an_adapter(self):
        self.assertFalse(os.path.exists(os.path.join(ADDON_DIR, "lib", "ubl_generator.py")))
        from odoo.addons.goodahead_invoice_import.adapters import ubl_generator

        self.assertTrue(callable(ubl_generator.generate_peppol_invoice_xml))
