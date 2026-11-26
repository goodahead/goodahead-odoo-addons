# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""`lib/apix_package.py` without the ERP: the module is loaded from its file,
so nothing of Odoo is imported, and the Finvoice sniff is a stand-in.

Run without Odoo::

    python3 goodahead/goodahead_invoice_import_apix/tests/test_apix_package_pure.py

Not listed in ``tests/__init__.py`` on purpose: it is a plain `unittest` case,
not an Odoo one. Invented data only.
"""

import importlib.util
import io
import os
import sys
import unittest
import zipfile

LIB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "lib", "apix_package.py")

FINVOICE = b'<?xml version="1.0"?><Finvoice Version="3.0"><InvoiceNumber>1</InvoiceNumber></Finvoice>'
PDF = b"%PDF-1.4\n%%EOF\n"


def load_apix_package():
    spec = importlib.util.spec_from_file_location("apix_package_pure", LIB)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sniff(content):
    return b"<Finvoice" in bytes(content[:2048])


def make_zip(*files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files:
            archive.writestr(name, content)
    return buffer.getvalue()


class TestApixPackagePure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        loaded_before = {name for name in sys.modules if name == "odoo" or name.startswith("odoo.")}
        cls.apix_package = load_apix_package()
        cls.odoo_imported = {
            name for name in sys.modules if name == "odoo" or name.startswith("odoo.")
        } - loaded_before

    def test_loads_without_the_erp(self):
        self.assertFalse(self.odoo_imported, "apix_package imports nothing from odoo")

    def test_zip_names_finvoice_and_image(self):
        package = self.apix_package.unpack(
            make_zip(("a.pdf", PDF), ("doc.xml", FINVOICE), ("img.pdf", PDF)),
            "doc.xml",
            "img.pdf",
            sniff=sniff,
        )
        self.assertEqual(package.finvoice_file.name, "doc.xml")
        self.assertEqual(package.finvoice_file.mimetype, "application/xml")
        self.assertEqual(package.image_file.name, "img.pdf")
        self.assertEqual([f.name for f in package.other_files()], ["a.pdf"])

    def test_the_sniff_decides_what_is_finvoice(self):
        body = make_zip(("x/inv.xml", FINVOICE), ("x/scan.pdf", PDF))
        self.assertEqual(self.apix_package.unpack(body, sniff=sniff).finvoice_file.name, "inv.xml")
        self.assertIsNone(self.apix_package.unpack(body, sniff=lambda content: False).finvoice)

    def test_plain_finvoice_body_and_errors(self):
        package = self.apix_package.unpack(FINVOICE, "invoice.xml", sniff=sniff)
        self.assertEqual([f.name for f in package.files], ["invoice.xml"])
        with self.assertRaises(self.apix_package.PackageError) as caught:
            self.apix_package.unpack(b"not a package", sniff=sniff)
        self.assertEqual(caught.exception.code, self.apix_package.NOT_A_PACKAGE)
        body = make_zip(("invoice.xml", FINVOICE))
        with self.assertRaises(self.apix_package.PackageError) as caught:
            self.apix_package.unpack(body[: len(body) // 2], sniff=sniff)
        self.assertEqual(caught.exception.code, self.apix_package.TRUNCATED)

    def test_the_sniff_is_required(self):
        with self.assertRaises(TypeError):
            self.apix_package.unpack(FINVOICE)


if __name__ == "__main__":
    unittest.main()
