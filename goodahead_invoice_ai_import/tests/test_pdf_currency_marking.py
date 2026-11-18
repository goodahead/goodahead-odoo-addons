# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Decide from the PDF's text layer, not the model, whether a page prints money.

The model's `currency_symbol` answer can differ between runs on identical
bytes, so the text layer decides whenever it can be read. No test
here reaches the Claude API: the lib tests use a stub client and the
TransactionCase installs `_NoClaudeMixin`. All PDFs are built in-process.
"""

import io
import json
from unittest.mock import patch

from odoo.tests import BaseCase, TransactionCase, tagged

from odoo.addons.goodahead_invoice_ai_import.lib import pdf_currency
from odoo.addons.goodahead_invoice_ai_import.lib.claude_client import (
    CURRENCY_INFERRED_KEY,
    CURRENCY_MARKING_KEY,
    CURRENCY_SYMBOL_KEY,
    ClaudeExtractionError,
    ClaudeInvoiceExtractor,
    currency_marking_verdict,
    nothing_was_printed,
)
from odoo.addons.goodahead_invoice_ai_import.lib.ubl_generator import (
    generate_peppol_invoice_xml,
)

from .test_currency_from_country import (
    _NoClaudeMixin,
    _StubClient,
    _always_active,
    _body,
    _fake_lookup,
    _model_inferred_extraction,
)

# The routing line of the sample freight bill: `ILS` (also the ISO code of the
# Israeli new shekel) beside a round number, with no amount word before it.
_ROUTING_LINE = "*ZZX  RZONE01/05/26       ILS 40318"

# Charge block of an invented US freight bill (carrier "ZZX"), laid out the way
# such bills print their text layer. Every amount is bare; the only ISO-like
# token is `ILS` in a routing field (see
# `test_the_naive_rule_would_have_broken_the_fix`). The figures add up:
# 3,748.50 less 88% discount, plus fuel surcharge and fees, is 600.07.
_FREIGHT_CHARGE_BLOCK = [
    "ORIGINAL INVOICE",
    "PAGE 1  OF  2",
    "ZZXE     PRIORITY",
    "Freight Bill Number 555000000101",
    "Ship Date / Invoice Date 01/27/2026 / 02/03/2026",
    "Bill of Lading Number ZZ00000-00001",
    "Terms PREPAID",
    "Origin / Destination NWK / TMP",
    "Total Amount Due 600.07",
    "Payment Due Date 03/05/2026",
    "PIECES PALLETS HM DESCRIPTION WT(LBS) NMFC CLASS RATE TOTAL CHARGES",
    "4 REFRIGERATOR / FREEZER 180 360 1874.25 3,748.50",
    "CARRIER REST API",
    "APPOINTMENT DELIVERY **",
    _ROUTING_LINE,
    "UPDATE PER INSPECTION",
    "0000088 CUBIC FEET",
    "012775 FUEL SURCHG LTL SHPT 28.40% 127.75",
    "880  LESS DISCOUNT .880 3,298.68-",
    "0700 NOTIFY CHARGE 7.00",
    "0001550 WEIGHT VALIDATION FEE 15.50",
    "Rate Tariff: ZZ-000000-100-000",
    "PLEASE PAY THIS AMOUNT",
    "600.07",
    "555000000101 000000060007 02032026 1",
]

# One money-bearing excerpt per invented sample document, with the expected
# markings. Each one reproduces a layout that the rules have to get right: a
# bare amount beside a `$` one, `CHECKS IN USD:` with no amount, a `$` fee in
# running text, a percentage beside a `$`, and two pages with no marking at all.
_SAMPLE_DOCUMENTS = (
    (
        "truck rental invoice",
        "Total charges           312.64             $312.64",
        ["$"],
    ),
    (
        "cash sale receipt",
        "Amount Paid:    \n$248.90\nCHECKS IN USD: \nSubtotal: $229.10",
        ["$", "USD"],
    ),
    (
        "equipment supplier invoice",
        "Sale Amount 641.20\nFreight 0.00\nTax 0.00\nTotal $641.20",
        ["$"],
    ),
    (
        "manufacturer invoice",
        "DELIVERY  Freight Charge1 85.00 85.00\n$432.18\n$432.18",
        ["$"],
    ),
    (
        "restaurant equipment invoice",
        "*A $35.00 FEE APPLIES TO ANY RETURNED CHECK\n$517.46",
        ["$"],
    ),
    (
        "truck leasing invoice",
        "TOTAL\n$41.70 \nAdmin Fee (20%) - - $6.95",
        ["$"],
    ),
    (
        "fuel card statement",
        "ACCOUNT NUMBER CREDIT LIMIT DAYS THIS PERIOD BILL CLOSING DATE "
        "PAYMENT DUE DATE** AMOUNT DUE\n"
        "0000-00-000000-0 7500.00 14 OCT-02-2026 OCT-16-2026 3184.62\n"
        "3090.15 3090.15 3186.92 2.30 3184.62",
        [],
    ),
    (
        "freight carrier bill",
        "\n".join(_FREIGHT_CHARGE_BLOCK),
        [],
    ),
)


def _pdf_of(lines):
    """A one-page PDF with a real text layer, built in-process with reportlab.

    `$`, `€`, `£` and `¥` survive the WinAnsiEncoding round trip intact.
    """
    from reportlab.lib.pagesizes import letter  # noqa: PLC0415
    from reportlab.pdfgen import canvas  # noqa: PLC0415

    buf = io.BytesIO()
    page = canvas.Canvas(buf, pagesize=letter)
    y = 740
    for line in lines:
        page.drawString(36, y, line)
        y -= 12
    page.showPage()
    page.save()
    return buf.getvalue()


def _freight_pdf():
    return _pdf_of(_FREIGHT_CHARGE_BLOCK)


# Long enough to pass `MIN_TEXT_LAYER_ALNUM` and free of money, so any marking
# found on these pages comes from the annotation or the form field.
_PLAIN_BODY = [
    "ACME SUPPLY COMPANY",
    "Invoice number 44812 dated 03 February 2026",
    "Remit to PO Box 1180 Trenton New Jersey 08650",
    "Terms net thirty days from the date of this invoice",
    "Line one commercial refrigeration unit model RX220 quantity one",
    "Delivery scheduled for the fourteenth of February 2026",
]


def _pdf_escape(text):
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _pdf_with_annotations(
    lines,
    annotation=None,
    field_value=None,
    annot_flags=4,
    default_value=None,
    appearance_only=None,
    value_in_a_stream=False,
    filler_annotations=0,
):
    """A page whose currency is in an annotation or an AcroForm field.

    Built from raw PDF objects rather than reportlab, so the result is the same
    under pypdf and PyPDF2. `annotation` goes into a `/FreeText` `/Contents`;
    `field_value` into a `/Widget` `/V` listed in the AcroForm `/Fields`. The
    `/FreeText` also carries an `/AP` drawing the same string, which the check
    does not read; `appearance_only` builds an annotation with only the `/AP`.

    * `annot_flags` — `/F`; bit 2 (value 2) is Hidden, bit 6 (32) is NoView.
    * `default_value` — `/DV`, beside or instead of `/V`.
    * `value_in_a_stream` — `/V` as an indirect stream object.
    * `filler_annotations` — money-free annotations placed ahead of the real
      one (all references to one object), to test `MAX_ANNOTATIONS_PER_PAGE`.
    """
    body = ["BT", "/F1 11 Tf", "36 740 Td", "12 TL"]
    for line in lines:
        body.append("(%s) Tj T*" % _pdf_escape(line))
    body.append("ET")
    content = "\n".join(body).encode("latin-1")

    drawn = annotation if annotation is not None else appearance_only
    has_field = field_value is not None or default_value is not None
    annots = [10] * filler_annotations
    if drawn is not None:
        annots.append(6)
    if has_field:
        annots.append(7)

    objects = {
        1: (
            "<</Type /Catalog /Pages 2 0 R %s>>"
            % ("/AcroForm <</Fields [7 0 R]>>" if has_field else "")
        ).encode("latin-1"),
        2: b"<</Type /Pages /Kids [3 0 R] /Count 1>>",
        3: (
            "<</Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            "/Resources <</Font <</F1 5 0 R>>>> /Contents 4 0 R %s>>"
            % (
                "/Annots [%s]" % " ".join("%d 0 R" % n for n in annots)
                if annots
                else ""
            )
        ).encode("latin-1"),
        4: b"<</Length %d>>\nstream\n%s\nendstream" % (len(content), content),
        5: b"<</Type /Font /Subtype /Type1 /BaseFont /Helvetica>>",
    }
    if drawn is not None:
        appearance = (
            "BT /F1 11 Tf 2 6 Td (%s) Tj ET" % _pdf_escape(drawn)
        ).encode("latin-1")
        objects[6] = (
            "<</Type /Annot /Subtype /FreeText /Rect [300 600 500 620] /F %d "
            "%s/DA (/Helv 11 Tf 0 g) /AP <</N 8 0 R>>>>"
            % (
                annot_flags,
                "/Contents (%s) " % _pdf_escape(annotation)
                if annotation is not None
                else "",
            )
        ).encode("latin-1")
        objects[8] = (
            b"<</Type /XObject /Subtype /Form /BBox [0 0 200 20] "
            b"/Resources <</Font <</F1 5 0 R>>>> /Length %d>>\nstream\n%s\nendstream"
            % (len(appearance), appearance)
        )
    if has_field:
        pieces = ["<</Type /Annot /Subtype /Widget /FT /Tx /T (total)"]
        if field_value is not None:
            pieces.append(
                "/V 9 0 R"
                if value_in_a_stream
                else "/V (%s)" % _pdf_escape(field_value)
            )
        if default_value is not None:
            pieces.append("/DV (%s)" % _pdf_escape(default_value))
        pieces.append("/Rect [300 560 500 580] /F 4 /DA (/Helv 11 Tf 0 g)>>")
        objects[7] = " ".join(pieces).encode("latin-1")
        if value_in_a_stream and field_value is not None:
            raw = field_value.encode("latin-1")
            objects[9] = b"<</Length %d>>\nstream\n%s\nendstream" % (
                len(raw),
                raw,
            )
    if filler_annotations:
        objects[10] = (
            b"<</Type /Annot /Subtype /FreeText /Rect [10 10 60 20] /F 4 "
            b"/Contents (page 1 of 1) /DA (/Helv 11 Tf 0 g)>>"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + objects[num] + b"\nendobj\n"
    top = max(objects) + 1
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % top
    for num in range(1, top):
        out += (
            b"%010d 00000 n \n" % offsets[num]
            if num in offsets
            else b"0000000000 65535 f \n"
        )
    out += b"trailer\n<</Size %d /Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (
        top,
        xref_at,
    )
    return bytes(out)


def _pdf_of_alnum(count):
    """A page whose text layer has EXACTLY `count` alphanumeric characters and
    no currency anywhere in it."""
    line = "ABCDEFGHIJ"
    lines = [line] * (count // len(line))
    remainder = count % len(line)
    if remainder:
        lines.append(line[:remainder])
    return _pdf_of(lines)


def _scanned_pdf():
    """A page with ink and no text, as a scanner produces; its verdict is UNKNOWN."""
    from reportlab.lib.pagesizes import letter  # noqa: PLC0415
    from reportlab.pdfgen import canvas  # noqa: PLC0415

    buf = io.BytesIO()
    page = canvas.Canvas(buf, pagesize=letter)
    page.rect(40, 40, 400, 600, stroke=1, fill=0)
    page.line(60, 600, 420, 600)
    page.showPage()
    page.save()
    return buf.getvalue()


@tagged("post_install", "-at_install")
class TestWhatCountsAsACurrencyMarking(BaseCase):
    """The regex half: pure text in, markings out. No PDF, no database."""

    def test_the_sample_documents(self):
        """Six sample documents print a currency; the freight bill and the fuel
        statement print none."""
        for name, excerpt, expected in _SAMPLE_DOCUMENTS:
            with self.subTest(document=name):
                self.assertEqual(
                    pdf_currency.currency_markings_in_text(excerpt),
                    expected,
                    name,
                )

    def test_the_naive_rule_would_have_broken_the_fix(self):
        """A bare ISO code needs an amount shape: `ILS 40318` in a routing field
        is not a marking."""
        text = "\n".join(_FREIGHT_CHARGE_BLOCK)
        self.assertIn("ILS", text, "precondition: the trap is on the page")
        self.assertEqual(
            pdf_currency.currency_markings_in_text(text),
            [],
            "ILS in a routing field is not a currency marking",
        )

    def test_a_code_against_an_amount_is_a_marking(self):
        for text in (
            "Total USD 600.07",
            "Total 1,234.56 CAD",
            "Summe EUR 1.234,56",
            "Yhteensä 1 234,56 EUR",
        ):
            with self.subTest(text=text):
                self.assertTrue(
                    pdf_currency.currency_markings_in_text(text), text
                )

    def test_a_code_after_a_currency_word_is_a_marking(self):
        """`CHECKS IN USD:` with no amount on the line is still a marking."""
        for text in (
            "CHECKS IN USD: ",
            "Currency: GBP",
            "All amounts are in SEK",
            "Payable in CHF",
        ):
            with self.subTest(text=text):
                self.assertTrue(
                    pdf_currency.currency_markings_in_text(text), text
                )

    def test_the_false_positives_thought_about_first(self):
        """Common false positives: `$` in a part number, `kr` inside words,
        code-like abbreviations and address lines."""
        for text in (
            "Item ZZQ-7710$K Countertop Clear Safety Shield",  # $ in a part no.
            "Levereras i en kruka, kräver bevattning",  # kr inside words
            "TOP FLOOR, 15 MAD AVENUE, SUITE 400",  # TOP and MAD, no money
            "Send payment to: P O BOX 900118 EXAMPLETON OH 45999-0118",
            "I/L PRO Number",  # PRO, LBS, NMFC, ZZX: none of them codes
            "Origin / Destination NWK / TMP",
            # Carries an amount, so the `kr` letter guard is actually reached.
            "Levereras i en kruka for 1 500,00 kruka",
            "Kr. Johansson, Storgatan 14",  # `Kr.` as an initial, no amount
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    pdf_currency.currency_markings_in_text(text), [], text
                )

    def test_the_krona_guard_can_fail(self):
        """Control for the row above: the same sentence with a real `kr`."""
        self.assertEqual(
            pdf_currency.currency_markings_in_text(
                "Levereras i en kruka for 1 500,00 kr"
            ),
            ["kr"],
        )

    def test_what_it_over_reports_on_purpose(self):
        """Known over-reports, kept on purpose.

        Missing a printed marking is the more dangerous error, so `ALL`
        (Albanian lek), `CUP` after `total` and `元` without a letter guard are
        all read as markings.
        """
        for text, expected in (
            ("TOTAL ALL 1,234.56", ["ALL"]),
            ("Total CUP 50", ["CUP"]),
            ("公元 2026,00 年", ["元"]),
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    pdf_currency.currency_markings_in_text(text), expected, text
                )

    def test_what_it_under_reports_on_purpose(self):
        """The deliberate under-report: a bare code against a round amount with
        no announcing word, which is the shape of `ILS 40318`."""
        self.assertEqual(
            pdf_currency.currency_markings_in_text("CAD 500"),
            [],
            "the residual: a bare code, a gap, an integer, nothing else",
        )
        self.assertEqual(
            pdf_currency.currency_markings_in_text("Total: CAD 500"),
            ["CAD"],
            "an announcing word makes a round amount count",
        )
        self.assertEqual(
            pdf_currency.currency_markings_in_text(_ROUTING_LINE),
            [],
            "control: the routing-field shape stays unmatched",
        )

    def test_the_symbols(self):
        """Every symbol on the list is matched, with and without a nearby amount."""
        self.assertEqual(
            len(set(pdf_currency.UNAMBIGUOUS_SYMBOLS)),
            len(pdf_currency.UNAMBIGUOUS_SYMBOLS),
            "the list has a duplicate in it",
        )
        # The list size is quoted in comments elsewhere; pin it so it cannot drift.
        self.assertEqual(
            len(pdf_currency.ISO_4217_CURRENT),
            153,
            "the comments on `_CODE` and the still-open-hole docstring both "
            "quote this figure",
        )
        for symbol in pdf_currency.UNAMBIGUOUS_SYMBOLS:
            with self.subTest(symbol=symbol, codepoint="U+%04X" % ord(symbol)):
                self.assertEqual(
                    pdf_currency.currency_markings_in_text(
                        "Total %s 1.234,00" % symbol
                    ),
                    [symbol],
                )
                # No adjacency requirement, which is the documented decision —
                # so it has to hold with no amount anywhere near it.
                self.assertEqual(
                    pdf_currency.currency_markings_in_text("a %s b" % symbol),
                    [symbol],
                )

    def test_every_announcing_word(self):
        """Every alternative of the two announcing-word regexes is matched."""
        for phrase in (
            "Currency: GBP",
            "Currencies: GBP",
            "Denominated in GBP",
            "Payable in GBP",
            "Paid in GBP",
            "Billed in GBP",
            "Invoiced in GBP",
            "Quoted in GBP",
            "Amounts are in GBP",
            "Amount in GBP",
            "Totals in GBP",
            "CHECKS IN GBP:",
            "Cheques in GBP",
            "Funds in GBP",
            "Remit in GBP",
            # Amount words: these need a number after the code (see the next test).
            "Total GBP 500",
            "Subtotal GBP 500",
            "Balance GBP 500",
            "Amount due GBP 500",
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(
                    pdf_currency.currency_markings_in_text(phrase), ["GBP"], phrase
                )

    def test_an_amount_word_needs_a_number_and_a_currency_word_does_not(self):
        """Amount words (`total`, `balance`, ...) need a number after the code;
        currency words (`payable in`, `checks in`, ...) do not."""
        for text in ("Totals TRY", "Grand total SAR", "subtotals NOK"):
            with self.subTest(text=text, rule="amount word, no number"):
                self.assertEqual(
                    pdf_currency.currency_markings_in_text(text), [], text
                )
        # Controls: currency words need no number, amount words match with one.
        for text, expected in (
            ("Currency: GBP", ["GBP"]),
            ("CHECKS IN USD: ", ["USD"]),
            ("All amounts are in SEK", ["SEK"]),
            ("Total: CAD 500", ["CAD"]),
            ("TOTAL ALL 1,234.56", ["ALL"]),
            # Known over-reports: any number after the code is enough.
            ("Balance PEN 12", ["PEN"]),
            ("Amount due TOP 3", ["TOP"]),
        ):
            with self.subTest(text=text, rule="control"):
                self.assertEqual(
                    pdf_currency.currency_markings_in_text(text), expected, text
                )

    def test_printed_currencies_in_unusual_formats(self):
        """Printed currencies that must not be missed: fullwidth and small dollar
        signs, Swiss apostrophes, narrow and thin spaces, round kronor."""
        for text, expected in (
            ("TOTAL ＄1,234.00", ["＄"]),  # U+FF04 fullwidth dollar
            ("TOTAL ﹩1,234.00", ["﹩"]),  # U+FE69 small dollar
            ("Total CHF 1'234.55", ["CHF"]),  # Swiss apostrophe, leading code
            ("Total 1'234.55 CHF", ["CHF"]),  # … and trailing code
            ("Total CHF 1’234.55", ["CHF"]),  # U+2019, the typographic twin
            ("Total EUR 1 234,56", ["EUR"]),  # U+202F narrow no-break
            ("Total EUR 1 234,56", ["EUR"]),  # U+2009 thin space
            ("Total EUR 1 234,56", ["EUR"]),  # U+00A0 no-break space
            ("Summa 1 500 kr", ["kr"]),  # round kronor are ordinary
            ("Summa 1 234,00 kr", ["kr"]),  # … and so are decimal ones
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    pdf_currency.currency_markings_in_text(text), expected, text
                )

    def test_a_currency_sign_tight_after_an_amount(self):
        """A currency sign directly after a two-decimal amount is matched.

        CJK ideographs are word characters, so a trailing `\\b` after the
        amount cannot hold before `元` or `円` in tight typesetting.
        """
        for text, expected in (
            ("1,234.00元", ["元"]),
            ("合计 1,234.00元", ["元"]),
            ("1,234.00元整", ["元"]),
            ("1234.00円", ["円"]),
            # The spaced form.
            ("1,234.00 元", ["元"]),
            # The equivalent tight `kr` form.
            ("1 234,00kr", ["kr"]),
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    pdf_currency.currency_markings_in_text(text), expected, text
                )

    def test_the_hole_that_is_still_open(self):
        """Known residuals: currencies spelled out as words, and `元` after an
        amount without decimals, are not matched."""
        for text in (
            "All amounts are in US Dollars",
            "Betrag in Euro: 1.234,56",
            "Summa 1 500 kronor",
            "1234元整",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    pdf_currency.currency_markings_in_text(text), [], text
                )

    def test_the_order_is_rule_order_and_not_document_order(self):
        """Markings are returned in rule order, not document order."""
        self.assertEqual(
            pdf_currency.currency_markings_in_text(
                "Paid USD 100.00 then a fee of 5,00 EUR and 3,00 kr"
            ),
            ["kr", "USD", "EUR"],
        )
        self.assertEqual(
            pdf_currency.currency_markings_in_text("USD 100.00 ... later ... 5,00 €"),
            ["€", "USD"],
        )

    def test_the_dollar_needs_a_number_and_the_krona_needs_one_too(self):
        self.assertEqual(pdf_currency.currency_markings_in_text("$1,234.00"), ["$"])
        self.assertEqual(pdf_currency.currency_markings_in_text("1 234,00 $"), ["$"])
        self.assertEqual(
            pdf_currency.currency_markings_in_text("Summa 1 234,00 kr"), ["kr"]
        )
        self.assertEqual(
            pdf_currency.currency_markings_in_text("总计 1,234.00 元"),
            ["元"],
        )

    def test_nothing_is_read_out_of_nothing(self):
        for junk in (None, "", 0, [], {}):
            with self.subTest(junk=junk):
                self.assertEqual(pdf_currency.currency_markings_in_text(junk), [])


@tagged("post_install", "-at_install")
class TestTheTextLayerVerdict(BaseCase):
    """The bytes half: a real PDF through `odoo.tools.pdf`."""

    def test_the_freight_bill_prints_nothing(self):
        verdict = pdf_currency.scan_pdf_for_currency(_freight_pdf())
        self.assertEqual(verdict["verdict"], pdf_currency.NOTHING_PRINTED)
        self.assertEqual(verdict["markings"], [])

    def test_a_marked_page_is_seen_as_marked(self):
        """Control: a page that prints `$` is reported as printed."""
        verdict = pdf_currency.scan_pdf_for_currency(
            _pdf_of(_FREIGHT_CHARGE_BLOCK + ["Total Amount Due $600.07"])
        )
        self.assertEqual(verdict["verdict"], pdf_currency.PRINTED)
        self.assertEqual(verdict["markings"], ["$"])

    def test_a_scan_is_unknown_and_not_nothing(self):
        """A page without a text layer is UNKNOWN, not nothing printed."""
        verdict = pdf_currency.scan_pdf_for_currency(_scanned_pdf())
        self.assertEqual(verdict["verdict"], pdf_currency.UNKNOWN)

    def test_an_unreadable_file_is_unknown(self):
        for junk in (b"", b"%PDF", b"not a pdf at all", None):
            with self.subTest(junk=junk):
                self.assertEqual(
                    pdf_currency.scan_pdf_for_currency(junk)["verdict"],
                    pdf_currency.UNKNOWN,
                )

    def test_the_threshold_is_about_a_text_layer_not_a_blank_page(self):
        """A page carrying only a stamp-sized text layer reads as UNKNOWN."""
        self.assertEqual(
            pdf_currency.scan_pdf_for_currency(_pdf_of(["Page 1 of 2"]))["verdict"],
            pdf_currency.UNKNOWN,
        )

    def test_a_currency_in_an_annotation_is_printed(self):
        """A currency drawn by a `/FreeText` annotation is found; `extract_text()`
        alone never reads `/Annots`."""
        pdf = _pdf_with_annotations(_PLAIN_BODY, annotation="$1,234.00")
        verdict = pdf_currency.scan_pdf_for_currency(pdf)
        self.assertEqual(verdict["verdict"], pdf_currency.PRINTED)
        self.assertEqual(verdict["markings"], ["$"])

    def test_a_currency_in_an_acroform_field_is_printed(self):
        """A currency typed into an AcroForm field (`/V`) is found."""
        pdf = _pdf_with_annotations(_PLAIN_BODY, field_value="$1,234.00")
        verdict = pdf_currency.scan_pdf_for_currency(pdf)
        self.assertEqual(verdict["verdict"], pdf_currency.PRINTED)
        self.assertEqual(verdict["markings"], ["$"])

    def test_the_annotation_sweep_can_still_answer_nothing(self):
        """Control: an annotation without money still yields `nothing_printed`."""
        pdf = _pdf_with_annotations(
            _PLAIN_BODY, annotation="Received at the dock on 14 February"
        )
        verdict = pdf_currency.scan_pdf_for_currency(pdf)
        self.assertEqual(verdict["verdict"], pdf_currency.NOTHING_PRINTED)
        self.assertEqual(verdict["markings"], [])

    def test_an_annotation_does_not_buy_a_text_layer(self):
        """Annotation text does not count toward the text-layer threshold, but a
        money-bearing field is still evidence of a printed currency."""
        pdf = _pdf_with_annotations(
            ["Page 1 of 2"], field_value="Received 14 February"
        )
        verdict = pdf_currency.scan_pdf_for_currency(pdf)
        self.assertEqual(verdict["verdict"], pdf_currency.UNKNOWN)
        self.assertEqual(verdict["alnum"], 8, "the content stream alone")

        money = pdf_currency.scan_pdf_for_currency(
            _pdf_with_annotations(["Page 1 of 2"], field_value="$1,234.00")
        )
        self.assertEqual(
            money["verdict"],
            pdf_currency.PRINTED,
            "a money-bearing field is evidence on its own; the threshold "
            "guards the NEGATIVE answer",
        )
        self.assertEqual(money["markings"], ["$"])
        self.assertEqual(
            money["alnum"],
            8,
            "and it still bought no text layer — `alnum` is the content "
            "stream and nothing else",
        )

    def test_a_hidden_annotation_is_not_printed(self):
        """A Hidden annotation (`/F` bit 2) is not counted."""
        hidden = pdf_currency.scan_pdf_for_currency(
            _pdf_with_annotations(_PLAIN_BODY, annotation="$1,234.00", annot_flags=2)
        )
        self.assertEqual(hidden["verdict"], pdf_currency.NOTHING_PRINTED)
        self.assertEqual(hidden["markings"], [])

        # NoView (bit 6) only hides the annotation on screen; it still prints.
        noview = pdf_currency.scan_pdf_for_currency(
            _pdf_with_annotations(_PLAIN_BODY, annotation="$1,234.00", annot_flags=32)
        )
        self.assertEqual(noview["verdict"], pdf_currency.PRINTED)
        self.assertEqual(noview["markings"], ["$"])

    def test_a_default_value_loses_to_the_value_that_is_shown(self):
        """`/DV` is ignored when `/V` is present, since the field displays `/V`."""
        both = pdf_currency.scan_pdf_for_currency(
            _pdf_with_annotations(
                _PLAIN_BODY, field_value="0.00", default_value="$1,234.00"
            )
        )
        self.assertEqual(both["verdict"], pdf_currency.NOTHING_PRINTED)
        self.assertEqual(both["markings"], [])

        # With no `/V` the field displays `/DV`, so it counts.
        default_only = pdf_currency.scan_pdf_for_currency(
            _pdf_with_annotations(_PLAIN_BODY, default_value="$1,234.00")
        )
        self.assertEqual(default_only["verdict"], pdf_currency.PRINTED)
        self.assertEqual(default_only["markings"], ["$"])

    def test_a_field_value_held_in_a_stream_is_read(self):
        """A `/V` stored as an indirect stream object is decoded and read."""
        verdict = pdf_currency.scan_pdf_for_currency(
            _pdf_with_annotations(
                _PLAIN_BODY, field_value="$1,234.00", value_in_a_stream=True
            )
        )
        self.assertEqual(verdict["verdict"], pdf_currency.PRINTED)
        self.assertEqual(verdict["markings"], ["$"])

    def test_the_annotation_hole_that_is_still_open(self):
        """Known residual: money carried only in an `/AP` appearance stream is
        not read."""
        verdict = pdf_currency.scan_pdf_for_currency(
            _pdf_with_annotations(_PLAIN_BODY, appearance_only="PAID $1,234.00")
        )
        self.assertEqual(verdict["verdict"], pdf_currency.NOTHING_PRINTED)
        self.assertEqual(verdict["markings"], [])

    def test_the_annotation_surface_is_bounded_in_both_directions(self):
        """Both annotation caps hold exactly where they claim; a currency past
        either cap is not seen."""
        cap = pdf_currency.MAX_ANNOTATIONS_PER_PAGE
        past = pdf_currency.scan_pdf_for_currency(
            _pdf_with_annotations(
                _PLAIN_BODY, annotation="$1,234.00", filler_annotations=cap
            )
        )
        self.assertEqual(
            (past["verdict"], past["markings"]),
            (pdf_currency.NOTHING_PRINTED, []),
            "the %dth annotation is past the cap and is not looked at" % (cap + 1),
        )
        # Control: one filler fewer and the annotation is inside the cap.
        inside = pdf_currency.scan_pdf_for_currency(
            _pdf_with_annotations(
                _PLAIN_BODY, annotation="$1,234.00", filler_annotations=cap - 1
            )
        )
        self.assertEqual(
            (inside["verdict"], inside["markings"]),
            (pdf_currency.PRINTED, ["$"]),
            "the last annotation inside the cap is still read",
        )

        chars = pdf_currency.MAX_ANNOTATION_CHARS
        self.assertEqual(
            len(pdf_currency._annotation_string("x" * (chars + 1000))),
            chars,
            "a long value is truncated rather than scanned whole",
        )
        self.assertEqual(
            pdf_currency.currency_markings_in_text(
                pdf_currency._annotation_string("x" * chars + "$1,234.00")
            ),
            [],
            "and a currency past the character cap is lost — the cap is a "
            "decision to stop looking, named here rather than discovered",
        )
        # Control: the cap truncates the tail, so a leading currency is read.
        self.assertEqual(
            pdf_currency.currency_markings_in_text(
                pdf_currency._annotation_string("$1,234.00" + "x" * chars)
            ),
            ["$"],
        )

    def test_an_annotation_string_decodes_by_its_bom(self):
        """Annotation strings are decoded as UTF-16 only when they carry a BOM;
        BOM-less UTF-16 would garble ordinary even-length byte strings."""
        for raw, expected in (
            (b"$1,234.00", "$1,234.00"),
            (b"$1,234.00 ", "$1,234.00 "),
            (b"Total $1,234.00 ", "Total $1,234.00 "),
            # The BOM case.
            (b"\xfe\xff\x00$\x001", "$1"),
        ):
            with self.subTest(raw=raw):
                text = pdf_currency._annotation_string(raw)
                self.assertEqual(text, expected)
                self.assertEqual(
                    pdf_currency.currency_markings_in_text(text), ["$"]
                )

    def test_a_short_page_that_prints_a_currency_is_printed(self):
        """A page under the threshold that prints a currency is still `printed`:
        a found marking needs no quorum."""
        short = ["CREDIT NOTE CN-2026-14", "Total EUR 1.234,00", "Paid in full"]
        self.assertEqual(
            pdf_currency.currency_markings_in_text("\n".join(short)),
            ["EUR"],
            "precondition: the answer is in the text",
        )
        verdict = pdf_currency.scan_pdf_for_currency(_pdf_of(short))
        self.assertEqual(verdict["verdict"], pdf_currency.PRINTED)
        self.assertEqual(verdict["markings"], ["EUR"])
        self.assertLess(
            verdict["alnum"],
            pdf_currency.MIN_TEXT_LAYER_ALNUM,
            "and it answered despite being under the threshold",
        )

    def test_the_threshold_is_pinned_on_both_sides(self):
        """`MIN_TEXT_LAYER_ALNUM` is pinned by value and by its boundary behaviour."""
        self.assertEqual(
            pdf_currency.MIN_TEXT_LAYER_ALNUM,
            120,
            "120 is comfortably under a short one-page bill (395 alphanumeric "
            "characters here; the sample freight bill has more) and "
            "comfortably over a stamp, a page number or a fax header. Moving "
            "it is a decision about which documents stop being answered "
            "about, so it changes here too",
        )

        below = pdf_currency.scan_pdf_for_currency(
            _pdf_of_alnum(pdf_currency.MIN_TEXT_LAYER_ALNUM - 1)
        )
        self.assertEqual(below["alnum"], pdf_currency.MIN_TEXT_LAYER_ALNUM - 1)
        self.assertEqual(below["verdict"], pdf_currency.UNKNOWN)

        at = pdf_currency.scan_pdf_for_currency(
            _pdf_of_alnum(pdf_currency.MIN_TEXT_LAYER_ALNUM)
        )
        self.assertEqual(at["alnum"], pdf_currency.MIN_TEXT_LAYER_ALNUM)
        self.assertEqual(at["verdict"], pdf_currency.NOTHING_PRINTED)

        # 395 alphanumeric characters stands for a short one-page bill, smaller
        # than the sample freight bill above. The threshold has to stay under
        # it with room, or the check quietly stops answering about short
        # one-page bills.
        self.assertLess(
            pdf_currency.MIN_TEXT_LAYER_ALNUM,
            395,
            "a short one-page bill has about 395 alphanumeric characters — a "
            "threshold at or above it reads `unknown` on a document the check "
            "demonstrably can read",
        )
        self.assertLess(
            395,
            pdf_currency.scan_pdf_for_currency(_freight_pdf())["alnum"],
            "precondition: the sample freight bill is larger than the short "
            "bill this figure stands for",
        )
        self.assertEqual(
            pdf_currency.scan_pdf_for_currency(_pdf_of_alnum(395))["verdict"],
            pdf_currency.NOTHING_PRINTED,
        )

    def test_the_recorded_markings_are_capped(self):
        """The stored markings are capped at `MAX_RECORDED_MARKINGS`."""
        # ISO codes rather than symbols: only `$ € £ ¥` survive the Type1/WinAnsi
        # round trip in `_pdf_of`.
        many = (
            "$1.00 USD 1.00 EUR 1.00 GBP 1.00 CAD 1.00 AUD 1.00 "
            "CHF 1.00 SEK 1.00 NOK 1.00 DKK 1.00"
        )
        found = pdf_currency.currency_markings_in_text(many)
        self.assertGreater(
            len(found),
            pdf_currency.MAX_RECORDED_MARKINGS,
            "precondition: more markings than the cap",
        )
        verdict = pdf_currency.scan_pdf_for_currency(_pdf_of(_PLAIN_BODY + [many]))
        self.assertEqual(verdict["verdict"], pdf_currency.PRINTED)
        self.assertEqual(
            len(verdict["markings"]), pdf_currency.MAX_RECORDED_MARKINGS
        )
        self.assertEqual(
            verdict["markings"], found[: pdf_currency.MAX_RECORDED_MARKINGS]
        )

    def test_the_scan_records_how_much_it_read(self):
        """The verdict records how many alphanumeric characters it was read from."""
        freight = pdf_currency.scan_pdf_for_currency(_freight_pdf())
        self.assertEqual(freight["verdict"], pdf_currency.NOTHING_PRINTED)
        self.assertGreater(freight["alnum"], pdf_currency.MIN_TEXT_LAYER_ALNUM)
        self.assertEqual(
            pdf_currency.scan_pdf_for_currency(b"not a pdf at all")["alnum"], 0
        )


@tagged("post_install", "-at_install")
class TestWhichSourceDecides(BaseCase):
    """`nothing_was_printed` with both sources on the table."""

    def _verdict(self, pdf_bytes, data):
        data = dict(data)
        data[CURRENCY_MARKING_KEY] = currency_marking_verdict(pdf_bytes, data)
        return data

    def test_the_text_layer_beats_the_model_saying_usd(self):
        """The page prints nothing and the model says `USD`: the text layer decides."""
        data = self._verdict(_freight_pdf(), {CURRENCY_SYMBOL_KEY: "USD"})
        self.assertTrue(nothing_was_printed(data))
        self.assertFalse(data[CURRENCY_MARKING_KEY]["agree"])
        self.assertEqual(data[CURRENCY_MARKING_KEY]["decided_by"], "text_layer")

    def test_the_text_layer_beats_the_model_saying_null(self):
        """A printed `¥` the model did not report: the text layer decides, so the
        gate stays closed."""
        pdf = _pdf_of(_FREIGHT_CHARGE_BLOCK + ["TOTAL ¥ 3,748.50"])
        data = self._verdict(pdf, {CURRENCY_SYMBOL_KEY: None})
        self.assertFalse(nothing_was_printed(data))
        self.assertFalse(data[CURRENCY_MARKING_KEY]["agree"])
        self.assertEqual(data[CURRENCY_MARKING_KEY]["markings"], ["¥"])

    def test_without_a_text_layer_the_model_decides_exactly_as_before(self):
        """Without a text layer the model's answer decides, in both directions."""
        scan = _scanned_pdf()
        said_nothing = self._verdict(scan, {CURRENCY_SYMBOL_KEY: None})
        said_something = self._verdict(scan, {CURRENCY_SYMBOL_KEY: "USD"})
        self.assertTrue(nothing_was_printed(said_nothing))
        self.assertFalse(nothing_was_printed(said_something))
        for data in (said_nothing, said_something):
            self.assertEqual(data[CURRENCY_MARKING_KEY]["decided_by"], "model")
            self.assertIsNone(data[CURRENCY_MARKING_KEY]["agree"])

    def test_json_without_a_symbol_key_infers_nothing(self):
        """An extraction without `currency_symbol`, re-read over a scan, claims
        nothing."""
        data = self._verdict(_scanned_pdf(), {})
        self.assertFalse(nothing_was_printed(data))
        self.assertFalse(data[CURRENCY_MARKING_KEY]["model_reported"])

    def test_the_stored_verdict_names_its_own_source(self):
        """The stored verdict records which source decided and whether they agreed."""
        data = self._verdict(_freight_pdf(), {CURRENCY_SYMBOL_KEY: "USD"})
        stored = data[CURRENCY_MARKING_KEY]
        self.assertEqual(
            sorted(stored),
            [
                "agree",
                "alnum",
                "decided_by",
                "markings",
                "model_reported",
                "model_symbol",
                "text_layer",
            ],
        )
        self.assertEqual(stored["text_layer"], pdf_currency.NOTHING_PRINTED)
        self.assertEqual(stored["model_symbol"], "USD")
        # How much text the verdict was read from.
        self.assertGreater(stored["alnum"], pdf_currency.MIN_TEXT_LAYER_ALNUM)
        # It is written into `extraction_data` and into the `extraction.json`
        # attachment, so it has to survive a round trip through JSON.
        self.assertEqual(json.loads(json.dumps(stored)), stored)

    def test_a_disagreement_is_logged(self):
        """A disagreement between the model and the text layer is a warning."""
        with self.assertLogs(
            "odoo.addons.goodahead_invoice_ai_import.lib.claude_client",
            level="WARNING",
        ) as captured:
            self._verdict(_freight_pdf(), {CURRENCY_SYMBOL_KEY: "USD"})
        self.assertTrue(
            any("disagree" in line for line in captured.output), captured.output
        )


