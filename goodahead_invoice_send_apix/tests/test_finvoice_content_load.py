# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Local preparation time of the new content (finvoice content, spec §6
"Local preparation time"): building the values, writing and schema-checking
a 200-line invoice with every new content adds p95 ≤ 100 ms over the same
invoice without it, and the whole preparation stays within p95 ≤ 5 s."""

import logging
import time
import unittest

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo.tests import tagged

from odoo.addons.goodahead_einvoice_base.lib import finvoice

from .common import SendApixCommon

_logger = logging.getLogger(__name__)
RUNS = 20
LINES = 200


@tagged("post_install", "-at_install")
class TestFinvoiceContentLoad(SendApixCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.s24 = cls._make_tax(cls.company, 24.0, "S")
        cls.ae0 = cls._make_tax(cls.company, 0.0, "AE")
        cls.product = cls.env["product.product"].create(
            {"name": "ZZ Barcoded", "barcode": "4006381333931"})

    def _invoice(self, rich):
        lines = []
        for index in range(LINES):
            line = {"name": f"ZZ line {index}", "quantity": 1 + index % 5,
                    "price_unit": 10.0 + index % 7, "tax_ids": [(6, 0, self.s24.ids)]}
            if rich:
                line["discount"] = 10.0 if index % 2 else 0.0
                if index % 10 == 0:
                    line["tax_ids"] = [(6, 0, self.ae0.ids)]
                if index % 3 == 0:
                    line["product_id"] = self.product.id
            lines.append(line)
            if rich and index % 20 == 0:
                lines.append({"display_type": "line_note", "name": f"ZZ note {index}"})
        extra = {"ref": "PO-1, S00012", "invoice_origin": "S00012",
                 "einvoice_buyer_reference": "Test User"} if rich else {}
        return self._make_invoice(lines=lines, **extra)

    @staticmethod
    def _p95(move):
        timings = []
        for _run in range(RUNS):
            move.invalidate_recordset()
            start = time.perf_counter()
            xml = finvoice.write(move._apix_finvoice_values())
            problems = finvoice.validate_schema(xml)
            timings.append(time.perf_counter() - start)
            assert not problems, problems
        return sorted(timings)[int(RUNS * 0.95) - 1]

    def test_200_lines_with_every_new_content(self):
        plain, rich = self._invoice(False), self._invoice(True)
        p95_plain, p95_rich = self._p95(plain), self._p95(rich)
        _logger.info("200-line preparation p95: plain %.3f s, with the new content %.3f s",
                     p95_plain, p95_rich)
        self.assertLess(p95_rich, 5.0)
        self.assertLess(p95_rich - p95_plain, 0.1,
                        f"added p95 {p95_rich - p95_plain:.3f} s (plain {p95_plain:.3f} s)")
