# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The prompt is one country-neutral set of PROCEDURES plus one locale block.

🔴 READ THIS BEFORE TRUSTING ANY TEST IN THIS FILE. Every assertion here is
about COMPOSITION and SELECTION — which text is built, and which block is
chosen for which country. **Not one of them says anything about how the model
reads an invoice.** Nothing here calls the Claude API, and nothing that can be
run without spending money could show that a Finnish bill comes back with a
Y-tunnus in `business_id`. That needs real documents from both locales put
through a real call and the answers looked at, and it has not been done.

What CAN be measured here, and is:

* that the composed US prompt is **byte-identical** to the one the US deployment is in
  production on (`tests/data/system_prompt_us_55ac5e7.txt`),
* that the four ordered procedures reach every locale,
* that the extraction **schema keys** are the same in every locale, which is
  what the dual-core design promises downstream,
* that an unknown or missing country does **not** get the US block, and
* that each instrument above can still fail.
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

# The prompt as it shipped at 19.0.3.12.0, commit 55ac5e7, before the split.
# Extracted from that commit's `lib/claude_client.py` by `ast.literal_eval` of
# the `SYSTEM_PROMPT` assignment — so it is the string the interpreter built,
# not a re-typing of the source. 24,915 characters.
#
# ⚠️ ONE DELIBERATE DEPARTURE FROM 55ac5e7, made in the prompt and in this file
# together: the two example addresses and the example VAT number were real
# parties' data and were swapped for same-length invented ones (Fairview AR /
# Riverton NY, `FI00000158`). The length, the shape and every instruction are
# unchanged.
GOLDEN = (
    pathlib.Path(__file__).parent / "data" / "system_prompt_us_55ac5e7.txt"
).read_text(encoding="utf-8")

ALL_LOCALES = (LOCALE_US, LOCALE_EU, LOCALE_GENERIC)

# 🔴 A word that must appear in EXACTLY ONE locale's block. The list is
# self-checking: `test_each_us_marker_belongs_to_exactly_one_locale` asserts
# each of these is in the US prompt and in neither of the others, so a marker
# that drifts into the shared procedures turns this list from a guard into a
# failure rather than into a quiet pass.
# ⚠️ IT HAS ALREADY EARNED ITS KEEP. Three markers in the first draft of this
# list were wrong and the self-check said so: `remit_to_address` is a schema
# KEY and is in all three blocks; `ISO 11649` is named by the US payment block
# as well as the EU one; and `Please mail payments to` is split by a line
# continuation and never matches as written. A marker list that is never wrong
# is a marker list nobody is checking.
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

# 🔴 A REVIEW FINDING — AND THE NUMBER IS THE POINT. *(measured by
# mutation, in-process)* with the two lists above as the only instrument,
# **emptying all five prose slots of the generic block — 2,756 characters, every
# word of its TAX, IDENTIFIERS and BANK AND REMITTANCE guidance — left every
# assertion in this file passing**, and reducing the EU block's same five slots
# to just the five `EU_ONLY_MARKERS` strings removed 3,697 more and also passed.
#
# The reason is structural. Eight of the thirteen slots carry schema lines, so
# `test_the_schema_keys_are_identical_in_every_locale` pins those. The other
# five carry only prose, and the only assertions that ever read a non-US
# composition were 8 US markers asserted ABSENT, 5 EU markers asserted present
# and 13 asserted absent from generic. So the US block was pinned to the byte by
# a golden file, the EU block by five words, and the generic block — the one
# every country outside the US and the EEA gets — by nothing but the absence of
# thirteen other words.
#
# This map is the missing positive instrument: one fragment from the inside of
# every prose slot of every block. It carries the SAME self-check the marker
# lists do (`test_each_prose_fragment_is_in_its_own_block_and_in_no_other`), and
# that is what makes it worth having rather than a second list to drift: a
# fragment that is wrong, or that drifts into the shared procedures, fails
# loudly instead of passing quietly.
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

