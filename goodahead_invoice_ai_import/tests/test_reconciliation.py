# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The figures are checked against each other, and a null amount is a failure.

`_validate_extraction` checks that the line totals, tax and totals add up. The
per-line check is the only one that can see a misread quantity, because the line
total that feeds the document sums is unaffected by it. The extractor uses a stub
client, and each rejection test has a control that must still validate.
"""

import copy
import json

from odoo.tests import BaseCase, tagged

from odoo.addons.goodahead_invoice_ai_import.lib.claude_client import (
    ClaudeExtractionError,
    ClaudeInvoiceExtractor,
)


class _StubClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def messages_create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


# A US bill that adds up: two lines, 6.625% sales tax on one, freight untaxed.
GOOD = {
    "type": "Invoice",
    "invoice_number": "ZZ-REC-1",
    "issue_date": "2026-03-04",
    "currency": "USD",
    "seller": {"name": "ZZ Restaurant Supply LLC", "country_code": "US"},
    "buyer": {"name": "Example Holdings LLC", "country_code": "US"},
    "lines": [
        {
            "name": "Commercial mixer, 20qt",
            "quantity": 2.0,
            "unit_code": "C62",
            "price_unit": 500.0,
            "line_total": 1000.0,
            "tax_percent": 6.625,
            "tax_amount": 66.25,
            "tax_category": "S",
        },
        {
            "name": "Freight",
            "quantity": 1.0,
            "unit_code": "C62",
            "price_unit": 120.0,
            "line_total": 120.0,
            "tax_percent": 0.0,
            "tax_amount": 0.0,
            "tax_category": "Z",
        },
    ],
    "tax_subtotals": [
        {
            "tax_percent": 6.625,
            "taxable_amount": 1000.0,
            "tax_amount": 66.25,
            "tax_category": "S",
        },
        {
            "tax_percent": 0.0,
            "taxable_amount": 120.0,
            "tax_amount": 0.0,
            "tax_category": "Z",
        },
    ],
    "totals": {
        "line_extension": 1120.0,
        "tax_exclusive": 1120.0,
        "tax_inclusive": 1186.25,
        "payable": 1186.25,
    },
}


@tagged("post_install", "-at_install")
class TestReconciliation(BaseCase):

    def _validate(self, data):
        """Run the real extraction path over `data` and return the parsed result."""
        body = {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": json.dumps(data)}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        return ClaudeInvoiceExtractor(client=_StubClient(body)).extract_invoice(b"%PDF")

    def _reject(self, data, pattern):
        with self.assertRaisesRegex(ClaudeExtractionError, pattern) as caught:
            self._validate(data)
        # The parsed JSON must survive every rejection so it can be fixed and
        # re-imported without another paid call.
        self.assertIsNotNone(
            caught.exception.data,
            "the extraction did not ride out on the exception, so the record "
            "cannot be fixed and re-imported without paying for another call",
        )
        return caught.exception

    # ------------------------------------------------------------------
    # The controls
    # ------------------------------------------------------------------

    def test_a_whole_good_invoice_still_validates(self):
        """Control for the whole file: a consistent invoice validates."""
        result = self._validate(copy.deepcopy(GOOD))

        self.assertEqual(result["data"]["totals"]["payable"], 1186.25)

    def test_a_document_with_no_tax_at_all_validates(self):
        """An untaxed document is normal; the check becomes exclusive == inclusive."""
        data = copy.deepcopy(GOOD)
        data["tax_subtotals"] = []
        for line in data["lines"]:
            line["tax_amount"] = 0.0
            line["tax_percent"] = 0.0
            line["tax_category"] = "Z"
        data["totals"]["tax_inclusive"] = 1120.0
        data["totals"]["payable"] = 1120.0

        self.assertEqual(self._validate(data)["data"]["totals"]["payable"], 1120.0)

    def test_a_document_with_no_tax_subtotals_is_reconciled_against_its_lines(self):
        """Without tax subtotals the tax is reconciled against the lines' own amounts."""
        data = copy.deepcopy(GOOD)
        data["tax_subtotals"] = []
        data["totals"]["tax_inclusive"] = 1500.0        # lines say 66.25 of tax
        data["totals"]["payable"] = 1500.0

        error = self._reject(data, "The tax does not add up")

        self.assertIn("the lines' own tax amounts", str(error))

    def test_rounding_across_many_lines_is_absorbed(self):
        """Per-line rounding against a total computed before rounding is tolerated."""
        data = copy.deepcopy(GOOD)
        data["lines"] = [
            {
                "name": "Item %s" % n,
                "quantity": 3.0,
                "price_unit": 3.33,
                "line_total": 9.99,
                "tax_percent": 0.0,
                "tax_amount": 0.0,
                "tax_category": "Z",
            }
            for n in range(40)
        ]
        data["tax_subtotals"] = []
        # 40 x 9.99 is 399.60; the vendor billed 40 x 3 x 3.3325 and printed
        # 399.90, which is 0.30 out and well inside 0.5%.
        data["totals"] = {
            "line_extension": 399.90,
            "tax_exclusive": 399.90,
            "tax_inclusive": 399.90,
            "payable": 399.90,
        }

        self.assertEqual(self._validate(data)["data"]["totals"]["payable"], 399.90)

    def test_a_credit_note_in_either_sign_convention_validates(self):
        """Credit notes printed positive or negative both validate; the checks are linear."""
        for sign in (1, -1):
            with self.subTest(sign=sign):
                data = copy.deepcopy(GOOD)
                data["type"] = "CreditNote"
                for line in data["lines"]:
                    for key in ("quantity", "line_total", "tax_amount"):
                        line[key] = sign * abs(line[key])
                for sub in data["tax_subtotals"]:
                    for key in ("taxable_amount", "tax_amount"):
                        sub[key] = sign * abs(sub[key])
                for key in data["totals"]:
                    data["totals"][key] = sign * abs(data["totals"][key])

                self.assertEqual(self._validate(data)["data"]["type"], "CreditNote")

    def test_sections_that_disagree_about_the_sign_are_rejected(self):
        """Control: negative lines under a positive total are rejected."""
        data = copy.deepcopy(GOOD)
        data["type"] = "CreditNote"
        for line in data["lines"]:
            line["line_total"] = -line["line_total"]

        self._reject(data, "The line totals do not add up")

    def test_an_invoice_carrying_one_negative_line_validates(self):
        """A negative line (discount, deposit) on an invoice is accepted."""
        data = copy.deepcopy(GOOD)
        data["lines"].append({
            "name": "Loyalty discount",
            "quantity": 1.0,
            "price_unit": -20.0,
            "line_total": -20.0,
            "tax_percent": 0.0,
            "tax_amount": 0.0,
            "tax_category": "Z",
        })
        data["tax_subtotals"][1]["taxable_amount"] = 100.0
        data["totals"]["line_extension"] = 1100.0
        data["totals"]["tax_exclusive"] = 1100.0
        data["totals"]["tax_inclusive"] = 1166.25
        data["totals"]["payable"] = 1166.25

        self.assertEqual(self._validate(data)["data"]["totals"]["payable"], 1166.25)

    # ------------------------------------------------------------------
    # The three document sums
    # ------------------------------------------------------------------

    def test_lines_that_do_not_sum_to_the_line_extension_are_rejected(self):
        """A dropped or misread line is rejected with a message naming the figures."""
        data = copy.deepcopy(GOOD)
        data["lines"][0]["line_total"] = 100.0      # was 1000.00

        error = self._reject(data, "line totals do not add up")

        self.assertIn("line_extension", str(error))
        self.assertIn("220.00", str(error))         # what the lines actually sum to

    def test_a_thousands_separator_read_as_a_decimal_point_is_caught(self):
        """A document total with `1,120.00` read as `1.120` is caught."""
        data = copy.deepcopy(GOOD)
        data["totals"]["line_extension"] = 1.120

        self._reject(data, "line totals do not add up")

    def test_tax_that_does_not_bridge_exclusive_to_inclusive_is_rejected(self):
        data = copy.deepcopy(GOOD)
        data["totals"]["tax_inclusive"] = 1200.0    # 1120.00 + 66.25 is 1186.25

        error = self._reject(data, "The tax does not add up")

        self.assertIn("tax_inclusive", str(error))

    def test_a_payable_that_is_not_the_inclusive_total_is_rejected(self):
        data = copy.deepcopy(GOOD)
        data["totals"]["payable"] = 1186.52         # transposed

        error = self._reject(data, "totals.payable")

        self.assertIn("tax_inclusive", str(error))

    # ------------------------------------------------------------------
    # The per-line check — the only one that can see a quantity
    # ------------------------------------------------------------------

    def test_a_quantity_misread_by_a_thousand_is_caught(self):
        """A quantity misread by a factor of a thousand is caught by the line check.

        The document sums still add up, which the test also asserts.
        """
        data = copy.deepcopy(GOOD)
        data["lines"][0]["quantity"] = 2000.0       # "2.000" read as two thousand

        error = self._reject(data, "decimal point or its thousands separator")

        self.assertIn("Commercial mixer", str(error))
        # With the line total untouched the document sums are still satisfied.
        data_sums_fine = copy.deepcopy(data)
        data_sums_fine["lines"][0]["quantity"] = 2.0
        self._validate(data_sums_fine)

    def test_a_quantity_misread_the_other_way_is_caught(self):
        """`2500` pieces read as `2.500`. Same misreading, other direction."""
        data = copy.deepcopy(GOOD)
        data["lines"][0]["quantity"] = 0.002

        self._reject(data, "decimal point or its thousands separator")

    def test_a_discounted_line_is_not_a_quantity_error(self):
        """Control: a discounted line is accepted.

        `line_total` is quantity * price_unit minus discount, so strict equality
        would reject every discounted invoice.
        """
        data = copy.deepcopy(GOOD)
        data["lines"][0]["price_unit"] = 1000.0     # list price, 50% discount
        # the line total, the tax and every document figure are unchanged

        self.assertEqual(self._validate(data)["data"]["totals"]["payable"], 1186.25)

    def test_a_free_of_charge_line_is_not_a_quantity_error(self):
        """A 100% discounted line (total zero) is not a quantity error."""
        data = copy.deepcopy(GOOD)
        data["lines"].append({
            "name": "Sample, free of charge",
            "quantity": 1.0,
            "price_unit": 45.0,
            "line_total": 0.0,
            "tax_percent": 0.0,
            "tax_amount": 0.0,
            "tax_category": "Z",
        })

        self.assertEqual(self._validate(data)["data"]["totals"]["payable"], 1186.25)

    def test_a_line_without_a_unit_price_is_not_rejected_for_it(self):
        """Without `price_unit` the per-line check is skipped, not failed."""
        data = copy.deepcopy(GOOD)
        del data["lines"][0]["price_unit"]

        self.assertEqual(self._validate(data)["data"]["totals"]["payable"], 1186.25)

    # ------------------------------------------------------------------
    # A null amount is a failure, not a zero
    # ------------------------------------------------------------------

    def test_a_null_line_total_is_a_loud_failure(self):
        """A null line total is rejected instead of silently becoming 0.00."""
        data = copy.deepcopy(GOOD)
        data["lines"][0]["line_total"] = None

        error = self._reject(data, "line_total is null")

        self.assertIn("Commercial mixer", str(error))

    def test_an_absent_line_total_is_the_same_failure(self):
        """An absent line total is rejected like a null one."""
        data = copy.deepcopy(GOOD)
        del data["lines"][0]["line_total"]

        self._reject(data, "line_total is missing")

    def test_a_null_total_is_a_loud_failure(self):
        for key in ("line_extension", "tax_exclusive", "tax_inclusive", "payable"):
            with self.subTest(key=key):
                data = copy.deepcopy(GOOD)
                data["totals"][key] = None

                self._reject(data, "%s is null" % key)

    def test_a_null_subtotal_tax_amount_is_a_loud_failure(self):
        data = copy.deepcopy(GOOD)
        data["tax_subtotals"][0]["tax_amount"] = None

        self._reject(data, "tax_amount is null")

    def test_a_null_line_tax_is_accepted_when_the_subtotal_states_it(self):
        """A receipt with one tax total and no per-line tax reconciles from the subtotal."""
        data = copy.deepcopy(GOOD)
        for line in data["lines"]:
            line["tax_amount"] = None

        result = self._validate(data)

        self.assertIsNone(result["data"]["lines"][0]["tax_amount"])

    def test_a_null_line_tax_with_no_subtotal_still_fails_a_taxed_document(self):
        """Control: with neither line tax nor subtotal, a taxed document fails."""
        data = copy.deepcopy(GOOD)
        data["tax_subtotals"] = []
        for line in data["lines"]:
            line["tax_amount"] = None

        self._reject(data, "The tax does not add up")

    def test_a_line_tax_that_is_not_a_number_is_still_rejected(self):
        data = copy.deepcopy(GOOD)
        data["lines"][0]["tax_amount"] = "66.25"

        self._reject(data, "not a number")

    def test_a_null_taxable_amount_is_a_loud_failure(self):
        data = copy.deepcopy(GOOD)
        data["tax_subtotals"][0]["taxable_amount"] = None

        self._reject(data, "taxable_amount is null")

    def test_an_amount_that_arrives_as_a_string_is_rejected(self):
        """Amounts must be JSON numbers; a string's separators are undecided."""
        data = copy.deepcopy(GOOD)
        data["totals"]["payable"] = "1,186.25"

        self._reject(data, "not a number")

    def test_a_boolean_is_not_a_number(self):
        """`bool` is an `int` in Python, so `True` must be rejected explicitly."""
        data = copy.deepcopy(GOOD)
        data["lines"][0]["tax_amount"] = True

        self._reject(data, "not a number")
