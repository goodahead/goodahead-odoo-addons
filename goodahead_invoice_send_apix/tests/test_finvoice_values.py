# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The Finvoice values of an invoice and its PDF image.

Every built message must pass the Finvoice 3.0 schema. Invented data only.
"""

import unittest

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

import io  # noqa: E402
import zipfile  # noqa: E402

from odoo.tests import tagged  # noqa: E402

from odoo.addons.goodahead_einvoice_base.lib import finvoice  # noqa: E402

from .common import FakeApixSendClient, SendApixCommon  # noqa: E402

INVOICE_REPORT = "account.account_invoices"
BARCODE_IMG = "/report/barcode?barcode_type=Code128"
#: The page of the Finnish invoice layout (goodahead_finnish_invoice); the
#: ERP's stock invoice has none. Under the test runner a report renders as HTML.
FINNISH_LAYOUT = b'class="fi_page"'


def _package_image(package):
    """The ``invoice.pdf`` inside a sent package."""
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        return archive.read("invoice.pdf")


@tagged("post_install", "-at_install")
class TestFinvoiceValues(SendApixCommon):
    def _valid(self, values, receiver="einvoice"):
        xml = finvoice.write(values, receiver=receiver)
        self.assertEqual(finvoice.validate_schema(xml), [])
        return xml

    def test_invoice_values(self):
        product = self.env["product.product"].create(
            {"name": "ZZ Book", "default_code": "ZZ-BOOK", "uom_id": self.unit_pieces.id}
        )
        move = self._make_invoice(lines=[
            {"product_id": product.id, "quantity": 2, "price_unit": 20.0,
             "product_uom_id": self.unit_pieces.id},
            {"name": "ZZ consulting", "quantity": 3, "price_unit": 50.0,
             "product_uom_id": self.unit_hours.id},
            {"name": "ZZ coffee", "quantity": 1.5, "price_unit": 10.0,
             "product_uom_id": self.unit_kilograms.id},
        ], payment_reference="10001235")
        values = move._apix_finvoice_values(attempt_id=7)
        self.assertEqual(values["message"]["identifier"], f"{move.id}-7")
        self.assertEqual(values["message"]["sender_edicode"], "003700000011")
        self.assertEqual(values["message"]["sender_intermediator"], "APIX")
        self.assertEqual(values["receiver"],
                         {"edicode": "003700000019", "operator_identifier": "ZZOPER"})
        seller = values["seller"]
        self.assertEqual(seller["name"], "ZZ Sender Oy")
        self.assertEqual(seller["business_id"], "0000001-9")
        self.assertEqual(seller["iban"], "FI5112345600000051")
        self.assertEqual(seller["bic"], "ZZBAFIHH")
        self.assertEqual(values["buyer"]["name"], "ZZ Customer Oy")
        self.assertEqual(values["buyer"]["business_id"], "0000002-7")
        invoice = values["invoice"]
        self.assertEqual(invoice["type"], "invoice")
        self.assertEqual(invoice["number"], move.name)
        self.assertEqual(invoice["date"], "2026-11-25")
        self.assertFalse(invoice["original_number"])
        self.assertEqual(invoice["payment_reference"], "10001235")
        self.assertAlmostEqual(invoice["totals"]["untaxed"], move.amount_untaxed)
        self.assertAlmostEqual(invoice["totals"]["tax"], move.amount_tax)
        self.assertAlmostEqual(
            invoice["totals"]["total"] + invoice["totals"]["rounding"], move.amount_total
        )
        lines = values["lines"]
        self.assertEqual(len(lines), 3)
        book, hours, coffee = lines
        self.assertEqual(book["code"], "ZZ-BOOK")
        self.assertEqual(book["name"], "ZZ Book")
        self.assertEqual((book["quantity"], book["rec20_code"], book["unit_name"]), (2, "C62", "kpl"))
        self.assertAlmostEqual(book["price_unit"], 20.0)
        self.assertEqual(book["base_quantity"], 1)
        self.assertEqual(hours["code"], "")
        self.assertEqual(hours["name"], "ZZ consulting")
        self.assertEqual((hours["quantity"], hours["rec20_code"], hours["unit_name"]),
                         (3, "HUR", "h"))
        self.assertEqual((coffee["quantity"], coffee["rec20_code"]), (1.5, "KGM"))
        self.assertGreater(book["tax_percent"], 0)
        self.assertAlmostEqual(sum(line["untaxed"] for line in lines), move.amount_untaxed)
        self.assertAlmostEqual(sum(tax["amount"] for tax in values["taxes"]), move.amount_tax)
        self.assertAlmostEqual(sum(tax["base"] for tax in values["taxes"]), move.amount_untaxed)
        self.assertEqual(values["units_without_code"], [])
        epi = values["epi"]
        self.assertEqual(epi["iban"], "FI5112345600000051")
        self.assertEqual(epi["remittance_reference"], "10001235")
        self.assertAlmostEqual(epi["amount"], move.amount_total)
        self.assertEqual(epi["due_date"], move.invoice_date_due.isoformat())
        xml = self._valid(values)
        self.assertIn(b'QuantityUnitCodeUN="HUR"', xml)

    def test_buyer_is_the_invoice_contact_with_commercial_fallback(self):
        desk = self.env["res.partner"].create({
            "name": "ZZ Billing Desk", "type": "invoice", "parent_id": self.customer.id,
            "street": "Laskutuskuja 9",
        })
        values = self._make_invoice(customer=desk)._apix_finvoice_values()
        buyer = values["buyer"]
        self.assertEqual(buyer["name"], "ZZ Customer Oy")
        self.assertEqual(buyer["contact_name"], "ZZ Billing Desk")
        self.assertEqual(buyer["street"], "Laskutuskuja 9")
        self.assertEqual(buyer["city"], "Espoo")
        self.assertEqual(values["receiver"]["edicode"], "003700000019")
        self._valid(values)

    def test_line_priced_per_hundred_states_its_base_quantity(self):
        move = self._make_invoice(lines=[
            {"name": "ZZ screw 4x40", "quantity": 5, "price_unit": 12.5,
             "product_uom_id": self.unit_hundred.id},
        ])
        line = move._apix_finvoice_values()["lines"][0]
        self.assertEqual(line["quantity"], 500)
        self.assertEqual(line["rec20_code"], "C62")
        self.assertEqual(line["unit_name"], "kpl")
        self.assertEqual(line["base_quantity"], 100)
        self.assertAlmostEqual(line["price_unit"], 12.5)
        self.assertAlmostEqual(line["untaxed"], 62.5)
        xml = self._valid(move._apix_finvoice_values())
        self.assertIn(b"<UnitPriceBaseQuantity", xml)

    def test_unit_without_code_goes_with_its_name_and_is_listed(self):
        move = self._make_invoice(lines=[
            {"name": "ZZ pallet hire", "quantity": 2, "price_unit": 30.0,
             "product_uom_id": self.unit_without_code.id},
        ])
        values = move._apix_finvoice_values()
        line = values["lines"][0]
        self.assertIsNone(line["rec20_code"])
        self.assertEqual(line["unit_name"], "ZZ pallet")
        self.assertEqual(values["units_without_code"], ["ZZ pallet"])
        xml = self._valid(values)
        self.assertNotIn(b"QuantityUnitCodeUN", xml)
        self.assertIn(b'QuantityUnitCode="ZZ pallet"', xml)

    def test_credit_note_reversing_an_invoice_names_the_original(self):
        invoice = self._make_invoice()
        credit = invoice._reverse_moves([{"invoice_date": invoice.invoice_date}])
        credit.action_post()
        self.assertEqual(credit.reversed_entry_id, invoice)
        values = credit._apix_finvoice_values()
        self.assertEqual(values["invoice"]["type"], "credit_note")
        self.assertEqual(values["invoice"]["original_number"], invoice.name)
        self.assertGreater(values["invoice"]["totals"]["untaxed"], 0)
        self.assertGreater(values["lines"][0]["quantity"], 0)
        xml = self._valid(values)
        self.assertIn(f"<OriginalInvoiceNumber>{invoice.name}<".encode(), xml)
        self.assertIn(b"<InvoiceTypeCode>INV02<", xml)
        self.assertIn(b"-100,00", xml)

    def test_credit_note_without_an_original(self):
        credit = self._make_invoice(move_type="out_refund")
        values = credit._apix_finvoice_values()
        self.assertEqual(values["invoice"]["type"], "credit_note")
        self.assertFalse(values["invoice"]["original_number"])
        xml = self._valid(values)
        self.assertNotIn(b"OriginalInvoiceNumber", xml)

    def test_printed_letter_values(self):
        values = self._make_invoice()._apix_finvoice_values()
        self._valid(values, receiver="print")

    def test_invoice_of_the_second_company_opened_from_the_first(self):
        move = self._make_invoice(company=self.company_b)
        env = move.with_user(self.accountant).with_context(
            allowed_company_ids=[self.company.id, self.company_b.id]
        )
        self.assertEqual(env.env.company, self.company)
        values = env._apix_finvoice_values()
        self.assertEqual(values["seller"]["name"], "ZZ Second Company Oy")
        self.assertEqual(values["seller"]["business_id"], "0000003-5")
        self.assertEqual(values["seller"]["iban"], "FI1612345600000002")
        self.assertEqual(values["message"]["sender_edicode"], "003700000035")
        self.assertEqual(values["epi"]["iban"], "FI1612345600000002")
        self.assertEqual(values["epi"]["beneficiary_name"], "ZZ Second Company Oy")
        self._valid(values)

    def test_readiness_warns_only_about_units_the_send_leaves_uncoded(self):
        """Review R11, AC-48/AC-49: a unit the send codes through its
        reference unit (100 pcs → C62) is no warning; a unit without any
        code still is."""
        move = self._make_invoice(lines=[
            {"name": "ZZ boxes", "quantity": 2, "price_unit": 10.0,
             "product_uom_id": self.unit_hundred.id},
        ])
        self.assertEqual(move._apix_finvoice_values()["units_without_code"], [])
        keys = [item["key"] for item in move._apix_readiness("einvoice")["items"]]
        self.assertNotIn("units_without_code", keys)
        uncoded = self._make_invoice(lines=[
            {"name": "ZZ pallets", "quantity": 2, "price_unit": 10.0,
             "product_uom_id": self.unit_without_code.id},
        ])
        keys = [item["key"] for item in uncoded._apix_readiness("einvoice")["items"]]
        self.assertIn("units_without_code", keys)

    def test_address_and_operator_come_as_a_pair(self):
        """Review R3, ADR-0010: the contact's own e-invoice address goes with
        the contact's operator, never with the commercial customer's."""
        commercial = self._make_customer("ZZ Pair Group Oy", "003700000019", "0000006-0")
        contact = self.env["res.partner"].create({
            "name": "ZZ Pair Unit", "type": "invoice", "parent_id": commercial.id,
            "edicode": "003700000043",
        })
        self.assertFalse(contact.einvoice_operator_id)
        self.assertEqual(commercial.einvoice_operator_id, self.operator)
        move = self._make_invoice(contact)
        self.assertEqual(move._apix_finvoice_values()["receiver"],
                         {"edicode": "003700000043", "operator_identifier": ""})
        keys = [item["key"] for item in move._apix_readiness("einvoice")["items"]]
        self.assertIn("recipient_operator", keys, "the contact's address has no operator")
        # without an address of its own, the commercial customer's pair is used
        contact.edicode = False
        move.invalidate_recordset()
        self.assertEqual(move._apix_finvoice_values()["receiver"],
                         {"edicode": "003700000019", "operator_identifier": "ZZOPER"})

    def test_company_without_bank_account_still_builds_values(self):
        self.bank_account.active = False
        move = self._make_invoice(partner_bank_id=False, post=False)
        values = move._apix_finvoice_values()
        self.assertFalse(values["epi"]["iban"])
        self.assertFalse(values["seller"]["iban"])

    # -- the PDF image ---------------------------------------------------

    def test_pdf_image_is_the_printed_invoice_report(self):
        move = self._make_invoice()
        filename, content = move._apix_render_pdf()
        self.assertTrue(filename.endswith(".pdf"))
        printed = self.env["ir.actions.report"]._render_qweb_pdf(INVOICE_REPORT, move.ids)[0]
        self.assertEqual(content, printed)

    def _finnish_payable_invoice(self):
        """A payable invoice printed with the Finnish layout. The test
        chart's taxes are not Finnish: no tax."""
        if "fi_bank_barcode" not in self.env["account.move"]._fields:
            self.skipTest("the Finnish invoice layout is not installed")
        self.company.account_fiscal_country_id = self.finland
        move = self._make_invoice(
            payment_reference="10001235",
            lines=[{"name": "ZZ consulting", "quantity": 1, "price_unit": 100.0, "tax_ids": []}],
        )
        self.assertTrue(move._fi_uses_finnish_invoice_report())
        return move

    def test_button_package_carries_the_printed_finnish_invoice(self):
        """Review R13, AC-45: the image in the package sent by "Send via
        APIX" is the invoice as printed — the Finnish layout."""
        fake = FakeApixSendClient("accepted").patch(self)
        move = self._finnish_payable_invoice()
        result = move.with_user(self.accountant)._apix_send_now("button")
        self.assertEqual(result.outcome, "accepted", result.message)
        image = _package_image(fake.calls[0]["zip"])
        printed = self.env["ir.actions.report"]._render_qweb_pdf(INVOICE_REPORT, move.ids)[0]
        self.assertEqual(image, printed)
        self.assertIn(FINNISH_LAYOUT, image, "the Finnish invoice layout")

    def test_send_and_print_package_carries_the_printed_finnish_invoice(self):
        """Review R13, AC-45: from Send & Print the package carries the very
        invoice PDF the dialog printed and stored on the invoice."""
        fake = FakeApixSendClient("accepted").patch(self)
        move = self._finnish_payable_invoice()
        self._press_send(self.env["account.move.send.wizard"].with_user(self.accountant).with_context(
            active_model="account.move", active_ids=move.ids,
            allowed_company_ids=self.company.ids,
        ).create({"sending_methods": ["apix"]}))
        self.assertEqual(len(fake.calls), 1)
        image = _package_image(fake.calls[0]["zip"])
        stored = move.invoice_pdf_report_id
        self.assertTrue(stored, "the dialog stored the printed invoice")
        self.assertEqual(image, stored.raw)
        self.assertIn(FINNISH_LAYOUT, image, "the Finnish invoice layout")

    def test_credit_note_image_has_no_bank_bar_code(self):
        if "fi_bank_barcode" not in self.env["account.move"]._fields:
            self.skipTest("the Finnish invoice layout is not installed")
        # The Finnish layout prints for companies with Finland as fiscal
        # country; the test chart's taxes are not Finnish, so no tax here.
        self.company.account_fiscal_country_id = self.finland
        credit = self._make_invoice(
            move_type="out_refund", payment_reference="10001235",
            lines=[{"name": "ZZ refund", "quantity": 1, "price_unit": 100.0, "tax_ids": []}],
        )
        _filename, content = credit._apix_render_pdf()
        self.assertTrue(credit._fi_uses_finnish_invoice_report())
        self.assertNotIn(BARCODE_IMG.encode(), content)
