# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The prompt is one country-neutral set of procedures plus one locale block.

These tests cover composition and selection only: which text is built and
which block is chosen. They say nothing about extraction quality and never
call the Claude API. They check that:

* the composed US prompt is byte-identical to
  `tests/data/system_prompt_us_55ac5e7.txt`,
* the four ordered procedures reach every locale,
* the extraction schema keys are the same in every locale,
* an unknown or missing country does not get the US block, and
* each instrument can still fail.
"""

import json
import pathlib
import re
from unittest.mock import patch

from odoo.tests import BaseCase, TransactionCase, tagged

from odoo.addons.goodahead_invoice_ai_import.lib import claude_client
from odoo.addons.goodahead_invoice_ai_import.lib.claude_client import (
    LOCALE_EU,
    LOCALE_GENERIC,
    LOCALE_US,
    SYSTEM_PROMPT,
    ClaudeInvoiceExtractor,
    build_system_prompt,
    locale_for_country,
)

# Golden copy of the US prompt as a single string, captured with
# `ast.literal_eval` so it is the exact string the interpreter builds. The
# composed US prompt must stay byte-identical to it.
GOLDEN = (
    pathlib.Path(__file__).parent / "data" / "system_prompt_us_55ac5e7.txt"
).read_text(encoding="utf-8")

ALL_LOCALES = (LOCALE_US, LOCALE_EU, LOCALE_GENERIC)

# Words that must appear in exactly one locale's block. Self-checked by
# `test_each_us_marker_belongs_to_exactly_one_locale`.
US_ONLY_MARKERS = (
    "SALES AND USE TAX",
    "federal EIN",
    "resale certificate",
    "ABA, RTN, routing transit",
    '"Remit To"',
    '"state_code": "2-letter US state code',
    "GLL=US gallon",
    "20 for check",
)

EU_ONLY_MARKERS = (
    "VALUE-ADDED TAX",
    "Y-tunnus",
    "viitenumero",
    "58 for SEPA",
    "EU VAT number",
)

# One fragment from inside every prose slot of every block, so each block's own
# wording has positive coverage (schema slots are covered by the schema-key
# test). Self-checked by
# `test_each_prose_fragment_is_in_its_own_block_and_in_no_other`.
PROSE_SLOTS = (
    "locale_origin",
    "locale_tax",
    "locale_identifiers",
    "locale_payment",
    "locale_rules",
)

#: The other eight. They carry JSON schema lines, so the KEYS in them are
#: already pinned by `test_the_schema_keys_are_identical_in_every_locale` and
#: their value domains by `TestTheValueDomainsForkOnPurpose` below.
SCHEMA_SLOTS = (
    "locale_seller_identifiers",
    "locale_seller_region",
    "locale_seller_bank",
    "locale_buyer_identifier",
    "locale_buyer_region",
    "locale_payment_means",
    "locale_unit_code",
    "locale_tax_fields",
)

BLOCK_PROSE_FRAGMENTS = {
    LOCALE_US: {
        "locale_origin": ("Most documents are from United States vendors",),
        "locale_tax": (
            "there is no input/output mechanism and no VAT registration "
            "number involved",
        ),
        "locale_identifiers": (
            "many US invoices show none, which is normal, so use null rather "
            "than guessing",
        ),
        "locale_payment": (
            "A US invoice usually has no structured payment reference",
        ),
        "locale_rules": ("- A non-US VAT number keeps its country prefix",),
    },
    LOCALE_EU: {
        "locale_origin": (
            "Most documents are from vendors in the European Union",
        ),
        "locale_tax": (
            "a standard rate beside a reduced one for food, books or transport",
            "käännetty verovelvollisuus",
        ),
        "locale_identifiers": (
            "THE TWO ARE DIFFERENT FIELDS EVEN WHEN THEY SHARE DIGITS",
        ),
        "locale_payment": (
            "do not re-space or re-group them",
            "This is uncommon in the EU",
        ),
        "locale_rules": ("never built out of a national registration number",),
    },
    LOCALE_GENERIC: {
        "locale_origin": (
            "Documents arrive from vendors in different countries",
        ),
        "locale_tax": (
            "read what the document shows rather than assuming a system",
        ),
        "locale_identifiers": (
            "COPY EACH EXACTLY AND NEVER DERIVE ONE FROM THE OTHER",
        ),
        "locale_payment": ("do not pad, group, re-space or truncate it",),
        "locale_rules": (
            "A tax registration number keeps whatever country prefix the "
            "document prints",
        ),
    },
}

# The scan is per clause (split on `.`, `;` and `:`) and skips clauses carrying
# a prohibition, so the shared rule "Do not add or remove hyphens" is not
# reported while a recipe one clause away from its ban still is.
_RECIPE_PATTERNS = (
    r"remov\w*\s+(?:the\s+)?hyphen",
    r"hyphen\s+(?:is\s+)?removed",
    r"drop\w*\s+(?:the\s+)?hyphen",
    r"prepend\w*",
    # The lookahead excludes "in front of", which the DATES procedure uses.
    r"\bin front\b(?!\s+of)",
    r"add\w*\s+(?:a\s+|the\s+)?country\s+prefix",
    r"convert\w*\s*:",
)

_PROHIBITIONS = ("do not", "never", "rather than", "must not", "cannot")


def derivation_recipes(text):
    """Clauses that tell the model how to BUILD one identifier from another.

    Returns the offending clauses, so a failure names the text rather than a
    boolean. Empty means the prompt states the rule without also stating the
    transformation it bans — which is what
    `test_the_derivation_scanner_can_still_fail` proves this can distinguish.
    """
    flat = " ".join(text.split())
    found = []
    for clause in re.split(r"(?<=[.;:])\s+", flat):
        if not any(re.search(p, clause, re.IGNORECASE) for p in _RECIPE_PATTERNS):
            continue
        if any(word in clause.lower() for word in _PROHIBITIONS):
            continue
        found.append(clause)
    return found


# A digit-count range for a payment reference. Such ranges are country-specific
# (Norwegian KID and Swedish OCR references run to 25 digits).
_DIGIT_RANGE_RE = re.compile(r"\b\d+\s*[-–]\s*\d+\s+digits\b")


def _tokens(text):
    """Word-level tokens.

    Comparing token multisets answers "was anything lost" without having to
    guess the right fragment to assert.
    """
    return re.findall(r"\S+", text)


def token_loss(reference, candidate):
    """Tokens in ``reference`` that ``candidate`` does not have as many of.

    Returns a sorted list of ``(token, how_many_fewer)``. Empty means nothing
    was lost. It is deliberately a MULTISET difference and not a set one: a
    sentence deleted from a prompt that repeats most of its words elsewhere
    would be invisible to a set.
    """
    from collections import Counter

    missing = Counter(_tokens(reference)) - Counter(_tokens(candidate))
    return sorted(missing.items())


def _schema_keys(prompt):
    """Every key of the `Output JSON schema:` block, as a flat set of paths.

    The block is not JSON — it carries prose as its values and `{{`/`}}` for
    `str.format` — so it is read with a key regex per nesting level rather
    than parsed. `test_the_schema_reader_can_still_fail` proves the reader
    notices a key that is not there.
    """
    body = prompt.split("Output JSON schema:", 1)[1]
    body = body.split("\nImportant rules:", 1)[0]
    return set(re.findall(r'^\s*"([a-z_]+)":', body, flags=re.MULTILINE))


@tagged("post_install", "-at_install")
class TestPromptCompositionIsLossless(BaseCase):
    """Splitting the prompt into locale blocks loses no wording."""

    def test_the_composed_us_prompt_is_byte_identical_to_the_shipped_one(self):
        """The composed US prompt is byte-identical to the golden copy."""
        composed = build_system_prompt(LOCALE_US)
        if composed != GOLDEN:
            lost = token_loss(GOLDEN, composed)
            gained = token_loss(composed, GOLDEN)
            self.fail(
                "the composed US prompt is not the shipped one.\n"
                "tokens lost: %r\ntokens gained: %r" % (lost[:40], gained[:40])
            )

    def test_the_shipped_constant_is_the_composed_us_prompt(self):
        """`SYSTEM_PROMPT` is the composed US prompt, which other tests rely on."""
        self.assertEqual(SYSTEM_PROMPT, build_system_prompt(LOCALE_US))

    def test_nothing_of_the_shipped_prompt_is_lost_by_tokens(self):
        """The weaker instrument, kept because it is the one that still works
        if byte identity ever has to go."""
        self.assertEqual(token_loss(GOLDEN, build_system_prompt(LOCALE_US)), [])

    def test_the_token_instrument_can_still_fail(self):
        """Control: `token_loss` reports a deletion."""
        mangled = GOLDEN.replace("Never convert, reformat or \"correct\" an "
                                 "identifier", "", 1)
        lost = token_loss(GOLDEN, mangled)
        self.assertTrue(lost, "the token instrument cannot see a deletion")
        self.assertIn("reformat", dict(lost))

    def test_the_schema_reader_can_still_fail(self):
        """The control for `_schema_keys`, for the same reason."""
        keys = _schema_keys(SYSTEM_PROMPT)
        self.assertIn("invoice_number", keys)
        self.assertNotIn("zz_not_a_field", keys)
        self.assertNotIn(
            "invoice_number",
            _schema_keys(SYSTEM_PROMPT.replace('"invoice_number":', '"zz":')),
        )


@tagged("post_install", "-at_install")
class TestEveryLocaleKeepsTheProcedures(BaseCase):
    """The four ordered procedures are country-neutral and reach every locale."""

    PROCEDURE_HEADINGS = ("DOCUMENT TYPE.", "CURRENCY.", "DATES.", "NUMBERS.")

    # One fragment from the inside of each procedure, chosen because losing the
    # heading and losing the rule are different accidents.
    PROCEDURE_RULES = (
        "FLIPS THE SIGN OF THE WHOLE DOCUMENT",
        "A SYMBOL IS NOT A CURRENCY",
        "A PRIOR ONLY, NEVER PROOF",
        "the LAST one is the decimal separator",
        "FOR A QUANTITY, THE SAME THREE DIGITS ARE NORMALLY DECIMAL",
        "Hyvityslasku",
        "CNY in China, JPY in Japan",
        "output null for that date",
        "Do NOT fall back on USD, on the symbol alone, or on the country",
    )

    def test_every_locale_carries_all_four_procedure_headings(self):
        for locale in ALL_LOCALES:
            prompt = " ".join(build_system_prompt(locale).split())
            for heading in self.PROCEDURE_HEADINGS:
                with self.subTest(locale=locale, heading=heading):
                    self.assertIn(heading, prompt)

    def test_every_locale_carries_the_rules_inside_the_procedures(self):
        for locale in ALL_LOCALES:
            prompt = " ".join(build_system_prompt(locale).split())
            for rule in self.PROCEDURE_RULES:
                with self.subTest(locale=locale, rule=rule):
                    self.assertIn(rule, prompt)

    def test_every_locale_keeps_the_never_rewrite_an_identifier_rule(self):
        """Every locale keeps the never-rewrite-an-identifier rule."""
        for locale in ALL_LOCALES:
            prompt = " ".join(build_system_prompt(locale).split())
            with self.subTest(locale=locale):
                self.assertIn(
                    "Never convert, reformat or \"correct\" an identifier",
                    prompt,
                )
                self.assertNotIn("remove hyphen, prepend", prompt)

    def test_no_locale_block_states_a_recipe_for_deriving_an_identifier(self):
        """No locale block tells the model how to derive one identifier from
        another; vendor matching relies on identifiers copied verbatim."""
        for locale in ALL_LOCALES:
            with self.subTest(locale=locale):
                self.assertEqual(
                    derivation_recipes(build_system_prompt(locale)),
                    [],
                    "a locale block tells the model how to build one "
                    "identifier out of another",
                )

    def test_the_derivation_scanner_can_still_fail(self):
        """Control: the scanner flags derivation recipes, including a reworded
        one, but not the shared prohibition."""
        legacy = ("For Finnish Y-tunnus without prefix, convert: remove hyphen, "
                  "prepend 'FI'")
        self.assertTrue(derivation_recipes(legacy))

        was = ("THE TWO ARE DIFFERENT FIELDS EVEN WHEN THEY SHARE DIGITS. A "
               "Finnish VAT number is that country's business id with the "
               "hyphen removed and FI in front, and it is STILL two values: "
               "copy each into its own field in the form the page prints it, "
               "and never derive one from the other.")
        self.assertTrue(
            derivation_recipes(was),
            "the scanner cannot see the wording this finding was about",
        )
        self.assertNotIn("remove hyphen, prepend", was)  # a literal check misses it

        rule = ("Copy identifiers and account numbers EXACTLY as printed. Do "
                "not add or remove hyphens, do not add a country prefix that "
                "is not there, and do not convert between identifier types")
        self.assertEqual(derivation_recipes(rule), [])

    def test_no_locale_block_states_a_digit_range_for_a_payment_reference(self):
        """No locale block states a digit-count range for a payment reference."""
        for locale in ALL_LOCALES:
            with self.subTest(locale=locale):
                hits = _DIGIT_RANGE_RE.findall(build_system_prompt(locale))
                self.assertEqual(
                    hits, [],
                    "a locale block states a digit-count range for a payment "
                    "reference; no country's range is every country's",
                )

    def test_the_digit_range_scanner_can_still_fail(self):
        """Control for the digit-range scanner."""
        self.assertEqual(
            _DIGIT_RANGE_RE.findall(
                "Finnish national references: numeric string, 2-20 digits"
            ),
            ["2-20 digits"],
        )
        # Shapes it must not report, as they appear in the shipped prompt.
        for clean in ("9-digit US ABA routing number", "two check digits",
                      "5 digits or ZIP+4", "format 12-3456789"):
            with self.subTest(clean=clean):
                self.assertEqual(_DIGIT_RANGE_RE.findall(clean), [])

    def test_no_locale_slot_is_left_unfilled(self):
        """An unfilled slot would ship its own marker to the API."""
        for locale in ALL_LOCALES:
            with self.subTest(locale=locale):
                self.assertNotIn("@@", build_system_prompt(locale))

    def test_every_locale_still_formats_with_the_categories_block(self):
        """`str.format` runs over the COMPOSED prompt, so a literal brace in a
        locale block raises `KeyError` on every invoice in that country — and
        only in that country, which is the worst place for it to hide."""
        for locale in ALL_LOCALES:
            with self.subTest(locale=locale):
                filled = build_system_prompt(locale).format(
                    categories_block="Available: x"
                )
                self.assertIn("Available: x", filled)
                self.assertNotIn("{categories_block}", filled)


@tagged("post_install", "-at_install")
class TestTheLocaleBlocksDiffer(BaseCase):
    """Two locales that never disagree are one locale built twice."""

    def test_each_us_marker_belongs_to_exactly_one_locale(self):
        us = build_system_prompt(LOCALE_US)
        eu = build_system_prompt(LOCALE_EU)
        generic = build_system_prompt(LOCALE_GENERIC)
        for marker in US_ONLY_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(marker, us)
                self.assertNotIn(marker, eu)
                self.assertNotIn(marker, generic)

    def test_the_eu_block_carries_the_wording_a_real_deployment_used(self):
        """The EU block carries its expected vocabulary."""
        eu = build_system_prompt(LOCALE_EU)
        for marker in EU_ONLY_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(marker, eu)
        self.assertNotIn("Y-tunnus", build_system_prompt(LOCALE_US))

    def test_the_generic_block_names_neither_country(self):
        """The generic block carries neither the US nor the EU vocabulary."""
        generic = build_system_prompt(LOCALE_GENERIC)
        for marker in US_ONLY_MARKERS + EU_ONLY_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, generic)

    def test_the_schema_keys_are_identical_in_every_locale(self):
        """The schema keys are identical in every locale, since downstream code
        reads these names."""
        reference = _schema_keys(build_system_prompt(LOCALE_US))
        self.assertIn("routing_number", reference)
        self.assertIn("iban", reference)
        for locale in (LOCALE_EU, LOCALE_GENERIC):
            with self.subTest(locale=locale):
                self.assertEqual(_schema_keys(build_system_prompt(locale)),
                                 reference)


@tagged("post_install", "-at_install")
class TestEveryBlocksOwnWordingIsPinned(BaseCase):
    """Each block's prose slots are pinned by at least one fragment."""

    def test_the_thirteen_slots_are_five_prose_and_eight_schema(self):
        """The thirteen slots split into five prose and eight schema slots, so a
        new slot cannot go unpinned."""
        in_template = set(claude_client._SLOT_RE.findall(claude_client.PROCEDURES))
        self.assertEqual(len(in_template), 13)
        self.assertEqual(set(PROSE_SLOTS) | set(SCHEMA_SLOTS), in_template)
        self.assertEqual(set(PROSE_SLOTS) & set(SCHEMA_SLOTS), set())

    def test_every_prose_slot_of_every_block_is_pinned(self):
        for locale in ALL_LOCALES:
            with self.subTest(locale=locale):
                self.assertEqual(
                    set(BLOCK_PROSE_FRAGMENTS[locale]), set(PROSE_SLOTS)
                )

    def test_each_prose_fragment_is_in_its_own_block_and_in_no_other(self):
        """Each fragment is in its own block (coverage) and in no other
        (self-check)."""
        built = {
            locale: " ".join(build_system_prompt(locale).split())
            for locale in ALL_LOCALES
        }
        for locale, slots in BLOCK_PROSE_FRAGMENTS.items():
            for slot, fragments in slots.items():
                for fragment in fragments:
                    with self.subTest(locale=locale, slot=slot):
                        self.assertIn(fragment, built[locale])
                        for other in ALL_LOCALES:
                            if other != locale:
                                self.assertNotIn(fragment, built[other])


