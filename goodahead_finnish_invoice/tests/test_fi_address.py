# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Finnish address formatting helpers.

Pure Python: the helpers only read a few attributes of a contact, so plain
objects stand in for ERP records. Runs with plain unittest and under the ERP's
runner, like ``test_fi_bank_barcode.py``.
"""

import unittest

if __name__.startswith("odoo.addons."):
    from odoo.tests import BaseCase, tagged

    from odoo.addons.goodahead_finnish_invoice.lib import fi_address
else:  # plain unittest, no ERP
    from lib import fi_address

    BaseCase = unittest.TestCase

    def tagged(*_tags):
        return lambda cls: cls


FINLAND = "FI"
SWEDEN = "SE"


class _Contact:
    """The attributes of a contact the helpers read; item access like a record."""

    def __init__(self, name, parent=None, street="", street2="", zip="", city="", country=FINLAND, state=None):
        self.name = name
        self.street = street
        self.street2 = street2
        self.zip = zip
        self.city = city
        self.country_id = country
        self.state_id = state
        self.commercial_partner_id = parent.commercial_partner_id if parent else self
        self.display_name = f"{parent.name}, {name}" if parent else name

    def __getitem__(self, field):
        return getattr(self, field)


def _company():
    return _Contact("Example Buyer Oy", street="Esimerkkikatu 1", zip="00100", city="Helsinki")


@tagged("post_install", "-at_install")
class TestSamePlace(BaseCase):
    def test_no_delivery_address_is_the_same_place(self):
        company = _company()
        self.assertTrue(fi_address.same_place(company, None))
        self.assertTrue(fi_address.same_place(company, company))

    def test_another_contact_at_the_same_address(self):
        company = _company()
        site = _Contact("Reception", parent=company, street="Esimerkkikatu 1", zip="00100", city="Helsinki")
        self.assertTrue(fi_address.same_place(company, site))

    def test_surrounding_spaces_and_empty_parts_compare_equal(self):
        company = _company()
        site = _Contact("Reception", parent=company, street=" Esimerkkikatu 1 ", street2=None,
                        zip="00100", city="Helsinki ")
        self.assertTrue(fi_address.same_place(company, site))

    def test_a_different_street_city_or_postcode_is_another_place(self):
        company = _company()
        for changes in ({"street": "Varastotie 5"}, {"city": "Tampere"}, {"zip": "00200"}, {"street2": "B 12"}):
            with self.subTest(changes=changes):
                values = {"street": "Esimerkkikatu 1", "zip": "00100", "city": "Helsinki", **changes}
                site = _Contact("Warehouse", parent=company, **values)
                self.assertFalse(fi_address.same_place(company, site))

    def test_another_country_or_state_is_another_place(self):
        company = _company()
        abroad = _Contact("Branch", parent=company, street="Esimerkkikatu 1", zip="00100", city="Helsinki",
                          country=SWEDEN)
        self.assertFalse(fi_address.same_place(company, abroad))
        other_state = _Contact("Branch", parent=company, street="Esimerkkikatu 1", zip="00100", city="Helsinki",
                               state="X")
        self.assertFalse(fi_address.same_place(company, other_state))


@tagged("post_install", "-at_install")
class TestDeliveryName(BaseCase):
    def test_site_of_the_same_customer_uses_its_own_name(self):
        company = _company()
        site = _Contact("Warehouse", parent=company, street="Varastotie 5", city="Tampere")
        self.assertEqual(fi_address.delivery_name(site, company), "Warehouse")

    def test_site_without_a_name_falls_back_to_the_full_name(self):
        company = _company()
        site = _Contact("", parent=company, street="Varastotie 5", city="Tampere")
        self.assertEqual(fi_address.delivery_name(site, company), "Example Buyer Oy, ")

    def test_site_of_another_company_keeps_the_full_name(self):
        company = _company()
        other = _Contact("Another Example Oy")
        site = _Contact("Reception", parent=other, street="Vieraskatu 3", city="Oulu")
        self.assertEqual(fi_address.delivery_name(site, company), "Another Example Oy, Reception")


if __name__ == "__main__":
    unittest.main()
