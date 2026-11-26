# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Order numbers, buyer reference and delivery party in the invoice values
(finvoice content T13; AC-09, AC-10, AC-11, AC-14, AC-15, AC-27). Every
message built here is checked against the Finvoice 3.0 schema."""

import unittest

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo.tests import tagged

from odoo.addons.goodahead_einvoice_base.lib import finvoice

from .common import SendApixCommon


@tagged("post_install", "-at_install")
class TestFinvoiceValuesReferences(SendApixCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Partner = cls.env["res.partner"]
        cls.site = Partner.create({
            "name": "ZZ Site B", "type": "delivery", "parent_id": cls.customer.id,
            "street": "Toimituskatu 5", "zip": "20100", "city": "Turku",
            "country_id": cls.finland.id,
        })
        cls.same_address = Partner.create({
            "name": "ZZ Same Place", "type": "delivery", "parent_id": cls.customer.id,
            "street": " asiakkaantie   2 ", "zip": "00200", "city": "ESPOO",
            "country_id": cls.finland.id,
        })
        cls.no_zip = Partner.create({
            "name": "ZZ Site C", "type": "delivery", "parent_id": cls.customer.id,
            "street": "Toimituskatu 7", "city": "Turku", "country_id": cls.finland.id,
        })
        cls.short_name = Partner.create({
            "name": "Z", "type": "delivery", "parent_id": cls.customer.id,
            "street": "Toimituskatu 9", "zip": "20100", "city": "Turku",
            "country_id": cls.finland.id,
        })

    def _values(self, move):
        values = move._apix_finvoice_values()
        self.assertEqual(finvoice.validate_schema(finvoice.write(values)), [])
        return values

    # -- AC-09 ---------------------------------------------------------------------

    def test_references_go_to_their_own_places(self):
        move = self._make_invoice(ref="PO-4711", invoice_origin="S00012",
                                  einvoice_buyer_reference="Test User",
                                  payment_reference="RF471234567890")
        values = self._values(move)
        invoice = values["invoice"]
        self.assertEqual((invoice["order_number"], invoice["seller_reference"],
                          invoice["buyer_reference"]), ("PO-4711", "S00012", "Test User"))
        self.assertEqual(values["epi"]["remittance_reference"], "RF471234567890")
        root = finvoice.parse(finvoice.write(values))[0].element
        details = root.find("InvoiceDetails")
        self.assertEqual(details.findtext("OrderIdentifier"), "PO-4711")
        self.assertEqual(details.findtext("SellerReferenceIdentifier"), "S00012")
        self.assertEqual(details.findtext("BuyerReferenceIdentifier"), "Test User")

    def test_nothing_without_references(self):
        invoice = self._values(self._make_invoice())["invoice"]
        self.assertEqual((invoice["order_number"], invoice["seller_reference"],
                          invoice["buyer_reference"]), (None, None, None))

    # -- AC-10 ---------------------------------------------------------------------

    def test_an_echo_of_our_order_number_is_not_the_customers(self):
        invoice = self._values(self._make_invoice(ref="S00012", invoice_origin="S00012"))["invoice"]
        self.assertIsNone(invoice["order_number"])
        self.assertEqual(invoice["seller_reference"], "S00012")

    def test_grouped_invoice(self):
        move = self._make_invoice(ref="PO-1, S00012, PO-2, S00013",
                                  invoice_origin="S00012, S00013")
        invoice = self._values(move)["invoice"]
        self.assertEqual(invoice["order_number"], "PO-1, PO-2")
        self.assertEqual(invoice["seller_reference"], "S00012, S00013")

    def test_reversing_credit_note_takes_the_reversed_invoices(self):
        original = self._make_invoice(ref="PO-1, S00012", invoice_origin="S00012")
        credit = original._reverse_moves([{"ref": f"Reversal of: {original.name}"}])
        credit.action_post()
        invoice = self._values(credit)["invoice"]
        self.assertEqual(invoice["order_number"], "PO-1")
        self.assertNotIn("Reversal", invoice["order_number"])

    def test_reversing_an_invoice_without_one(self):
        original = self._make_invoice(ref="S00012", invoice_origin="S00012")
        credit = original._reverse_moves([{"ref": f"Reversal of: {original.name}"}])
        credit.action_post()
        self.assertIsNone(self._values(credit)["invoice"]["order_number"])

    def test_plain_credit_note_uses_its_own_reference(self):
        credit = self._make_invoice(move_type="out_refund", ref="PO-9, S00020",
                                    invoice_origin="S00020")
        self.assertEqual(self._values(credit)["invoice"]["order_number"], "PO-9")

    # -- AC-11 ---------------------------------------------------------------------

    def test_too_long_references_are_left_out_never_cut(self):
        move = self._make_invoice(ref="P" * 71, invoice_origin=", ".join(["S00000"] * 12))
        values = self._values(move)
        self.assertIsNone(values["invoice"]["order_number"])
        self.assertIsNone(values["invoice"]["seller_reference"])
        references = move._apix_finvoice_references()
        self.assertEqual(references["order_number_problem"], "too_long")
        self.assertEqual(references["seller_reference_problem"], "too_long")

    def test_seventy_characters_fit(self):
        move = self._make_invoice(ref="P" * 70, invoice_origin="S" * 70)
        invoice = self._values(move)["invoice"]
        self.assertEqual((invoice["order_number"], invoice["seller_reference"]),
                         ("P" * 70, "S" * 70))

    # -- AC-27 ---------------------------------------------------------------------

    def test_numeric_order_number_with_a_payment_reference(self):
        move = self._make_invoice(invoice_origin="1232", payment_reference="1009")
        values = self._values(move)
        self.assertEqual(values["invoice"]["seller_reference"], "1232")
        self.assertEqual(values["epi"]["remittance_reference"], "1009")
        data = finvoice.to_invoice_data(finvoice.parse(finvoice.write(values))[0])
        self.assertEqual(data["payment_reference"], "1009")

    def test_numeric_order_number_without_one_is_withheld(self):
        move = self._make_invoice(invoice_origin="1232", payment_reference=False)
        values = self._values(move)
        self.assertIsNone(values["invoice"]["seller_reference"])
        self.assertTrue(move._apix_finvoice_references()["seller_reference_withheld"])
        data = finvoice.to_invoice_data(finvoice.parse(finvoice.write(values))[0])
        self.assertNotEqual(data["payment_reference"], "1232")

    # -- AC-14, AC-15 ---------------------------------------------------------------

    def test_different_delivery_address_is_sent(self):
        move = self._make_invoice(partner_shipping_id=self.site.id)
        values = self._values(move)
        self.assertEqual(values["delivery"], {
            "name": "ZZ Site B", "street": "Toimituskatu 5", "zip": "20100", "city": "Turku",
            "country_code": "FI",
        })
        self.assertEqual(values["buyer"]["name"], "ZZ Customer Oy")
        self.assertEqual(move._apix_finvoice_delivery()["state"], "sent")

    def test_second_street_line_is_sent(self):
        """review F6: the second street line is compared, so it is sent too,
        as the second DeliveryStreetName (schema-checked by `_values`)."""
        self.site.street2 = "Building C, loading bay 4"
        values = self._values(self._make_invoice(partner_shipping_id=self.site.id))
        self.assertEqual(values["delivery"]["street2"], "Building C, loading bay 4")
        root = finvoice.parse(finvoice.write(values))[0].element
        streets = root.findall(
            "DeliveryPartyDetails/DeliveryPostalAddressDetails/DeliveryStreetName")
        self.assertEqual([street.text for street in streets],
                         ["Toimituskatu 5", "Building C, loading bay 4"])

    def test_an_address_differing_only_in_its_second_street_line(self):
        self.same_address.street2 = "Warehouse door 7 " + "x" * 40
        move = self._make_invoice(partner_shipping_id=self.same_address.id)
        values = self._values(move)
        self.assertEqual(move._apix_finvoice_delivery()["state"], "sent")
        root = finvoice.parse(finvoice.write(values))[0].element
        streets = root.findall(
            "DeliveryPartyDetails/DeliveryPostalAddressDetails/DeliveryStreetName")
        self.assertEqual(len(streets), 2)
        self.assertEqual(len(streets[1].text), 35)

    def test_one_character_second_street_line_is_not_sent(self):
        self.site.street2 = "B"
        values = self._values(self._make_invoice(partner_shipping_id=self.site.id))
        self.assertNotIn("street2", values["delivery"])

    def test_long_delivery_name_is_cut_in_the_message(self):
        self.site.name = "ZZ Site With A Considerably Long Delivery Name Oy"
        values = self._values(self._make_invoice(partner_shipping_id=self.site.id))
        root = finvoice.parse(finvoice.write(values))[0].element
        name = root.findtext("DeliveryPartyDetails/DeliveryOrganisationName")
        self.assertEqual(len(name), 35)

    def test_no_delivery_party_for_the_contact_itself_or_the_same_address(self):
        for partner, state in ((self.customer, "none"), (self.same_address, "same")):
            with self.subTest(partner=partner.name):
                move = self._make_invoice(partner_shipping_id=partner.id)
                self.assertNotIn("delivery", self._values(move))
                self.assertEqual(move._apix_finvoice_delivery()["state"], state)

    def test_incomplete_delivery_address_is_left_out_and_named(self):
        cases = ((self.no_zip, ["zip"]), (self.short_name, ["name"]))
        for partner, missing in cases:
            with self.subTest(partner=partner.name):
                move = self._make_invoice(partner_shipping_id=partner.id)
                self.assertNotIn("delivery", self._values(move))
                delivery = move._apix_finvoice_delivery()
                self.assertEqual((delivery["state"], delivery["missing"]),
                                 ("incomplete", missing))
