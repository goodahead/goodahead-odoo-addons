# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Tests of the pure VAT category rules (finvoice content T01; AC-04, AC-06).

Runs without the ERP:

    python -I -m unittest discover \
        -s goodahead/goodahead_einvoice_base/tests -t goodahead

and inside the ERP's test runner.
"""

import unittest

try:  # inside the ERP test runner
    from odoo.tests import BaseCase as _TestCase
    from odoo.tests import tagged
except ImportError:  # plain unittest, no ERP installed
    _TestCase = unittest.TestCase

    def tagged(*_tags):
        return lambda cls: cls

from ..lib.finvoice import vat


@tagged("post_install", "-at_install")
class TestVatConstants(_TestCase):
    def test_categories(self):
        self.assertEqual(vat.CATEGORIES, ("S", "Z", "E", "AE", "K", "G", "O"))
        self.assertEqual(vat.NO_VAT_CATEGORIES, ("Z", "E", "AE", "K", "G", "O"))

    def test_default_reasons_are_the_specified_wording(self):
        self.assertEqual(
            vat.DEFAULT_REASONS["AE"],
            ("Käännetty verovelvollisuus", "Reverse charge"),
        )
        self.assertEqual(
            vat.DEFAULT_REASONS["K"],
            ("Yhteisömyynti, AVL 72 a §", "Intra-Community supply, VAT Directive art. 138"),
        )
        self.assertEqual(
            vat.DEFAULT_REASONS["G"],
            ("Vienti EU:n ulkopuolelle, AVL 70 §", "Export outside the EU"),
        )
        self.assertEqual(
            vat.DEFAULT_REASONS["E"],
            ("Arvonlisäverosta vapautettu myynti", "VAT exempt supply"),
        )
        self.assertEqual(
            vat.DEFAULT_REASONS["Z"],
            ("Nollaverokannan myynti", "Zero-rated supply"),
        )
        self.assertEqual(
            vat.DEFAULT_REASONS["O"],
            ("Ei arvonlisäveron alaista myyntiä", "Outside the scope of VAT"),
        )

    def test_every_no_vat_category_has_two_lines_within_70(self):
        self.assertEqual(set(vat.DEFAULT_REASONS), set(vat.NO_VAT_CATEGORIES))
        for category, lines in vat.DEFAULT_REASONS.items():
            with self.subTest(category=category):
                self.assertEqual(len(lines), 2)
                for line in lines:
                    self.assertLessEqual(len(line), vat.REASON_LINE_MAX)
                    self.assertTrue(line.strip())

    def test_default_exemption_codes(self):
        self.assertEqual(vat.DEFAULT_EXEMPTION_CODES, {
            "AE": "VATEX-EU-AE",
            "K": "VATEX-EU-IC",
            "G": "VATEX-EU-G",
            "O": "VATEX-EU-O",
        })

    def test_report_table(self):
        self.assertEqual(vat.REPORT_CATEGORIES, {
            "FI": {"fi_311": "K", "fi_312": "AE", "fi_319": "AE"},
        })


@tagged("post_install", "-at_install")
class TestDefaultCategory(_TestCase):
    def test_positive_rate_is_standard(self):
        self.assertEqual(vat.default_category(24.0, "FI", set()), "S")
        self.assertEqual(vat.default_category(25.5, None, {"fi_311"}), "S")

    def test_report_lines_name_one_category(self):
        self.assertEqual(vat.default_category(0.0, "FI", {"fi_311"}), "K")
        self.assertEqual(vat.default_category(0.0, "FI", {"fi_312"}), "AE")
        self.assertEqual(vat.default_category(0.0, "FI", {"fi_319"}), "AE")

    def test_signs_are_stripped(self):
        self.assertEqual(vat.default_category(0.0, "FI", {"-fi_311"}), "K")
        self.assertEqual(vat.default_category(0.0, "FI", {"+fi_312"}), "AE")

    def test_zero_rated_turnover_names_none(self):
        self.assertIsNone(vat.default_category(0.0, "FI", {"fi_304"}))
        self.assertIsNone(vat.default_category(0.0, "FI", set()))

    def test_two_lines_naming_the_same_category(self):
        self.assertEqual(vat.default_category(0.0, "FI", {"fi_312", "fi_319"}), "AE")

    def test_two_categories_mean_unclassified(self):
        self.assertIsNone(vat.default_category(0.0, "FI", {"fi_311", "fi_312"}))

    def test_a_line_naming_none_does_not_cancel_one_that_names(self):
        self.assertEqual(vat.default_category(0.0, "FI", {"fi_304", "fi_311"}), "K")

    def test_another_countrys_report_is_unclassified(self):
        self.assertIsNone(vat.default_category(0.0, "SE", {"fi_311"}))
        self.assertIsNone(vat.default_category(0.0, None, {"fi_311"}))


@tagged("post_install", "-at_install")
class TestEffectiveCategory(_TestCase):
    def test_set_wins(self):
        self.assertEqual(vat.effective_category("G", 0.0, "FI", {"fi_311"}), ("G", "set"))

    def test_default(self):
        self.assertEqual(vat.effective_category(None, 24.0, "FI", set()), ("S", "default"))
        self.assertEqual(vat.effective_category(False, 0.0, "FI", {"fi_311"}), ("K", "default"))

    def test_unclassified(self):
        self.assertEqual(
            vat.effective_category(None, 0.0, "FI", {"fi_311", "fi_312"}),
            (None, "unclassified"),
        )
        self.assertEqual(vat.effective_category("", 0.0, "FI", {"fi_304"}), (None, "unclassified"))

    def test_categories_outside_the_seven_are_sent_as_set(self):
        for category in ("L", "M", "B"):
            with self.subTest(category=category):
                self.assertEqual(vat.effective_category(category, 0.0, "FI", set()),
                                 (category, "set"))


@tagged("post_install", "-at_install")
class TestCodePairing(_TestCase):
    def test_the_one_standard_codes(self):
        self.assertTrue(vat.code_fits_category("VATEX-EU-AE", "AE"))
        self.assertTrue(vat.code_fits_category("VATEX-EU-IC", "K"))
        self.assertTrue(vat.code_fits_category("VATEX-EU-G", "G"))
        self.assertTrue(vat.code_fits_category("VATEX-EU-O", "O"))

    def test_a_standard_code_of_another_category(self):
        self.assertFalse(vat.code_fits_category("VATEX-EU-IC", "AE"))
        self.assertFalse(vat.code_fits_category("VATEX-EU-AE", "K"))
        self.assertFalse(vat.code_fits_category("VATEX-EU-G", "E"))
        self.assertFalse(vat.code_fits_category("VATEX-EU-132-1A", "AE"))

    def test_exempt_takes_the_article_and_national_codes(self):
        self.assertTrue(vat.code_fits_category("VATEX-EU-132-1A", "E"))
        self.assertTrue(vat.code_fits_category("VATEX-EU-79-C", "E"))
        self.assertTrue(vat.code_fits_category("VATEX-FR-FRANCHISE", "E"))

    def test_export_takes_the_export_article_codes(self):
        # finvoice content T08: a localisation ships 0 % export taxes with G
        # and the directive's article 146 code.
        self.assertTrue(vat.code_fits_category("VATEX-EU-146-1E", "G"))
        self.assertTrue(vat.code_fits_category("VATEX-EU-146-1E", "E"))
        self.assertFalse(vat.code_fits_category("VATEX-EU-146-1E", "AE"))
        self.assertFalse(vat.code_fits_category("VATEX-EU-132-1A", "G"))

    def test_standard_and_zero_rated_take_none(self):
        for category in ("S", "Z"):
            with self.subTest(category=category):
                self.assertFalse(vat.code_fits_category("VATEX-EU-132", category))
                self.assertFalse(vat.code_fits_category("VATEX-EU-AE", category))

    def test_no_code_always_fits(self):
        for category in vat.CATEGORIES + (None,):
            with self.subTest(category=category):
                self.assertTrue(vat.code_fits_category(None, category))
                self.assertTrue(vat.code_fits_category("", category))


@tagged("post_install", "-at_install")
class TestCheckTax(_TestCase):
    def test_consistent(self):
        self.assertEqual(vat.check_tax(24.0, "S", None, None), [])
        self.assertEqual(vat.check_tax(0.0, "AE", "VATEX-EU-AE", "Reverse charge"), [])
        self.assertEqual(vat.check_tax(0.0, None, None, ""), [])
        self.assertEqual(vat.check_tax(24.0, None, None, None), [])

    def test_no_vat_category_on_a_taxed_rate(self):
        for category in vat.NO_VAT_CATEGORIES:
            with self.subTest(category=category):
                problems = vat.check_tax(24.0, category, None, None)
                self.assertEqual(len(problems), 1)
                self.assertIn("above zero", problems[0])

    def test_standard_on_zero(self):
        problems = vat.check_tax(0.0, "S", None, None)
        self.assertEqual(len(problems), 1)
        self.assertIn("0 %", problems[0])

    def test_code_of_another_category(self):
        problems = vat.check_tax(0.0, "AE", "VATEX-EU-IC", None)
        self.assertEqual(len(problems), 1)
        self.assertIn("VATEX-EU-IC", problems[0])

    def test_reason_of_four_lines(self):
        problems = vat.check_tax(0.0, "E", None, "a\nb\nc\nd")
        self.assertEqual(len(problems), 1)
        self.assertIn("3 lines of 70 characters", problems[0])

    def test_reason_line_of_71(self):
        problems = vat.check_tax(0.0, "E", None, "x" * 71)
        self.assertEqual(len(problems), 1)
        self.assertIn("3 lines of 70 characters", problems[0])

    def test_reason_of_three_lines_of_70(self):
        self.assertEqual(vat.check_tax(0.0, "E", None, "\n".join(["x" * 70] * 3)), [])

    def test_categories_outside_the_seven_are_not_refused(self):
        self.assertEqual(vat.check_tax(24.0, "L", None, None), [])
        self.assertEqual(vat.check_tax(0.0, "B", "VATEX-EU-AE", None), [])

    def test_several_problems(self):
        self.assertEqual(len(vat.check_tax(24.0, "AE", "VATEX-EU-G", "y" * 80)), 3)

    def test_code_the_e_invoice_cannot_hold(self):
        """review F3: VatExemptionReasonCode is 1-20 letters, digits or
        hyphens; the ERP lists longer national codes."""
        for code in ("VATEX-FR-298SEXDECIESA", "VATEX-FR-CGI261D-1BIS", "VATEX EU 79"):
            with self.subTest(code=code):
                problems = vat.check_tax(0.0, "E", code, None)
                self.assertEqual(len(problems), 1)
                self.assertIn("20", problems[0])
                self.assertIn(code, problems[0])
        self.assertEqual(vat.check_tax(0.0, "E", "VATEX-EU-132-1B", None), [])
        self.assertEqual(vat.check_tax(0.0, "E", "A" * 20, None), [])


@tagged("post_install", "-at_install")
class TestSendableVat(_TestCase):
    """review F3: what the message can carry of a tax the check skipped."""

    def test_fitting_settings_are_kept(self):
        self.assertEqual(
            vat.sendable_vat(["Own text", "Second"], "VATEX-EU-132-1B", "E"),
            {"reason_lines": ["Own text", "Second"], "exemption_code": "VATEX-EU-132-1B",
             "left_out": []})

    def test_a_code_the_schema_cannot_take_is_left_out(self):
        self.assertEqual(
            vat.sendable_vat(["Own text"], "VATEX-FR-298SEXDECIESA", "E"),
            {"reason_lines": ["Own text"], "exemption_code": None,
             "left_out": ["exemption_code"]})

    def test_at_most_three_lines_of_70(self):
        lines = ["One", "x" * 71, "Two", "Three", "Four"]
        self.assertEqual(
            vat.sendable_vat(lines, None, "E"),
            {"reason_lines": ["One", "Two", "Three"], "exemption_code": None,
             "left_out": ["reason_lines"]})

    def test_nothing_left_means_the_default_wording(self):
        self.assertEqual(
            vat.sendable_vat(["y" * 80], None, "AE"),
            {"reason_lines": list(vat.DEFAULT_REASONS["AE"]), "exemption_code": None,
             "left_out": ["reason_lines"]})

    def test_no_category_carries_nothing(self):
        self.assertEqual(vat.sendable_vat([], None, None),
                         {"reason_lines": [], "exemption_code": None, "left_out": []})


@tagged("post_install", "-at_install")
class TestReasonLines(_TestCase):
    def test_set_reason(self):
        self.assertEqual(vat.reason_lines("Own text\n\n  Second  \n", "AE"),
                         ["Own text", "Second"])

    def test_default_wording_while_empty(self):
        self.assertEqual(vat.reason_lines("", "AE"),
                         ["Käännetty verovelvollisuus", "Reverse charge"])
        self.assertEqual(vat.reason_lines(None, "K"), list(vat.DEFAULT_REASONS["K"]))
        self.assertEqual(vat.reason_lines("  \n ", "G"), list(vat.DEFAULT_REASONS["G"]))

    def test_none_for_standard_or_no_category(self):
        self.assertEqual(vat.reason_lines("Text", "S"), [])
        self.assertEqual(vat.reason_lines("Text", None), [])
        self.assertEqual(vat.reason_lines(None, "S"), [])

    def test_categories_outside_the_seven_use_their_set_text_only(self):
        self.assertEqual(vat.reason_lines("Text", "L"), ["Text"])
        self.assertEqual(vat.reason_lines(None, "L"), [])

    def test_default_text(self):
        self.assertEqual(vat.default_reason_text("AE"),
                         "Käännetty verovelvollisuus\nReverse charge")
        self.assertEqual(vat.default_reason_text("S"), "")
        self.assertEqual(vat.default_reason_text(None), "")


# ---------------------------------------------------------------------------
# finvoice content T02: line resolution and grouping
# ---------------------------------------------------------------------------


def _tax(tax_id, rate, category, percent=True):
    return {"tax_id": tax_id, "rate": rate, "category": category, "percent": percent}


S24 = _tax(1, 24.0, "S")
S14 = _tax(2, 14.0, "S")
K0 = _tax(3, 0.0, "K")
G0 = _tax(4, 0.0, "G")
AE0 = _tax(5, 0.0, "AE")
E0_A = _tax(6, 0.0, "E")
E0_B = _tax(7, 0.0, "E")
FIXED = _tax(8, 2.0, None, percent=False)
UNCLASSIFIED0 = _tax(9, 0.0, None)

REASONS = {
    1: {"reason_lines": [], "exemption_code": None},
    2: {"reason_lines": [], "exemption_code": None},
    3: {"reason_lines": list(vat.DEFAULT_REASONS["K"]), "exemption_code": "VATEX-EU-IC"},
    4: {"reason_lines": list(vat.DEFAULT_REASONS["G"]), "exemption_code": "VATEX-EU-G"},
    5: {"reason_lines": list(vat.DEFAULT_REASONS["AE"]), "exemption_code": "VATEX-EU-AE"},
    6: {"reason_lines": ["Exempt A"], "exemption_code": "VATEX-EU-132-1A"},
    7: {"reason_lines": ["Exempt B"], "exemption_code": "VATEX-EU-132-1B"},
    9: {"reason_lines": [], "exemption_code": None},
}


def _entries_sum(entries, key):
    return round(sum(entry[key] for entry in entries), 2)


@tagged("post_install", "-at_install")
class TestResolveLine(_TestCase):
    def test_one_tax(self):
        self.assertEqual(vat.resolve_line([S24]),
                         {"rate": 24.0, "category": "S", "warning": None})
        self.assertEqual(vat.resolve_line([K0]),
                         {"rate": 0.0, "category": "K", "warning": None})

    def test_unclassified_tax(self):
        self.assertEqual(vat.resolve_line([UNCLASSIFIED0]),
                         {"rate": 0.0, "category": None, "warning": None})

    def test_no_tax(self):
        self.assertEqual(vat.resolve_line([]),
                         {"rate": 0.0, "category": None, "warning": "no_percentage_tax"})

    def test_only_a_fixed_amount_tax(self):
        self.assertEqual(vat.resolve_line([FIXED]),
                         {"rate": 0.0, "category": None, "warning": "no_percentage_tax"})

    def test_mixed_categories_take_the_highest_rate(self):
        self.assertEqual(vat.resolve_line([AE0, S24]),
                         {"rate": 24.0, "category": "S", "warning": "mixed_categories"})

    def test_rate_is_the_sum_of_percentage_taxes(self):
        self.assertEqual(vat.resolve_line([S24, S14, FIXED]),
                         {"rate": 38.0, "category": "S", "warning": None})

    def test_tie_of_different_categories(self):
        self.assertEqual(vat.resolve_line([K0, G0]),
                         {"rate": 0.0, "category": None, "warning": "tied_categories"})

    def test_two_taxes_of_one_category_are_no_tie(self):
        self.assertEqual(vat.resolve_line([E0_A, E0_B]),
                         {"rate": 0.0, "category": "E", "warning": None})


@tagged("post_install", "-at_install")
class TestGroupBreakdown(_TestCase):
    def test_ac02_three_entries_never_merged_at_zero(self):
        lines = [
            {"untaxed": 100.0, "taxes": [S24]},
            {"untaxed": 50.0, "taxes": [S24]},
            {"untaxed": 120.0, "taxes": [K0]},
            {"untaxed": 80.0, "taxes": [G0]},
        ]
        entries = vat.group_breakdown(lines, {1: 36.0}, REASONS)
        self.assertEqual([(e["percent"], e["category"]) for e in entries],
                         [(24.0, "S"), (0.0, "K"), (0.0, "G")])
        self.assertEqual([e["base"] for e in entries], [150.0, 120.0, 80.0])
        self.assertEqual([e["amount"] for e in entries], [36.0, 0.0, 0.0])
        self.assertEqual(_entries_sum(entries, "base"), 350.0)
        self.assertEqual(_entries_sum(entries, "amount"), 36.0)
        self.assertEqual(entries[0]["reason_lines"], [])
        self.assertIsNone(entries[0]["exemption_code"])
        self.assertEqual(entries[1]["reason_lines"], list(vat.DEFAULT_REASONS["K"]))
        self.assertEqual(entries[1]["exemption_code"], "VATEX-EU-IC")
        self.assertEqual(entries[2]["exemption_code"], "VATEX-EU-G")
        self.assertEqual([e["tax_ids"] for e in entries], [[1], [3], [4]])
        self.assertEqual([e["warning"] for e in entries], [None, None, None])

    def test_lines_without_a_category_go_in_the_zero_entry_without_one(self):
        lines = [
            {"untaxed": 10.0, "taxes": []},
            {"untaxed": 20.0, "taxes": [FIXED]},
            {"untaxed": 30.0, "taxes": [UNCLASSIFIED0]},
            {"untaxed": 40.0, "taxes": [K0, G0]},
            {"untaxed": 50.0, "taxes": [K0]},
        ]
        entries = vat.group_breakdown(lines, {}, REASONS)
        self.assertEqual([(e["percent"], e["category"], e["base"]) for e in entries],
                         [(0.0, None, 100.0), (0.0, "K", 50.0)])
        self.assertEqual(entries[0]["reason_lines"], [])
        self.assertIsNone(entries[0]["exemption_code"])
        self.assertEqual(_entries_sum(entries, "base"), 150.0)

    def test_mixed_line_goes_with_the_highest_rate(self):
        lines = [{"untaxed": 100.0, "taxes": [S24, AE0]}, {"untaxed": 10.0, "taxes": [AE0]}]
        entries = vat.group_breakdown(lines, {1: 24.0}, REASONS)
        self.assertEqual([(e["percent"], e["category"], e["base"], e["amount"])
                          for e in entries], [(24.0, "S", 100.0, 24.0), (0.0, "AE", 10.0, 0.0)])

    def test_shared_reason_takes_the_largest_base(self):
        lines = [
            {"untaxed": 100.0, "taxes": [E0_A]},
            {"untaxed": 300.0, "taxes": [E0_B]},
            {"untaxed": 50.0, "taxes": [E0_A]},
        ]
        entries = vat.group_breakdown(lines, {}, REASONS)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual((entry["percent"], entry["category"], entry["base"]), (0.0, "E", 450.0))
        self.assertEqual(entry["reason_lines"], ["Exempt B"])
        self.assertEqual(entry["exemption_code"], "VATEX-EU-132-1B")
        self.assertEqual(entry["warning"], "shared_reason")
        self.assertEqual(entry["tax_ids"], [6, 7])

    def test_same_reason_is_no_warning(self):
        reasons = {**REASONS, 7: REASONS[6]}
        lines = [{"untaxed": 100.0, "taxes": [E0_A]}, {"untaxed": 300.0, "taxes": [E0_B]}]
        self.assertIsNone(vat.group_breakdown(lines, {}, reasons)[0]["warning"])

    def test_tax_amount_split_over_entries_by_base(self):
        # 24 % alone on one line and with 14 % on another: two entries (24, 38).
        lines = [{"untaxed": 100.0, "taxes": [S24]}, {"untaxed": 300.0, "taxes": [S24, S14]}]
        entries = vat.group_breakdown(lines, {1: 96.0, 2: 42.0}, REASONS)
        self.assertEqual([(e["percent"], e["base"], e["amount"]) for e in entries],
                         [(24.0, 100.0, 24.0), (38.0, 300.0, 114.0)])
        self.assertEqual(_entries_sum(entries, "amount"), 138.0)

    def test_split_keeps_the_cents(self):
        lines = [{"untaxed": 10.0, "taxes": [S24]}, {"untaxed": 10.0, "taxes": [S24, S14]},
                 {"untaxed": 10.0, "taxes": [S24, K0]}]
        entries = vat.group_breakdown(lines, {1: 7.21, 2: 1.4}, REASONS)
        self.assertEqual(_entries_sum(entries, "amount"), 8.61)

    def test_bases_sum_to_the_untaxed_total(self):
        lines = [{"untaxed": 0.1, "taxes": [S24]} for _i in range(10)]
        lines += [{"untaxed": 0.2, "taxes": [K0]} for _i in range(10)]
        entries = vat.group_breakdown(lines, {1: 0.24}, REASONS)
        self.assertEqual(_entries_sum(entries, "base"), 3.0)
        self.assertEqual([e["base"] for e in entries], [1.0, 2.0])


if __name__ == "__main__":
    unittest.main()
