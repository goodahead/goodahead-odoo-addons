# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Profile gates for tests that can only pass on some of the UBL import builds.

The builds are `legacy` (Odoo 18.0 before April 2026 and early 19.0 builds),
`search_plan` (newer 19.0 builds) and `search_plan_18` (Odoo 18.0 from April
2026 on: 19.0's partner pipeline with 18.0's product plan). On 18.0 every
`search_plan`-only test is skipped. A gate names one or more profiles; the
test runs on any of them and is skipped on the others.
`test_legacy_name_slot.TestBothProfilesAreCovered` checks that the gates really
fired. Three registries are kept, per profile:

  * `PROFILE_GATED`   - filled at import time: every gated test method's qualname,
    compared against `EXPECTED_GATED` to catch a dropped test module or gate.
  * `PROFILE_RAN`     - filled at run time when the test ran on that profile.
  * `PROFILE_SKIPPED` - filled at run time for each gated profile the build is not.

A test gated on two profiles is in both profiles' `PROFILE_GATED`; on a run it
lands in `PROFILE_RAN` for the live profile and in `PROFILE_SKIPPED` for the
other. At the end of a run `PROFILE_RAN | PROFILE_SKIPPED` must equal
`PROFILE_GATED` per profile. The gate reads the recorded profile
(`_ai_core_profile`), not a re-detection. Prefer a profile-agnostic assertion
over a gate, and a method gate over a class gate.
"""

import functools

from odoo.addons.goodahead_invoice_import.models.core_profile import (
    KNOWN_PROFILES,
    PROFILE_LEGACY,
    PROFILE_SEARCH_PLAN,
    PROFILE_SEARCH_PLAN_18,
)

#: profile -> every gated test method's qualname (import time).
PROFILE_GATED = {profile: set() for profile in KNOWN_PROFILES}

#: profile -> the gated tests that executed on that profile in this run.
PROFILE_RAN = {profile: set() for profile in KNOWN_PROFILES}

#: profile -> the gated tests that skipped that profile in this run.
PROFILE_SKIPPED = {profile: set() for profile in KNOWN_PROFILES}

#: Stated, not derived: the run-time sets cannot notice a test module dropped from
#: `tests/__init__.py` or a deleted gate, because the decorator never evaluates.
#: Keep it equal to the skip counts recorded in `CHANGELOG.md`.
EXPECTED_GATED = {
    PROFILE_SEARCH_PLAN: 21,
    PROFILE_SEARCH_PLAN_18: 22,
    PROFILE_LEGACY: 12,
}


def _check_profiles(profiles):
    if not profiles:
        raise ValueError("a profile gate needs at least one profile")
    for profile in profiles:
        if profile not in PROFILE_GATED:
            raise ValueError("unknown core profile %r" % (profile,))
    return frozenset(profiles)


def _gated_test_names(cls):
    """The test methods a class gate covers, as the loader sees them.

    ``vars(cls)``, not ``dir(cls)``: Odoo's loader collects only the class's own
    ``__dict__``, so inherited methods must not be counted.
    """
    return sorted(
        name
        for name, value in vars(cls).items()
        if name.startswith("test") and callable(value)
    )


def _enter(test, profiles, qualname):
    """Record the run and skip the test unless the live build is one of
    `profiles`."""
    live = test.env["account.move"]._ai_core_profile()
    for profile in profiles:
        if profile != live:
            PROFILE_SKIPPED[profile].add(qualname)
    if live not in profiles:
        test.skipTest(
            "needs core profile %s; this build records %r"
            % (" or ".join(repr(p) for p in sorted(profiles)), live)
        )
    PROFILE_RAN[live].add(qualname)


def skip_unless_profile(*profiles):
    """Skip a TestCase class unless the live build is on one of `profiles`."""
    profiles = _check_profiles(profiles)

    def decorate(cls):
        names = _gated_test_names(cls)
        if not names:
            # A class gate over nothing could never fire.
            raise ValueError(
                "%s has no test methods of its own to gate" % (cls.__name__,)
            )
        for name in names:
            for profile in profiles:
                PROFILE_GATED[profile].add("%s.%s" % (cls.__name__, name))
        original_setup = cls.setUp

        def setUp(self, *args, **kwargs):
            qualname = "%s.%s" % (type(self).__name__, self._testMethodName)
            _enter(self, profiles, qualname)
            return original_setup(self, *args, **kwargs)

        cls.setUp = setUp
        return cls

    return decorate


def skip_method_unless_profile(*profiles):
    """Skip one test method unless the live build is on one of `profiles`.

    Lets a class keep its portable tests on every build while gating only the
    ones that call a core method missing on another build.
    """
    profiles = _check_profiles(profiles)

    def decorate(method):
        # A gate on a non-test helper would swell the registry while gating nothing.
        if not method.__name__.startswith("test"):
            raise ValueError(
                "%s is not a test method; a profile gate on it fires nothing"
                % (method.__qualname__,)
            )
        for profile in profiles:
            PROFILE_GATED[profile].add(method.__qualname__)

        @functools.wraps(method)
        def wrapper(self, *args, **kwargs):
            _enter(self, profiles, method.__qualname__)
            return method(self, *args, **kwargs)

        return wrapper

    return decorate
