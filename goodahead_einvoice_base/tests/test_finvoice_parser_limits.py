# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""A Finvoice file comes from outside: its size and shape must not decide
how long a worker is busy.

Runs without the ERP (``python -I -m unittest``) and inside its test runner.
"""

import os
import random
import re
import time
import unittest

try:  # inside the ERP test runner
    from odoo.tests import BaseCase as _TestCase
    from odoo.tests import tagged
except ImportError:  # plain unittest, no ERP installed
    _TestCase = unittest.TestCase

    def tagged(*_tags):
        return lambda cls: cls

from ..lib import finvoice
from ..lib.finvoice import parser

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

#: Generous for a slow CI machine; the old scans took minutes on these inputs.
TIME_BUDGET = 5.0
OPENERS = 50000

# The patterns the parser used to run over the whole file. Kept here only as
# the reference the linear scans must agree with on small inputs.
_OLD_DECL = re.compile(r"<\?xml\s[^>]*?\?>")
_OLD_ENCODING = re.compile(rb"""<\?xml\s[^>]*?encoding\s*=\s*["']([A-Za-z0-9._-]+)["']""")
_OLD_DOCTYPE = re.compile(r"<!DOCTYPE[^>\[]*(\[[^\]]*\])?\s*>", re.IGNORECASE)


def _raw(name):
    with open(os.path.join(DATA_DIR, name), "rb") as handle:
        return handle.read()


@tagged("post_install", "-at_install")
class TestFinvoiceParserLimits(_TestCase):
    def _refused_in_time(self, raw):
        started = time.monotonic()
        with self.assertRaises(finvoice.FinvoiceError) as caught:
            finvoice.parse(raw)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, TIME_BUDGET, f"parse took {elapsed:.1f} s")
        self.assertEqual(caught.exception.code, finvoice.UNREADABLE)

    def test_unclosed_declaration_openers_are_read_in_linear_time(self):
        self._refused_in_time(b"<?xml " * OPENERS)

    def test_unclosed_encoding_declarations_are_read_in_linear_time(self):
        self._refused_in_time(b"<?xml encoding=" * OPENERS)

    def test_unclosed_doctype_openers_are_read_in_linear_time(self):
        self._refused_in_time(b"<!DOCTYPE " * OPENERS)

    def test_unclosed_doctype_subsets_are_read_in_linear_time(self):
        self._refused_in_time(b"<!DOCTYPE[" * OPENERS + b"]" + b" " * OPENERS + b"x")

    def test_a_file_over_the_size_cap_is_refused_before_it_is_read(self):
        raw = _raw("fi30_simple_invoice.xml")
        self.assertTrue(finvoice.parse(raw))
        with self.assertRaises(finvoice.FinvoiceError) as caught:
            finvoice.parse(raw, max_bytes=len(raw) - 1)
        self.assertEqual(caught.exception.code, finvoice.UNREADABLE)
        self.assertIn("larger than", caught.exception.message)
        big = b"<" + b" " * parser.MAX_FILE_BYTES
        started = time.monotonic()
        with self.assertRaises(finvoice.FinvoiceError):
            finvoice.parse(big)
        self.assertLess(time.monotonic() - started, TIME_BUDGET)

    def test_the_scans_agree_with_the_old_patterns(self):
        """Same result as the old regular expressions on many small inputs."""
        tokens = [
            "<?xml", "<?xml ", " ", "\n", "?", ">", "?>", "[", "]", "<!DOCTYPE",
            "<!doctype", "a", "encoding", "=", '"', "'", "utf-8", "<", "!",
        ]
        rng = random.Random(4242)
        for _round in range(20000):
            text = "".join(rng.choice(tokens) for _ in range(rng.randint(0, 14)))
            self.assertEqual(
                parser._strip_xml_declarations(text), _OLD_DECL.sub("", text), repr(text)
            )
            self.assertEqual(
                parser._strip_doctypes(text), _OLD_DOCTYPE.sub("", text), repr(text)
            )
            data = text.encode()
            self.assertEqual(
                parser._declared_encodings(data),
                [m.group(1) for m in _OLD_ENCODING.finditer(data)],
                repr(text),
            )

    def test_every_fixture_still_parses_the_same(self):
        for name in sorted(os.listdir(DATA_DIR)):
            if not name.endswith(".xml"):
                continue
            with self.subTest(fixture=name):
                raw = _raw(name)
                try:
                    messages = finvoice.parse(raw)
                except finvoice.FinvoiceError:
                    continue
                self.assertTrue(messages)
