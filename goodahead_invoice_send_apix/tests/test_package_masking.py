# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Pure tests of the per-invoice package and the masked exchange record.
Contract: public-api.md §6.4.

Runs without the ERP:

    python -I -m unittest discover \
        -s goodahead/goodahead_invoice_send_apix/tests -t goodahead

and inside the ERP's test runner. Every id, key and number here is invented.
"""

import hashlib
import io
import unittest
import zipfile

try:  # inside the ERP test runner
    from odoo.tests import BaseCase as _TestCase
    from odoo.tests import tagged
except ImportError:  # plain unittest, no ERP installed
    _TestCase = unittest.TestCase

    def tagged(*_tags):
        return lambda cls: cls

from ..lib import masking, package

MESSAGE = b'<?xml version="1.0" encoding="UTF-8"?>\n<Finvoice Version="3.0"/>\n'
PDF = b"%PDF-1.4\n% invented test image\n%%EOF\n"

TEST_TRANSFER_ID = "zz-transfer-id-0001"
TEST_TRANSFER_KEY = "zz-transfer-key-secret-0001"
TEST_DIGEST = "SHA-256:" + hashlib.sha256(
    f"ZZsoft+1.14+{TEST_TRANSFER_ID}+20261125100000+{TEST_TRANSFER_KEY}".encode()
).hexdigest()
SEND_URL = (
    "https://test-api.apix.fi/invoices?soft=ZZsoft&ver=1.14"
    f"&TraID={TEST_TRANSFER_ID}&t=20261125100000&d={TEST_DIGEST}"
)


def _members(zip_bytes):
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        return [(info.filename, archive.read(info)) for info in archive.infolist()]


@tagged("post_install", "-at_install")
class TestBuildPackage(_TestCase):
    def test_holds_the_message_and_the_pdf_image(self):
        members = _members(package.build_package(MESSAGE, PDF, "10045"))
        self.assertEqual(members, [("10045.xml", MESSAGE), ("invoice.pdf", PDF)])

    def test_regenerated_package_is_byte_identical(self):
        first = package.build_package(MESSAGE, PDF, "INV/2026/00001")
        second = package.build_package(bytes(MESSAGE), bytes(PDF), "INV/2026/00001")
        self.assertEqual(first, second)

    def test_fixed_timestamps(self):
        with zipfile.ZipFile(io.BytesIO(package.build_package(MESSAGE, PDF, "1"))) as archive:
            stamps = {info.date_time for info in archive.infolist()}
        self.assertEqual(stamps, {package.FIXED_DATE_TIME})

    def test_safe_file_name_from_the_invoice_number(self):
        members = _members(package.build_package(MESSAGE, PDF, "INV/2026 00001"))
        self.assertEqual(members[0][0], "INV_2026_00001.xml")
        self.assertEqual(package.safe_file_name("../../etc/passwd"), "etc_passwd")
        self.assertEqual(package.safe_file_name("Lasku ä/1"), "Lasku_1")
        self.assertEqual(package.safe_file_name(""), "invoice")
        self.assertEqual(package.safe_file_name(None), "invoice")
        self.assertEqual(package.safe_file_name("///"), "invoice")

    def test_is_a_valid_zip(self):
        zip_bytes = package.build_package(MESSAGE, PDF, "1")
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            self.assertIsNone(archive.testzip())


@tagged("post_install", "-at_install")
class TestMaskExchange(_TestCase):
    def test_masks_transfer_id_and_digest(self):
        zip_bytes = package.build_package(MESSAGE, PDF, "10045")
        record = masking.mask_exchange("PUT", SEND_URL, 200, zip_bytes)
        self.assertNotIn(TEST_TRANSFER_ID, record)
        self.assertNotIn(TEST_DIGEST, record)
        self.assertNotIn(TEST_DIGEST.split(":", 1)[1], record)
        self.assertNotIn(TEST_TRANSFER_KEY, record)
        self.assertIn("TraID=***", record)
        self.assertIn("d=***", record)
        self.assertIn("soft=ZZsoft", record)
        self.assertIn("t=20261125100000", record)
        self.assertIn("PUT", record)
        self.assertIn("200", record)

    def test_zip_body_becomes_size_and_sha256(self):
        zip_bytes = package.build_package(MESSAGE, PDF, "10045")
        record = masking.mask_exchange("PUT", SEND_URL, 200, zip_bytes)
        self.assertIn(str(len(zip_bytes)), record)
        self.assertIn(hashlib.sha256(zip_bytes).hexdigest(), record)
        self.assertNotIn("10045.xml", record)
        self.assertNotIn("PK", record)

    def test_masks_uid(self):
        url = f"https://test-api.apix.fi/addressquery?bid=0000001-9&uid={TEST_TRANSFER_ID}&t=1&d=x"
        record = masking.mask_exchange("GET", url, 200, None)
        self.assertNotIn(TEST_TRANSFER_ID, record)
        self.assertIn("uid=***", record)
        self.assertIn("bid=0000001-9", record)

    def test_response_body_is_kept(self):
        answer = "<Response><Status>OK</Status><StatusCode>2000</StatusCode></Response>"
        record = masking.mask_exchange("PUT", SEND_URL, 200, answer)
        self.assertIn(answer, record)
        record = masking.mask_exchange("PUT", SEND_URL, 200, answer.encode())
        self.assertIn(answer, record)

    def test_url_without_query_is_unchanged(self):
        url = "https://test-api.apix.fi/invoices"
        record = masking.mask_exchange("GET", url, 503, b"")
        self.assertIn(url, record)
        self.assertIn("503", record)

    def test_parameter_names_are_not_case_sensitive(self):
        url = f"https://test-api.apix.fi/x?traid={TEST_TRANSFER_ID}&D={TEST_DIGEST}"
        record = masking.mask_exchange("GET", url, None, None)
        self.assertNotIn(TEST_TRANSFER_ID, record)
        self.assertNotIn(TEST_DIGEST, record)