@tagged("post_install", "-at_install")
class TestTheValueDomainsForkOnPurpose(BaseCase):
    """Schema keys are shared, but value domains may differ per locale.

    The `payment_means_code` fork is intended (`58`, SEPA credit transfer, only
    in the EU block). For `unit_code` the non-US lists are open-ended ("etc.");
    the US block is frozen by the golden file.
    """

    #: Named once: every block must offer these, whatever else it adds.
    SHARED_UNITS = ("C62=each/piece", "HUR=hour", "KGM=kg", "MTR=meter",
                    "LTR=liter")
    SHARED_MEANS = ("30 for", "20 for", "48 for")

    def _slot(self, locale, slot):
        return claude_client.LOCALE_BLOCKS[locale][slot]

    def test_every_block_offers_the_shared_unit_codes(self):
        for locale in ALL_LOCALES:
            for unit in self.SHARED_UNITS:
                with self.subTest(locale=locale, unit=unit):
                    self.assertIn(unit, self._slot(locale, "locale_unit_code"))

    def test_the_non_us_unit_lists_are_open(self):
        """The non-US unit lists are open-ended; the US list is frozen."""
        for locale in (LOCALE_EU, LOCALE_GENERIC):
            with self.subTest(locale=locale):
                self.assertIn("etc.", self._slot(locale, "locale_unit_code"))

    def test_every_block_offers_the_shared_payment_means_codes(self):
        for locale in ALL_LOCALES:
            for code in self.SHARED_MEANS:
                with self.subTest(locale=locale, code=code):
                    self.assertIn(code, self._slot(locale, "locale_payment_means"))

    def test_only_the_eu_block_names_the_sepa_credit_transfer(self):
        """The fork that IS wanted, pinned so that it stays a decision."""
        self.assertIn("58 for SEPA", self._slot(LOCALE_EU, "locale_payment_means"))
        for locale in (LOCALE_US, LOCALE_GENERIC):
            with self.subTest(locale=locale):
                self.assertNotIn("58", self._slot(locale, "locale_payment_means"))


