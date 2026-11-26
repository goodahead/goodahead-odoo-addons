# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Ask the PDF, not the model, whether the page prints a currency (19.0.3.18.0,
extended by review in 19.0.3.19.0).

🔴 WHAT REVIEW ADDED HERE, and two of the three were the mechanism's own
bounds being wrong rather than its code: a currency in an ANNOTATION or an
ACROFORM FIELD was invisible to `extract_text()` on a page twice over the
threshold (`test_a_currency_in_an_annotation_is_printed`); the threshold ran
BEFORE the markings and discarded positive evidence
(`test_a_short_page_that_prints_a_currency_is_printed`); and the ISO carve-out's
justification — *"two further questions still have to answer yes"* — is true
only on the branch that does not write
(`test_what_it_under_reports_on_purpose`). The fixtures for the first are
HAND-ROLLED PDF BYTES rather than reportlab, because the finding is about a
place the canvas API does not reach the same way on both libraries.

🔴 WHAT THIS IS BUILT FROM. A freight bill imported on a test database came
back with `currency_symbol: "USD"`. That page printed no
currency marking at all — every amount bare, confirmed with `pdftotext` over
both pages and by eye in the preview. The model reported a code that is not on
the document, the 19.0.3.3.0 gate closed on it, and the chatter note that is
the whole visibility of that feature was never posted. The currency was right
and it arrived unmarked, which is exactly the silence the feature exists to
remove. `_FREIGHT_CHARGE_BLOCK` below is an invented bill of the same shape.

🔴 AND THE SAME BYTES ANSWERED DIFFERENTLY TWICE. An earlier live run on this
bill returned `currency_symbol: null`; a later run returned `"USD"`. A rule about money was hinged on one model-reported observation that
has been seen to flip.

🔴 THE DEFECT PREDATES THIS BRANCH. It is in `19.0.3.3.0`, merged to `master`
and deployed; this branch is only where the fix lands, because it is the active
line.

🔴 NOTHING HERE MAY REACH THE CLAUDE API, and nothing here has an excuse to:
every test below is a stored extraction or a PDF built in-process. The lib
classes use the same `_StubClient` the currency tests have always used — there
is no transport in this module to reach, `goodahead_claude` owns it — and the
TransactionCase installs `_NoClaudeMixin`, which makes `extract_invoice` raise.

🔴 THE PDFs ARE BUILT HERE, NOT COMMITTED. Every document in this file is
invented: `_FREIGHT_CHARGE_BLOCK` is a freight bill's charge block and
`_EIGHT_SAMPLE_DOCUMENTS` the money-bearing excerpt of eight sample invoices,
written in the shapes real US vendor invoices print (bare amounts, `$`
amounts, a bare `USD`, a currency code in a routing field). No customer
document, name or address is in the repository. The measurement over the
original documents, which these samples reproduce, was run against both
pypdf 6.20.0 and PyPDF2 2.12.1, and the two agreed file for file.
"""

import io
import json
from unittest.mock import patch

from odoo.tests import BaseCase, TransactionCase, tagged

from odoo.addons.goodahead_invoice_import.lib import pdf_currency
from odoo.addons.goodahead_invoice_ai_import.lib.claude_client import (
    CURRENCY_INFERRED_KEY,
    CURRENCY_MARKING_KEY,
    CURRENCY_SYMBOL_KEY,
    ClaudeExtractionError,
    ClaudeInvoiceExtractor,
    currency_marking_verdict,
    nothing_was_printed,
)
from odoo.addons.goodahead_invoice_import.adapters.ubl_generator import (
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

# ---------------------------------------------------------------------------
# Sample documents (invented)
# ---------------------------------------------------------------------------

# An invented freight bill's charge block, in the layout of a US LTL carrier's
# invoice. Every figure is bare — no symbol, no code — and the block carries
# the one token that matters more than any of them:
#
#     *ZZF  PZONE03/09/26       ILS 30572
#
# `ILS` is the ISO 4217 code for the Israeli new shekel, printed here in a
# routing field of a domestic US freight bill. See
# `test_the_naive_rule_would_have_broken_the_fix`.
_FREIGHT_CHARGE_BLOCK = [
    "ORIGINAL INVOICE",
    "PAGE 1  OF  2",
    "ZZFE     PRIORITY",
    "Freight Bill Number 000000000001",
    "Ship Date / Invoice Date 03/09/2026 / 03/16/2026",
    "Bill of Lading Number M000000-00001",
    "Terms PREPAID",
    "Origin / Destination ORL / ALB",
    "Total Amount Due 612.84",
    "Payment Due Date 04/15/2026",
    "PIECES PALLETS HM DESCRIPTION WT(LBS) NMFC CLASS RATE TOTAL CHARGES",
    "2 STEEL SHELVING 200 400 2109.00 4,218.00",
    "ZZFRT REST API",
    "APPOINTMENT DELIVERY **",
    "*ZZF  PZONE03/09/26       ILS 30572",
    "UPDATE PER INSPECTION",
    "0000061 CUBIC FEET",
    "011605 FUEL SURCHG LTL SHPT 28.40% 131.70",
    "891  LESS DISCOUNT .891 3,758.86-",
    "0700 NOTIFY CHARGE 7.00",
    "0001550 WEIGHT VALIDATION FEE 15.50",
    "Rate Tariff: 000000000-100-000",
    "PLEASE PAY THIS AMOUNT",
    "612.84",
    "000000000001 000000061284 03162026 1",
]

# One money-bearing excerpt per sample document, invented in the shapes that
# US vendor invoices print, with the expected verdict beside it. Six print `$`
# (one of them also a bare `USD`), two print nothing at all.
_EIGHT_SAMPLE_DOCUMENTS = (
    (
        "zz-sample-1_invoice.pdf (ZZ Parts Co)",
        "Total charges           312.40             $312.40",
        ["$"],
    ),
    (
        "zz-sample-2_cash_sale.pdf (ZZ Supply Inc)",
        "Amount Paid:    \n$188.15\nCHECKS IN USD: \nSubtotal: $164.15",
        ["$", "USD"],
    ),
    (
        "zz-sample-3_invoice.pdf (ZZ Foods LLC)",
        "Sale Amount 402.90\nFreight 0.00\nTax 0.00\nTotal $402.90",
        ["$"],
    ),
    (
        "zz-sample-4_invoice.pdf (ZZ Manufacturing Inc)",
        "DELIVERY  Freight Charge1 60.00 60.00\n$355.20\n$355.20",
        ["$"],
    ),
    (
        "zz-sample-5_invoice.pdf (ZZ Kitchen Imports)",
        "*A $35.00 FEE APPLIES TO ANY RETURNED CHECK\n$619.75",
        ["$"],
    ),
    (
        "zz-sample-6_lease.pdf (ZZ Truck Leasing)",
        "TOTAL\n$41.10 \nAdmin Fee (20%) - - $6.85",
        ["$"],
    ),
    (
        "zz-sample-7_statement.pdf (ZZ fuel card statement)",
        "ACCOUNT NUMBER CREDIT LIMIT DAYS THIS PERIOD BILL CLOSING DATE "
        "PAYMENT DUE DATE** AMOUNT DUE\n"
        "0000-00-000000-0 5000.00 14 JUL-02-2026 JUL-16-2026 2140.66\n"
        "2088.12 2088.12 2142.96 2.30 2140.66",
        [],
    ),
    (
        "zz-sample-8_freight.pdf (ZZ Freight LLC)",
        "\n".join(_FREIGHT_CHARGE_BLOCK),
        [],
    ),
)


def _pdf_of(lines):
    """A one-page PDF with a real text layer, built in-process.

    reportlab is a hard Odoo requirement (`requirements.txt:84-86`), as is
    pypdf/PyPDF2 (`:66-68`), so this adds nothing to `external_dependencies`.
    The round trip through `odoo.tools.pdf` was measured in the container:
    `$`, `€`, `£` and `¥` all survive drawString → WinAnsiEncoding →
    `extract_text()` intact.
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


