# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The figures are checked against each other, and a null amount is a failure.

🔴 WHY THIS FILE EXISTS. `_validate_extraction` checked presence, types, a
three-letter currency, the `type` enum, a non-empty `lines` and a seller name —
and DID NO ARITHMETIC AT ALL. Meanwhile the prompt itself uses "the line totals,
tax and total must add up" as its tiebreak for a thousands separator and
"quantity × unit price must equal the line total" as its tiebreak for a
quantity. The prompt already believed the invariant; nothing verified it.

🔴 AND WHY THE PER-LINE CHECK IS DIFFERENT IN KIND FROM THE OTHER THREE. The
generator emits `PriceAmount = line_total / quantity`, so a quantity misread by
a factor of a thousand comes with a unit price misread by a thousand the other
way, and the line total — the only figure that reaches the document sums — is
untouched. Every document total reconciles perfectly. The line is the only
place it can be seen.

⚠️ NOTHING HERE TOUCHES THE NETWORK: the extractor is built with a stub client,
so a real call cannot slip through.

⚠️ EVERY REJECTION TEST BELOW HAS A CONTROL BESIDE IT, and the controls are not
ceremony. A validator that rejects everything passes every rejection test in
this file and breaks every import in production. The two that matter most are
`test_a_whole_good_invoice_still_validates` and
`test_a_discounted_line_is_not_a_quantity_error`.
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