@tagged("post_install", "-at_install")
class TestLocaleSelection(BaseCase):
    """Which block is chosen, and what happens when nothing says."""

    def test_the_united_states_selects_the_us_block(self):
        self.assertEqual(locale_for_country("US"), LOCALE_US)

    def test_a_lower_case_or_padded_code_still_selects(self):
        """Case and padding do not change the selection."""
        self.assertEqual(locale_for_country(" us "), LOCALE_US)

    def test_finland_selects_the_eu_block(self):
        self.assertEqual(locale_for_country("FI"), LOCALE_EU)

    def test_an_unknown_country_does_not_get_the_us_block(self):
        """An unknown country gets no locale, not the US block."""
        for code in ("JP", "CN", "BR", "ZZ"):
            with self.subTest(code=code):
                self.assertIsNone(locale_for_country(code))
                self.assertNotEqual(locale_for_country(code), LOCALE_US)

    def test_no_country_at_all_is_not_the_united_states(self):
        for value in (None, "", "   ", False):
            with self.subTest(value=value):
                self.assertIsNone(locale_for_country(value))

    def test_an_unselected_locale_composes_the_generic_block(self):
        self.assertEqual(build_system_prompt(None),
                         build_system_prompt(LOCALE_GENERIC))

    def test_a_missing_locale_also_says_so(self):
        """A missing locale composes the generic block and logs a warning."""
        for value in (None, "", "   ", False):
            with self.subTest(value=value):
                with self.assertLogs(claude_client.__name__, level=30) as logs:
                    prompt = build_system_prompt(value)
                self.assertEqual(prompt, build_system_prompt(LOCALE_GENERIC))
                self.assertTrue(
                    any("country-neutral" in line for line in logs.output),
                    logs.output,
                )

    def test_a_named_locale_says_nothing(self):
        """Control: the shipped locales do not warn.

        `assertLogs` fails when nothing is logged, so one control line is logged
        and must be the only one.
        """
        for locale in ALL_LOCALES:
            with self.subTest(locale=locale):
                with self.assertLogs(claude_client.__name__, level=30) as logs:
                    build_system_prompt(locale)
                    claude_client._logger.warning("zz control line")
                self.assertEqual(
                    [line for line in logs.output if "zz control line" not in line],
                    [],
                )

    def test_an_unknown_locale_name_composes_generic_and_says_so(self):
        """A typo in a locale name must not silently ship the US prompt, and
        must not be silent either."""
        with self.assertLogs(claude_client.__name__, level=30) as logs:
            prompt = build_system_prompt("us-east-1")
        self.assertEqual(prompt, build_system_prompt(LOCALE_GENERIC))
        self.assertTrue(any("us-east-1" in line for line in logs.output))

    def test_canada_is_not_the_united_states(self):
        """Canada does not inherit the US block (no EIN, ABA or state sales tax)."""
        self.assertIsNone(locale_for_country("CA"))


