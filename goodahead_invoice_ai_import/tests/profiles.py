# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Profile-gated tests — because two benches that never disagree are one bench
run twice (the dual-core design).

A test that can only pass on one of the two 19.0 import pipelines carries
``@skip_unless_profile(...)`` and is skipped on the other, and
`test_legacy_name_slot.TestBothProfilesAreCovered` checks that the gates really
fired on this bench.

🔴 THERE ARE THREE REGISTRIES, NOT ONE, AND THE DIFFERENCE IS THE WHOLE POINT
(found in review). The original version kept only `PROFILE_GATED`,
filled inside ``decorate(...)`` — i.e. when Python evaluates the decorator line,
not when a test runs. `tests/__init__.py` imports every test module on both
benches, so that registry is populated IDENTICALLY on a search-plan bench, on a
legacy bench, and on a bench where every gated test skipped. The assertion built
on it was satisfied by the decorators being present in the source, which is the
same shape of check as the `hasattr` probe `models/core_profile.py` exists to
avoid: one that cannot fail for a run-level reason.

  * `PROFILE_GATED`   — import time. An INVENTORY: every gated test method's
    qualname. Its job is to be counted against `EXPECTED_GATED` below, which is
    what catches a whole test module dropped from `tests/__init__.py` or a gate
    deleted — neither of which the run-time sets can see, because a decorator
    that never evaluates registers nothing anywhere.
  * `PROFILE_RAN`     — run time, recorded AFTER the profile check passes. The
    tests that actually executed on this bench.
  * `PROFILE_SKIPPED` — run time, recorded when the check declines.

`PROFILE_RAN | PROFILE_SKIPPED` must equal `PROFILE_GATED` per profile at the end
of a run: every gate was reached, and each one decided. That is asserted by
`TestBothProfilesAreCovered`, which carries ``test_sequence`` so it runs LAST —
Odoo's loader sorts the suite on exactly that attribute
(`odoo/tests/loader.py:107`, identical on both measured frameworks), and without
it the assertion would read a half-filled set and mean nothing.

The gate reads the RECORDED profile (`_ai_core_profile`), not a re-detection, so
a bench whose `ir.config_parameter` disagrees with its registry skips loudly
rather than silently testing the wrong thing.