@tagged("post_install", "-at_install")
class TestTheFreightBillImports(BaseCase):
    """End to end through the real `extract_invoice` with the unmarked bill."""

    def _extract(self, data, pdf_bytes):
        return ClaudeInvoiceExtractor(
            client=_StubClient(_body(data)),
            currency_from_country=_fake_lookup,
            currency_is_active=_always_active,
        ).extract_invoice(pdf_bytes)

    def test_a_wrong_usd_is_marked_and_the_note_can_be_posted(self):
        """The model reports `USD` for an unmarked page: the currency is kept and
        marked as inferred."""
        data = _model_inferred_extraction(**{CURRENCY_SYMBOL_KEY: "USD"})
        result = self._extract(data, _freight_pdf())
        self.assertEqual(result["data"]["currency"], "USD")
        self.assertEqual(
            result["data"].get(CURRENCY_INFERRED_KEY),
            "US",
            "the page prints no currency, so the bill has to say the value "
            "was deduced",
        )

    def test_the_earlier_run_still_works(self):
        """The model answering `currency_symbol: null` gives the same result."""
        data = _model_inferred_extraction(**{CURRENCY_SYMBOL_KEY: None})
        result = self._extract(data, _freight_pdf())
        self.assertEqual(result["data"].get(CURRENCY_INFERRED_KEY), "US")

    def test_a_marked_page_still_blocks_the_fallback(self):
        """Control: on a page that prints `$`, nothing is marked as inferred."""
        pdf = _pdf_of(_FREIGHT_CHARGE_BLOCK + ["Total Amount Due $600.07"])
        data = _model_inferred_extraction(**{CURRENCY_SYMBOL_KEY: None})
        result = self._extract(data, pdf)
        self.assertNotIn(CURRENCY_INFERRED_KEY, result["data"])

    def test_a_printed_symbol_the_model_missed_fails_the_import(self):
        """A printed `¥` the model missed, with no currency resolved, fails."""
        pdf = _pdf_of(_FREIGHT_CHARGE_BLOCK + ["TOTAL ¥ 3,748.50"])
        data = _model_inferred_extraction(
            currency=None, **{CURRENCY_SYMBOL_KEY: None}
        )
        with self.assertRaises(ClaudeExtractionError):
            self._extract(data, pdf)

    def test_the_verdict_rides_in_the_extraction(self):
        data = _model_inferred_extraction(**{CURRENCY_SYMBOL_KEY: "USD"})
        result = self._extract(data, _freight_pdf())
        self.assertEqual(
            result["data"][CURRENCY_MARKING_KEY]["decided_by"], "text_layer"
        )