@tagged("post_install", "-at_install")
class TestTheSlotGuardCanStillFail(BaseCase):
    """The unfilled-slot guard raises a `ValueError` naming the slot."""

    def test_a_slot_no_block_fills_is_named(self):
        template = claude_client.PROCEDURES + "\n@@locale_zz@@\n"
        with patch.object(claude_client, "PROCEDURES", template):
            with self.assertRaises(ValueError) as caught:
                build_system_prompt(LOCALE_US)
        self.assertIn("locale_zz", str(caught.exception))

    def test_a_marker_the_slot_regex_cannot_parse_is_named(self):
        """`@@locale-tax@@` has a hyphen, so `_SLOT_RE` (`[a-z_]+`) does not
        match it and the substitution leaves it standing. The guard must still
        name it, rather than report an empty list of slots, so it never
        reaches the API."""
        template = claude_client.PROCEDURES + "\n@@locale-tax@@\n"
        with patch.object(claude_client, "PROCEDURES", template):
            with self.assertRaises(ValueError) as caught:
                build_system_prompt(LOCALE_US)
        message = str(caught.exception)
        self.assertIn("@@locale-tax@@", message)
        self.assertNotIn("[]", message)

    def test_the_guard_passes_the_prompt_it_is_meant_to_pass(self):
        """The other half of a control: it must not fire on the real template,
        or the two above would be measuring an exception that is always raised.
        """
        for locale in ALL_LOCALES:
            with self.subTest(locale=locale):
                self.assertNotIn("@@", build_system_prompt(locale))