# 🔴 A REVIEW FINDING. A PHRASE LIST CANNOT TELL AN INSTRUCTION FROM A
# PROHIBITION, AND THE FIRST DRAFT OF THIS SCANNER PROVED IT: searching the
# whole prompt for "remove hyphen" reports a hit in ALL THREE locales, because
# the shared `Important rules:` tail says *"Do not add or remove hyphens"*. A
# list that flags the rule it is enforcing is worse than no list.
#
# So the scan is per CLAUSE (split on `.`, `;` and `:`) and skips any clause
# that carries a prohibition. That is not a flourish — it is the finding itself:
# the EU block's derivation recipe and the ban on it sat in the SAME sentence,
# one clause apart, so a sentence-level or document-level negation check reports
# clean on exactly the text that motivated this.
_RECIPE_PATTERNS = (
    r"remov\w*\s+(?:the\s+)?hyphen",
    r"hyphen\s+(?:is\s+)?removed",
    r"drop\w*\s+(?:the\s+)?hyphen",
    r"prepend\w*",
    # ⚠️ The lookahead is load-bearing: the DATES procedure says a wrong null
    # "puts a false statement in front of an accountant", which is not a recipe.
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


# 🔴 A REVIEW FINDING. an earlier fork of this module says *"**Finnish** national
# references: numeric string, 2-20 digits"*. The EU block widened that to every
# country in `_EU_EEA`, which includes NO and SE — and a Norwegian KID or a
# Swedish OCR reference runs to **25** digits. The generic block states no
# length at all and is the correct version.
_DIGIT_RANGE_RE = re.compile(r"\b\d+\s*[-–]\s*\d+\s+digits\b")


def _tokens(text):
    """Word-level tokens, order discarded.

    The instrument the dual-core design asks for: a locale split can drop an
    instruction while every string assertion still passes, because each one
    asserts a DIFFERENT fragment. Comparing the token multiset of the composed
    US prompt against the shipped one answers "was anything lost" without
    anybody having to think of the right fragment first.
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
    """🔴 The whole point of step 9: move wording without losing any."""

    def test_the_composed_us_prompt_is_byte_identical_to_the_shipped_one(self):
        """The US deployment is in production on this exact text.

        Byte identity is a stronger claim than "no token was lost", and it is
        the one worth making: it proves no instruction moved, was reworded, or
        changed its place in the ordering. If this ever has to be relaxed, the
        token check below is what is left, and every difference has to be
        named in the commit message.
        """
        composed = build_system_prompt(LOCALE_US)
        if composed != GOLDEN:
            lost = token_loss(GOLDEN, composed)
            gained = token_loss(composed, GOLDEN)
            self.fail(
                "the composed US prompt is not the shipped one.\n"
                "tokens lost: %r\ntokens gained: %r" % (lost[:40], gained[:40])
            )

    def test_the_shipped_constant_is_the_composed_us_prompt(self):
        """`SYSTEM_PROMPT` is no longer a literal — it is what the US locale
        composes to. Every pre-existing prompt assertion in
        `test_claude_client.py` and `test_currency_from_country.py` reads this
        name, so if it ever stopped meaning "the US deployment's prompt" those
        ~110 assertions would keep passing against the wrong text."""
        self.assertEqual(SYSTEM_PROMPT, build_system_prompt(LOCALE_US))

    def test_nothing_of_the_shipped_prompt_is_lost_by_tokens(self):
        """The weaker instrument, kept because it is the one that still works
        if byte identity ever has to go."""
        self.assertEqual(token_loss(GOLDEN, build_system_prompt(LOCALE_US)), [])

    def test_the_token_instrument_can_still_fail(self):
        """🔴 The project's own rule: a zero is trustworthy only when a fixture
        proves the tool can still report something.

        Without this, `token_loss` returning `[]` would be indistinguishable
        from `token_loss` being broken — and a tokeniser that returns nothing
        reports a clean bill of health for every input.
        """
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
    """The four ordered procedures are country-neutral BY DESIGN, and §8 says
    they stay. That claim is verified here rather than assumed — 19.0.2.2.0
    corrected two over-reaches in exactly this area."""

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
        """🔴 And an earlier fork of this module breaks it, which is why it is pinned here.

        *(read)* the fork's
        `lib/claude_client.py` at 19.0.1.1.3 — a
        prompt a Finnish deployment ran — ends with *"For Finnish Y-tunnus
        without prefix, convert: remove hyphen, prepend 'FI'"*, while its own
        `business_id` line says "exactly as printed". That instruction tells the
        model to REWRITE an identifier, and this module keys vendor matching on
        identifiers copied verbatim (`goodahead.partner.identifier`), so a
        rewritten one is a key that matches nothing.

        The EU block takes the fork's vocabulary and leaves that instruction behind.

        ⚠️ a review finding: THIS TEST ALONE PINS THE FORK'S SPELLING, NOT THE RULE.
        `assertNotIn("remove hyphen, prepend", ...)` passed against the EU
        block's own *"with the hyphen removed and FI in front"* — the same
        transformation, reworded, one clause from the ban on it. The two
        assertions below stay because the exact string is worth pinning; the
        rule is pinned by `test_no_locale_block_states_a_recipe_for_deriving_an_
        identifier`, which does not care how it is spelled.
        """
        for locale in ALL_LOCALES:
            prompt = " ".join(build_system_prompt(locale).split())
            with self.subTest(locale=locale):
                self.assertIn(
                    "Never convert, reformat or \"correct\" an identifier",
                    prompt,
                )
                self.assertNotIn("remove hyphen, prepend", prompt)

    def test_no_locale_block_states_a_recipe_for_deriving_an_identifier(self):
        """🔴 The RULE, not the fork's spelling of it (found in review).

        This module keys vendor matching on identifiers copied verbatim
        (`goodahead.partner.identifier`), so a rewritten one is a key that
        matches nothing. A model that is told how to derive a VAT number from a
        business id and then told not to has been given a rope; the repair is
        that the transformation is not stated at all.
        """
        for locale in ALL_LOCALES:
            with self.subTest(locale=locale):
                self.assertEqual(
                    derivation_recipes(build_system_prompt(locale)),
                    [],
                    "a locale block tells the model how to build one "
                    "identifier out of another",
                )

    def test_the_derivation_scanner_can_still_fail(self):
        """🔴 The control, and it is two controls.

        A zero from an instrument nobody has proved can be non-zero is not a
        measurement. Fixture 1 is the fork's line verbatim; fixture 2 is the EU
        block's own wording before this release, which the string assertion
        above passes against — so this proves the scanner sees what that one
        cannot. Fixture 3 is the shared `Important rules:` prohibition, which
        must NOT be reported: an instrument that flags the rule it enforces
        would have to be switched off, and a switched-off instrument reports a
        clean bill of health for every input.
        """
        fork_line = ("For Finnish Y-tunnus without prefix, convert: remove hyphen, "
                     "prepend 'FI'")
        self.assertTrue(derivation_recipes(fork_line))

        was = ("THE TWO ARE DIFFERENT FIELDS EVEN WHEN THEY SHARE DIGITS. A "
               "Finnish VAT number is that country's business id with the "
               "hyphen removed and FI in front, and it is STILL two values: "
               "copy each into its own field in the form the page prints it, "
               "and never derive one from the other.")
        self.assertTrue(
            derivation_recipes(was),
            "the scanner cannot see the wording this finding was about",
        )
        self.assertNotIn("remove hyphen, prepend", was)  # the old test's blind spot

        rule = ("Copy identifiers and account numbers EXACTLY as printed. Do "
                "not add or remove hyphens, do not add a country prefix that "
                "is not there, and do not convert between identifier types")
        self.assertEqual(derivation_recipes(rule), [])

    def test_no_locale_block_states_a_digit_range_for_a_payment_reference(self):
        """🔴 a review finding. A Finland-only fact widened to a continent.

        an earlier fork of this module says *"Finnish national references: numeric string, 2-20
        digits"*. `_EU_EEA` carries `NO` and `SE`, and a Norwegian KID and a
        Swedish OCR reference run to **25** digits. The prompt's own escape two
        clauses later is "report null rather than an invoice number dressed up
        as a reference", `payment_reference` is optional, `_validate_extraction`
        never looks at it and `ubl_generator` simply omits `cbc:PaymentID` — so
        a 22-digit KID produces a bill the vendor's bank cannot match, and
        nothing logs it.

        The generic block already states no length, and that is the version the
        EU block now carries.
        """
        for locale in ALL_LOCALES:
            with self.subTest(locale=locale):
                hits = _DIGIT_RANGE_RE.findall(build_system_prompt(locale))
                self.assertEqual(
                    hits, [],
                    "a locale block states a digit-count range for a payment "
                    "reference; no country's range is every country's",
                )

    def test_the_digit_range_scanner_can_still_fail(self):
        """The control for the one above — the fork's line verbatim."""
        self.assertEqual(
            _DIGIT_RANGE_RE.findall(
                "Finnish national references: numeric string, 2-20 digits"
            ),
            ["2-20 digits"],
        )
        # ⚠️ And the shapes it must NOT report, all three of which are in the
        # shipped prompt: "9-digit ABA routing number", "two check digits",
        # "5 digits or ZIP+4".
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
        """Taken from an earlier fork of this module at 19.0.1.1.3, not invented here."""
        eu = build_system_prompt(LOCALE_EU)
        for marker in EU_ONLY_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(marker, eu)
        self.assertNotIn("Y-tunnus", build_system_prompt(LOCALE_US))

    def test_the_generic_block_names_neither_country(self):
        """🔴 The point of having a third block at all. An unknown country gets
        a prompt that makes no national assumption — not the US one, and not
        Finland's either."""
        generic = build_system_prompt(LOCALE_GENERIC)
        for marker in US_ONLY_MARKERS + EU_ONLY_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, generic)

    def test_the_schema_keys_are_identical_in_every_locale(self):
        """🔴 the dual-core design's promise to everything downstream: *"the extraction
        schema keys stay the same in both … so nothing downstream forks"*.

        `ubl_generator`, `_ai_match_existing_vendor`, `bank_numbers` and the
        stored `extraction.json` all read these names. A locale block that
        renamed `routing_number` to `clearing_number` would read perfectly
        well and produce a bill with no bank details.
        """
        reference = _schema_keys(build_system_prompt(LOCALE_US))
        self.assertIn("routing_number", reference)
        self.assertIn("iban", reference)
        for locale in (LOCALE_EU, LOCALE_GENERIC):
            with self.subTest(locale=locale):
                self.assertEqual(_schema_keys(build_system_prompt(locale)),
                                 reference)