@tagged("post_install", "-at_install")
class TestTheNoteIsPostedOnTheBill(_NoClaudeMixin, TransactionCase):
    """The chatter note on the resulting bill.

    `_NoClaudeMixin` makes `extract_invoice` raise, so the extraction is built
    with `currency_marking_verdict` and the real resolver directly.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.company.claude_api_key = "test-key"
        cls.AccountMove = cls.env["account.move"]

    def _usd_extraction(self, pdf_bytes):
        """A model answer of `USD` for the page, run through the real resolver."""
        extracted = _model_inferred_extraction(**{CURRENCY_SYMBOL_KEY: "USD"})
        extracted[CURRENCY_MARKING_KEY] = currency_marking_verdict(
            pdf_bytes, extracted
        )
        ClaudeInvoiceExtractor(
            client=None,
            currency_from_country=self.AccountMove._ai_currency_for_country,
            currency_is_active=self.AccountMove._ai_currency_is_active,
        )._resolve_currency_from_country(extracted)
        return extracted

    def _run(self, extracted, pdf_bytes=b"%PDF-1.4 fake"):
        att = self.env["ir.attachment"].create(
            {
                "name": "zz-freight.pdf",
                "raw": pdf_bytes,
                "mimetype": "application/pdf",
            }
        )
        rec = self.env["ai.invoice.import"].create(
            {
                "name": "zz-freight.pdf",
                "company_id": self.company.id,
                "pdf_attachment_id": att.id,
            }
        )
        ai_result = {
            "extracted": extracted,
            "usage": {"input_tokens": 10, "output_tokens": 20},
            "model_name": "test-model",
            "xml_bytes": generate_peppol_invoice_xml(extracted),
        }
        with patch.object(
            type(self.AccountMove),
            "_ai_extract_and_generate_ubl",
            return_value=ai_result,
        ):
            result = rec._process_import()
        return rec, result

    def _reimport(self, extracted, pdf_bytes=b"%PDF-1.4 fake"):
        """Press the real re-import button on a failed record holding `extracted`.

        Returns `(record, stored JSON afterwards)`; the action parses its own
        copy from `extraction_data`, so the dict passed in is not the result.
        """
        att = self.env["ir.attachment"].create(
            {
                "name": "zz-freight.pdf",
                "raw": pdf_bytes,
                "mimetype": "application/pdf",
            }
        )
        rec = self.env["ai.invoice.import"].create(
            {
                "name": "zz-freight.pdf",
                "company_id": self.company.id,
                "pdf_attachment_id": att.id,
                "state": "failed",
                "extraction_data": json.dumps(extracted),
            }
        )
        rec.action_reimport_from_json()
        self.assertEqual(rec.state, "done", rec.error_message or "")
        self.assertTrue(rec.move_id, "the re-import produced no bill")
        return rec, json.loads(rec.extraction_data)

    def _bodies(self, move):
        return "\n".join((m.body or "") for m in move.message_ids)

    def test_the_bill_says_the_currency_was_not_printed(self):
        """The bill gets a note saying no currency was printed."""
        extracted = self._usd_extraction(_freight_pdf())
        self.assertEqual(
            extracted.get(CURRENCY_INFERRED_KEY), "US", "precondition"
        )
        rec, result = self._run(extracted)
        self.assertTrue(result.startswith("OK"), result)
        bodies = self._bodies(rec.move_id)
        self.assertIn("No currency symbol or ISO code was found", bodies)
        self.assertIn("United States", bodies)
        self.assertIn(
            CURRENCY_MARKING_KEY,
            rec.extraction_data or "",
            "the verdict has to be stored, or a later audit cannot tell which "
            "source decided",
        )

    def test_the_marker_is_re_validated_against_the_stored_verdict(self):
        """A stored verdict hand-edited to `printed` is kept on re-import and drops
        the marker."""
        extracted = self._usd_extraction(_freight_pdf())
        extracted[CURRENCY_MARKING_KEY] = dict(
            extracted[CURRENCY_MARKING_KEY],
            text_layer=pdf_currency.PRINTED,
            markings=["$"],
        )
        rec, stored = self._reimport(extracted, pdf_bytes=_freight_pdf())
        self.assertNotIn(
            "No currency symbol or ISO code was found", self._bodies(rec.move_id)
        )
        self.assertNotIn(CURRENCY_INFERRED_KEY, stored)
        self.assertEqual(
            stored[CURRENCY_MARKING_KEY]["markings"],
            ["$"],
            "the hand-edit survived; the page prints nothing",
        )

    def test_an_ordinary_marked_bill_posts_no_such_note(self):
        """Control: a bill whose page prints `$` gets no such note."""
        pdf = _pdf_of(_FREIGHT_CHARGE_BLOCK + ["Total Amount Due $600.07"])
        extracted = self._usd_extraction(pdf)
        rec, result = self._run(extracted)
        self.assertTrue(result.startswith("OK"), result)
        self.assertNotIn(
            "No currency symbol or ISO code was found", self._bodies(rec.move_id)
        )

    # -----------------------------------------------------------------
    # Records stored before the text-layer verdict existed
    # -----------------------------------------------------------------

    def _pre_verdict_extraction(self):
        """An old extraction: marker set, `currency_symbol: null`, and no
        `currency_marking` key."""
        extracted = _model_inferred_extraction(**{CURRENCY_SYMBOL_KEY: None})
        extracted[CURRENCY_INFERRED_KEY] = "US"
        extracted.pop(CURRENCY_MARKING_KEY, None)
        return extracted

    def _old_usd_extraction(self):
        """An old extraction where the model wrongly reported `USD`: no marker and
        no verdict."""
        extracted = _model_inferred_extraction(**{CURRENCY_SYMBOL_KEY: "USD"})
        extracted.pop(CURRENCY_MARKING_KEY, None)
        extracted.pop(CURRENCY_INFERRED_KEY, None)
        return extracted

    def test_an_old_record_gains_its_marker_on_re_import(self):
        """Re-import re-runs the resolver, so the missing marker and note are added."""
        extracted = self._old_usd_extraction()
        self.assertNotIn(
            CURRENCY_MARKING_KEY, extracted, "precondition: no stored verdict"
        )
        self.assertNotIn(
            CURRENCY_INFERRED_KEY, extracted, "precondition: no marker either"
        )
        rec, stored = self._reimport(extracted, pdf_bytes=_freight_pdf())
        self.assertEqual(
            stored[CURRENCY_MARKING_KEY]["text_layer"],
            pdf_currency.NOTHING_PRINTED,
            "the bytes say the page prints nothing",
        )
        self.assertEqual(
            stored.get(CURRENCY_INFERRED_KEY),
            "US",
            "and the marker the model's wrong `USD` suppressed is now set",
        )
        self.assertIn(
            "No currency symbol or ISO code was found", self._bodies(rec.move_id)
        )

    def test_the_add_direction_can_still_decline(self):
        """Control: over a page that prints `$`, re-import adds no marker."""
        pdf = _pdf_of(_FREIGHT_CHARGE_BLOCK + ["Total Amount Due $600.07"])
        extracted = self._old_usd_extraction()
        rec, stored = self._reimport(extracted, pdf_bytes=pdf)
        self.assertEqual(
            stored[CURRENCY_MARKING_KEY]["text_layer"], pdf_currency.PRINTED
        )
        self.assertNotIn(CURRENCY_INFERRED_KEY, stored)
        self.assertNotIn(
            "No currency symbol or ISO code was found", self._bodies(rec.move_id)
        )

    def test_an_old_record_is_re_decided_from_its_own_pdf(self):
        """An old record whose page prints `$` is re-decided from its PDF: the
        marker drops and no note is posted."""
        pdf = _pdf_of(_FREIGHT_CHARGE_BLOCK + ["Total Amount Due $600.07"])
        extracted = self._pre_verdict_extraction()
        self.assertNotIn(
            CURRENCY_MARKING_KEY, extracted, "precondition: no stored verdict"
        )
        rec, stored = self._reimport(extracted, pdf_bytes=pdf)
        self.assertEqual(
            stored[CURRENCY_MARKING_KEY]["text_layer"], pdf_currency.PRINTED
        )
        self.assertEqual(stored[CURRENCY_MARKING_KEY]["markings"], ["$"])
        self.assertNotIn(CURRENCY_INFERRED_KEY, stored)
        self.assertNotIn(
            "No currency symbol or ISO code was found", self._bodies(rec.move_id)
        )

    def test_the_back_catalogue_repair_can_still_say_nothing_was_printed(self):
        """Control: an old record over an unmarked page keeps its marker and note."""
        extracted = self._pre_verdict_extraction()
        rec, stored = self._reimport(extracted, pdf_bytes=_freight_pdf())
        self.assertEqual(
            stored[CURRENCY_MARKING_KEY]["text_layer"],
            pdf_currency.NOTHING_PRINTED,
        )
        self.assertEqual(stored.get(CURRENCY_INFERRED_KEY), "US")
        self.assertIn(
            "No currency symbol or ISO code was found", self._bodies(rec.move_id)
        )

    def test_with_the_attachment_gone_the_repair_lands_on_unknown(self):
        """With an unreadable PDF the verdict is UNKNOWN and the model's answer
        stands."""
        extracted = self._pre_verdict_extraction()
        rec, stored = self._reimport(extracted, pdf_bytes=b"not a pdf at all")
        self.assertEqual(
            stored[CURRENCY_MARKING_KEY]["text_layer"], pdf_currency.UNKNOWN
        )
        self.assertEqual(stored[CURRENCY_MARKING_KEY]["decided_by"], "model")
        self.assertEqual(stored.get(CURRENCY_INFERRED_KEY), "US")

    def test_a_stored_unknown_is_re_asked_on_the_next_re_import(self):
        """A stored UNKNOWN is not an answer: the next re-import asks the PDF again."""
        extracted = self._pre_verdict_extraction()
        _rec, after_the_bad_one = self._reimport(
            extracted, pdf_bytes=b"not a pdf at all"
        )
        self.assertEqual(
            after_the_bad_one[CURRENCY_MARKING_KEY]["text_layer"],
            pdf_currency.UNKNOWN,
            "precondition: the unreadable attachment stored an UNKNOWN",
        )
        pdf = _pdf_of(_FREIGHT_CHARGE_BLOCK + ["Total Amount Due $600.07"])
        _rec2, after_the_fix = self._reimport(after_the_bad_one, pdf_bytes=pdf)
        self.assertEqual(
            after_the_fix[CURRENCY_MARKING_KEY]["text_layer"],
            pdf_currency.PRINTED,
            "the attachment is readable now and the question was re-asked",
        )
        self.assertEqual(after_the_fix[CURRENCY_MARKING_KEY]["markings"], ["$"])

    def test_a_stored_answer_is_kept_even_though_the_pdf_disagrees(self):
        """A stored answer (`printed` / `nothing_printed`) is kept on re-import even
        when the PDF disagrees."""
        extracted = self._usd_extraction(_freight_pdf())
        extracted[CURRENCY_MARKING_KEY] = dict(
            extracted[CURRENCY_MARKING_KEY],
            text_layer=pdf_currency.PRINTED,
            markings=["€"],
        )
        pdf = _pdf_of(_FREIGHT_CHARGE_BLOCK + ["Total Amount Due $600.07"])
        _rec, stored = self._reimport(extracted, pdf_bytes=pdf)
        self.assertEqual(
            stored[CURRENCY_MARKING_KEY]["markings"],
            ["€"],
            "the hand-edited verdict survived; the PDF prints `$`",
        )

    def test_an_empty_stored_verdict_is_re_asked(self):
        """An empty stored verdict is treated as absent and recomputed."""
        extracted = self._pre_verdict_extraction()
        extracted[CURRENCY_MARKING_KEY] = {}
        _rec, stored = self._reimport(extracted, pdf_bytes=_freight_pdf())
        self.assertEqual(
            stored[CURRENCY_MARKING_KEY]["text_layer"],
            pdf_currency.NOTHING_PRINTED,
        )

    def test_the_note_names_which_instrument_answered(self):
        """The note says whether the text layer or the model answered."""
        on_bytes = self._usd_extraction(_freight_pdf())
        rec, _result = self._run(on_bytes)
        self.assertIn("own text layer", self._bodies(rec.move_id))

        # A scan: the text layer cannot answer, so the model's
        # `currency_symbol: null` decides.
        on_the_model = _model_inferred_extraction(**{CURRENCY_SYMBOL_KEY: None})
        on_the_model[CURRENCY_MARKING_KEY] = currency_marking_verdict(
            _scanned_pdf(), on_the_model
        )
        ClaudeInvoiceExtractor(
            client=None,
            currency_from_country=self.AccountMove._ai_currency_for_country,
            currency_is_active=self.AccountMove._ai_currency_is_active,
        )._resolve_currency_from_country(on_the_model)
        self.assertEqual(
            on_the_model[CURRENCY_MARKING_KEY]["decided_by"],
            "model",
            "precondition: a scan, so the text layer cannot answer",
        )
        self.assertEqual(
            on_the_model.get(CURRENCY_INFERRED_KEY), "US", "precondition"
        )
        rec2, _result2 = self._run(on_the_model)
        self.assertIn("no readable text layer", self._bodies(rec2.move_id))