class _StubClient:
    def __init__(self):
        self.calls = []

    def messages_create(self, **kwargs):
        self.calls.append(kwargs)
        return {"stop_reason": "refusal", "content": [], "usage": {}}


@tagged("post_install", "-at_install")
class TestTheComposedPromptIsTheOneSent(BaseCase):
    """The control. Everything above reads module functions; this reads the
    `system` argument the client was actually handed."""

    def _sent(self, locale):
        client = _StubClient()
        extractor = ClaudeInvoiceExtractor(client=client, locale=locale)
        with self.assertRaises(claude_client.ClaudeExtractionError):
            extractor.extract_invoice(b"%PDF-1.4 fake")
        return client.calls[0]["system"]

    def test_the_us_locale_sends_the_us_block(self):
        sent = self._sent(LOCALE_US)
        self.assertIn("SALES AND USE TAX", sent)
        self.assertNotIn("Y-tunnus", sent)

    def test_the_eu_locale_sends_the_eu_block(self):
        sent = self._sent(LOCALE_EU)
        self.assertIn("Y-tunnus", sent)
        self.assertNotIn("SALES AND USE TAX", sent)

    def test_an_extractor_with_no_locale_sends_neither(self):
        """An extractor without a locale uses the generic block, not the US one."""
        sent = self._sent(None)
        self.assertNotIn("SALES AND USE TAX", sent)
        self.assertNotIn("Y-tunnus", sent)
        self.assertIn("A SYMBOL IS NOT A CURRENCY", sent)

    def test_the_categories_block_still_reaches_the_call(self):
        client = _StubClient()
        extractor = ClaudeInvoiceExtractor(client=client, locale=LOCALE_EU)
        with self.assertRaises(claude_client.ClaudeExtractionError):
            extractor.extract_invoice(
                b"%PDF-1.4 fake", [{"code": "zz", "name": "ZZ Cat"}]
            )
        self.assertIn('"zz": ZZ Cat', client.calls[0]["system"])