🔴 **A gate is the LAST resort, not the first.** Skipping a test is how a real
gap becomes a clean run, so the order to try is: make the assertion
profile-agnostic (state the rule, not one build's answer) · gate only the one
assertion that cannot be · gate the method · gate the class. The class gate is
the bluntest of the four and `skip_method_unless_profile` exists so that a class
holding one build-specific test and six portable ones does not lose the six.
"""

import functools

from odoo.addons.goodahead_invoice_import.models.core_profile import (
    PROFILE_LEGACY,
    PROFILE_SEARCH_PLAN,
)

#: profile -> every gated test method's qualname. Import time: an inventory of
#: what the SOURCE gates, not of what a run did.
PROFILE_GATED = {PROFILE_SEARCH_PLAN: set(), PROFILE_LEGACY: set()}

#: profile -> the gated tests that EXECUTED on this bench. Run time.
PROFILE_RAN = {PROFILE_SEARCH_PLAN: set(), PROFILE_LEGACY: set()}

#: profile -> the gated tests that SKIPPED on this bench. Run time.
PROFILE_SKIPPED = {PROFILE_SEARCH_PLAN: set(), PROFILE_LEGACY: set()}

#: 🔴 STATED, NOT DERIVED, AND THAT IS THE POINT. The run-time sets cannot see a
#: test module dropped from `tests/__init__.py` or a `@skip_unless_profile` line
#: deleted: the decorator simply never evaluates and every set shrinks together,
#: consistently. A number written down here is the only thing that notices.
#: Keep it equal to the skip counts recorded in `CHANGELOG.md`.
#: *(measured 2026-10-10, 19.0.3.12.0)* a search-plan bench therefore skips 9
#: and a legacy bench skips 20, and those are the numbers that run's log must
#: show. (19.0.3.9.0 read 17 / 6; step 7 added one search-plan gate —
#: `test_core_never_reaches_the_qweb_render`, which could not fail on the other
#: bench — and the three-test legacy class `TestLegacyCoreHasNoSubstitutePdf`,
#: reaching 18 / 9. 🔴 19.0.3.12.0 added TWO more search-plan gates and both are
#: a review finding: `test_no_generated_by_odoo_pdf_on_the_bill` and
#: `test_only_one_pdf_because_core_rendered_no_second_one` were vacuous on the
#: legacy core for the SAME reason the gate above them was added for, and were
#: left standing when it was. Finding the class is not the same as sweeping it.)
#: 🆕 19.0.3.17.0 added ONE more search-plan gate —
#: `test_this_core_really_does_read_the_substitute_pdf_parameter`, the first
#: absolute assertion anywhere on the capability probe (found in review).
#: So a search-plan bench skips 9 and a legacy bench skips 21 gated tests (plus
#: the four environment skips §5 names, which no registry here can see: a
#: legacy run's log reads 25).
#: 🆕 19.0.3.23.0 added TWO legacy gates, both in
#: `test_own_company_is_never_the_vendor` — the two that drive a FULL import
#: through the legacy decoder. The other nine tests in that file are portable
#: on purpose: they call `account.edi.common._import_partner` /
#: `_import_partner_bank`, `res.partner._ai_find_vendor` and
#: `account.move._ai_vendor_candidates` directly, and all four exist on both
#: cores. So a search-plan bench now skips 11 and a legacy bench still skips 21.
EXPECTED_GATED = {PROFILE_SEARCH_PLAN: 21, PROFILE_LEGACY: 11}


def _gated_test_names(cls):
    """The test methods a class gate covers, as the LOADER sees them.

    ⚠️ ``vars(cls)``, not ``dir(cls)``: Odoo's loader collects
    ``test_case_class.__dict__.items()`` (`odoo/tests/loader.py:31`) unless the
    class opts into inherited methods, so a name reachable only by inheritance
    is not a test on this class and must not be counted as one — otherwise
    `EXPECTED_GATED` is compared against a number the run can never reach.
    """
    return sorted(
        name
        for name, value in vars(cls).items()
        if name.startswith("test") and callable(value)
    )


def _record(registry, profile, qualname):
    registry[profile].add(qualname)


def skip_unless_profile(profile):
    """Skip a TestCase class unless the live build is on `profile`."""
    if profile not in PROFILE_GATED:
        raise ValueError("unknown core profile %r" % (profile,))

    def decorate(cls):
        names = _gated_test_names(cls)
        if not names:
            # A class gate over nothing is a gate that can never fire, and the
            # count below would then be unreachable. Loud, at import.
            raise ValueError(
                "%s has no test methods of its own to gate" % (cls.__name__,)
            )
        for name in names:
            PROFILE_GATED[profile].add("%s.%s" % (cls.__name__, name))
        original_setup = cls.setUp

        def setUp(self, *args, **kwargs):
            qualname = "%s.%s" % (type(self).__name__, self._testMethodName)
            live = self.env["account.move"]._ai_core_profile()
            if live != profile:
                _record(PROFILE_SKIPPED, profile, qualname)
                self.skipTest(
                    "needs core profile %r; this build records %r" % (profile, live)
                )
            _record(PROFILE_RAN, profile, qualname)
            return original_setup(self, *args, **kwargs)

        cls.setUp = setUp
        return cls

    return decorate


def skip_method_unless_profile(profile):
    """Skip ONE test method unless the live build is on `profile`.

    🔴 Why this exists rather than widening the class gate. Four of
    `TestCreditMemoBank`'s tests call `_import_ubl_retrieve_partner_bank`
    directly — a core method the legacy build does not have, so `super()` raises
    there and nothing can make them portable. The other eight tests in the same
    class are the `test_older_hook_*` pair and the end-to-end checks, and they
    are the ones that matter MOST on the legacy build. Gating the class would
    have taken the module's live legacy bank hook out of the legacy run
    entirely, which is the opposite of what the gate is for.

    It registers under the same profile registries as the class decorator, so a
    typo still trips `TestBothProfilesAreCovered`.
    """
    if profile not in PROFILE_GATED:
        raise ValueError("unknown core profile %r" % (profile,))

    def decorate(method):
        # ⚠️ A gate on a non-test helper is invisible to every count: it swells
        # the registry while gating nothing a run can execute. a review finding,
        # failure 3. Loud, at import.
        if not method.__name__.startswith("test"):
            raise ValueError(
                "%s is not a test method; a profile gate on it fires nothing"
                % (method.__qualname__,)
            )
        PROFILE_GATED[profile].add(method.__qualname__)

        @functools.wraps(method)
        def wrapper(self, *args, **kwargs):
            live = self.env["account.move"]._ai_core_profile()
            if live != profile:
                _record(PROFILE_SKIPPED, profile, method.__qualname__)
                self.skipTest(
                    "needs core profile %r; this build records %r" % (profile, live)
                )
            _record(PROFILE_RAN, profile, method.__qualname__)
            return method(self, *args, **kwargs)

        return wrapper

    return decorate
