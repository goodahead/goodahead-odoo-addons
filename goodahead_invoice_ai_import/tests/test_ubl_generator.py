# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""UBL generation from the extracted invoice JSON."""

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
            "seller": {"name": "NORDIC DEMO SUPPLIER OY", "vat": "FI10000001",
                       "country_code": "FI"},
            "buyer": {"name": "NORDIC DEMO OY", "vat": "FI20000002", "country_code": "FI"},
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
        """Core classifies the generated XML as `account.edi.xml.ubl_bis3`.

        The generator emits no `UBLVersionID`: Odoo 18.0 reads it before the
        CustomizationID and would classify the document as plain UBL 2.1.
        Control: an NLCIUS CustomizationID classifies as something else.
        """
        xml = generate_peppol_invoice_xml(self._data(44.95, 24.73))
        move = self.env["account.move"]

        def classify(raw):
            builder = move._get_ubl_cii_builder_from_xml_tree(etree.fromstring(raw))
            return builder._name if builder is not None else None

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
        """Core matches `res.currency.name` exactly.

        A lower-case or padded code matches nothing in `_import_currency`, which
        then silently substitutes the company currency.
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
        # The note must not be written to both Note and PaymentTerms/Note.
        data = self._data(10.0, 10.0)
        data["note"] = "Viivastyskorko 11,5 %. Huomautusaika 14 pv."
        xml = generate_peppol_invoice_xml(data).decode()
        self.assertEqual(xml.count(data["note"]), 1, "note must not be duplicated")

    # ------------------------------------------------------------------
    # Nullable fields fall back to their defaults
    #
    # `dict.get(key, default)` does not default a key present with value null,
    # and the prompt schema allows null for these fields.
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
        """Control: `PaymentMeans` is emitted at all for this fixture."""
        xml = generate_peppol_invoice_xml(self._with()).decode()

        self.assertIn("<cbc:PaymentMeansCode>30</cbc:PaymentMeansCode>", xml)

    def test_a_null_unit_code_does_not_abort_the_import(self):
        """lxml's `set()` rejects None, so a null unit code must be defaulted."""
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
        """A null amount raises a named error instead of becoming 0.00.

        Checked here as well as in the validator because the re-import path
        replays a hand-edited extraction without re-validating it.
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
        """Control for the test above."""
        xml = generate_peppol_invoice_xml(self._data(10.0, 10.0)).decode()

        self.assertIn("<cbc:PayableAmount", xml)

    def test_a_null_line_tax_omits_the_line_tax_total_and_writes_no_zero(self):
        """A document without per-line tax omits the line TaxTotal instead of writing 0.00.

        The line rate and the document subtotal, which Odoo's decoder reads, remain.
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
        """`float(True)` is 1.0, so a boolean amount must raise instead."""
        data = self._data(10.0, 10.0)
        data["totals"]["payable"] = True

        with self.assertRaises(UBLGenerationError) as caught:
            generate_peppol_invoice_xml(data)

        self.assertIn("totals.payable", str(caught.exception))

    def test_a_boolean_on_a_negative_credit_note_is_not_flipped_into_an_amount(self):
        """The credit-note sign normaliser must not turn `True` into -1 first."""
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