@tagged("post_install", "-at_install")
class TestCurrencyStaysRequired(BaseCase):
    """A null currency remains a loud failure."""

    def _extraction(self, **overrides):
        data = {
            "type": "Invoice",
            "invoice_number": "INV-1",
            "issue_date": "2026-03-06",
            "currency": "EUR",
            "seller": {"name": "ZZ Vendor"},
            "lines": [{
                "name": "Widget", "quantity": 1, "price_unit": 10.0,
                "line_total": 10.0, "tax_amount": 0.0,
            }],
            "tax_subtotals": [],
            "totals": {
                "line_extension": 10.0, "tax_exclusive": 10.0,
                "tax_inclusive": 10.0, "payable": 10.0,
            },
        }
        data.update(overrides)
        return data

    def _run(self, data, **kwargs):
        class _Client:
            def messages_create(self, **kw):
                return {
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": json.dumps(data)}],
                    "usage": {},
                }

        return ClaudeInvoiceExtractor(client=_Client(), **kwargs).extract_invoice(
            b"%PDF-1.4 fake"
        )

    def test_a_null_currency_is_still_a_loud_failure(self):
        with self.assertRaisesRegex(
            claude_client.ClaudeExtractionError, "currency"
        ):
            self._run(self._extraction(currency=None), locale=LOCALE_EU)

    def test_the_validator_cannot_see_the_locale_and_that_is_why_this_runs_once(
        self,
    ):
        """Validation does not read the locale, so one locale is enough here."""
        blind = ("_validate_extraction", "_currency_is_usable",
                 "_resolve_currency_from_country")
        for name in blind:
            method = getattr(claude_client.ClaudeInvoiceExtractor, name)
            with self.subTest(method=name):
                self.assertNotIn("locale", method.__code__.co_names)
        # Control: the one method that does read it.
        self.assertIn(
            "locale",
            claude_client.ClaudeInvoiceExtractor.extract_invoice.__code__.co_names,
        )

    def test_the_control_a_stated_currency_passes(self):
        result = self._run(self._extraction(), locale=LOCALE_EU)
        self.assertEqual(result["data"]["currency"], "EUR")


