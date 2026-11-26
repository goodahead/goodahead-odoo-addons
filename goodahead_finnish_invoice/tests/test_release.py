# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Release readiness of the add-on.

Pure Python, runs with plain unittest and under the ERP's runner: every Python
file carries the licence header, a README explains what the add-on does and
how to set it up, and no ticket key is left in code, comments, data or docs.
"""

import glob
import os
import re
import unittest

if __name__.startswith("odoo.addons."):
    from odoo.tests import BaseCase, tagged
else:  # plain unittest, no ERP
    BaseCase = unittest.TestCase

    def tagged(*_tags):
        return lambda cls: cls


ADDON = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEADER = "# Copyright 2026 Goodahead Oy\n# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).\n"
# A tracker key like ABC-123; standards and encodings are not tickets.
TICKET_RE = re.compile(r"\b(?!UTF-|LGPL-|AGPL-|ISO-|RF-)[A-Z][A-Z0-9]{1,9}-\d{1,6}\b")


def _files(*patterns):
    for pattern in patterns:
        for path in sorted(glob.glob(os.path.join(ADDON, "**", pattern), recursive=True)):
            if os.sep + "tests" + os.sep in path or ".ruff_cache" in path:
                continue
            yield path


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


@tagged("post_install", "-at_install")
class TestRelease(BaseCase):
    def test_licence_header_on_every_python_file(self):
        for path in glob.glob(os.path.join(ADDON, "**", "*.py"), recursive=True):
            if ".ruff_cache" in path:
                continue
            with self.subTest(path=os.path.relpath(path, ADDON)):
                self.assertTrue(_read(path).startswith(HEADER))

    def test_no_ticket_keys(self):
        for path in _files("*.py", "*.xml", "*.po", "*.md", "*.csv"):
            with self.subTest(path=os.path.relpath(path, ADDON)):
                self.assertEqual(TICKET_RE.findall(_read(path)), [])

    def test_readme_explains_setup(self):
        path = os.path.join(ADDON, "README.md")
        self.assertTrue(os.path.isfile(path), "README.md is missing")
        readme = _read(path)
        for heading in ("## Setup", "## Bank bar code", "## Licence"):
            with self.subTest(heading=heading):
                self.assertIn(heading, readme)