@tagged("post_install", "-at_install")
class TestEveryBlocksOwnWordingIsPinned(BaseCase):
    """🔴 a review finding. Positive coverage for the two blocks that had none.

    The marker lists above measure the US block; the EU block was pinned by five
    phrases and the generic block by nothing at all. *(measured by mutation)*
    2,756 characters could be deleted from the generic block with the whole
    suite green. The failure that buys: a later reword drops the generic
    block's *"Do NOT put an IBAN in `account_number`, or a bank code in
    `iban`"*, 470 tests stay green on both benches, and the symptom appears only
    as vendor-matching keys that stop matching — on the deployments (Japan,
    Brazil, Canada, the UK, Switzerland) that neither bench represents.
    """

    def test_the_thirteen_slots_are_five_prose_and_eight_schema(self):
        """🔴 The guard that makes the list below finite AND complete.

        A fourteenth slot added to `PROCEDURES` and to all three blocks would
        otherwise ship with no fragment pinning it, and nothing would say so —
        which is this finding happening again one slot at a time.
        """
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
        """🔴 Both jobs in one assertion, and that is deliberate.

        Present in its own composition is the POSITIVE coverage the EU and
        generic blocks did not have. Absent from the other two is the
        SELF-CHECK — the same discipline that caught three bad markers in
        `US_ONLY_MARKERS`' first draft — and it is what stops a fragment that
        has drifted into the shared `PROCEDURES` from turning this list into a
        guard that cannot fail.
        """
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
    """🔴 a review finding. The schema KEYS do not fork — the VALUES do.

    `test_the_schema_keys_are_identical_in_every_locale` is correct about keys
    and blind to this: `unit_code` and `payment_means_code` carry their value
    domain in the prose beside the key, and the three blocks list different
    ones. So the same PDF yields a different `unit_code` depending on the
    importing company's country, on a field written verbatim to the UBL
    `unitCode` attribute that core maps to a UoM.

    **The judgement, stated rather than left implicit.** The fork is WANTED for
    `payment_means_code` — `58` is the SEPA credit transfer and naming it on a
    US document would be noise — and it is NOT wanted for `unit_code`, where a
    unit is a fact about the document and not about the reader. A US vendor
    really does bill in cases and a Finnish one in tonnes, and either can send
    an invoice to either company. The repair is not one shared list (the US
    block is byte-frozen and cannot change) but an OPEN one: the non-US blocks
    end in "etc." again, which is how the fork's own line ended before it was copied
    here (F11). Both halves are pinned below so the next reader inherits the
    decision rather than the symptom.
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
        """⚠️ The US block is excluded and the reason is not a preference:
        `test_the_composed_us_prompt_is_byte_identical_to_the_shipped_one` is
        the acceptance test of this whole release, so the US list cannot gain a
        character. The EU list was built from the fork's, whose line ends
        *"…MTR=meter, LTR=liter, etc."* — an explicitly open list that was
        closed when it was copied. The deployment the vocabulary came FROM got
        a narrower instruction than it had.
        """
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
    """🔴 Which block is chosen, and what happens when nothing says."""

    def test_the_united_states_selects_the_us_block(self):
        self.assertEqual(locale_for_country("US"), LOCALE_US)

    def test_a_lower_case_or_padded_code_still_selects(self):
        """The company's `country_id.code` is clean, but the callers are not
        the only entry point and a silent downgrade to the generic block would
        change the US deployment's prompt without changing anything visible."""
        self.assertEqual(locale_for_country(" us "), LOCALE_US)

    def test_finland_selects_the_eu_block(self):
        self.assertEqual(locale_for_country("FI"), LOCALE_EU)

    def test_an_unknown_country_does_not_get_the_us_block(self):
        """🔴 The same mistake the dual-core design had just fixed in the phone rule: a
        country nobody considered must not inherit the shaping of the country
        the module happened to be written in."""
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
        """🔴 a review finding. The docstring promised this and the code did
        not do it.

        `key = str(locale or "").strip().lower() or LOCALE_GENERIC` turned a
        MISSING locale into the known key "generic", so `LOCALE_BLOCKS.get`
        succeeded and the warning branch was skipped. *(measured)* `None`, `""`
        and `"   "` all composed generic with no log line at all; only a
        non-empty unknown string warned.

        On the Odoo path `_ai_prompt_locale` logs its own warning, so this bites
        every OTHER caller — and `ClaudeInvoiceExtractor(locale=None)` is the
        documented default, which means "this extractor is running the weakest
        prompt we ship" was unobservable exactly where it is most likely.
        """
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
        """The control for the one above: the three shipped locales are the
        ordinary case and must not warn, or the warning means nothing.

        ⚠️ `assertLogs` fails when NOTHING is logged, so the quiet case is
        asserted by logging one line of our own and finding it alone.
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
        """🔴 The deliberate boundary, and it is the one worth stating. The US
        block names EINs, ABA routing numbers and state sales tax. Canada has
        none of those three, so inheriting the block because the countries are
        neighbours would be exactly the silent-US-default this step exists to
        remove. CA gets the generic block until somebody writes a CA one."""
        self.assertIsNone(locale_for_country("CA"))


@tagged("post_install", "-at_install")
class TestTheSlotGuardCanStillFail(BaseCase):
    """🔴 a review finding. The guard that stops `@@locale_tax@@` reaching the
    API was the one instrument in this file with no control.

    `test_no_locale_slot_is_left_unfilled` asserts the three shipped locales
    contain no `@@`. That is a property of today's data, not a proof that the
    guard fires — and this file supplies that proof twice over for its two other
    instruments (`test_the_token_instrument_can_still_fail`,
    `test_the_schema_reader_can_still_fail`) and promises it in its own module
    docstring.

    Two separate things were measured and both are repaired here:

    1. A slot present in `PROCEDURES` but missing from a BLOCK never reached
       the `ValueError` at all — `_SLOT_RE.sub(lambda m: block[m.group(1)])`
       raised `KeyError` from inside the lambda first. Loud, so not dangerous,
       but it is not the error the code says it raises and it names a bare key.
    2. The `ValueError` branch was reachable only for a MALFORMED marker that
       `_SLOT_RE` cannot match — and in exactly that case `_SLOT_RE.findall`
       returns `[]`, so the message read, verbatim:
       `the 'us' prompt locale left a slot unfilled: []`. The one error whose
       job is to name the broken slot could not name it.
    """

    def test_a_slot_no_block_fills_is_named(self):
        template = claude_client.PROCEDURES + "\n@@locale_zz@@\n"
        with patch.object(claude_client, "PROCEDURES", template):
            with self.assertRaises(ValueError) as caught:
                build_system_prompt(LOCALE_US)
        self.assertIn("locale_zz", str(caught.exception))

    def test_a_marker_the_slot_regex_cannot_parse_is_named(self):
        """The case that produced `[]`. `@@locale-tax@@` has a hyphen, so
        `_SLOT_RE` (`[a-z_]+`) does not match it, the substitution leaves it
        standing, and it would have gone to the API."""
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
        """🔴 The default must be generic, because the lib-only callers — every
        test in `test_claude_client.py` among them — pass no locale, and a
        default of `us` would make the module's own suite measure one country
        while claiming to measure the rule."""
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
    """🔴 19.0.2.3.0, and the whole currency-from-country feature on `master`
    rests on it. Moving wording around must not relax it.

    ⚠️ a review finding: THE THREE-LOCALE LOOP THAT USED TO BE HERE RAN ONE CODE
    PATH THREE TIMES. `self.locale` is read in exactly one place —
    `extract_invoice` → `build_system_prompt` — and `_validate_extraction`,
    `_resolve_currency_from_country`, `_currency_is_usable` and
    `required_fields` never see it, so `for locale in ALL_LOCALES` around a
    null-currency assertion could not distinguish the three. The class's stated
    purpose is a claim about the PROMPT, and that claim is covered where it can
    actually fail: `PROCEDURE_RULES` carries *"Do NOT fall back on USD, on the
    symbol alone, or on the country"* and asserts it in all three locales. The
    loop is gone and the fact it was hiding is stated by
    `test_the_validator_cannot_see_the_locale_and_that_is_why_this_runs_once`.
    """

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
        """The replacement for the loop, and it asserts the REASON.

        If validation ever did become locale-aware, this is the assertion that
        would have to be rewritten — which is the point. A loop that cannot
        distinguish its iterations hides that; naming the attribute does not.
        """
        blind = ("_validate_extraction", "_currency_is_usable",
                 "_resolve_currency_from_country")
        for name in blind:
            method = getattr(claude_client.ClaudeInvoiceExtractor, name)
            with self.subTest(method=name):
                self.assertNotIn("locale", method.__code__.co_names)
        # 🔴 The control, and it is free: the ONE method that does read it.
        # Without this the loop above would pass against a reader that has
        # stopped seeing attribute names at all.
        self.assertIn(
            "locale",
            claude_client.ClaudeInvoiceExtractor.extract_invoice.__code__.co_names,
        )

    def test_the_control_a_stated_currency_passes(self):
        result = self._run(self._extraction(), locale=LOCALE_EU)
        self.assertEqual(result["data"]["currency"], "EUR")


@tagged("post_install", "-at_install")
class TestTheFallbackCurrencyIsAnArgument(BaseCase):
    """The lib half of F2, and it needs no company and no registry.

    `generate_peppol_invoice_xml` is where the argument is consumed, so this is
    where "the argument is honoured" and "the literal is what is left without
    one" can be stated with a value no bench's company could ever hold.
    """

    def _code(self, **kwargs):
        from lxml import etree

        from odoo.addons.goodahead_invoice_import.adapters.ubl_generator import (
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
        from odoo.addons.goodahead_invoice_import.adapters import ubl_generator

        self.assertEqual(self._code(), ubl_generator.DEFAULT_CURRENCY)
        self.assertEqual(self._code(default_currency=None),
                         ubl_generator.DEFAULT_CURRENCY)


@tagged("post_install", "-at_install")
class TestDefaultCurrencyComesFromTheCompany(TransactionCase):
    """`DEFAULT_CURRENCY = "USD"` was a literal in `adapters/ubl_generator.py`, and
    on a Finnish deployment it denominated an unmarked re-import in dollars."""

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
        """🔴 The portability bug, in one assertion. This test used to read
        `assertEqual(..., "USD")` — which passes on BOTH benches, because the
        old code could not see the company at all and always wrote USD. The
        assertion was therefore true on a Finnish database for the wrong
        reason, which is the shape of defect this whole feature line is about.
        """
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
        """🔴 A REVIEW FINDING — AND IT IS THE DEFECT THIS RELEASE
        NAMES, COMMITTED ONE LEVEL UP.

        All three tests covering the new `default_currency=` argument assert
        against `company.currency_id.name`. On the US bench that is
        `USD`, and `ubl_generator.DEFAULT_CURRENCY` is also `"USD"` — so
        **deleting the keyword argument, restoring the exact bug, left all
        three passing on the bench that actually runs**. The fix was
        load-bearing only on the bench that is not the routine one, which is
        the same shape as `test_currency_falls_back_to_usd` passing on both
        benches because the code could not see the company.

        The repair is to move the literal out of the way. With
        `DEFAULT_CURRENCY` patched to a sentinel, the two answers differ on
        EVERY bench: wired, the code is the company's currency; unwired, it is
        `ZZZ`.
        """
        from lxml import etree

        from odoo.addons.goodahead_invoice_import.adapters import ubl_generator

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
        # ⚠️ The existing company, with its country rewritten inside the
        # transaction — NOT a new one. `res.company.create` on this tree runs
        # `stock_dropshipping._create_per_company_rules`, which needs a chart
        # of accounts and journals that a bare company does not have; the first
        # draft of this class errored in `setUpClass` on exactly that. The
        # rollback at the end of each test puts the country back.
        cls.company = cls.env.company

    def test_a_us_company_gets_the_us_block(self):
        self.company.country_id = self.env.ref("base.us")
        self.assertEqual(self.AM._ai_prompt_locale(self.company), LOCALE_US)

    def test_a_finnish_company_gets_the_eu_block(self):
        self.company.country_id = self.env.ref("base.fi")
        self.assertEqual(self.AM._ai_prompt_locale(self.company), LOCALE_EU)

    def test_a_company_with_no_country_gets_neither_and_says_so(self):
        """🔴 It is not an error — plenty of databases never set it — but it is
        not silent either, because the resulting prompt is weaker than both
        shipped ones and nothing on the bill would show that."""
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
