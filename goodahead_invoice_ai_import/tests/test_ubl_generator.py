# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""UBL generation: a discounted line must emit the NET unit price so Odoo's
importer doesn't add a bogus 'Rounding' line and tax the gross amount."""

from lxml import etree

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_import.adapters.ubl_generator import (
    UBLGenerationError,
    generate_peppol_invoice_xml,
)


@tagged("post_install", "-at_install")
class TestUblGenerator(TransactionCase):
    def _data(self, price_unit, line_total, qty=1.0):
        return {
            "type": "Invoice",
            "invoice_number": "T-1",
            "issue_date": "2026-08-06",
            "currency": "EUR",
            "seller": {"name": "ZZ Seller Oy", "vat": "FI00000078",
                       "country_code": "FI"},
            "buyer": {"name": "ZZ Buyer Oy", "vat": "FI00000123", "country_code": "FI"},
            "lines": [{"name": "Widget", "quantity": qty, "unit_code": "C62",
                       "price_unit": price_unit, "line_total": line_total,
                       "tax_percent": 25.5, "tax_amount": 0.0,
                       "tax_category": "S"}],
            "tax_subtotals": [{"tax_percent": 25.5, "taxable_amount": line_total,
                               "tax_amount": 0.0, "tax_category": "S"}],
            "totals": {"line_extension": line_total, "tax_exclusive": line_total,
                       "tax_inclusive": line_total, "payable": line_total},
        }

    def test_the_generated_xml_is_classified_bis3_on_whichever_core_this_is(self):
        """🔴 A REVIEW FINDING — VERIFIED RATHER THAN ARGUED, AND IT IS
        THE ONLY DOOR BETWEEN STEPS 4/6 AND A LIVE US DEPLOYMENT.

        `account.move._ai_decode_ubl_into_move` sets `import_file_type` from
        core's own `_get_import_file_type` and the whole US-deployment analysis
        rests on the answer being `account.edi.xml.ubl_bis3`: on the search-plan
        core that classification routes the document through
        `_ubl_import_invoice`, whose body calls NEITHER
        `account.edi.common._import_partner` NOR `_import_lines`, so the create
        suppression and the product narrowing are inert there. If a generated
        XML were ever classified UBL 2.0 or Factur-X instead, both would fire on
        the US deployment — a behaviour change no step claims.

        *(read, both cores)* it cannot, and the reason is worth pinning rather
        than restating. `ca1f0aea` tests `UBLVersionID` BEFORE `CustomizationID`
        and `189df353` tests it after, so the two orders differ — but this
        generator emits no `UBLVersionID` at all, its root is `Invoice-2` /
        `CreditNote-2` rather than `CrossIndustryInvoice`, and its
        `CUSTOMIZATION_ID` constant is character-for-character the BIS 3.0
        branch on both. The classification is therefore a constant, and a
        constant is a thing a test holds still.

        CONTROL: the same bytes with the CustomizationID edited to the NLCIUS
        one classify as something else, so a check that answered `ubl_bis3` for
        any input could not pass this.
        """
        xml = generate_peppol_invoice_xml(self._data(44.95, 24.73))
        move = self.env["account.move"]

        def classify(raw):
            return move._get_import_file_type(
                {
                    "name": "invoice.xml",
                    "raw": raw,
                    "xml_tree": etree.fromstring(raw),
                    "type": "xml",
                    "mimetype": "application/xml",
                    "attachment": self.env["ir.attachment"],
                    "origin_attachment": self.env["ir.attachment"],
                    "origin_import_file_type": "pdf",
                }
            )

        self.assertEqual(classify(xml), "account.edi.xml.ubl_bis3")
        self.assertNotIn(
            b"UBLVersionID",
            xml,
            "the legacy core reads UBLVersionID FIRST; emitting one would make "
            "the classification depend on which core is underneath",
        )

        decoy = xml.replace(
            b"urn:cen.eu:en16931:2017#compliant#"
            b"urn:fdc:peppol.eu:2017:poacc:billing:3.0",
            b"urn:cen.eu:en16931:2017#compliant#urn:fdc:nen.nl:nlcius:v1.0",
        )
        self.assertNotEqual(decoy, xml, "CONTROL: the decoy really differs")
        self.assertNotEqual(
            classify(decoy),
            "account.edi.xml.ubl_bis3",
            "CONTROL: the classifier can answer something else",
        )

    def test_discounted_line_emits_net_unit_price(self):
        # list 44.95, net 24.73 (discount) -> PriceAmount must be the net 24.73
        xml = generate_peppol_invoice_xml(self._data(44.95, 24.73)).decode()
        self.assertIn("24.73", xml)
        self.assertNotIn("44.95", xml, "gross list price must not be emitted")

    def test_multi_qty_net_unit_price(self):
        # qty 4, net line 100.00 -> unit price 25.00
        xml = generate_peppol_invoice_xml(self._data(30.0, 100.0, qty=4.0)).decode()
        self.assertIn("25.0", xml)
        self.assertNotIn("30.0", xml)

    def test_the_currency_code_is_normalised_before_it_is_emitted(self):
        """Core matches `res.currency.name` EXACTLY.

        A lower-case "cny" or a stray space matches nothing in
        `_import_currency`, which then substitutes the COMPANY currency — USD
        here — with only a log line. That is the wrong-value failure this
        change exists to stop, arriving one layer below the prompt. The value
        also comes out of Claude's JSON, so `str()` is there for the same
        reason it is in `peppol_endpoint_for_party`.
        """
        for printed in ("cny", " CNY ", "Cny"):
            with self.subTest(printed=printed):
                data = self._data(10.0, 10.0)
                data["currency"] = printed
                tree = etree.fromstring(generate_peppol_invoice_xml(data))
                code = tree.find(
                    ".//{urn:oasis:names:specification:ubl:schema:xsd:"
                    "CommonBasicComponents-2}DocumentCurrencyCode"
                )
                self.assertEqual(code.text, "CNY")
                amounts = tree.findall(
                    ".//{urn:oasis:names:specification:ubl:schema:xsd:"
                    "CommonBasicComponents-2}PayableAmount"
                )
                self.assertEqual([a.get("currencyID") for a in amounts], ["CNY"])

    def test_note_emitted_once(self):
        # The invoice note must appear exactly once (was duplicated in both the
        # document Note and PaymentTerms/Note -> doubled on the imported bill).
        data = self._data(10.0, 10.0)
        data["note"] = "Viivastyskorko 11,5 %. Huomautusaika 14 pv."
        xml = generate_peppol_invoice_xml(data).decode()
        self.assertEqual(xml.count(data["note"]), 1, "note must not be duplicated")

    # ------------------------------------------------------------------
    # 🔴 `.get(key, default)` DOES NOT DEFAULT A NULL
    #
    # Three fields were read as `container.get(key, "a default")`, and all three
    # are documented in the prompt's schema as things the model may answer null
    # to. `dict.get`'s second argument applies when the key is ABSENT; a key
    # PRESENT WITH VALUE NULL returns None and sails past it. So a perfectly
    # compliant extraction produced `<cbc:PaymentMeansCode>None</cbc:Payment
    # MeansCode>`, an empty tax-category id, or — for `unit_code` — a TypeError
    # out of lxml that took the whole import down with a message about argument
    # types.
    #
    # ⚠️ These were MEASURED against the previous revision before being fixed,
    # not read. The `unit_code` one in particular behaved differently from the
    # other two, and reading would not have told anyone that.
    # ------------------------------------------------------------------

    def _with(self, **line_overrides):
        data = self._data(10.0, 10.0)
        data["seller"]["iban"] = "FI2112345600000785"
        data["lines"][0].update(line_overrides)
        return data

    def test_a_null_payment_means_code_does_not_emit_the_word_none(self):
        data = self._with()
        data["payment_means_code"] = None

        xml = generate_peppol_invoice_xml(data).decode()

        self.assertIn("<cbc:PaymentMeansCode>30</cbc:PaymentMeansCode>", xml)
        self.assertNotIn("None", xml)

    def test_the_payment_means_block_is_reached_at_all(self):
        """The control for the test above. `PaymentMeans` is only emitted when
        there is an account or a payment reference, so without this an assertion
        about its content could pass against an XML that has no such element."""
        xml = generate_peppol_invoice_xml(self._with()).decode()

        self.assertIn("<cbc:PaymentMeansCode>30</cbc:PaymentMeansCode>", xml)

    def test_a_null_unit_code_does_not_abort_the_import(self):
        """lxml's `set()` rejects None outright, so this one was loud — but
        loud about the wrong thing: `Argument must be bytes or unicode, got
        'NoneType'` names no field and no document."""
        xml = generate_peppol_invoice_xml(self._with(unit_code=None)).decode()

        self.assertIn('unitCode="C62"', xml)

    def test_a_null_tax_category_does_not_emit_an_empty_id(self):
        xml = generate_peppol_invoice_xml(self._with(tax_category=None)).decode()

        self.assertNotIn("<cbc:ID/>", xml)
        self.assertNotIn("<cbc:ID></cbc:ID>", xml)

    # ------------------------------------------------------------------
    # A null AMOUNT, by contrast, must stop everything
    # ------------------------------------------------------------------

    def test_a_null_amount_is_a_named_failure_and_not_a_zero(self):
        """🔴 `_fmt_decimal(None)` returned "0.00".

        So a null `line_total`, `tax_amount`, `taxable_amount` or `totals` entry
        passed validation, passed generation, built a bill, and was wrong with
        nothing to see — which inverts the whole null-rather-than-guess
        principle, whose justification is that a missing value is VISIBLE.

        ⚠️ This lives here as well as in the validator because the validator
        cannot reach the RE-IMPORT path: `action_reimport_from_json` replays a
        stored, hand-edited extraction straight into this module without
        re-validating it, so a human who deletes a figure while fixing another
        one was being handed a silent zero.
        """
        cases = {
            "line 1: line_total": lambda d: d["lines"][0].update(line_total=None),
            "tax subtotal 1: tax_amount":
                lambda d: d["tax_subtotals"][0].update(tax_amount=None),
            "tax subtotal 1: taxable_amount":
                lambda d: d["tax_subtotals"][0].update(taxable_amount=None),
            "totals.payable": lambda d: d["totals"].update(payable=None),
            "totals.tax_inclusive": lambda d: d["totals"].update(tax_inclusive=None),
        }
        for label, mutate in cases.items():
            with self.subTest(field=label):
                data = self._data(10.0, 10.0)
                mutate(data)

                with self.assertRaises(UBLGenerationError) as caught:
                    generate_peppol_invoice_xml(data)

                self.assertIn(label, str(caught.exception))

    def test_a_complete_document_still_generates(self):
        """The control for the block above: a validator that rejected every
        document would pass all five cases and break every import."""
        xml = generate_peppol_invoice_xml(self._data(10.0, 10.0)).decode()

        self.assertIn("<cbc:PayableAmount", xml)

    def test_a_null_line_tax_omits_the_line_tax_total_and_writes_no_zero(self):
        """19.0.3.29.0: a document that prints one tax total and none per line.

        The line's TaxTotal is left out rather than written as 0.00. The
        line's rate and the document's subtotal are still there, and they are
        what Odoo's decoder reads.
        """
        ns = {
            "cac": "urn:oasis:names:specification:ubl:schema:xsd:"
                   "CommonAggregateComponents-2",
            "cbc": "urn:oasis:names:specification:ubl:schema:xsd:"
                   "CommonBasicComponents-2",
        }
        data = self._data(10.0, 10.0)
        data["lines"][0]["tax_amount"] = None
        tree = etree.fromstring(generate_peppol_invoice_xml(data))

        line = tree.find("cac:InvoiceLine", ns)
        self.assertIsNone(line.find("cac:TaxTotal", ns))
        self.assertEqual(
            line.findtext("cac:Item/cac:ClassifiedTaxCategory/cbc:Percent",
                          namespaces=ns),
            "25.50",
        )
        self.assertIsNotNone(tree.find("cac:TaxTotal/cac:TaxSubtotal", ns))

        # CONTROL: a stated line tax still writes the element.
        full = etree.fromstring(generate_peppol_invoice_xml(self._data(10.0, 10.0)))
        self.assertIsNotNone(full.find("cac:InvoiceLine/cac:TaxTotal", ns))

    def test_a_boolean_amount_is_a_named_failure_and_not_one(self):
        """`float(True)` is 1.0. A hand-edited `true` on a re-import must stop
        the import, not become a $1.00 bill."""
        data = self._data(10.0, 10.0)
        data["totals"]["payable"] = True

        with self.assertRaises(UBLGenerationError) as caught:
            generate_peppol_invoice_xml(data)

        self.assertIn("totals.payable", str(caught.exception))

    def test_a_boolean_on_a_negative_credit_note_is_not_flipped_into_an_amount(self):
        """The sign normaliser runs first on a credit note printed negative;
        it must not turn `True` into -1 before the check above sees it."""
        data = self._data(10.0, 10.0)
        data["type"] = "CreditNote"
        line = data["lines"][0]
        line["quantity"], line["line_total"] = -1.0, True
        for key in data["totals"]:
            data["totals"][key] = -10.0
        data["tax_subtotals"][0]["taxable_amount"] = -10.0

        with self.assertRaises(UBLGenerationError) as caught:
            generate_peppol_invoice_xml(data)

        self.assertIn("line 1: line_total", str(caught.exception))

    def test_an_absent_line_tax_is_still_a_named_failure(self):
        data = self._data(10.0, 10.0)
        del data["lines"][0]["tax_amount"]

        with self.assertRaises(UBLGenerationError) as caught:
            generate_peppol_invoice_xml(data)

        self.assertIn("line 1: tax_amount", str(caught.exception))
