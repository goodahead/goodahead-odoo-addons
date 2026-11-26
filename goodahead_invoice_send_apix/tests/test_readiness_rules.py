# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Pure tests of the readiness rules. Contract: public-api.md §5.

Runs without the ERP:

    python -I -m unittest discover \
        -s goodahead/goodahead_invoice_send_apix/tests -t goodahead

and inside the ERP's test runner. Every name and code here is invented.
"""

import glob
import os
import unittest

try:  # inside the ERP test runner
    from odoo.tests import BaseCase as _TestCase
    from odoo.tests import tagged
except ImportError:  # plain unittest, no ERP installed
    _TestCase = unittest.TestCase

    def tagged(*_tags):
        return lambda cls: cls

from ..lib import readiness

LIB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "lib")

FIX_AT = {
    "sending_off": None,
    "recipient_edicode": "fix_it",
    "recipient_operator": "fix_it",
    "recipient_name": "contact",
    "recipient_postal_address": "contact",
    "recipient_business_id": "contact",
    "recipient_email": "contact",
    "sender_name": "company",
    "sender_business_id": "company",
    "sender_bank_account": "company",
    "due_date": "contact",
    "units_without_code": "unit_codes",
}


def _recipient(**overrides):
    values = {
        "name": "Example Buyer Oy",
        "edicode": "003712345671",
        "operator_identifier": "ZZOPERATOR",
        "operator_listed": True,
        "business_id": "1234567-1",
        "vat": "FI12345671",
        "street": "Esimerkkikatu 1",
        "zip": "00100",
        "city": "Helsinki",
        "country_code": "FI",
        "email": "buyer@example.com",
    }
    values.update(overrides)
    return values


def _sender(**overrides):
    values = {
        "name": "Example Seller Oy",
        "business_id": "7654321-2",
        "edicode": False,
        "iban": "FI2112345600000785",
        "bic": "ZZBAFIHH",
        "street": "Myyjäntie 2",
        "zip": "00200",
        "city": "Espoo",
        "country_code": "FI",
    }
    values.update(overrides)
    return values


def _lines(*units):
    return [{"unit_name": name, "rec20_code": code} for name, code in units]


def _evaluate(channel="einvoice", recipient=None, sender=None, **kwargs):
    kwargs.setdefault("sending_enabled", True)
    kwargs.setdefault("lines", _lines(("kpl", "C62"), ("h", "HUR")))
    kwargs.setdefault("due_date", "2026-12-31")
    return readiness.evaluate(
        channel,
        recipient if recipient is not None else _recipient(),
        sender if sender is not None else _sender(),
        **kwargs,
    )


def _keys(result, severity=None):
    return [i["key"] for i in result["items"] if severity is None or i["severity"] == severity]


def _item(result, key):
    return next(i for i in result["items"] if i["key"] == key)


@tagged("post_install", "-at_install")
class TestReadinessShape(_TestCase):
    def test_complete_invoice_is_ready_on_every_channel(self):
        for channel in ("einvoice", "letter", "email"):
            with self.subTest(channel=channel):
                result = _evaluate(channel)
                self.assertEqual(result, {"ready": True, "items": []})

    def test_item_shape(self):
        result = _evaluate(recipient=_recipient(edicode=""))
        item = _item(result, "recipient_edicode")
        self.assertEqual(set(item), {"key", "severity", "party", "fix_at", "message"})
        self.assertEqual(item["severity"], "block")
        self.assertEqual(item["party"], "recipient")
        self.assertEqual(item["fix_at"], "fix_it")
        self.assertTrue(item["message"])

    def test_every_item_points_where_it_is_fixed(self):
        result = _evaluate(
            recipient={"operator_listed": False},
            sender={},
            due_date=None,
            lines=_lines(("laatikko", None)),
        )
        result_letter = _evaluate("letter", recipient={}, sender={}, lines=_lines(("x", None)))
        result_email = _evaluate("email", recipient={})
        seen = {}
        for res in (result, result_letter, result_email):
            for item in res["items"]:
                seen[item["key"]] = item["fix_at"]
        expected = dict(FIX_AT)
        expected.pop("sending_off")
        self.assertEqual(seen, expected)

    def test_unknown_channel_is_an_error(self):
        with self.assertRaises(ValueError):
            _evaluate("fax")


@tagged("post_install", "-at_install")
class TestEinvoiceChannel(_TestCase):
    def _blocks(self, key, **kwargs):
        result = _evaluate("einvoice", **kwargs)
        self.assertFalse(result["ready"], result)
        self.assertEqual(_keys(result), [key])
        self.assertEqual(_item(result, key)["severity"], "block")
        return _item(result, key)

    def _warns(self, key, **kwargs):
        result = _evaluate("einvoice", **kwargs)
        self.assertTrue(result["ready"], result)
        self.assertEqual(_keys(result), [key])
        self.assertEqual(_item(result, key)["severity"], "warn")
        return _item(result, key)

    def test_recipient_edicode_blocks(self):
        self._blocks("recipient_edicode", recipient=_recipient(edicode=False))

    def test_recipient_operator_missing_blocks(self):
        self._blocks(
            "recipient_operator",
            recipient=_recipient(operator_identifier="", operator_listed=False),
        )

    def test_recipient_operator_not_on_the_list_blocks(self):
        item = self._blocks("recipient_operator", recipient=_recipient(operator_listed=False))
        self.assertIn("ZZOPERATOR", item["message"])

    def test_recipient_name_blocks(self):
        self._blocks("recipient_name", recipient=_recipient(name="  "))

    def test_recipient_postal_address_warns(self):
        for field in ("street", "zip", "city", "country_code"):
            with self.subTest(field=field):
                self._warns("recipient_postal_address", recipient=_recipient(**{field: False}))

    def test_recipient_business_id_warns_when_both_missing(self):
        self._warns("recipient_business_id", recipient=_recipient(business_id=False, vat=False))

    def test_recipient_business_id_warning_names_the_apix_note_and_where_to_fix_it(self):
        result = _evaluate(recipient=_recipient(business_id=False, vat=False))
        item = _item(result, "recipient_business_id")
        self.assertEqual(item["severity"], "warn")
        self.assertTrue(result["ready"], "a warning never blocks")
        # APIX accepts the invoice but notes it ("HUOM: Ostajan Y-tunnus/ALV-tunniste puuttuu")
        self.assertIn("Ostajan Y-tunnus/ALV-tunniste puuttuu", item["message"])
        # where to fix it: the customer's Business ID / VAT, or the lookup
        self.assertIn("Business ID", item["message"])
        self.assertIn("VAT", item["message"])
        self.assertIn("Look up e-invoice address", item["message"])

    def test_recipient_business_id_warns_on_einvoice_only(self):
        recipient = _recipient(business_id=False, vat=False)
        for channel in ("letter", "email"):
            with self.subTest(channel=channel):
                self.assertNotIn("recipient_business_id", _keys(_evaluate(channel, recipient)))

    def test_business_id_or_vat_is_enough(self):
        self.assertEqual(_evaluate(recipient=_recipient(business_id=False))["items"], [])
        self.assertEqual(_evaluate(recipient=_recipient(vat=False))["items"], [])

    def test_email_is_not_asked(self):
        self.assertEqual(_evaluate(recipient=_recipient(email=False))["items"], [])

    def test_sender_name_blocks(self):
        item = self._blocks("sender_name", sender=_sender(name=""))
        self.assertEqual(item["party"], "sender")

    def test_sender_business_id_blocks(self):
        self._blocks("sender_business_id", sender=_sender(business_id=""))

    def test_sender_with_own_edicode_needs_no_business_id(self):
        result = _evaluate(sender=_sender(business_id="", edicode="003776543212"))
        self.assertEqual(result, {"ready": True, "items": []})

    def test_sender_bank_account_blocks(self):
        for field in ("iban", "bic"):
            with self.subTest(field=field):
                self._blocks("sender_bank_account", sender=_sender(**{field: None}))

    def test_sender_postal_address_is_not_asked(self):
        result = _evaluate(sender=_sender(street="", zip="", city="", country_code=""))
        self.assertEqual(result["items"], [])

    def test_due_date_blocks(self):
        self._blocks("due_date", due_date=None)

    def test_units_without_code_warn_and_name_the_units(self):
        item = self._warns(
            "units_without_code",
            lines=_lines(("kpl", "C62"), ("laatikko", None), ("rulla", ""), ("laatikko", None)),
        )
        self.assertEqual(item["party"], "line")
        self.assertIn("laatikko", item["message"])
        self.assertIn("rulla", item["message"])
        self.assertEqual(item["message"].count("laatikko"), 1)

    def test_no_lines_no_unit_warning(self):
        self.assertEqual(_evaluate(lines=[])["items"], [])
        self.assertEqual(_evaluate(lines=None)["items"], [])

    def test_everything_missing_lists_every_item(self):
        result = _evaluate(
            recipient={"operator_listed": False},
            sender={},
            due_date=None,
            lines=_lines(("laatikko", None)),
        )
        self.assertFalse(result["ready"])
        self.assertEqual(
            set(_keys(result, "block")),
            {
                "recipient_edicode",
                "recipient_operator",
                "recipient_name",
                "sender_name",
                "sender_business_id",
                "sender_bank_account",
                "due_date",
            },
        )
        self.assertEqual(
            set(_keys(result, "warn")),
            {"recipient_postal_address", "recipient_business_id", "units_without_code"},
        )

    def test_foreign_customer_without_business_id_is_ready(self):
        # AC-19
        foreign = _recipient(
            name="Example GmbH",
            edicode="ZZ0099887766",
            business_id=False,
            vat=False,
            street="Beispielstraße 3",
            zip="10115",
            city="Berlin",
            country_code="DE",
        )
        result = _evaluate(recipient=foreign)
        self.assertTrue(result["ready"])
        self.assertEqual(_keys(result, "block"), [])
        self.assertEqual(_keys(result), ["recipient_business_id"])


@tagged("post_install", "-at_install")
class TestLetterChannel(_TestCase):
    def test_letter_needs_name_and_full_postal_address(self):
        for field in ("street", "zip", "city", "country_code"):
            with self.subTest(field=field):
                result = _evaluate("letter", recipient=_recipient(**{field: ""}))
                self.assertFalse(result["ready"])
                self.assertEqual(_keys(result, "block"), ["recipient_postal_address"])
        result = _evaluate("letter", recipient=_recipient(name=""))
        self.assertEqual(_keys(result, "block"), ["recipient_name"])

    def test_letter_needs_no_e_invoice_details(self):
        result = _evaluate(
            "letter",
            recipient=_recipient(edicode="", operator_identifier="", operator_listed=False, business_id="", vat=""),
            sender=_sender(business_id="", iban="", bic=""),
            due_date=None,
        )
        self.assertEqual(result, {"ready": True, "items": []})

    def test_letter_needs_the_sender_name(self):
        result = _evaluate("letter", sender=_sender(name=""))
        self.assertEqual(_keys(result, "block"), ["sender_name"])

    def test_letter_warns_about_units(self):
        result = _evaluate("letter", lines=_lines(("laatikko", None)))
        self.assertTrue(result["ready"])
        self.assertEqual(_keys(result), ["units_without_code"])


@tagged("post_install", "-at_install")
class TestEmailChannel(_TestCase):
    def test_only_the_email_blocks(self):
        result = _evaluate(
            "email",
            recipient=_recipient(email="", edicode="", name="", street=""),
            sender=_sender(name="", business_id="", iban=""),
            due_date=None,
            lines=_lines(("laatikko", None)),
        )
        self.assertFalse(result["ready"])
        self.assertEqual(_keys(result), ["recipient_email"])
        self.assertEqual(_item(result, "recipient_email")["fix_at"], "contact")

    def test_email_with_address_is_ready_while_sending_is_off(self):
        result = _evaluate("email", sending_enabled=False)
        self.assertEqual(result, {"ready": True, "items": []})


@tagged("post_install", "-at_install")
class TestSendingOff(_TestCase):
    def test_sending_off_is_the_only_item(self):
        # AC-28: nothing else is shown while the company's sending is off.
        for channel in ("einvoice", "letter"):
            with self.subTest(channel=channel):
                result = _evaluate(
                    channel,
                    recipient={},
                    sender={},
                    due_date=None,
                    sending_enabled=False,
                    lines=_lines(("laatikko", None)),
                )
                self.assertFalse(result["ready"])
                self.assertEqual(len(result["items"]), 1)
                item = result["items"][0]
                self.assertEqual(item["key"], "sending_off")
                self.assertEqual(item["severity"], "block")
                self.assertEqual(item["party"], "company")
                self.assertIsNone(item["fix_at"])


@tagged("post_install", "-at_install")
class TestPartnerLevel(_TestCase):
    def test_partner_level_skips_invoice_items(self):
        # res.partner.send_readiness: no invoice, so no due date or units.
        result = readiness.evaluate(
            "einvoice",
            _recipient(),
            _sender(),
            sending_enabled=True,
            lines=[],
            due_date=None,
            invoice_level=False,
        )
        self.assertEqual(result, {"ready": True, "items": []})


@tagged("post_install", "-at_install")
class TestPurity(_TestCase):
    def test_no_erp_import_in_lib(self):
        # AC-20: the rules are plain Python; no ERP, no network.
        for path in glob.glob(os.path.join(LIB_DIR, "**", "*.py"), recursive=True):
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            with self.subTest(path=os.path.basename(path)):
                self.assertNotRegex(source, r"(?m)^\s*(from|import)\s+odoo\b")
                self.assertNotRegex(source, r"(?m)^\s*(from|import)\s+(requests|urllib|http|socket)\b")

    def test_inputs_are_not_mutated(self):
        recipient, sender, lines = _recipient(), _sender(), _lines(("x", None))
        before = (dict(recipient), dict(sender), [dict(x) for x in lines])
        _evaluate(recipient=recipient, sender=sender, lines=lines)
        self.assertEqual((recipient, sender, lines), before)


# ---------------------------------------------------------------------------
# finvoice content T14: VAT, buyer VAT number, reference and delivery gaps
# — every one a warning
# ---------------------------------------------------------------------------

CONTENT_FIX_AT = {
    "vat_unclassified_tax": "taxes",
    "vat_line_no_percentage_tax": "invoice",
    "vat_line_mixed_categories": "taxes",
    "vat_line_tied_categories": "taxes",
    "vat_shared_reason": "taxes",
    "vat_settings_left_out": "taxes",
    "buyer_vat_missing": "contact",
    "order_number_too_long": "invoice",
    "seller_reference_too_long": "invoice",
    "seller_reference_withheld": "invoice",
    "delivery_address_incomplete": "contact",
}


def _tax(tax_id, name, category):
    return {"tax_id": tax_id, "name": name, "category": category}


def _vat_line(label, warning=None, rate=0.0, category=None, taxes=()):
    return {"line": label, "name": label, "rate": rate, "category": category,
            "warning": warning, "taxes": list(taxes)}


def _all_gaps(**overrides):
    kwargs = {
        "vat_lines": [
            _vat_line("Line 1", taxes=[_tax(9, "0%", None)]),
            _vat_line("Line 2", "no_percentage_tax"),
            _vat_line("Line 3", "mixed_categories", 24.0, "S",
                      [_tax(1, "24%", "S"), _tax(5, "0% EU S", "AE")]),
            _vat_line("Line 4", "tied_categories", 0.0, None,
                      [_tax(3, "0% EU G", "K"), _tax(4, "0% Export", "G")]),
            _vat_line("Line 5", rate=0.0, category="E",
                      taxes=[dict(_tax(8, "0% FR exempt", "E"), left_out=["exemption_code"])]),
        ],
        "vat_entries": [
            {"percent": 0.0, "category": "E", "tax_ids": [6, 7], "warning": "shared_reason"},
            {"percent": 0.0, "category": "AE", "tax_ids": [5], "warning": None},
        ],
        "tax_names": {6: "0% Exempt A", 7: "0% Exempt B"},
        "references": {"order_number_problem": "too_long",
                       "seller_reference_problem": "too_long",
                       "seller_reference_withheld": True},
        "delivery": {"state": "incomplete", "missing": ["zip"]},
        "buyer_vat_present": False,
    }
    kwargs.update(overrides)
    return kwargs


@tagged("post_install", "-at_install")
class TestContentWarnings(_TestCase):
    def test_every_gap_warns_and_never_blocks(self):
        result = _evaluate(**_all_gaps())
        self.assertTrue(result["ready"])
        self.assertEqual(sorted(_keys(result)), sorted(CONTENT_FIX_AT))
        self.assertEqual(_keys(result, "block"), [])
        for item in result["items"]:
            with self.subTest(key=item["key"]):
                self.assertEqual(item["severity"], "warn")
                self.assertEqual(item["fix_at"], CONTENT_FIX_AT[item["key"]])
                self.assertTrue(item["message"])

    def test_nothing_without_gaps(self):
        result = _evaluate(
            vat_lines=[_vat_line("Line 1", rate=24.0, category="S", taxes=[_tax(1, "24%", "S")])],
            vat_entries=[{"percent": 24.0, "category": "S", "tax_ids": [1], "warning": None}],
            references={}, delivery={"state": "sent", "missing": []}, buyer_vat_present=True,
        )
        self.assertEqual(result, {"ready": True, "items": []})

    def test_unclassified_tax_is_named_once(self):
        result = _evaluate(vat_lines=[
            _vat_line("Line 1", taxes=[_tax(9, "0% ZZ", None)]),
            _vat_line("Line 2", taxes=[_tax(9, "0% ZZ", None)]),
        ])
        items = [i for i in result["items"] if i["key"] == "vat_unclassified_tax"]
        self.assertEqual(len(items), 1)
        self.assertIn("0% ZZ", items[0]["message"])
        self.assertIn("Administrator", items[0]["message"])

    def test_messages_name_the_lines_and_taxes(self):
        result = _evaluate(**_all_gaps())
        self.assertIn("Line 2", _item(result, "vat_line_no_percentage_tax")["message"])
        self.assertIn("Line 3", _item(result, "vat_line_mixed_categories")["message"])
        self.assertIn("Line 4", _item(result, "vat_line_tied_categories")["message"])
        shared = _item(result, "vat_shared_reason")["message"]
        self.assertIn("0% Exempt A", shared)
        self.assertIn("0% Exempt B", shared)
        self.assertIn("70", _item(result, "order_number_too_long")["message"])
        self.assertIn("70", _item(result, "seller_reference_too_long")["message"])
        self.assertIn("payment reference", _item(result, "seller_reference_withheld")["message"])
        # review F8: labels, not field names
        self.assertIn("post code", _item(result, "delivery_address_incomplete")["message"])
        self.assertNotIn("zip", _item(result, "delivery_address_incomplete")["message"])
        self.assertIn("VAT number", _item(result, "buyer_vat_missing")["message"])
        self.assertIn("0% FR exempt", _item(result, "vat_settings_left_out")["message"])

    def test_buyer_vat_only_for_reverse_charge_or_intra_eu(self):
        entries = [{"percent": 0.0, "category": "G", "tax_ids": [4], "warning": None}]
        self.assertNotIn("buyer_vat_missing",
                         _keys(_evaluate(vat_entries=entries, buyer_vat_present=False)))
        for category in ("AE", "K"):
            entries = [{"percent": 0.0, "category": category, "tax_ids": [4], "warning": None}]
            with self.subTest(category=category):
                self.assertIn("buyer_vat_missing",
                              _keys(_evaluate(vat_entries=entries, buyer_vat_present=False)))
                self.assertNotIn("buyer_vat_missing",
                                 _keys(_evaluate(vat_entries=entries, buyer_vat_present=True)))

    def test_buyer_vat_for_a_mixed_line_with_reverse_charge(self):
        """review F5: 24 % S + 0 % AE on one line resolves to S, so no AE
        entry exists; the line's own taxes still need the buyer's VAT number."""
        lines = [_vat_line("Line 1", "mixed_categories", 24.0, "S",
                           [_tax(1, "24%", "S"), _tax(5, "0% EU services", "AE")])]
        entries = [{"percent": 24.0, "category": "S", "tax_ids": [1, 5], "warning": None}]
        self.assertIn("buyer_vat_missing", _keys(_evaluate(
            vat_lines=lines, vat_entries=entries, buyer_vat_present=False)))
        self.assertNotIn("buyer_vat_missing", _keys(_evaluate(
            vat_lines=lines, vat_entries=entries, buyer_vat_present=True)))
        plain = [_vat_line("Line 1", rate=24.0, category="S", taxes=[_tax(1, "24%", "S")])]
        self.assertNotIn("buyer_vat_missing", _keys(_evaluate(
            vat_lines=plain, vat_entries=entries[:0], buyer_vat_present=False)))

    def test_one_warning_for_a_buyer_without_any_identifier(self):
        """A reverse-charge invoice to a buyer with neither Business ID nor
        VAT number: the VAT number warning only (it fixes both); without the
        reverse charge the Business ID / VAT warning stays."""
        bare = _recipient(business_id=False, vat=False)
        entries = [{"percent": 0.0, "category": "AE", "tax_ids": [5], "warning": None}]
        keys = _keys(_evaluate(recipient=bare, vat_entries=entries, buyer_vat_present=False))
        self.assertIn("buyer_vat_missing", keys)
        self.assertNotIn("recipient_business_id", keys)
        keys = _keys(_evaluate(recipient=bare, vat_entries=entries[:0], buyer_vat_present=False))
        self.assertIn("recipient_business_id", keys)
        self.assertNotIn("buyer_vat_missing", keys)

    def test_delivery_gaps_are_named_by_label(self):
        """review F8: name, street, post code and town, never field names."""
        result = _evaluate(delivery={"state": "incomplete",
                                     "missing": ["name", "street", "zip", "city"]})
        message = _item(result, "delivery_address_incomplete")["message"]
        self.assertIn("name, street, post code, town", message)
        for field in ("zip", "city"):
            self.assertNotIn(field, message)

    def test_settings_left_out_name_the_tax_and_what_is_left_out(self):
        """review F3: one warning per tax, naming what the e-invoice leaves out."""
        lines = [
            _vat_line("Line 1", rate=0.0, category="E", taxes=[
                dict(_tax(8, "0% FR exempt", "E"), left_out=["exemption_code", "reason_lines"])]),
            _vat_line("Line 2", rate=0.0, category="E", taxes=[
                dict(_tax(8, "0% FR exempt", "E"), left_out=["exemption_code", "reason_lines"])]),
        ]
        items = [i for i in _evaluate(vat_lines=lines)["items"]
                 if i["key"] == "vat_settings_left_out"]
        self.assertEqual(len(items), 1)
        message = items[0]["message"]
        self.assertIn("0% FR exempt", message)
        self.assertIn("exemption reason code", message)
        self.assertIn("VAT reason", message)

    def test_letter_gets_the_reference_warnings_only(self):
        result = _evaluate("letter", **_all_gaps())
        self.assertEqual(sorted(_keys(result)), sorted([
            "order_number_too_long", "seller_reference_too_long",
            "seller_reference_withheld"]))

    def test_email_and_sending_off_get_none(self):
        self.assertEqual(_keys(_evaluate("email", **_all_gaps())), [])
        self.assertEqual(_keys(_evaluate(sending_enabled=False, **_all_gaps())), ["sending_off"])

    def test_contact_level_gets_none(self):
        result = _evaluate(invoice_level=False, **_all_gaps())
        self.assertFalse(set(_keys(result)) & set(CONTENT_FIX_AT))