# A body big enough to be far over `MIN_TEXT_LAYER_ALNUM` and to carry no money
# at all, so that anything the scanner finds on these pages came from the
# annotation or the form field and from nowhere else.
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
    """A page whose currency is in an ANNOTATION or an ACROFORM FIELD.

    🔴 WRITTEN BY HAND, NOT BY reportlab, and that is the point of it. The
    whole finding (found in review) is that `extract_text()` walks the content
    stream and never touches `/Annots`, so the fixture has to put a string in
    a place reportlab's canvas API does not reach the same way on both
    libraries the deployment can resolve. Raw objects are identical under
    pypdf 6.20.0 and PyPDF2 2.12.1, which is what makes the measurement
    portable.

    `annotation` lands in a `/FreeText`'s `/Contents`. `field_value` lands in
    a `/Widget`'s `/V` and in the AcroForm `/Fields`, which is what
    `fill_form_fields_pdf` and every "fill it in and save" vendor bill
    produces.

    ⚠️ THE `/AP` ON THE `/FreeText` IS DECORATION, AND IT MISLED A READER
    (found in review). It draws the same string, so `test_a_currency_in_an_
    annotation_is_printed` reads as though the appearance-stream path were
    covered. It is not: `_annotation_text` reads `/Contents`, `/V` and `/DV`
    and never executes an appearance stream. It is kept because it is what a
    real stamp looks like, and `appearance_only=` builds the uncovered case so
    the gap is a test rather than an impression.

    The remaining knobs each exist for one review finding:

    * `annot_flags` — `/F`. Bit 2 (value 2) is Hidden, bit 6 (32) is NoView.
    * `default_value` — `/DV`, the field's DEFAULT value, beside or instead of
      `/V`.
    * `value_in_a_stream` — `/V` as an indirect STREAM object rather than a
      string, which is legal and which the decoder used to drop.
    * `filler_annotations` — how many money-free `/FreeText` annotations to put
      in `/Annots` AHEAD of the real one, so a test can place the money
      annotation past `MAX_ANNOTATIONS_PER_PAGE`. They are N references to ONE
      object, because the loop resolves each reference separately and a
      `/Annots` array of a million references to one annotation is the cheap
      shape of the attack the cap exists for.
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
    """A page with ink on it and no text: what a scanner produces. The honest
    answer about it is UNKNOWN, which is the whole reason the verdict has three
    values instead of two."""
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

    def test_the_eight_sample_documents(self):
        """🔴 THE MEASUREMENT, and the row that matters is the last one.

        Six of the eight print `$` and the check has to SAY so — a check that
        called every page unmarked would open the gate on the whole corpus and
        would pass a test that only looked at the freight bill. Two print nothing
        at all: the freight bill this fix exists for, and a fuel card
        statement, which is a second independent instance of the same class and
        was not known about until this was measured."""
        for name, excerpt, expected in _EIGHT_SAMPLE_DOCUMENTS:
            with self.subTest(document=name):
                self.assertEqual(
                    pdf_currency.currency_markings_in_text(excerpt),
                    expected,
                    name,
                )

    def test_the_naive_rule_would_have_broken_the_fix(self):
        """🔴 THE NEGATIVE CONTROL, AND IT IS NOT HYPOTHETICAL.

        A rule that matched any word-bounded uppercase triple on the ISO 4217
        list finds exactly one thing on bill 000000000001:

            *ZZF  PZONE03/09/26       ILS 30572

        `ILS` is the Israeli new shekel. That rule would have called the page
        marked, closed the gate, posted no note and left the bill as broken as
        it is today — while looking like the cautious choice. Measured the same
        way over all eight documents it finds `ILS` here and `USD` in
        *"CHECKS IN USD"*, and nothing else.

        So a bare code has to look like money. `ILS 30572` does not: `30572`
        has no decimal part, and every amount on all eight documents has one."""
        text = "\n".join(_FREIGHT_CHARGE_BLOCK)
        self.assertIn("ILS", text, "precondition: the trap is on the page")
        self.assertEqual(
            pdf_currency.currency_markings_in_text(text),
            [],
            "ILS in a routing field is not a currency marking",
        )

    def test_a_code_against_an_amount_is_a_marking(self):
        for text in (
            "Total USD 612.84",
            "Total 1,234.56 CAD",
            "Summe EUR 1.234,56",
            "Yhteensä 1 234,56 EUR",
        ):
            with self.subTest(text=text):
                self.assertTrue(
                    pdf_currency.currency_markings_in_text(text), text
                )

    def test_a_code_after_a_currency_word_is_a_marking(self):
        """The second sample invoice prints *"CHECKS IN USD:"* with no amount on the
        line, and that is a currency marking by anyone's reading."""
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
        """🔴 THE FOUR NAMED IN THE BRIEF, each checked rather than assumed.

        A `$` in a part number, `kr` inside a Swedish word, a three-letter code
        that is an abbreviation, and `USD`-shaped text in an address line."""
        for text in (
            "Item ACSS-4832$B Countertop Clear Safety Shield",  # $ in a part no.
            "Levereras i en kruka, kräver bevattning",  # kr inside words
            "TOP FLOOR, 15 MAD AVENUE, SUITE 400",  # TOP and MAD, no money
            "Send payment to: P O BOX 223125 PITTSBURGH PA 15251-2125",
            "I/L PRO Number",  # PRO, LBS, NMFC, ZZF: none of them codes
            "Origin / Destination NEW / TPA",
            # 🔴 THIS ROW HAS AN AMOUNT IN IT SINCE 19.0.3.19.0, and that is
            # the whole repair. A review showed the old fixture
            # `"Levereras i en kruka, kräver bevattning"` proved nothing about
            # the `kr` lookaround it was named for: with no amount anywhere in
            # the string `_KRONA_RE` fails on the amount and the guard is
            # never reached. A fixture that cannot reach the rule it is the
            # coverage for is not coverage.
            "Levereras i en kruka for 1 500,00 kruka",
            "Kr. Johansson, Storgatan 14",  # `Kr.` as an initial, no amount
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    pdf_currency.currency_markings_in_text(text), [], text
                )

    def test_the_krona_guard_can_fail(self):
        """🔴 THE CONTROL ON THE ROW ABOVE. Same sentence, same amount, with
        the letters taken off the `kr` — if the lookarounds were deleted the
        row above would still pass and this one would still pass, so this one
        exists to show the rule is reached at all."""
        self.assertEqual(
            pdf_currency.currency_markings_in_text(
                "Levereras i en kruka for 1 500,00 kr"
            ),
            ["kr"],
        )

    def test_what_it_over_reports_on_purpose(self):
        """⚠️ THE DIRECTION THE BIAS GOES, written down so it is a decision and
        not a surprise.

        `ALL` is the Albanian lek and `TOP` the Tongan paʻanga, so a line
        reading "TOTAL ALL 1,234.56" is called a marking. That closes the gate
        and costs exactly today's behaviour — the import either fails loudly
        with the JSON stored, or succeeds with the model's own currency and no
        note. Nobody is misled.

        The opposite error — missing a marking that IS printed — opens the gate
        on a document the model could not settle and lets two countries choose
        a currency instead. That is the `¥`-from-Shanghai case 19.0.2.3.0 made
        terminal on purpose, and it is a factor of about twenty.

        🆕 19.0.3.19.0 added two more, both of them the price of a repair, and
        both named here rather than discovered later:

        * `Total CUP 50` reads `CUP`, the Cuban peso, because `total` became an
          announcing word so that `Total: CAD 500` would stop being missed.
          On a restaurant-supply invoice that is a measuring cup.
        * `公元 2026,00 年` reads `元`. `_YUAN_RE` has no letter-adjacency guard
          and deliberately keeps none — see the comment on it. The row for
          this in `test_the_false_positives_thought_about_first` passed only
          because `2026` has no decimals, so the rule was never reached; it is
          asserted here, in the direction it actually goes.

        🔴 THE ARGUMENT FOR DECLINING THAT GUARD USED TO BE `1234元整`, HERE AND
        IN TWO OTHER PLACES, AND IT WAS VACUOUS (19.0.3.20.0).
        `1234元整` has no decimal part, so `_AMOUNT` never reaches it and the
        proposed guard would have cost nothing on that string. The real
        under-report was the amount's trailing `\\b`, which cannot hold between
        a digit and a CJK ideograph — see `test_the_under_reports_review_7_
        measured`. The decline of the guard stands; the example does not, and
        the amountless form is now named in `test_the_hole_that_is_still_open`
        rather than used as an argument for something else."""
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
        """⚠️ THE ONE PLACE THE BIAS IS REVERSED, and the price of not matching
        `ILS 30572`. A bare code against a round amount with no decimals and
        NO INTRODUCING WORD is missed.

        🔴 AND THE BOUND THIS DOCSTRING USED TO CLAIM WAS FALSE. It said *"two
        further questions still have to answer yes before anything is inferred
        — both parties in ONE country, and an already-set currency equal to
        that country's own"*, and said it in three places. A review read the
        resolver: the second question (`claude_client:1450`) is guarded by
        `already_set`, so on the branch that WRITES a currency it is never
        asked. On that branch exactly one question is left — both parties in
        one country — plus "is that currency traded here", which is about
        configuration and not about the page.

        So the hole was made smaller instead, in the direction the file's bias
        already goes: `total`, `subtotal`, `balance` and `amount due` are now
        announcing words, and `Total: CAD 500` IS a marking. What is left is a
        code with no announcing word and no decimals, which is the shape
        `ILS 30572` has — and declining to match that shape is the fix this
        whole feature is.
        """
        self.assertEqual(
            pdf_currency.currency_markings_in_text("CAD 500"),
            [],
            "the residual: a bare code, a gap, an integer, nothing else",
        )
        self.assertEqual(
            pdf_currency.currency_markings_in_text("Total: CAD 500"),
            ["CAD"],
            "🔴 THE RED of review: this was [] and USD was written over "
            "it under a note saying nothing was printed",
        )
        self.assertEqual(
            pdf_currency.currency_markings_in_text(
                "*ZZF  PZONE03/09/26       ILS 30572"
            ),
            [],
            "🔴 AND THE CONTROL ON THAT REPAIR. A `rejected_codes` rule — "
            "record the codes the carve-out turned down, refuse to write when "
            "one is present — reads `ILS 30572` and `CAD 500` as the same "
            "shape, so it declines on the one bill this feature exists for. "
            "The repair had to come from the words around them instead",
        )
        # ⚠️ "ANY RULE" IS WHAT THIS USED TO CLAIM, AND IT IS FALSE AT BYTE
        # LEVEL (19.0.3.20.0). The two strings do differ in a way
        # a rule can read: five ungrouped digits against three, on a bill whose
        # every other figure is two-decimal. A rule of the form "an integer of
        # four or more digits with no grouping separator is not an amount"
        # separates them. Whether it is a GOOD rule is a different question —
        # round four-digit totals like `1500` exist — but it exists, so the
        # decline is narrowed to the rule review proposed rather than stated
        # as an absolute. Nothing is asserted about the hypothetical rule
        # itself — it is not in the code, and an assertion on a rule that does
        # not exist is the shape of test this file keeps removing.

    def test_the_symbols(self):
        """EVERY character on the list, not nine of them.

        A review counted 9 of 32 exercised. A symbol that is on the list and
        has never been through the regex is a character someone can delete, or
        mistype into a confusable, with nothing going red — `₮` and `₸` differ
        by one stroke and `￠` and `¢` by a code point."""
        self.assertEqual(
            len(set(pdf_currency.UNAMBIGUOUS_SYMBOLS)),
            len(pdf_currency.UNAMBIGUOUS_SYMBOLS),
            "the list has a duplicate in it",
        )
        # 🆕 19.0.3.20.0 (found in review): the code list's SIZE is quoted in
        # three prose places — `pdf_currency.py:262`, the docstring above and
        # the CHANGELOG — and one of the three said 152. A number repeated in
        # prose and nowhere asserted is a number that drifts.
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
        """All fourteen-plus alternatives of the two announcing-word regexes,
        not four.

        Four were exercised before 19.0.3.19.0 and the list grew by four more
        in the same commit, so the untested majority was about to get larger.

        ⚠️ IT WAS ONE REGEX AND IT IS TWO SINCE 19.0.3.20.0 —
        `_CODE_AFTER_CURRENCY_WORD_RE` and `_CODE_AFTER_AMOUNT_WORD_RE`, split
        for review. This docstring named `_CODE_AFTER_KEYWORD_RE`, which no
        longer exists; a docstring that names a symbol the file does not have is
        the same defect class, one layer down."""
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
            # The four added in 19.0.3.19.0 for review. ⚠️ Since
            # 19.0.3.20.0 these four are a SEPARATE regex and each needs a
            # number after the code — see the test below for why.
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
        """🔴 THE RED of review — THE FOUR NEW WORDS DROPPED THE MONEY
        SHAPE, WHICH THE FILE CALLS ITS WHOLE DEFENCE.

        The single `_CODE_AFTER_KEYWORD_RE` this splits never required an
        amount, and that was sound
        while every alternative was a phrase that ANNOUNCES A CURRENCY and
        nothing else — `denominated in`, `payable in`, `checks in`, `remit in`.
        The file rejects a bare `in` five lines earlier for exactly this
        reason: *"a bare 'in' before a triple would match `IN NEW`, `IN USD`
        and `IN ALL` alike"*.

        `total`, `subtotal`, `balance` and `amount(s) due` do not announce a
        currency. They announce A NUMBER, and the 26-confusables paragraph
        names the money shape as the defence against the codes that are also
        words. Measured before this change, none of these has an amount
        anywhere on it:

            `Totals TRY` -> ['TRY']   `Grand total SAR` -> ['SAR']
            `subtotals NOK` -> ['NOK']

        ⚠️ IT DOES NOT BITE TODAY. A review re-measured all seven sample PDFs,
        the transcribed ZZ Freight LLC block and all eight `_EIGHT_SAMPLE_DOCUMENTS`
        excerpts under both rules: identical markings and verdicts on all
        eight. This is a contradiction being removed, not a failure being
        fixed — and the contradiction is the shape of defect this file has
        spent three reviews on.

        ⚠️ A DIGIT, NOT `_AMOUNT`. `Total: CAD 500` is review repair and
        `500` has no decimals, so demanding the two-decimal shape here would
        undo it. What survives is named rather than discovered later."""
        for text in ("Totals TRY", "Grand total SAR", "subtotals NOK"):
            with self.subTest(text=text, rule="amount word, no number"):
                self.assertEqual(
                    pdf_currency.currency_markings_in_text(text), [], text
                )
        # 🔴 THE CONTROL, three ways. The currency words still need no number;
        # the amount words still match when one is there; and R2's own repair
        # still stands.
        for text, expected in (
            ("Currency: GBP", ["GBP"]),
            ("CHECKS IN USD: ", ["USD"]),
            ("All amounts are in SEK", ["SEK"]),
            ("Total: CAD 500", ["CAD"]),
            ("TOTAL ALL 1,234.56", ["ALL"]),
            # ⚠️ NAMED, NOT FIXED: a number after the code is enough, so these
            # two are still over-reports. Both are genuinely shaped like the
            # invoice's money column, which is where the file's bias points.
            ("Balance PEN 12", ["PEN"]),
            ("Amount due TOP 3", ["TOP"]),
        ):
            with self.subTest(text=text, rule="control"):
                self.assertEqual(
                    pdf_currency.currency_markings_in_text(text), expected, text
                )

    def test_the_under_reports_review_6_measured(self):
        """🔴 THE RED of review, six rows, every one of them the DANGEROUS
        direction: a currency is printed and the check answered `[]`.

        The apostrophe and narrow-space rows are the sharpest, because the same
        amount written the other way round already matched — `1'234.55 CHF` was
        a marking while `CHF 1'234.55` was not, which is two spellings of one
        number disagreeing rather than a decision about either."""
        for text, expected in (
            ("TOTAL ＄1,234.00", ["＄"]),  # U+FF04 fullwidth dollar
            ("TOTAL ﹩1,234.00", ["﹩"]),  # U+FE69 small dollar
            ("Total CHF 1'234.55", ["CHF"]),  # Swiss apostrophe, leading code
            ("Total 1'234.55 CHF", ["CHF"]),  # … and it already worked here
            ("Total CHF 1’234.55", ["CHF"]),  # U+2019, the typographic twin
            ("Total EUR 1 234,56", ["EUR"]),  # U+202F narrow no-break
            ("Total EUR 1 234,56", ["EUR"]),  # U+2009 thin space
            ("Total EUR 1 234,56", ["EUR"]),  # U+00A0 already worked
            ("Summa 1 500 kr", ["kr"]),  # round kronor are ordinary
            ("Summa 1 234,00 kr", ["kr"]),  # … and so are decimal ones
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    pdf_currency.currency_markings_in_text(text), expected, text
                )

    def test_the_under_reports_review_7_measured(self):
        """🔴 THE RED of review — THE FIFTEENTH INSTANCE, AND IT IS `元`.

        `_AMOUNT` ends `[.,]\\d{2}\\b-?`. `元`, `円` and every CJK ideograph are
        `\\w` to Python's `re`, so the `\\b` between the `0` of `1,234.00` and
        the `元` that follows it CANNOT HOLD — two word characters have no
        boundary between them. The `after` branch of `_YUAN_RE` was therefore
        dead for every TIGHT form, which is the form Chinese actually prints.
        Measured before this change, every row the dangerous direction:

            `1,234.00元`      -> []
            `合计 1,234.00元`  -> []
            `1234.00円`       -> []
            `1,234.00 元`     -> ['元']   <- only with a space

        🔴 AND THE COMMENT ARGUED ABOUT THE WRONG THING IN THREE PLACES.
        `pdf_currency.py`, the 19.0.3.19.0 notes and
        `test_what_it_over_reports_on_purpose` all declined a trailing
        letter-guard on `元` because it "would throw away `1234元整`". That
        string has no decimals, so the guard would never have reached it: the
        whole paragraph argued about a trailing guard while the blocker sat
        three characters to its left. `kr` escapes the identical trap by
        accident, through the `_ROUND_AMOUNT` alternative 19.0.3.19.0 added —
        `-?\\d+` matches the bare `00` of `1 234,00kr` and lands against the
        `k`. `_YUAN_RE` got no such alternative and so got no such rescue."""
        for text, expected in (
            ("1,234.00元", ["元"]),
            ("合计 1,234.00元", ["元"]),
            ("1,234.00元整", ["元"]),
            ("1234.00円", ["円"]),
            # … and the spaced form that always worked, as the control that
            # this is a repair and not a replacement.
            ("1,234.00 元", ["元"]),
            # The `kr` row that escaped by accident, pinned so the asymmetry
            # cannot come back unnoticed.
            ("1 234,00kr", ["kr"]),
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    pdf_currency.currency_markings_in_text(text), expected, text
                )

    def test_the_hole_that_is_still_open(self):
        """⚠️ A CURRENCY NAMED ONLY AS A WORD IS NOT MATCHED, and this test is
        here so that stays a decision.

        A review measured it. Nothing in the file looks at a spelled-out
        currency name, so "All amounts are in US Dollars", "Betrag in Euro" and
        "Summa 1 500 kronor" all answer `[]` — the dangerous direction. It was
        not closed blind: a word list is a different kind of object from a
        153-code ISO set (it is per-language, it collides with ordinary nouns
        — `pound`, `crown`, `real`, `won` — and `won` is a verb), and getting
        it wrong adds false positives to the one rule that is allowed none.
        The module docstring names it as a residual; this pins the shape of it
        so a later change has to update a test rather than a sentence.

        🆕 19.0.3.20.0 (found in review): `1234元整` joins it. An amount with NO
        decimal part is not matched beside `元` — `_YUAN_RE` is guarded by the
        two-decimal amount alone, which is the price of keeping no
        letter-adjacency guard. It was the EXAMPLE three documents used to
        justify that decline, which made it look covered; it is the residual
        instead."""
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
        """The docstring said "in document order" until 19.0.3.19.0 and it was
        never true: five rules run in sequence and the result is deduped across
        them. Cosmetic — except the list is written into stored JSON for a
        person to read a year later, and the docstring is what tells them how
        to read it."""
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
        """🔴 THE CONTROL ON THE WHOLE FILE. A scanner that answered
        "nothing printed" for everything would pass the test above and would
        open the gate on every document in the building."""
        verdict = pdf_currency.scan_pdf_for_currency(
            _pdf_of(_FREIGHT_CHARGE_BLOCK + ["Total Amount Due $612.84"])
        )
        self.assertEqual(verdict["verdict"], pdf_currency.PRINTED)
        self.assertEqual(verdict["markings"], ["$"])

    def test_a_scan_is_unknown_and_not_nothing(self):
        """🔴 "NO TEXT LAYER" IS NOT "NOTHING PRINTED", and collapsing the two
        would open the gate on every scanned invoice at once — with no evidence
        behind it at all."""
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
        """A page carrying only a stamp reads as UNKNOWN; a real invoice's text
        layer does not. The smallest of the eight measured invoices had 395
        alphanumeric characters."""
        self.assertEqual(
            pdf_currency.scan_pdf_for_currency(_pdf_of(["Page 1 of 2"]))["verdict"],
            pdf_currency.UNKNOWN,
        )

    def test_a_currency_in_an_annotation_is_printed(self):
        """🔴 THE RED of review, and the failure class it re-opened.

        `extract_text()` walks the content stream and the Form XObjects the
        content stream invokes. It does NOT walk `/Annots`. So a page with an
        ordinary un-garbled body — 235 alphanumeric characters here, about
        twice the threshold — and `$1,234.00` drawn by a `/FreeText`
        annotation answered `nothing_printed` with `markings: []`, on pypdf
        6.20.0 and PyPDF2 2.12.1 alike. The gate opened, two countries picked a
        currency, and on a Shanghai-to-Shanghai bill printing `$` that is the
        `¥` case 19.0.2.3.0 made terminal, re-opened by the new mechanism."""
        pdf = _pdf_with_annotations(_PLAIN_BODY, annotation="$1,234.00")
        verdict = pdf_currency.scan_pdf_for_currency(pdf)
        self.assertEqual(verdict["verdict"], pdf_currency.PRINTED)
        self.assertEqual(verdict["markings"], ["$"])

    def test_a_currency_in_an_acroform_field_is_printed(self):
        """🔴 THE SAME RED THROUGH THE ORDINARY ROUTE. A vendor opens a
        template, types the amounts into the form fields and SAVES rather than
        printing to PDF: the values live in `/V` and the appearance streams.
        Odoo ships `fill_form_fields_pdf` in the very module this check reads
        PDFs with, so the house itself produces such files."""
        pdf = _pdf_with_annotations(_PLAIN_BODY, field_value="$1,234.00")
        verdict = pdf_currency.scan_pdf_for_currency(pdf)
        self.assertEqual(verdict["verdict"], pdf_currency.PRINTED)
        self.assertEqual(verdict["markings"], ["$"])

    def test_the_annotation_sweep_can_still_answer_nothing(self):
        """🔴 THE CONTROL ON THE TWO ABOVE. The same body and the same kind of
        annotation, carrying no money: a sweep that answered `printed` for
        anything with an `/Annots` array would pass both tests above and would
        close the gate on every commented PDF in the building, which is the
        failure the whole feature is a fix for."""
        pdf = _pdf_with_annotations(
            _PLAIN_BODY, annotation="Received at the dock on 14 February"
        )
        verdict = pdf_currency.scan_pdf_for_currency(pdf)
        self.assertEqual(verdict["verdict"], pdf_currency.NOTHING_PRINTED)
        self.assertEqual(verdict["markings"], [])

    def test_an_annotation_does_not_buy_a_text_layer(self):
        """⚠️ ANNOTATION TEXT IS NOT COUNTED TOWARD THE THRESHOLD, and that is
        deliberate. The threshold asks "does this file have a readable text
        layer"; a scanned page carrying one filled-in form field has not
        acquired one, and must still answer UNKNOWN about everything nobody can
        read rather than `nothing_printed`.

        🔴 THAT IS TRUE OF THE NEGATIVE ANSWER ONLY, AND THIS TEST COULD NOT
        SEE IT (19.0.3.20.0). Two paragraphs — on
        `MIN_TEXT_LAYER_ALNUM` and on `_annotation_text` — stated the invariant
        about the VERDICT: *"a scanned page carrying one filled-in form field
        must still answer UNKNOWN"*, *"a filled field can never make a scanned
        page look like it has a readable text layer"*. Markings come first, so
        a MONEY-BEARING field answers `printed`, not `unknown`. This test used
        a non-currency field, so it passed without being able to see the other
        half — a control that passes for the wrong reason. Both rows are
        asserted now, and the behaviour is right: a filled-in field really does
        print money. It was the two sentences that were wrong."""
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
        """🔴 THE RED of review. `/F` bit 2 is Hidden: no viewer renders
        such an annotation and no printer prints it. The sweep called it
        `printed ['$']` — text no human can see, counted as printed.

        The "A COMMENT IS COUNTED TOO, DELIBERATELY" paragraph defends counting
        VISIBLE NON-CONTENT, by subtype, and the code never looked at a subtype
        in its life. Counting INVISIBLE CONTENT is not the thing that paragraph
        decided."""
        hidden = pdf_currency.scan_pdf_for_currency(
            _pdf_with_annotations(_PLAIN_BODY, annotation="$1,234.00", annot_flags=2)
        )
        self.assertEqual(hidden["verdict"], pdf_currency.NOTHING_PRINTED)
        self.assertEqual(hidden["markings"], [])

        # 🔴 THE CONTROL, and it is the one that stops this becoming a flag
        # sweep. `/F` bit 6 (NoView, 32) means "do not show on SCREEN" — such
        # an annotation DOES print, and the question this file asks is what the
        # page prints. It stays counted, deliberately.
        noview = pdf_currency.scan_pdf_for_currency(
            _pdf_with_annotations(_PLAIN_BODY, annotation="$1,234.00", annot_flags=32)
        )
        self.assertEqual(noview["verdict"], pdf_currency.PRINTED)
        self.assertEqual(noview["markings"], ["$"])

    def test_a_default_value_loses_to_the_value_that_is_shown(self):
        """🔴 THE SECOND HALF of review. `/DV` is the field's DEFAULT
        value, not its value. A widget with `/V (0.00)` and
        `/DV ($1,234.00)` shows `0.00`; the sweep read the `$` off a key the
        renderer ignores whenever `/V` is present — a template placeholder the
        vendor overwrote, not a sticky note counted too."""
        both = pdf_currency.scan_pdf_for_currency(
            _pdf_with_annotations(
                _PLAIN_BODY, field_value="0.00", default_value="$1,234.00"
            )
        )
        self.assertEqual(both["verdict"], pdf_currency.NOTHING_PRINTED)
        self.assertEqual(both["markings"], [])

        # 🔴 THE CONTROL. With no `/V` at all the `/DV` IS what the field
        # displays, so it still counts. A repair that dropped `/DV` outright
        # would pass the assertion above and would lose a real printed
        # currency, which is the dangerous direction.
        default_only = pdf_currency.scan_pdf_for_currency(
            _pdf_with_annotations(_PLAIN_BODY, default_value="$1,234.00")
        )
        self.assertEqual(default_only["verdict"], pdf_currency.PRINTED)
        self.assertEqual(default_only["markings"], ["$"])

    def test_a_field_value_held_in_a_stream_is_read(self):
        """🔴 review. A `/V` that is an indirect STREAM object rather
        than a string is legal — generators use it for long values — and it
        used to hit neither branch of `_annotation_string` and return `""`.
        Measured: `nothing_printed, []`, the dangerous direction."""
        verdict = pdf_currency.scan_pdf_for_currency(
            _pdf_with_annotations(
                _PLAIN_BODY, field_value="$1,234.00", value_in_a_stream=True
            )
        )
        self.assertEqual(verdict["verdict"], pdf_currency.PRINTED)
        self.assertEqual(verdict["markings"], ["$"])

    def test_the_annotation_hole_that_is_still_open(self):
        """⚠️ AN `/AP`-ONLY ANNOTATION IS INVISIBLE TO THIS CHECK, and review
        F5 is that nothing said so.

        `_annotation_text` reads `/Contents`, `/V` and `/DV`. It does not
        execute the `/AP` appearance stream those render from, and the
        docstring said as much — but did not list it beside vector art and
        word-named currencies as a RESIDUAL, which is where it belongs: a
        flattened stamp, or a generator that wrote only an appearance, prints
        money and this answers `nothing_printed`.

        Worse, the fixture hid it. `_pdf_with_annotations` builds its
        `/FreeText` WITH an `/AP` drawing the same string, so
        `test_a_currency_in_an_annotation_is_printed` reads as though the
        appearance path were covered. The `/Contents` is what is read. Pinned
        here so a later change has to update a test rather than a sentence."""
        verdict = pdf_currency.scan_pdf_for_currency(
            _pdf_with_annotations(_PLAIN_BODY, appearance_only="PAID $1,234.00")
        )
        self.assertEqual(verdict["verdict"], pdf_currency.NOTHING_PRINTED)
        self.assertEqual(verdict["markings"], [])

    def test_the_annotation_surface_is_bounded_in_both_directions(self):
        """🔴 review — THE TWO CAPS SHIPPED WITH NO TEST, which is the
        shape of defect 19.0.3.19.0 §(g) exists for.

        `MAX_ANNOTATIONS_PER_PAGE` and `MAX_ANNOTATION_CHARS` were added because
        the `/Annots` loop was bounded by nothing while the `/Kids` walk was
        bounded at 10,000: an array of a million references, or one 6 MB
        `/Contents`, costs real time for no evidence — *(measured)*
        **4.3 s** of regex for a single 6 MB annotation, with and without a
        currency in it.

        🔴 A CAP IS A DECISION TO STOP LOOKING, SO IT IS ASSERTED AS ONE. Both
        rows below are the cap LOSING a currency that is really there, in the
        dangerous direction, and both are followed by the control that says the
        bound is where it claims to be and not one step tighter. The trade is
        the same one `test_the_recorded_markings_are_capped` makes: a currency
        only visible past either bound is one nobody reading the page would
        find either.
        """
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
        # 🔴 THE CONTROL. One fewer filler puts the same annotation at the last
        # index the loop reaches, so the cap is off-by-nothing and the row
        # above is the cap rather than a broken fixture.
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
        # 🔴 THE CONTROL, same string the other way round: the cap truncates
        # the TAIL and reads everything up to it.
        self.assertEqual(
            pdf_currency.currency_markings_in_text(
                pdf_currency._annotation_string("$1,234.00" + "x" * chars)
            ),
            ["$"],
        )

    def test_an_annotation_string_decodes_by_its_bom(self):
        """🔴 THE RED of review. The ladder read
        `("utf-16", "utf-8", "latin-1")`, and Python's `utf-16` codec with no
        BOM defaults to LITTLE-endian, which a PDF never means. Any even-length
        byte string that was not UTF-16 decoded to mojibake and the currency in
        it was lost:

            `b"$1,234.00"`  -> '$1,234.00'   (odd length, fell through)
            `b"$1,234.00 "` -> 'ㄤ㈬㐳〮‰'     markings []

        One trailing space, opposite answer, the dangerous direction. PDF text
        strings that are UTF-16 carry the BOM by specification, so the BOM is
        the test."""
        for raw, expected in (
            (b"$1,234.00", "$1,234.00"),
            (b"$1,234.00 ", "$1,234.00 "),
            (b"Total $1,234.00 ", "Total $1,234.00 "),
            # The BOM case, which always worked and has to keep working.
            (b"\xfe\xff\x00$\x001", "$1"),
        ):
            with self.subTest(raw=raw):
                text = pdf_currency._annotation_string(raw)
                self.assertEqual(text, expected)
                self.assertEqual(
                    pdf_currency.currency_markings_in_text(text), ["$"]
                )

    def test_a_short_page_that_prints_a_currency_is_printed(self):
        """🔴 THE RED of review: the threshold used to run BEFORE the
        markings were computed, so it discarded positive evidence it already
        held.

        Measured: a 42-alnum credit note printing `EUR 1.234,00` answered
        `unknown`, while `currency_markings_in_text` on the same text answered
        `['EUR']`. Pad the page past 120 and it answered `printed` — so the
        verdict on a page that unambiguously prints a currency depended on how
        much unrelated boilerplate sat around it. A found marking is evidence
        on its own and needs no quorum."""
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
        """🔴 NOTHING PINNED `MIN_TEXT_LAYER_ALNUM` (found in review). Mutated to
        9, to 300 or to 500 the whole suite stayed green, and 500 would turn
        the smallest of the eight measured invoices — 395 alnum, verdict `printed` — back into `unknown`,
        silently restoring model-only behaviour on short invoices. The only
        artifact that would have noticed is a CHANGELOG table, and nothing runs
        a table.

        🔴 AND THE BOUNDARY PAIR ALONE DOES NOT DO IT (19.0.3.20.0).
        The pair is written RELATIVE to the constant, so it moves with any
        mutation: it pins a RANGE, roughly `(42, 395)`, and not the value.
        A review replayed the mutants — 9, 395 and 500 are caught; **45, 50,
        120, 200, 300 and 394 all pass**, and 300 is one of the three this very
        docstring named. At 300 every real invoice under 300 alnum reads
        `unknown` and silently reverts to model-only behaviour, which is the
        harm, just at a smaller magnitude than 500.

        So the value is pinned outright, with the argument for it beside the
        assertion. The range assertions stay: they are what says WHY 120 and
        not some other number, and they are the ones that go red when the
        corpus changes rather than when the constant does."""
        self.assertEqual(
            pdf_currency.MIN_TEXT_LAYER_ALNUM,
            120,
            "120 is comfortably under the smallest measured real invoice (395 "
            "alphanumeric characters) and comfortably over a stamp, a page "
            "number or a fax header. Moving it is a decision about which real "
            "documents stop being answered about, so it changes here too",
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

        # 395 is the smallest text layer of the eight measured invoices. The
        # threshold has to stay under it with room, or the check quietly stops
        # answering about real one-page bills.
        self.assertLess(
            pdf_currency.MIN_TEXT_LAYER_ALNUM,
            395,
            "the smallest measured real invoice has 395 alphanumeric "
            "characters — a threshold at or above it reads `unknown` on a "
            "document the check demonstrably can read",
        )
        self.assertEqual(
            pdf_currency.scan_pdf_for_currency(_pdf_of_alnum(395))["verdict"],
            pdf_currency.NOTHING_PRINTED,
        )

    def test_the_recorded_markings_are_capped(self):
        """`MAX_RECORDED_MARKINGS` survived mutation to 1 (found in review). The
        cap is a promise about what goes into an attachment a person reads a
        year later, so it is asserted on both sides: the cap holds, and it is
        not 1."""
        # ⚠️ ISO CODES, NOT SYMBOLS, AND THE FIRST DRAFT OF THIS TEST FOUND OUT
        # WHY: `_pdf_of` writes a Type1 font through WinAnsiEncoding, where
        # only `$ € £ ¥` of the symbol list survive the round trip — a line of
        # nine symbols came back with four markings and the test failed on the
        # fixture rather than on the cap. Plain ASCII codes have no such
        # problem.
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
        """A review: a `nothing_printed` off a 121-character page and one
        off the ZZ Freight LLC bill's roughly 3,000 were indistinguishable in the stored record,
        and `nothing_printed` is the dangerous verdict."""
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
        """🔴 TODAY'S BUG, AS ONE ASSERTION. The page prints nothing, the model
        says `"USD"`, and until 19.0.3.18.0 the model won and the gate closed."""
        data = self._verdict(_freight_pdf(), {CURRENCY_SYMBOL_KEY: "USD"})
        self.assertTrue(nothing_was_printed(data))
        self.assertFalse(data[CURRENCY_MARKING_KEY]["agree"])
        self.assertEqual(data[CURRENCY_MARKING_KEY]["decided_by"], "text_layer")

    def test_the_text_layer_beats_the_model_saying_null(self):
        """🔴 THE DANGEROUS DIRECTION, AND IT GOES THE SAME WAY. A `¥` is on
        the page and the model reported nothing. Taking the text layer closes
        the gate and the import fails loudly, which is 19.0.2.3.0's deliberate
        behaviour — a `¥` is CNY or JPY, a factor of about twenty.

        That both disagreements resolve toward something a human sees is the
        argument for the text layer winning outright rather than only when it
        finds something."""
        pdf = _pdf_of(_FREIGHT_CHARGE_BLOCK + ["TOTAL ¥ 4,218.00"])
        data = self._verdict(pdf, {CURRENCY_SYMBOL_KEY: None})
        self.assertFalse(nothing_was_printed(data))
        self.assertFalse(data[CURRENCY_MARKING_KEY]["agree"])
        self.assertEqual(data[CURRENCY_MARKING_KEY]["markings"], ["¥"])

    def test_without_a_text_layer_the_model_decides_exactly_as_before(self):
        """⚠️ 19.0.3.3.0's behaviour, unchanged, on the documents this check
        cannot read. Both rows, so the fallback is a fallback and not a
        constant."""
        scan = _scanned_pdf()
        said_nothing = self._verdict(scan, {CURRENCY_SYMBOL_KEY: None})
        said_something = self._verdict(scan, {CURRENCY_SYMBOL_KEY: "USD"})
        self.assertTrue(nothing_was_printed(said_nothing))
        self.assertFalse(nothing_was_printed(said_something))
        for data in (said_nothing, said_something):
            self.assertEqual(data[CURRENCY_MARKING_KEY]["decided_by"], "model")
            self.assertIsNone(data[CURRENCY_MARKING_KEY]["agree"])

    def test_pre_19_0_3_3_0_json_still_infers_nothing(self):
        """A stored extraction from before `currency_symbol` existed, re-read
        through a scan that cannot answer either. The question was never asked,
        so nothing is claimed."""
        data = self._verdict(_scanned_pdf(), {})
        self.assertFalse(nothing_was_printed(data))
        self.assertFalse(data[CURRENCY_MARKING_KEY]["model_reported"])

    def test_the_stored_verdict_names_its_own_source(self):
        """🔴 THE AUDIT TRAIL. A year from now the only thing in front of a
        reader is this JSON, and it has to say which source decided and whether
        the two agreed — not merely what the answer was."""
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
        # 🆕 19.0.3.19.0. How much text the answer was read off, so that an
        # audit can tell a `nothing_printed` taken from 121 characters from one
        # taken from this bill's roughly 3,000.
        self.assertGreater(stored["alnum"], pdf_currency.MIN_TEXT_LAYER_ALNUM)
        # It is written into `extraction_data` and into the `extraction.json`
        # attachment, so it has to survive a round trip through JSON.
        self.assertEqual(json.loads(json.dumps(stored)), stored)

    def test_a_disagreement_is_logged(self):
        """⚠️ `assertLogs` takes an INTEGER level or a level NAME, and the
        level here is WARNING because a disagreement is the finding."""
        with self.assertLogs(
            "odoo.addons.goodahead_invoice_import.lib.invoice_data",
            level="WARNING",
        ) as captured:
            self._verdict(_freight_pdf(), {CURRENCY_SYMBOL_KEY: "USD"})
        self.assertTrue(
            any("disagree" in line for line in captured.output), captured.output
        )


@tagged("post_install", "-at_install")
class TestTheFreightBillImports(BaseCase):
    """🔴 THE DEFECT, END TO END THROUGH THE REAL `extract_invoice`."""

    def _extract(self, data, pdf_bytes):
        return ClaudeInvoiceExtractor(
            client=_StubClient(_body(data)),
            currency_from_country=_fake_lookup,
            currency_is_active=_always_active,
        ).extract_invoice(pdf_bytes)

    def test_the_reported_run_is_marked_and_the_note_can_be_posted(self):
        """🔴 THE RED. The reported answer, in its shape: `currency`
        already "USD" because the prompt's CURRENCY step 4 did the inference,
        and `currency_symbol: "USD"` for a page that prints no currency
        anywhere. Before this change `nothing_was_printed` read that symbol,
        the gate closed, no marker was set and the bill said nothing."""
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
        """The first live run on the SAME bytes returned
        `currency_symbol: null`. It was already handled, and it has to stay
        handled — the fix is about removing the dependence on which of the two
        answers came back, not about swapping one for the other."""
        data = _model_inferred_extraction(**{CURRENCY_SYMBOL_KEY: None})
        result = self._extract(data, _freight_pdf())
        self.assertEqual(result["data"].get(CURRENCY_INFERRED_KEY), "US")

    def test_a_marked_page_still_blocks_the_fallback(self):
        """🔴 THE CONTROL. The same extraction against a page that DOES print
        `$`. A change that marked everything would pass both tests above and
        would tell every accountant on every bill that the currency was
        guessed, which is how a warning stops being read."""
        pdf = _pdf_of(_FREIGHT_CHARGE_BLOCK + ["Total Amount Due $612.84"])
        data = _model_inferred_extraction(**{CURRENCY_SYMBOL_KEY: None})
        result = self._extract(data, pdf)
        self.assertNotIn(CURRENCY_INFERRED_KEY, result["data"])

    def test_a_printed_symbol_the_model_missed_fails_the_import(self):
        """🔴 THE DANGEROUS DIRECTION THROUGH THE REAL PATH. `¥` is on the
        page, the model reported nothing and resolved no currency. 19.0.2.3.0
        made that terminal on purpose and it stays terminal."""
        pdf = _pdf_of(_FREIGHT_CHARGE_BLOCK + ["TOTAL ¥ 4,218.00"])
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
    """The visible end of it: the chatter note that the reported bill did not get.

    `_NoClaudeMixin` makes `extract_invoice` raise, so this class builds the
    extraction with the module-level `currency_marking_verdict` and the real
    resolver instead — the same two calls `extract_invoice` makes, with nothing
    that could reach the API in between.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.company.claude_api_key = "test-key"
        cls.AccountMove = cls.env["account.move"]

    def _reported_extraction(self, pdf_bytes):
        """The reported answer, run through the real rule with the
        real `res.country` lookup. Nothing is hand-written into it."""
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
        rec = self.env["goodahead.invoice.import"].create(
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
        """🔴 THE ACTUAL RE-IMPORT BUTTON, not `_process_import` with the
        extractor patched.

        Until 19.0.3.20.0 every "re-import path" test in this file went through
        `_process_import`, because the heal lived in `_finalize_import`, which
        both paths share. That is how review stayed invisible: the half of
        the repair that was missing lives in `action_reimport_from_json`, which
        nothing drove. The record is put in exactly the state the button
        requires — `failed`, no bill, stored JSON — and the button is pressed.
        No Claude client is reachable: `_NoClaudeMixin` makes `extract_invoice`
        raise, and this path never calls it.

        Returns `(record, the JSON as it was stored afterwards)` — the dict
        passed in is NOT the one the action mutates, because the action parses
        its own copy out of `extraction_data`.
        """
        att = self.env["ir.attachment"].create(
            {
                "name": "zz-freight.pdf",
                "raw": pdf_bytes,
                "mimetype": "application/pdf",
            }
        )
        rec = self.env["goodahead.invoice.import"].create(
            {
                "name": "zz-freight.pdf",
                "company_id": self.company.id,
                "pdf_attachment_id": att.id,
                "state": "failed",
                "extraction_data": json.dumps(extracted),
            }
        )
        rec.action_reimport_from_json()
        self.assertEqual(rec.state, "imported", rec.error_message or "")
        self.assertTrue(rec.move_id, "the re-import produced no bill")
        return rec, json.loads(rec.extraction_data)

    def _bodies(self, move):
        return "\n".join((m.body or "") for m in move.message_ids)

    def test_the_bill_says_the_currency_was_not_printed(self):
        """🔴 THE DEFECT AS A USER SAW IT: this note, missing, on bill
        000000000001."""
        extracted = self._reported_extraction(_freight_pdf())
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
        """🔴 THE RE-IMPORT PATH, AND SINCE 19.0.3.20.0 IT IS DRIVEN THROUGH
        THE REAL BUTTON. A verdict hand-edited to say the page WAS marked must
        drop the marker, exactly as a hand-edited currency does — and it is
        also the control on `_ai_reask_the_pdf`, which must KEEP a stored
        answer rather than recompute over the hand-edit."""
        extracted = self._reported_extraction(_freight_pdf())
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
            "the hand-edit survived; the ZZ Freight LLC page prints nothing",
        )

    def test_an_ordinary_marked_bill_posts_no_such_note(self):
        """🔴 THE CONTROL, at the ORM layer this time."""
        pdf = _pdf_of(_FREIGHT_CHARGE_BLOCK + ["Total Amount Due $612.84"])
        extracted = self._reported_extraction(pdf)
        rec, result = self._run(extracted)
        self.assertTrue(result.startswith("OK"), result)
        self.assertNotIn(
            "No currency symbol or ISO code was found", self._bodies(rec.move_id)
        )

    # -----------------------------------------------------------------
    # The back catalogue (found in review)
    # -----------------------------------------------------------------

    def _pre_3_18_0_extraction(self):
        """A stored extraction exactly as 19.0.3.3.0–19.0.3.17.0 left it: the
        marker set, `currency_symbol: null`, and NO `currency_marking` key,
        because the question had not been invented yet."""
        extracted = _model_inferred_extraction(**{CURRENCY_SYMBOL_KEY: None})
        extracted[CURRENCY_INFERRED_KEY] = "US"
        extracted.pop(CURRENCY_MARKING_KEY, None)
        return extracted

    def _reported_extraction_as_stored(self):
        """🔴 THE REPORTED BILL AS IT SITS IN THE DATABASE, and the state
        neither of review two back-catalogue tests could reach.

        The model wrongly reported `currency_symbol: "USD"` for a page that
        prints no currency anywhere. Under 19.0.3.3.0–19.0.3.17.0 that closed
        the gate before the resolver could mark anything, so the stored JSON
        has **no marker and no verdict** — not `_pre_3_18_0_extraction`'s
        marker-already-set shape. It is the record class the whole feature
        exists for."""
        extracted = _model_inferred_extraction(**{CURRENCY_SYMBOL_KEY: "USD"})
        extracted.pop(CURRENCY_MARKING_KEY, None)
        extracted.pop(CURRENCY_INFERRED_KEY, None)
        return extracted

    def test_the_reported_bill_gains_its_marker_on_re_import(self):
        """🔴 THE RED of review — THE HEAL WENT ONE WAY ONLY.

        19.0.3.19.0 recomputed the verdict on re-import and stopped there. The
        only consumer on that path is `_ai_surviving_currency_marker`, which
        returns at `account_move.py` the moment the JSON carries no marker, so
        the repair could **drop** a wrong marker and could never **add** a
        missing one — and the missing case is the reported bill.
        Measured before this change: `currency_marking` was written with
        `text_layer: nothing_printed` off the record's own bytes, and the bill
        STILL got no note, because nothing re-asked the question the marker
        answers.

        `action_reimport_from_json` now re-runs the resolver as well."""
        extracted = self._reported_extraction_as_stored()
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
            "🔴 and the marker the model's wrong `USD` suppressed is now set",
        )
        self.assertIn(
            "No currency symbol or ISO code was found", self._bodies(rec.move_id)
        )

    def test_the_add_direction_can_still_decline(self):
        """🔴 THE CONTROL ON THE TEST ABOVE. The same unmarked record over a
        page that DOES print `$`: re-running the resolver must not hand a
        marker to every old bill. A repair that marked everything would pass
        the test above and would tell every accountant on every re-imported
        bill that the currency was guessed."""
        pdf = _pdf_of(_FREIGHT_CHARGE_BLOCK + ["Total Amount Due $612.84"])
        extracted = self._reported_extraction_as_stored()
        rec, stored = self._reimport(extracted, pdf_bytes=pdf)
        self.assertEqual(
            stored[CURRENCY_MARKING_KEY]["text_layer"], pdf_currency.PRINTED
        )
        self.assertNotIn(CURRENCY_INFERRED_KEY, stored)
        self.assertNotIn(
            "No currency symbol or ISO code was found", self._bodies(rec.move_id)
        )

    def test_a_pre_3_18_0_record_is_re_decided_from_its_own_pdf(self):
        """🔴 THE RED of review, and it is the dangerous direction.

        `action_reimport_from_json` loads the stored JSON and generates the
        UBL; it never asked the PDF. For every extraction stored before
        19.0.3.18.0 there is no `currency_marking` key, so
        `nothing_was_printed` falls back to the model's `currency_symbol` —
        which is null here — the marker survives, and the bill is told *"no
        currency symbol or ISO code was found anywhere on this document"* while
        `$612.84` is printed on the page and the bytes are on the same record,
        unread.

        So the bytes are read here. The marker drops and no note is posted."""
        pdf = _pdf_of(_FREIGHT_CHARGE_BLOCK + ["Total Amount Due $612.84"])
        extracted = self._pre_3_18_0_extraction()
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
        """🔴 THE CONTROL. The same pre-3.18.0 record over the ZZ Freight LLC bill,
        which really does print nothing: the marker has to SURVIVE and the note
        has to be posted. A repair that dropped every old marker would pass the
        test above and would silence the feature on the whole back
        catalogue."""
        extracted = self._pre_3_18_0_extraction()
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
        """⚠️ THE DEGRADATION WAS ALREADY WRITTEN AND NEVER CALLED.
        `scan_pdf_for_currency(None)` returns UNKNOWN, so a record whose PDF
        cannot be read falls back to the model exactly as 19.0.3.3.0 did —
        which here means the marker stands on `currency_symbol: null`."""
        extracted = self._pre_3_18_0_extraction()
        rec, stored = self._reimport(extracted, pdf_bytes=b"not a pdf at all")
        self.assertEqual(
            stored[CURRENCY_MARKING_KEY]["text_layer"], pdf_currency.UNKNOWN
        )
        self.assertEqual(stored[CURRENCY_MARKING_KEY]["decided_by"], "model")
        self.assertEqual(stored.get(CURRENCY_INFERRED_KEY), "US")

    def test_a_stored_unknown_is_re_asked_on_the_next_re_import(self):
        """🔴 THE RED of review — A TRANSIENT UNKNOWN BECAME PERMANENT.

        `currency_marking_verdict` always returns a TRUTHY dict, including for
        UNKNOWN, and the old guard was "recompute when the stored value is
        falsy". So the first re-import on a record whose attachment happened to
        be unreadable at that moment — an encrypted PDF, a truncated upload, a
        record with no `pdf_attachment_id` — wrote
        `{"text_layer": "unknown", "decided_by": "model", "alnum": 0}` into the
        JSON permanently. Every later re-import found a present, truthy key and
        skipped the recompute, **even after the attachment was fixed**. The
        degradation stopped being a degradation and became the stored answer.

        UNKNOWN means "nobody could tell". It is the one value that must not be
        preserved as though it were an answer."""
        extracted = self._pre_3_18_0_extraction()
        _rec, after_the_bad_one = self._reimport(
            extracted, pdf_bytes=b"not a pdf at all"
        )
        self.assertEqual(
            after_the_bad_one[CURRENCY_MARKING_KEY]["text_layer"],
            pdf_currency.UNKNOWN,
            "precondition: the unreadable attachment stored an UNKNOWN",
        )
        pdf = _pdf_of(_FREIGHT_CHARGE_BLOCK + ["Total Amount Due $612.84"])
        _rec2, after_the_fix = self._reimport(after_the_bad_one, pdf_bytes=pdf)
        self.assertEqual(
            after_the_fix[CURRENCY_MARKING_KEY]["text_layer"],
            pdf_currency.PRINTED,
            "the attachment is readable now and the question was re-asked",
        )
        self.assertEqual(after_the_fix[CURRENCY_MARKING_KEY]["markings"], ["$"])

    def test_a_stored_answer_is_kept_even_though_the_pdf_disagrees(self):
        """⚠️ THE HAND-EDIT KEEPS ITS LATITUDE, and this test is named for what
        the guard DOES.

        It was called `test_a_stored_verdict_is_never_overwritten` and the
        guard was `not extracted.get(KEY)` — absent **or falsy** — so a
        hand-edit leaving `"currency_marking": {}` in the JSON really was
        overwritten, while the comment said "only when the key is absent" and
        the test said "never overwritten". Three statements of one guard is how
        the fourteen got started (found in review). The guard now says what it
        means: a stored ANSWER (`printed` / `nothing_printed`) is kept, and
        only a non-answer — absent, not a dict, or `unknown` — is re-asked."""
        extracted = self._reported_extraction(_freight_pdf())
        extracted[CURRENCY_MARKING_KEY] = dict(
            extracted[CURRENCY_MARKING_KEY],
            text_layer=pdf_currency.PRINTED,
            markings=["€"],
        )
        pdf = _pdf_of(_FREIGHT_CHARGE_BLOCK + ["Total Amount Due $612.84"])
        _rec, stored = self._reimport(extracted, pdf_bytes=pdf)
        self.assertEqual(
            stored[CURRENCY_MARKING_KEY]["markings"],
            ["€"],
            "the hand-edited verdict survived; the PDF prints `$`",
        )

    def test_an_empty_stored_verdict_is_re_asked(self):
        """⚠️ THE OTHER HALF OF THE SENTENCE ABOVE. `{}` is not an answer
        either, and the guard has always treated it as absent. Asserted now
        that the comment and the test both say so."""
        extracted = self._pre_3_18_0_extraction()
        extracted[CURRENCY_MARKING_KEY] = {}
        _rec, stored = self._reimport(extracted, pdf_bytes=_freight_pdf())
        self.assertEqual(
            stored[CURRENCY_MARKING_KEY]["text_layer"],
            pdf_currency.NOTHING_PRINTED,
        )

    def test_the_note_names_which_instrument_answered(self):
        """A review §5: the note opened with a flat absolute — *"No currency
        symbol or ISO code is printed anywhere on this document"* — which on a
        PDF with no readable text layer is the model's word, and the model has
        been seen to flip on identical bytes. It now says which one looked."""
        on_bytes = self._reported_extraction(_freight_pdf())
        rec, _result = self._run(on_bytes)
        self.assertIn("own text layer", self._bodies(rec.move_id))

        # A SCAN, where the text layer cannot answer at all, so the model's
        # `currency_symbol: null` is what the sentence rests on. That is
        # 19.0.3.3.0's behaviour, and the note now says so.
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