# A real US bill, with a real New Jersey rate, that adds up: two lines, 6.625%
# sales tax on one of them, shipping untaxed.
GOOD = {
    "type": "Invoice",
    "invoice_number": "ZZ-REC-1",
    "issue_date": "2026-03-04",
    "currency": "USD",
    "seller": {"name": "ZZ Restaurant Supply LLC", "country_code": "US"},
    "buyer": {"name": "Alpha LLC", "country_code": "US"},
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
        # Claude has already been billed, so the parsed JSON must survive every
        # rejection — that is what makes a loud failure cheap instead of
        # expensive. Without it a human has nothing to fix and re-import, and
        # the only way forward is a second paid call.
        self.assertIsNotNone(
            caught.exception.data,
            "the extraction did not ride out on the exception, so the record "
            "cannot be fixed and re-imported without paying for another call",
        )
        return caught.exception

    # ------------------------------------------------------------------
    # The controls. Read these first.
    # ------------------------------------------------------------------

    def test_a_whole_good_invoice_still_validates(self):
        """🔴 THE CONTROL FOR THIS ENTIRE FILE.

        Two lines, two tax subtotals, one taxed and one not, a real rate and
        real figures. If this fails, every other test here is passing for the
        wrong reason and the import is broken for every invoice.
        """
        result = self._validate(copy.deepcopy(GOOD))

        self.assertEqual(result["data"]["totals"]["payable"], 1186.25)

    def test_a_document_with_no_tax_at_all_validates(self):
        """A resale certificate, an exempt buyer, an out-of-state vendor.

        The prompt says in as many words that a document with no tax is normal,
        so an empty `tax_subtotals` must not be read as a missing figure. With
        no tax anywhere the tax check becomes `tax_exclusive == tax_inclusive`,
        which is the right question to ask of an untaxed document.
        """
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
        """The control for the control above: "no tax subtotals" must not become
        "no tax check". A document that states its tax per line and not in a
        subtotal block is still reconciled, against the lines' own figures."""
        data = copy.deepcopy(GOOD)
        data["tax_subtotals"] = []
        data["totals"]["tax_inclusive"] = 1500.0        # lines say 66.25 of tax
        data["totals"]["payable"] = 1500.0

        error = self._reject(data, "The tax does not add up")

        self.assertIn("the lines' own tax amounts", str(error))

    def test_rounding_across_many_lines_is_absorbed(self):
        """The tolerance earns its keep here.

        Forty lines each rounded to the cent, against a total the vendor
        computed before rounding. A validator with no tolerance rejects this
        correct invoice, gets switched off, and then catches nothing at all.
        """
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
        """⚠️ Vendors print credit memos both ways, and the module's own
        `_normalise_credit_note_signs` exists because of it.

        Every check here is linear, so multiplying the whole document by -1
        changes nothing about whether it reconciles — which is exactly the
        property that makes it safe to check signs-as-extracted rather than
        normalising first.
        """
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
        """And the control for that: negative lines under a positive total is
        not a credit note in either convention. The generator would build a bill
        with a negative line extension and a positive payable out of it."""
        data = copy.deepcopy(GOOD)
        data["type"] = "CreditNote"
        for line in data["lines"]:
            line["line_total"] = -line["line_total"]

        self._reject(data, "The line totals do not add up")

    def test_an_invoice_carrying_one_negative_line_validates(self):
        """A discount line, a deposit deducted, an item credited on the same
        bill. The prompt says these are ordinary on an invoice and keep their
        minus sign, so the sums must accept them."""
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
        """A dropped line, or a misread one. The message has to name the figure:
        "the document does not reconcile" sends somebody back to a PDF with
        nothing to look for."""
        data = copy.deepcopy(GOOD)
        data["lines"][0]["line_total"] = 100.0      # was 1000.00

        error = self._reject(data, "line totals do not add up")

        self.assertIn("line_extension", str(error))
        self.assertIn("220.00", str(error))         # what the lines actually sum to

    def test_a_thousands_separator_read_as_a_decimal_point_is_caught(self):
        """The error class the NUMBERS procedure's own tiebreak is about.

        "If that reading is genuinely in doubt, settle it from the other amounts
        on the same document — the line totals, tax and total must add up, and
        only one reading makes them add up." That is a real check, and until now
        it was a check nothing performed. Here the DOCUMENT total is the misread
        one: `1,120.00` taken as `1.120`.
        """
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
        """🔴 THE ONE THAT MATTERS, and it is invisible to everything else.

        `1.250 LB` read as 1250 pounds. The vendor's printed unit price and
        printed line total are both correct and unchanged, so every document
        total below still adds up perfectly — which this test asserts, so that
        nobody later concludes the sums would have caught it.
        """
        data = copy.deepcopy(GOOD)
        data["lines"][0]["quantity"] = 2000.0       # "2.000" read as two thousand

        error = self._reject(data, "decimal point or its thousands separator")

        self.assertIn("Commercial mixer", str(error))
        # The proof of the claim in the docstring: with the line total untouched
        # the three document sums are all still satisfied.
        data_sums_fine = copy.deepcopy(data)
        data_sums_fine["lines"][0]["quantity"] = 2.0
        self._validate(data_sums_fine)

    def test_a_quantity_misread_the_other_way_is_caught(self):
        """`2500` pieces read as `2.500`. Same misreading, other direction."""
        data = copy.deepcopy(GOOD)
        data["lines"][0]["quantity"] = 0.002

        self._reject(data, "decimal point or its thousands separator")

    def test_a_discounted_line_is_not_a_quantity_error(self):
        """🔴 THE CONTROL THAT DECIDED THE SHAPE OF THIS CHECK.

        `quantity × price_unit == line_total` is NOT an invariant of this
        schema: `line_total` is "quantity * price_unit - discount", and the
        vendors this module was built against print a list price and a discount
        on every line — the generator's whole PriceAmount block exists for that,
        and `test_ubl_generator` carries the 44.95-list-against-24.73-net
        fixture. A strict equality here would reject every discounted invoice
        this business receives, which is far worse than the bug it would catch.
        """
        data = copy.deepcopy(GOOD)
        data["lines"][0]["price_unit"] = 1000.0     # list price, 50% discount
        # the line total, the tax and every document figure are unchanged

        self.assertEqual(self._validate(data)["data"]["totals"]["payable"], 1186.25)

    def test_a_free_of_charge_line_is_not_a_quantity_error(self):
        """The other legitimate case the ratio must not fire on: a list price
        against a line total of zero is a 100% discount, which is ordinary, and
        is also the one ratio that has no finite value."""
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
        """`price_unit` is the one side of this check that the schema does not
        guarantee. Absent, the check simply does not run; it must not become a
        rejection of its own."""
        data = copy.deepcopy(GOOD)
        del data["lines"][0]["price_unit"]

        self.assertEqual(self._validate(data)["data"]["totals"]["payable"], 1186.25)

    # ------------------------------------------------------------------
    # A null amount is a failure, not a zero
    # ------------------------------------------------------------------

    def test_a_null_line_total_is_a_loud_failure(self):
        """🔴 It used to be `0.00`.

        `ubl_generator._fmt_decimal(None)` returned "0.00", so a null line total
        passed validation, passed UBL generation, built a bill and was wrong
        with nothing anywhere to see — which inverts the whole
        null-rather-than-guess principle, whose justification is that a missing
        value is VISIBLE.
        """
        data = copy.deepcopy(GOOD)
        data["lines"][0]["line_total"] = None

        error = self._reject(data, "line_total is null")

        self.assertIn("Commercial mixer", str(error))

    def test_an_absent_line_total_is_the_same_failure(self):
        """Absent and null were identical downstream — `.get(key, 0)` covered
        one and `_fmt_decimal(None)` the other, and both produced a zero — so
        they are identical here."""
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
        """19.0.3.29.0: a receipt that prints one tax total and none per line.

        The document tax still reconciles, from the subtotal. The import does
        not fail; a bill whose total then differs from the printed one gets the
        red banner (`ai_total_mismatch`, 19.0.3.31.0).
        """
        data = copy.deepcopy(GOOD)
        for line in data["lines"]:
            line["tax_amount"] = None

        result = self._validate(data)

        self.assertIsNone(result["data"]["lines"][0]["tax_amount"])

    def test_a_null_line_tax_with_no_subtotal_still_fails_a_taxed_document(self):
        """The control for the test above: without a subtotal there is no tax
        figure anywhere, and a taxed document must still fail the tax check."""
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
        """The NUMBERS procedure says every amount is a JSON number. A string
        has not been through it — its separators have not been decided yet, and
        `float("1,234.56")` raises while `float("1.234")` quietly means 1.234
        when the document meant 1234."""
        data = copy.deepcopy(GOOD)
        data["totals"]["payable"] = "1,186.25"

        self._reject(data, "not a number")

    def test_a_boolean_is_not_a_number(self):
        """`bool` is an `int` in Python, so `True` would otherwise reconcile as
        1.0 — the same class of trap as `str(True)[:2] == "TR"` being read as
        Turkey, which this module has already been bitten by once."""
        data = copy.deepcopy(GOOD)
        data["lines"][0]["tax_amount"] = True

        self._reject(data, "not a number")
