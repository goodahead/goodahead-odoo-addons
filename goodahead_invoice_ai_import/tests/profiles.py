# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Profile gates for tests that can only pass on one of the two UBL import pipelines.

The pipelines are `legacy` (Odoo 18.0 and early 19.0 builds) and `search_plan`
(newer 19.0 builds); on 18.0 every `search_plan`-gated test is skipped. A gated
test is skipped on the other pipeline, and
`test_legacy_name_slot.TestBothProfilesAreCovered` checks that the gates really fired.
Three registries are kept:

  * `PROFILE_GATED`   - filled at import time: every gated test method's qualname,
    compared against `EXPECTED_GATED` to catch a dropped test module or gate.
  * `PROFILE_RAN`     - filled at run time when the profile check passes.
  * `PROFILE_SKIPPED` - filled at run time when the check declines.

At the end of a run `PROFILE_RAN | PROFILE_SKIPPED` must equal `PROFILE_GATED` per
profile. The gate reads the recorded profile (`_ai_core_profile`), not a re-detection.
Prefer a profile-agnostic assertion over a gate, and a method gate over a class gate.
"""

import functools

from odoo.addons.goodahead_invoice_ai_import.models.core_profile import (
    PROFILE_LEGACY,
    PROFILE_SEARCH_PLAN,
)

#: profile -> every gated test method's qualname (import time).
PROFILE_GATED = {PROFILE_SEARCH_PLAN: set(), PROFILE_LEGACY: set()}

#: profile -> the gated tests that executed in this run.
PROFILE_RAN = {PROFILE_SEARCH_PLAN: set(), PROFILE_LEGACY: set()}

#: profile -> the gated tests that skipped in this run.
PROFILE_SKIPPED = {PROFILE_SEARCH_PLAN: set(), PROFILE_LEGACY: set()}

#: Stated, not derived: the run-time sets cannot notice a test module dropped from
#: `tests/__init__.py` or a deleted gate, because the decorator never evaluates.
#: Keep it equal to the skip counts recorded in `CHANGELOG.md`.
EXPECTED_GATED = {PROFILE_SEARCH_PLAN: 21, PROFILE_LEGACY: 11}


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


def _record(registry, profile, qualname):
    registry[profile].add(qualname)


def skip_unless_profile(profile):
    """Skip a TestCase class unless the live build is on `profile`."""
    if profile not in PROFILE_GATED:
        raise ValueError("unknown core profile %r" % (profile,))

    def decorate(cls):
        names = _gated_test_names(cls)
        if not names:
            # A class gate over nothing could never fire.
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
    """Skip one test method unless the live build is on `profile`.

    Lets a class keep its portable tests on both builds while gating only the
    ones that call a core method missing on the other build.
    """
    if profile not in PROFILE_GATED:
        raise ValueError("unknown core profile %r" % (profile,))

    def decorate(method):
        # A gate on a non-test helper would swell the registry while gating nothing.
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
