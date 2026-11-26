# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The fix-it wizard's Save also keeps the Business ID APIX found.

AddressQuery answers with the receiver's name, Business ID, e-invoice
address and operator. On Save with a picked lookup result, the Business ID
goes to the commercial partner's ``company_registry`` (company mode: the
company's) only when it is empty; never over an existing value. An opt-in
box, off by default and shown only for a Finnish Business ID when the
target's VAT is empty, also sets the VAT number ``FI`` + its digits. Entry by
hand fills neither. Every name and number here is invented.
"""

import unittest
from unittest.mock import patch

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from lxml import etree  # noqa: E402

from odoo.exceptions import AccessError  # noqa: E402
from odoo.tests import new_test_user, tagged  # noqa: E402

from odoo.addons.goodahead_apix.lib.apix_client import ApixClient  # noqa: E402

from .common import SendApixCommon  # noqa: E402

FIX = "goodahead.invoice.send.fix"


def _found(business_id, edicode="003712345678", operator="ZZOPER", name="ZZ No Id Oy"):
    return {"receiver_name": name, "business_id": business_id, "edicode": edicode,
            "operator": operator}


@tagged("post_install", "-at_install")
class TestFixBusinessId(SendApixCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        both = (cls.company | cls.company_b).ids
        cls.writer = new_test_user(
            cls.env, login="zz_bid_writer",
            groups="account.group_account_invoice,base.group_partner_manager",
            company_id=cls.company.id, company_ids=[(6, 0, both)],
        )
        cls.admin = new_test_user(
            cls.env, login="zz_bid_admin",
            groups="account.group_account_manager,base.group_erp_manager",
            company_id=cls.company.id, company_ids=[(6, 0, both)],
        )

    def setUp(self):
        super().setUp()
        self.customer_none = self._make_customer("ZZ No Id Oy")
        self.desk = self.env["res.partner"].create(
            {"name": "ZZ No Id Billing", "type": "invoice", "parent_id": self.customer_none.id}
        )

    def _wizard(self, user, **target):
        return self.env[FIX].with_user(user).with_context(
            allowed_company_ids=[self.company.id, self.company_b.id],
        ).create(target)

    def _lookup_and_pick(self, wizard, row):
        with patch.object(ApixClient, "address_query", autospec=True, return_value=[row]):
            wizard.action_lookup()
        wizard.result_ids[0].action_use()

    # -- invoice mode ---------------------------------------------------------------------

    def test_empty_business_id_is_filled_on_the_commercial_partner(self):
        move = self._make_invoice(customer=self.desk)
        wizard = self._wizard(self.accountant, move_id=move.id)
        self._lookup_and_pick(wizard, _found("1234567-8"))
        self.assertFalse(self.customer_none.company_registry, "nothing before Save")
        wizard.action_save()
        self.assertEqual(self.customer_none.company_registry, "1234567-8")
        # written to the commercial partner; the ERP syncs this commercial
        # field down to its contacts
        self.assertEqual(self.desk.company_registry, "1234567-8")
        self.assertFalse(self.customer_none.vat, "the VAT box was off")
        self.assertFalse(self.desk.vat)

    def test_an_existing_business_id_is_never_overwritten(self):
        self.customer_none.company_registry = "0000004-1"
        move = self._make_invoice(customer=self.customer_none)
        wizard = self._wizard(self.accountant, move_id=move.id)
        self._lookup_and_pick(wizard, _found("1234567-8"))
        wizard.action_save()
        self.assertEqual(self.customer_none.company_registry, "0000004-1")

    def test_vat_box_offered_only_for_a_finnish_business_id_and_an_empty_vat(self):
        move = self._make_invoice(customer=self.customer_none)
        wizard = self._wizard(self.accountant, move_id=move.id)
        self.assertFalse(wizard.vat_offer, "nothing found yet")
        self._lookup_and_pick(wizard, _found("1234567-8"))
        self.assertTrue(wizard.vat_offer)
        self.assertFalse(wizard.set_vat, "off by default")
        self.assertEqual(wizard.vat_proposal, "FI12345678")
        # not a Finnish Business ID: no offer
        other = self._wizard(self.accountant, move_id=move.id)
        self._lookup_and_pick(other, _found("DE123456789"))
        self.assertFalse(other.vat_offer)
        # the target already has a VAT number: no offer
        self.customer_none.vat = "FI00000041"
        third = self._wizard(self.accountant, move_id=move.id)
        self._lookup_and_pick(third, _found("1234567-8"))
        self.assertFalse(third.vat_offer)

    def test_vat_box_on_sets_the_vat_number(self):
        move = self._make_invoice(customer=self.desk)
        wizard = self._wizard(self.accountant, move_id=move.id)
        self._lookup_and_pick(wizard, _found("1234567-8"))
        wizard.set_vat = True
        wizard.action_save()
        self.assertEqual(self.customer_none.vat, "FI12345678")
        self.assertEqual(self.customer_none.company_registry, "1234567-8")
        # the ERP syncs the commercial partner's VAT number to its contacts
        self.assertEqual(self.desk.vat, "FI12345678")

    def test_vat_box_never_overwrites_a_vat_number(self):
        move = self._make_invoice(customer=self.customer_none)
        wizard = self._wizard(self.accountant, move_id=move.id)
        self._lookup_and_pick(wizard, _found("1234567-8"))
        wizard.set_vat = True  # ticked over RPC although not offered later
        self.customer_none.vat = "FI00000041"
        wizard.action_save()
        self.assertEqual(self.customer_none.vat, "FI00000041")

    def test_entry_by_hand_fills_nothing(self):
        move = self._make_invoice(customer=self.customer_none)
        wizard = self._wizard(self.accountant, move_id=move.id)
        wizard.write({"manual_edicode": "003712345678", "manual_operator_id": self.operator.id,
                      "set_vat": True})
        wizard.action_save()
        self.assertEqual(self.customer_none.edicode, "003712345678")
        self.assertFalse(self.customer_none.company_registry)
        self.assertFalse(self.customer_none.vat)

    def test_a_result_without_a_business_id_fills_nothing(self):
        move = self._make_invoice(customer=self.customer_none)
        wizard = self._wizard(self.accountant, move_id=move.id)
        self._lookup_and_pick(wizard, _found(""))
        self.assertFalse(wizard.vat_offer)
        wizard.set_vat = True
        wizard.action_save()
        self.assertFalse(self.customer_none.company_registry)
        self.assertFalse(self.customer_none.vat)

    def test_vat_box_is_in_the_dialog(self):
        view = self.env.ref("goodahead_invoice_send_apix.goodahead_invoice_send_fix_view_form")
        arch = etree.fromstring(self.env[FIX].get_view(view.id, "form")["arch"])
        box = arch.xpath("//field[@name='set_vat']")
        self.assertEqual(len(box), 1)
        container = box[0].getparent()
        self.assertIn("vat_offer", container.get("invisible") or "")
        self.assertTrue(arch.xpath("//label[@for='set_vat'][@string='Also set VAT number']"))

    # -- partner mode ---------------------------------------------------------------------

    def test_contact_mode_fills_the_commercial_partner(self):
        wizard = self._wizard(self.writer, target_partner_id=self.desk.id)
        self._lookup_and_pick(wizard, _found("1234567-8"))
        wizard.set_vat = True
        wizard.action_save()
        self.assertEqual(self.desk.edicode, "003712345678")
        self.assertEqual(self.customer_none.company_registry, "1234567-8")
        self.assertEqual(self.customer_none.vat, "FI12345678")

    def test_contact_mode_refused_without_the_right_to_change_contacts(self):
        wizard = self.env[FIX].sudo().create({
            "target_partner_id": self.desk.id, "lookup_company_id": self.company.id,
        }).with_user(self.accountant).sudo(False)
        self._lookup_and_pick(wizard.sudo(), _found("1234567-8"))
        wizard.sudo().set_vat = True
        with self.assertRaises(AccessError):
            wizard.action_save()
        self.assertFalse(self.customer_none.company_registry)
        self.assertFalse(self.customer_none.vat)

    # -- company mode ---------------------------------------------------------------------

    def test_company_mode_fills_the_company_when_empty(self):
        self.company_b.write({"company_registry": False, "vat": False})
        wizard = self._wizard(self.admin, target_company_id=self.company_b.id)
        self._lookup_and_pick(wizard, _found("1234567-8", name="ZZ Second Company Oy"))
        self.assertTrue(wizard.vat_offer)
        wizard.set_vat = True
        wizard.action_save()
        self.assertEqual(self.company_b.company_registry, "1234567-8")
        self.assertEqual(self.company_b.vat, "FI12345678")

    def test_company_mode_keeps_an_existing_business_id(self):
        wizard = self._wizard(self.admin, target_company_id=self.company_b.id)
        self._lookup_and_pick(wizard, _found("1234567-8", name="ZZ Second Company Oy"))
        self.assertFalse(wizard.vat_offer, "the company has a VAT number")
        wizard.action_save()
        self.assertEqual(self.company_b.company_registry, "0000003-5")
        self.assertEqual(self.company_b.vat, "FI00000035")
