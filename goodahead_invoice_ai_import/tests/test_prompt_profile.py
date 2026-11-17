# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""A company picks its extraction prompt from a collection of profiles.

US and EU ship as built-in profiles that are composed from the module's code;
a duplicate of either is a custom profile composed from its own fields. Like
`test_prompt_locale`, nothing here calls the Claude API: these tests measure
which text is built and sent, not how the model reads it.
"""

import pathlib

from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_ai_import.lib import claude_client
from odoo.addons.goodahead_invoice_ai_import.lib.claude_client import (
    LOCALE_BLOCKS,
    LOCALE_EU,
    LOCALE_US,
    SLOT_NAMES,
    ClaudeInvoiceExtractor,
    build_system_prompt,
)

GOLDEN = (
    pathlib.Path(__file__).parent / "data" / "system_prompt_us_55ac5e7.txt"
).read_text(encoding="utf-8")


class _StubClient:
    def __init__(self):
        self.calls = []

    def messages_create(self, **kwargs):
        self.calls.append(kwargs)
        return {"stop_reason": "refusal", "content": [], "usage": {}}


def _sent(locale):
    client = _StubClient()
    extractor = ClaudeInvoiceExtractor(client=client, locale=locale)
    try:
        extractor.extract_invoice(b"%PDF-1.4 fake")
    except claude_client.ClaudeExtractionError:
        pass
    return client.calls[0]["system"]


@tagged("post_install", "-at_install")
class TestPromptProfiles(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.AM = cls.env["account.move"]
        cls.us = cls.env.ref("goodahead_invoice_ai_import.prompt_profile_us")
        cls.eu = cls.env.ref("goodahead_invoice_ai_import.prompt_profile_eu")
        # Creating a company would need a chart of accounts.
        cls.company = cls.env.company

    def _custom(self, **vals):
        profile = self.eu.copy({"name": "Finland", **vals})
        self.assertFalse(profile.builtin)
        return profile

    # -- the shipped profiles ------------------------------------------------

    def test_the_shipped_profiles_are_us_and_eu(self):
        builtin = self.env["goodahead.invoice.prompt.profile"].search(
            [("builtin", "=", True)]
        )
        self.assertEqual(builtin, self.us | self.eu)

    def test_a_builtin_profile_shows_the_text_it_sends(self):
        for profile, code in ((self.us, LOCALE_US), (self.eu, LOCALE_EU)):
            for name in SLOT_NAMES:
                with self.subTest(profile=code, slot=name):
                    self.assertEqual(profile[name], LOCALE_BLOCKS[code][name])

    def test_the_us_profile_still_sends_the_golden_prompt(self):
        self.company.ai_invoice_prompt_profile_id = self.us
        locale = self.AM._ai_prompt_locale(self.company)
        self.assertEqual(locale, LOCALE_US)
        self.assertEqual(build_system_prompt(locale), GOLDEN)

    def test_a_builtin_block_passed_as_a_mapping_is_byte_identical(self):
        """The custom path composes a shipped block exactly as the named one."""
        for code in (LOCALE_US, LOCALE_EU):
            with self.subTest(code=code):
                self.assertEqual(
                    build_system_prompt(dict(LOCALE_BLOCKS[code])),
                    build_system_prompt(code),
                )

    def test_a_builtin_profile_cannot_be_edited_or_deleted(self):
        with self.assertRaises(UserError):
            self.us.locale_tax = "TAX. Something else."
        with self.assertRaises(UserError):
            self.us.unlink()
        # Writing what it already holds — an upgrade reloading the data file —
        # is not an edit.
        self.us.write({"code": LOCALE_US, "builtin": True, "name": "US"})
        self.assertEqual(self.us.name, "US")

    def test_the_preview_is_the_prompt(self):
        self.assertIn("SALES AND USE TAX", self.us.prompt_preview)
        self.assertIn("Y-tunnus", self.eu.prompt_preview)
        self.assertNotIn("{categories_block}", self.us.prompt_preview)

    # -- choosing ------------------------------------------------------------

    def test_a_chosen_profile_overrides_the_country(self):
        self.company.country_id = self.env.ref("base.us")
        self.company.ai_invoice_prompt_profile_id = self.eu
        self.assertEqual(self.AM._ai_prompt_locale(self.company), LOCALE_EU)

    def test_no_profile_still_chooses_from_the_country(self):
        self.company.ai_invoice_prompt_profile_id = False
        self.company.country_id = self.env.ref("base.fi")
        self.assertEqual(self.AM._ai_prompt_locale(self.company), LOCALE_EU)

    # -- custom profiles -----------------------------------------------------

    def test_a_duplicate_is_an_editable_copy(self):
        custom = self._custom()
        self.assertFalse(custom.code)
        for name in SLOT_NAMES:
            self.assertEqual(custom[name], self.eu[name])
        custom.locale_origin = "Most documents are from Finnish vendors."
        self.assertEqual(
            custom.locale_origin, "Most documents are from Finnish vendors."
        )

    def test_a_custom_profile_is_the_prompt_that_is_sent(self):
        custom = self._custom(
            locale_origin="Most documents are from Finnish vendors {and} more."
        )
        self.company.ai_invoice_prompt_profile_id = custom
        sent = _sent(self.AM._ai_prompt_locale(self.company))
        # Braces typed into a profile reach the model as typed.
        self.assertIn("Most documents are from Finnish vendors {and} more.", sent)
        self.assertNotIn("from vendors in the European Union", sent)
        self.assertIn("Y-tunnus", sent)

    def test_an_empty_part_is_refused(self):
        with self.assertRaises(ValidationError):
            self._custom(locale_tax="  ")

    def test_a_slot_marker_is_refused(self):
        with self.assertRaises(ValidationError):
            self._custom(locale_rules="- see @@locale_tax@@")

    def test_the_schema_keys_cannot_change(self):
        renamed = self.eu.locale_seller_bank.replace('"bic"', '"swift"')
        with self.assertRaises(ValidationError):
            self._custom(locale_seller_bank=renamed)
        dropped = "\n".join(
            line for line in self.eu.locale_seller_bank.splitlines()
            if '"remit_to_address"' not in line
        )
        with self.assertRaises(ValidationError):
            self._custom(locale_seller_bank=dropped)

    def test_a_schema_description_can_change(self):
        reworded = self.eu.locale_seller_bank.replace(
            "IBAN with its country prefix or null",
            "Finnish IBAN (FI + 16 digits) or null",
        )
        custom = self._custom(locale_seller_bank=reworded)
        self.assertIn("FI + 16 digits", custom.prompt_preview)
