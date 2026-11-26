# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Lib purity across the platform: the Finvoice
writer and reader, the readiness rules, the package and masking, and the
APIX client are plain Python — no ERP import under any ``lib/``. Only the
APIX client talks to the network."""

import glob
import os
import unittest

try:  # inside the ERP test runner
    from odoo.tests import BaseCase as _TestCase
    from odoo.tests import tagged
except ImportError:  # plain unittest, no ERP installed
    _TestCase = unittest.TestCase

    def tagged(*_tags):
        return lambda cls: cls

GOODAHEAD = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIBS = ("goodahead_invoice_send_apix", "goodahead_einvoice_base", "goodahead_apix")
ERP_IMPORT = r"(?m)^\s*(from|import)\s+odoo\b"
NETWORK_IMPORT = r"(?m)^\s*(from|import)\s+(requests|urllib|http|socket)\b"


def _sources(addon):
    pattern = os.path.join(GOODAHEAD, addon, "lib", "**", "*.py")
    return sorted(glob.glob(pattern, recursive=True))


@tagged("post_install", "-at_install")
class TestLibPurity(_TestCase):
    def test_every_lib_is_found(self):
        for addon in LIBS:
            with self.subTest(addon=addon):
                self.assertTrue(_sources(addon), f"{addon}/lib not found")

    def test_no_erp_import_under_lib(self):
        for addon in LIBS:
            for path in _sources(addon):
                with open(path, encoding="utf-8") as handle:
                    source = handle.read()
                with self.subTest(path=os.path.relpath(path, GOODAHEAD)):
                    self.assertNotRegex(source, ERP_IMPORT)

    def test_only_the_apix_client_reaches_the_network(self):
        for addon in ("goodahead_invoice_send_apix", "goodahead_einvoice_base"):
            for path in _sources(addon):
                with open(path, encoding="utf-8") as handle:
                    source = handle.read()
                with self.subTest(path=os.path.relpath(path, GOODAHEAD)):
                    self.assertNotRegex(source, NETWORK_IMPORT)