@tagged("post_install", "-at_install")
class TestTheFallbackCurrencyIsAnArgument(BaseCase):
    """`generate_peppol_invoice_xml` honours `default_currency`."""

    def _code(self, **kwargs):
        from lxml import etree

        from odoo.addons.goodahead_invoice_ai_import.lib.ubl_generator import (
            generate_peppol_invoice_xml,
        )

        data = {
            "type": "Invoice",
            "invoice_number": "ZZ-LIB-1",
            "issue_date": "2026-03-04",
            "seller": {"name": "ZZ Vendor"},
            "buyer": {"name": "ZZ Buyer"},
            "lines": [{
                "name": "Widget", "quantity": 1, "price_unit": 10.0,
                "line_total": 10.0, "tax_amount": 0.0, "tax_percent": 0.0,
                "tax_category": "Z",
            }],
            "tax_subtotals": [],
            "totals": {
                "line_extension": 10.0, "tax_exclusive": 10.0,
                "tax_inclusive": 10.0, "payable": 10.0,
            },
        }
        tree = etree.fromstring(generate_peppol_invoice_xml(data, **kwargs))
        return tree.find(
            ".//{urn:oasis:names:specification:ubl:schema:xsd:"
            "CommonBasicComponents-2}DocumentCurrencyCode"
        ).text

    def test_a_stated_fallback_is_used(self):
        self.assertEqual(self._code(default_currency="ZZZ"), "ZZZ")

    def test_no_fallback_leaves_the_last_resort_literal(self):
        """The control. `generate_peppol_invoice_xml(data)` with one argument
        is what every lib-only caller passes, and it must keep working."""
        from odoo.addons.goodahead_invoice_ai_import.lib import ubl_generator

        self.assertEqual(self._code(), ubl_generator.DEFAULT_CURRENCY)
        self.assertEqual(self._code(default_currency=None),
                         ubl_generator.DEFAULT_CURRENCY)


@tagged("post_install", "-at_install")
class TestDefaultCurrencyComesFromTheCompany(TransactionCase):
    """The fallback currency comes from the company, not a hardcoded literal."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.AM = cls.env["account.move"]

    def _extracted(self):
        return {
            "type": "Invoice",
            "invoice_number": "ZZ-CUR-1",
            "issue_date": "2026-03-04",
            "seller": {"name": "ZZ Currency Vendor", "country_code": "US"},
            "buyer": {"name": "ZZ Buyer"},
            "lines": [{
                "name": "Widget", "quantity": 1, "price_unit": 10.0,
                "line_total": 10.0, "tax_amount": 0.0, "tax_percent": 0.0,
                "tax_category": "Z",
            }],
            "tax_subtotals": [],
            "totals": {
                "line_extension": 10.0, "tax_exclusive": 10.0,
                "tax_inclusive": 10.0, "payable": 10.0,
            },
        }

    def test_an_extraction_with_no_currency_falls_back_to_the_companys_own(self):
        """With no currency in the extraction, the company's currency is used."""
        from lxml import etree

        company = self.env.company
        xml = self.AM._ai_generate_ubl_from_extracted(self._extracted(), company)
        tree = etree.fromstring(xml)
        code = tree.find(
            ".//{urn:oasis:names:specification:ubl:schema:xsd:"
            "CommonBasicComponents-2}DocumentCurrencyCode"
        )
        self.assertEqual(code.text, company.currency_id.name)

    def test_the_fallback_is_the_companys_currency_and_not_the_literal(self):
        """The company's currency wins over the module literal.

        `DEFAULT_CURRENCY` is patched to a sentinel so the test also means
        something on a company whose currency is USD.
        """
        from lxml import etree

        from odoo.addons.goodahead_invoice_ai_import.lib import ubl_generator

        company = self.env.company
        with patch.object(ubl_generator, "DEFAULT_CURRENCY", "ZZZ"):
            xml = self.AM._ai_generate_ubl_from_extracted(
                self._extracted(), company
            )
        tree = etree.fromstring(xml)
        code = tree.find(
            ".//{urn:oasis:names:specification:ubl:schema:xsd:"
            "CommonBasicComponents-2}DocumentCurrencyCode"
        )
        self.assertNotEqual(
            code.text, "ZZZ",
            "the company's currency never reached the generator — the bill "
            "fell back to the module's last-resort literal",
        )
        self.assertEqual(code.text, company.currency_id.name)

    def test_a_stated_currency_still_wins_over_the_company(self):
        """The control: the fallback is a fallback. A EUR bill in a USD company
        stays EUR."""
        from lxml import etree

        extracted = self._extracted()
        extracted["currency"] = "EUR" if self.env.company.currency_id.name != "EUR" \
            else "USD"
        tree = etree.fromstring(
            self.AM._ai_generate_ubl_from_extracted(extracted, self.env.company)
        )
        code = tree.find(
            ".//{urn:oasis:names:specification:ubl:schema:xsd:"
            "CommonBasicComponents-2}DocumentCurrencyCode"
        )
        self.assertEqual(code.text, extracted["currency"])


@tagged("post_install", "-at_install")
class TestTheCompanysCountryChoosesTheBlock(TransactionCase):
    """Where the locale actually comes from on a running instance."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.AM = cls.env["account.move"]
        # Reuse the existing company (rolled back per test): creating one needs a
        # chart of accounts and journals.
        cls.company = cls.env.company

    def test_a_us_company_gets_the_us_block(self):
        self.company.country_id = self.env.ref("base.us")
        self.assertEqual(self.AM._ai_prompt_locale(self.company), LOCALE_US)

    def test_a_finnish_company_gets_the_eu_block(self):
        self.company.country_id = self.env.ref("base.fi")
        self.assertEqual(self.AM._ai_prompt_locale(self.company), LOCALE_EU)

    def test_a_company_with_no_country_gets_neither_and_says_so(self):
        """A company without a country gets no locale and a warning."""
        self.company.country_id = False
        with self.assertLogs(
            "odoo.addons.goodahead_invoice_ai_import.models.account_move",
            level=30,
        ) as logs:
            locale = self.AM._ai_prompt_locale(self.company)
        self.assertIsNone(locale)
        self.assertTrue(any("no country" in line for line in logs.output))

    def test_a_country_with_no_block_gets_neither_and_says_so(self):
        self.company.country_id = self.env.ref("base.jp")
        with self.assertLogs(
            "odoo.addons.goodahead_invoice_ai_import.models.account_move",
            level=30,
        ) as logs:
            locale = self.AM._ai_prompt_locale(self.company)
        self.assertIsNone(locale)
        self.assertTrue(any("JP" in line for line in logs.output))
