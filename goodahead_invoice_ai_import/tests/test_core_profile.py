# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Which core pipeline is this module running on, and can the question be answered wrong?

🔴 THE QUESTION THIS FILE ANSWERS. Two Odoo 19.0 builds, seven months apart,
import a vendor bill through two different pipelines. This module hooks both.
So it has to know which one it is on — and the obvious way of asking is
worthless:

    hasattr(self.env['account.edi.ubl'], '_import_ubl_retrieve_customer_search_plan')

is True on BOTH builds the moment this module is installed, because this module
defines that method. A probe that cannot say no is not a probe.

Sharper still, and the reason a name test answers the wrong question twice:
`_retrieve_partner` and `_retrieve_partner_with_name` exist in BOTH 19.0 builds.
On the search-plan core `_retrieve_partner_with_name` is a deprecated stub with
zero core callers. So the presence of a method proves nothing about whether
anything calls it.

Hence two things are tested here and they are not the same thing:

1. `core_defines_in_mro` — can it return **False** when only our own class
   defines the name? `test_only_our_module_defines_it_is_not_core` is the whole
   value of the probe; without it every other assertion below is decorative.
2. `classify_core_profile` — can it return all **three** answers? The fixture
   cores at the top of this file produce `search_plan`, `legacy` and `unknown`,
   so the classifier is shown to be capable of refusing, not only of agreeing.

⚠️ `unknown` is fixture-only, and always will be: a build that matches neither
profile is by definition one nobody has. `search_plan` and `legacy` are each
measured against a real core — the US bench and the legacy bench respectively.

🔴 WHAT CHANGED IN 19.0.3.7.0, AND WHY IT IS THE POINT OF THE WHOLE FILE.
`TestCoreProfileOnThisBuild` used to assert `search_plan` BY NAME, five times.
That is a bench-is-the-bench guard that is wrong on the other bench by
construction: run it on the legacy build and five tests fail, every one of them
saying "this is not the build I was written on" rather than "this build is
broken". A guard that can only pass on one of the two things it exists to tell
apart has stopped being a guard.

So these tests now assert the RULE: the live build matches exactly one of the
two known profiles, that profile is what got recorded, that recording is what
the refusal path reads, and that profile is what the INFO line names. Every one
of them can still fail — on a build matching both profiles, on a build matching
neither, on a recorded value that disagrees with a live re-detection, or on a
probe that answers from our own classes. WHICH profile each bench is remains
asserted, but at its own seam: `test_legacy_name_slot`'s two gated classes, one
per profile, which is also what keeps the two benches from being one bench run
twice (the dual-core design).
"""

import logging
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import BaseCase, TransactionCase, tagged

from odoo.addons.goodahead_invoice_ai_import.lib import claude_client
from odoo.addons.goodahead_invoice_import.models.core_profile import (
    CORE_PROFILE_PARAM,
    OUR_MODULE,
    PROFILE_LEGACY,
    PROFILE_SEARCH_PLAN,
    PROFILE_UNKNOWN,
    classify_core_profile,
    core_defines_in_mro,
)

# ---------------------------------------------------------------------------
# Three fixture cores. Each is the set of (model, method) pairs that core — not
# this module — defines on that build.
# ---------------------------------------------------------------------------

# The search-plan core, odoo/odoo 189df353 (2026-06-23).
SEARCH_PLAN_CORE = {
    ("account.edi.ubl", "_ubl_import_invoice"),
    ("account.edi.ubl", "_import_ubl_retrieve_customer_search_plan"),
    ("account.edi.ubl", "_import_ubl_create_missing_customer"),
    ("res.partner", "_import_retrieve_customer"),
    ("res.partner", "_import_retrieve_customer_from_name"),
    ("product.product", "_import_retrieve_product_from_supplierinfo"),
    ("account.edi.ubl", "_import_ubl_retrieve_products_search_plan"),
    # 🔴 It ALSO has every name the legacy profile asserts. That is not padding
    # — it is the trap. A profile built only out of "is this name present"
    # would match `legacy` here too.
    ("account.edi.common", "_import_partner"),
    ("account.edi.common", "_import_lines"),
    ("account.edi.common", "_import_product"),
    ("res.partner", "_retrieve_partner"),
    ("res.partner", "_retrieve_partner_with_name"),
}

# 19.0 from core 5baa557 (2026-09-01) on: the product plan has moved to
# `account.edi.common` (seen through `account.edi.ubl`, which inherits it).
SEARCH_PLAN_MOVED_PRODUCTS_CORE = (
    SEARCH_PLAN_CORE
    - {("account.edi.ubl", "_import_ubl_retrieve_products_search_plan")}
) | {("account.edi.ubl", "_import_retrieve_products_search_plan")}

# Legacy core: odoo/odoo ca1f0aea (2026-01-17) / 19.0 nightly deb 20260118.
LEGACY_CORE = {
    ("account.edi.common", "_import_partner"),
    ("account.edi.common", "_import_lines"),
    ("account.edi.common", "_import_product"),
    ("res.partner", "_retrieve_partner"),
    ("res.partner", "_retrieve_partner_with_name"),
}

# A third deployment's 17.0 core (the dual-core design) — a real third pipeline, kept here as the shape of
# a build this module must refuse rather than guess at.
NEITHER_CORE = {
    ("res.partner", "_retrieve_partner"),
    ("product.product", "_retrieve_product"),
}


def probe_for(core):
    """A `_ai_core_defines`-shaped callable answering from a fixture core."""
    return lambda model_name, method_name: (model_name, method_name) in core


def unknown_core_probe(move):
    """Patch the LIVE probe on `account.move` so the detection answers `unknown`.

    🔴 WHY THIS REPLACED "SET THE PARAMETER TO `unknown`" (found in review).
    The refusal reads the live registry now, not the stored parameter, because
    the write that fills that parameter is documented as allowed to fail. A test
    that forced the refusal by writing `unknown` into `ir.config_parameter` was
    therefore driving a proxy that no longer decides anything, and would pass on
    a build whose registry is perfectly recognisable. Patching `_ai_core_defines`
    puts a genuinely unrecognisable core underneath the whole chain instead,
    which is the condition the refusal exists for.
    """
    return patch.object(
        type(move),
        "_ai_core_defines",
        lambda self, model_name, method_name: (model_name, method_name)
        in NEITHER_CORE,
    )


class _FakeClass:
    """A stand-in for one contributing class in a registry class's MRO."""

    def __init__(self, module, methods):
        self.__module__ = module
        self.__dict__.update({name: (lambda self: None) for name in methods})


def mro_of(*classes):
    return list(classes)


# ---------------------------------------------------------------------------


@tagged("post_install", "-at_install")
class TestCoreDefinesMro(BaseCase):
    """The MRO walk. No database, no registry — these are the rules themselves."""

    def test_only_our_module_defines_it_is_not_core(self):
        """🔴 THE test. Without it the whole check is decorative.

        This is the exact situation on a legacy core once this module is installed: the
        name is on the model, and the only class that put it there is ours.
        """
        mro = mro_of(
            _FakeClass(OUR_MODULE + ".models.account_edi_ubl", ["_import_ubl_create_missing_customer"]),
            _FakeClass("odoo.addons.account_edi_ubl_cii.models.account_edi_ubl", ["_export_invoice"]),
            _FakeClass("odoo.orm.models", ["create", "write"]),
        )
        self.assertFalse(core_defines_in_mro(mro, "_import_ubl_create_missing_customer"))

    def test_a_class_outside_this_module_counts(self):
        mro = mro_of(
            _FakeClass("odoo.addons.account_edi_ubl_cii.models.account_edi_ubl", ["_ubl_import_invoice"]),
            _FakeClass("odoo.orm.models", ["create"]),
        )
        self.assertTrue(core_defines_in_mro(mro, "_ubl_import_invoice"))

    def test_our_override_does_not_mask_core(self):
        """Ours first in the MRO, core's below it — the search-plan build."""
        mro = mro_of(
            _FakeClass(OUR_MODULE + ".models.account_edi_ubl", ["_import_ubl_create_missing_customer"]),
            _FakeClass("odoo.addons.account_edi_ubl_cii.models.account_edi_ubl", ["_import_ubl_create_missing_customer"]),
            _FakeClass("odoo.orm.models", ["create"]),
        )
        self.assertTrue(core_defines_in_mro(mro, "_import_ubl_create_missing_customer"))

    def test_nobody_defines_it(self):
        mro = mro_of(_FakeClass("odoo.orm.models", ["create", "write"]))
        self.assertFalse(core_defines_in_mro(mro, "_ubl_import_invoice"))

    def test_in_module_separates_a_parent_models_definition_from_a_childs(self):
        """🔴 THE FIXTURE THAT PROVES THE SUBSTITUTE-PDF PROBE CAN FAIL.

        `_inherit` puts `account.edi.common`'s classes into `account.edi.ubl`'s
        MRO, so BOTH builds answer True to "somebody outside this module defines
        `_import_attachments`". The only question that separates them is which
        FILE defines it — the substitute-PDF override lives in
        `account_edi_ubl_cii/models/account_edi_ubl.py` on one core and does not
        exist on the other.

        So: the unqualified walk must say True here and the qualified one must
        say False. Without this pair the gate in
        `_ai_core_reads_substitute_pdf_param` is decorative.
        """
        legacy_like = mro_of(
            _FakeClass(
                "odoo.addons.account_edi_ubl_cii.models.account_edi_common",
                ["_import_attachments"],
            ),
            _FakeClass("odoo.orm.models", ["create"]),
        )
        self.assertTrue(core_defines_in_mro(legacy_like, "_import_attachments"))
        self.assertFalse(
            core_defines_in_mro(
                legacy_like,
                "_import_attachments",
                "odoo.addons.account_edi_ubl_cii.models.account_edi_ubl",
            )
        )

        search_plan_like = mro_of(
            _FakeClass(
                "odoo.addons.account_edi_ubl_cii.models.account_edi_ubl",
                ["_import_attachments"],
            ),
            *legacy_like,
        )
        self.assertTrue(
            core_defines_in_mro(
                search_plan_like,
                "_import_attachments",
                "odoo.addons.account_edi_ubl_cii.models.account_edi_ubl",
            )
        )

    def test_in_module_is_matched_exactly_and_not_by_prefix(self):
        """A prefix match would let somebody else's `…account_edi_ubl_extra`
        satisfy a marker written for `…account_edi_ubl`, i.e. report a
        capability on a build nobody has measured."""
        mro = mro_of(
            _FakeClass(
                "odoo.addons.account_edi_ubl_cii.models.account_edi_ubl_extra",
                ["_import_attachments"],
            ),
        )
        self.assertFalse(
            core_defines_in_mro(
                mro,
                "_import_attachments",
                "odoo.addons.account_edi_ubl_cii.models.account_edi_ubl",
            )
        )

    def test_our_own_class_never_satisfies_in_module(self):
        """And the original rule still holds with the narrowing applied: a name
        this module put there proves nothing about core."""
        mro = mro_of(
            _FakeClass(
                OUR_MODULE + ".models.account_edi_ubl",
                ["_import_attachments"],
            ),
        )
        self.assertFalse(
            core_defines_in_mro(
                mro,
                "_import_attachments",
                OUR_MODULE + ".models.account_edi_ubl",
            )
        )

    def test_a_module_whose_name_merely_starts_with_ours_is_not_ours(self):
        """`goodahead_invoice_ai_import_extra` is somebody else's module.

        A bare `startswith(OUR_MODULE)` would swallow it and report "we defined
        this", which is the failure in the safe-looking direction: the probe
        would under-report core and fall to `unknown`.
        """
        mro = mro_of(
            _FakeClass(
                "odoo.addons.goodahead_invoice_ai_import_extra.models.account_edi_ubl",
                ["_ubl_import_invoice"],
            ),
        )
        self.assertTrue(core_defines_in_mro(mro, "_ubl_import_invoice"))


@tagged("post_install", "-at_install")
class TestProfileClassification(BaseCase):
    """The fixture that proves the classifier can produce all three answers."""

    def test_a_search_plan_core_is_recognised(self):
        profile, _failures = classify_core_profile(probe_for(SEARCH_PLAN_CORE))
        self.assertEqual(profile, PROFILE_SEARCH_PLAN)

    def test_a_search_plan_core_with_the_moved_product_plan_is_recognised(self):
        profile, _failures = classify_core_profile(
            probe_for(SEARCH_PLAN_MOVED_PRODUCTS_CORE)
        )
        self.assertEqual(profile, PROFILE_SEARCH_PLAN)

    def test_a_search_plan_core_without_any_product_plan_is_unknown(self):
        """The narrowing would be a silent no-op, so the core is refused."""
        core = SEARCH_PLAN_CORE - {
            ("account.edi.ubl", "_import_ubl_retrieve_products_search_plan")
        }
        profile, failures = classify_core_profile(probe_for(core))
        self.assertEqual(profile, PROFILE_UNKNOWN)
        self.assertTrue(
            any(
                "_import_retrieve_products_search_plan" in failure
                for failure in failures[PROFILE_SEARCH_PLAN]
            )
        )

    def test_a_legacy_core_is_recognised(self):
        profile, _failures = classify_core_profile(probe_for(LEGACY_CORE))
        self.assertEqual(profile, PROFILE_LEGACY)

    def test_an_unrecognised_core_is_unknown(self):
        """🔴 The probe can say no. A third deployment's 17.0 shape is the fixture."""
        profile, failures = classify_core_profile(probe_for(NEITHER_CORE))
        self.assertEqual(profile, PROFILE_UNKNOWN)
        # And it says WHY, per profile — the ERROR log is only useful if it names
        # what was expected.
        self.assertTrue(failures[PROFILE_SEARCH_PLAN])
        self.assertTrue(failures[PROFILE_LEGACY])

    def test_a_search_plan_core_is_not_also_legacy(self):
        """The two profiles are mutually exclusive on the discriminator.

        A search-plan core carries every name the legacy profile asserts (see
        SEARCH_PLAN_CORE above), so this only passes because `legacy` asserts
        the search-plan ENTRY POINT is absent, not merely that some names exist.
        """
        _profile, failures = classify_core_profile(probe_for(SEARCH_PLAN_CORE))
        self.assertTrue(failures[PROFILE_LEGACY])

    def test_removing_only_the_discriminator_stops_search_plan_matching(self):
        """Take `_ubl_import_invoice` away and the search-plan answer collapses.

        It collapses to `legacy` rather than to `unknown`, because what is left
        IS the legacy shape — which is the whole claim the discriminator makes.
        """
        core = SEARCH_PLAN_CORE - {("account.edi.ubl", "_ubl_import_invoice")}
        profile, _failures = classify_core_profile(probe_for(core))
        self.assertNotEqual(profile, PROFILE_SEARCH_PLAN)

    def test_two_matching_profiles_is_unknown(self):
        """A core that answers to two profiles is a state nobody has reasoned
        about, so it is `unknown` rather than whichever came first in a dict."""
        always = (("res.partner", "_retrieve_partner", True),)
        profile, _failures = classify_core_profile(
            probe_for(LEGACY_CORE), markers={"a": always, "b": always}
        )
        self.assertEqual(profile, PROFILE_UNKNOWN)


#: The two profiles this module ships adapters for. A build must be exactly one.
KNOWN_PROFILES = (PROFILE_SEARCH_PLAN, PROFILE_LEGACY)


@tagged("post_install", "-at_install")
class TestCoreProfileOnThisBuild(TransactionCase):
    """Against the real registry, on EITHER build.

    Nothing here names a profile. What it asserts is that the probe gives this
    build one answer, the same answer twice, and the answer the rest of the
    module acts on.
    """

    def _unrecognisable_core(self):
        """Both halves of "this build is not one we know", because the refusal
        reads both: the cache is cleared AND the live registry is made
        unrecognisable. Clearing the parameter alone no longer refuses anything
        — that is the whole of F1 — and patching the probe alone is outvoted by
        a cache that still holds this bench's real answer."""
        self.env["ir.config_parameter"].sudo().search(
            [("key", "=", CORE_PROFILE_PARAM)]
        ).unlink()
        return unknown_core_probe(self.env["account.move"])

    def test_the_naive_probe_cannot_fail_here(self):
        """The control. `hasattr` says yes — and would say yes on the legacy core too.

        This test does not check the probe; it checks that the thing the probe
        replaces is worthless, so nobody re-introduces it.
        """
        self.assertTrue(
            hasattr(self.env["account.edi.ubl"], "_import_ubl_retrieve_customer_search_plan")
        )
        # ...and our own module is one of the classes putting it there.
        ours = [
            klass
            for klass in type(self.env["account.edi.ubl"]).__mro__
            if (getattr(klass, "__module__", "") or "").startswith(OUR_MODULE + ".")
            and "_import_ubl_retrieve_customer_search_plan" in vars(klass)
        ]
        self.assertTrue(ours, "this module should override the search plan on this tree")

    def test_the_mro_walk_sees_core_underneath_our_override(self):
        """A name THIS MODULE overrides and core defines on BOTH builds.

        `res.partner._retrieve_partner_with_name` is the legacy adapter's seam
        (`models/res_partner.py`), and core declares it on both cores — as the
        last live step of the legacy match, and as a deprecated zero-caller stub
        on the search-plan build. So it is the one subject that tests exactly
        what this test is named for, on either bench: our class is in the MRO,
        core's class is under it, and the walk must see past ours.

        ⚠️ It used to use `_import_ubl_retrieve_customer_search_plan`, which
        core defines on only one build — so the test asserted the build, not the
        walk, and failed on the other one for the wrong reason.
        """
        move = self.env["account.move"]
        ours = [
            klass
            for klass in type(self.env["res.partner"]).__mro__
            if (getattr(klass, "__module__", "") or "").startswith(OUR_MODULE + ".")
            and "_retrieve_partner_with_name" in vars(klass)
        ]
        self.assertTrue(
            ours,
            "CONTROL: this module must override the name slot, or the walk has "
            "nothing of ours to see past",
        )
        self.assertTrue(
            move._ai_core_defines("res.partner", "_retrieve_partner_with_name")
        )

    def test_an_absent_model_is_not_defined(self):
        move = self.env["account.move"]
        self.assertFalse(move._ai_core_defines("no.such.model.at.all", "_anything"))

    def test_this_build_matches_exactly_one_known_profile(self):
        """🔴 The rule, not the build.

        Two profiles matching would be a core carrying both pipelines; none
        matching is a core nobody has reasoned about. Both are `unknown`, and
        both are a refusal. This asserts the live registry lands on exactly one
        — and that the other one is on record as having failed, so a classifier
        that simply returned the first key would not pass.
        """
        profile, failures = self.env["account.move"]._ai_detect_core_profile()
        self.assertIn(profile, KNOWN_PROFILES, failures)
        self.assertFalse(failures[profile], failures[profile])
        other = [p for p in KNOWN_PROFILES if p != profile]
        for name in other:
            self.assertTrue(
                failures[name],
                "the other profile must have failed at least one marker, or the "
                "two profiles do not tell these builds apart",
            )

    def test_the_profile_is_recorded_where_support_can_read_it(self):
        """The recorded value must be a live re-detection's answer, not a stale
        one. A profile that silently flips after a core upgrade is the next
        version of this bug, and the stored parameter is where support looks."""
        detected, _failures = self.env["account.move"]._ai_detect_core_profile()
        stored = self.env["ir.config_parameter"].sudo().get_param(CORE_PROFILE_PARAM)
        self.assertIn(stored, KNOWN_PROFILES)
        self.assertEqual(stored, detected)

    def test_a_recognised_profile_does_not_refuse(self):
        detected, _failures = self.env["account.move"]._ai_detect_core_profile()
        self.assertEqual(
            self.env["account.move"]._ai_assert_core_profile(), detected
        )

    def test_an_unrecognised_core_logs_an_error_and_records_unknown(self):
        """The refusal path, driven end to end on a fixture core.

        Without this the ERROR branch and the `unknown` write would ship having
        never executed — which is how this bug class survives: a log line
        nobody has seen fire.
        """
        with self.assertLogs(
            "odoo.addons.goodahead_invoice_import.models.core_profile",
            logging.ERROR,
        ) as logs:
            profile = self.env["account.move"]._ai_record_core_profile(
                probe=probe_for(NEITHER_CORE)
            )
        self.assertEqual(profile, PROFILE_UNKNOWN)
        # The ERROR must name what was expected, not merely that something went
        # wrong — it is the only thing a reader on an unknown nightly gets.
        self.assertIn("_ubl_import_invoice", "".join(logs.output))
        self.assertEqual(
            self.env["ir.config_parameter"].sudo().get_param(CORE_PROFILE_PARAM),
            PROFILE_UNKNOWN,
        )

    def test_a_recognised_core_logs_the_profile_it_chose(self):
        """The control for the test above: a profile that silently flips after a
        core upgrade is the next version of this bug, so the INFO line is as
        load-bearing as the ERROR one.

        🔴 `logging.INFO`, NEVER THE STRING `"INFO"`, AND THIS IS NOT STYLE.
        *(measured)* The legacy framework runs
        `logging.addLevelName(logging.RUNBOT, "INFO")` (`odoo/netsvc.py:325` at
        `ca1f0aea`), which rebinds `logging._nameToLevel["INFO"]` to **25**. The
        search-plan framework fixed it — `:340` there registers RUNBOT under its
        own name. So `assertLogs(..., "INFO")` asks for level 25 on one build
        and level 20 on the other, and on the legacy build every INFO record is
        filtered out of the capture.

        The failure that produced is the worst kind: *"no logs of level INFO or
        higher triggered"*, which reads as "the code did not log" and is really
        "the test asked for the wrong level". The INFO line was in the bench's
        own log file the whole time, three hundred milliseconds earlier. An
        integer means the same thing on both frameworks; a level NAME does not.

        ⚠️ `"WARNING"` and `"ERROR"` happen to be unaffected (30 and 40 on both),
        so the sibling test above passed throughout and this one did not — which
        is exactly how a framework difference hides as a code defect.
        """
        with self.assertLogs(
            "odoo.addons.goodahead_invoice_import.models.core_profile",
            logging.INFO,
        ) as logs:
            recorded = self.env["account.move"]._ai_record_core_profile()
        self.assertIn(recorded, KNOWN_PROFILES)
        self.assertIn(recorded, "".join(logs.output))

    def test_a_good_build_with_no_recorded_profile_still_imports(self):
        """🔴 a review finding — the refusal fired on a GOOD build.

        `_ai_store_core_profile`'s write sits in a bare `except Exception` whose
        own comment names the readonly-cursor case, so "no parameter" is a
        documented state of a build that is entirely recognisable. The refusal
        used to read that parameter, recompute the live profile, throw the live
        answer away and raise anyway — on every `_process_import` and every
        `_ai_decode_ubl_into_move`, with a message naming the OTHER profile's
        unmet expectations, which are facts that are correct about this core.

        🔴 AND NOTHING COULD SEE IT: the same missing parameter skips every
        profile-gated test on both benches, so the suite reports clean while
        100 % of imports are refused.
        """
        params = self.env["ir.config_parameter"].sudo()
        params.search([("key", "=", CORE_PROFILE_PARAM)]).unlink()
        self.assertFalse(
            params.get_param(CORE_PROFILE_PARAM),
            "CONTROL: the parameter really is gone",
        )
        live, _failures = self.env["account.move"]._ai_detect_core_profile()
        self.assertIn(
            live, KNOWN_PROFILES, "CONTROL: this build IS one of the known two"
        )

        self.assertEqual(self.env["account.move"]._ai_assert_core_profile(), live)
        # ...and it repaired the cache on the way through, so support reads what
        # the code acted on rather than nothing.
        self.assertEqual(params.get_param(CORE_PROFILE_PARAM), live)

    def test_a_stale_parameter_does_not_outvote_a_recognisable_registry(self):
        """The same rule from the other side. A hand-edited or restored row
        saying `unknown` must not refuse a build the registry recognises —
        the parameter is a cache of the answer, not the answer."""
        self.env["ir.config_parameter"].sudo().set_param(
            CORE_PROFILE_PARAM, PROFILE_UNKNOWN
        )
        live, _failures = self.env["account.move"]._ai_detect_core_profile()

        self.assertEqual(self.env["account.move"]._ai_assert_core_profile(), live)

    def test_a_recognised_cache_does_not_outvote_an_unrecognisable_registry(self):
        """🔴 A REVIEW FINDING — THE GUARD TRUSTED THE CACHE IT TELLS
        YOU NOT TO TRUST.

        `_ai_assert_core_profile`'s own docstring says, in bold, *"THE REGISTRY
        IS THE AUTHORITY; THE PARAMETER IS A CACHE OF ITS ANSWER … It now
        refuses on the LIVE answer and on nothing else."* That was true of
        REFUSING and false of ACCEPTING: the first three lines of the method
        returned the stored parameter whenever it held a known profile, and the
        live probe was never asked.

        🔴 AND THE MODULE'S OWN TEXT ESTABLISHES THE PREMISE THAT MAKES IT
        REACHABLE. `_ai_store_core_profile:335-343` — *"🔴 THIS WRITE IS ALLOWED
        TO FAIL, WHICH IS WHY NOTHING MAY TREAT THE PARAMETER AS THE ANSWER."*
        So: a deployment takes an Odoo point-upgrade to a build the probe does
        not recognise; `_register_hook` classifies it `unknown` and tries to
        store that; on a worker whose registry build holds a read-only cursor
        the write fails, is logged at WARNING, and the parameter keeps the OLD
        answer. Both call sites then accept it, the import runs on an unmeasured
        core where the vendor hooks are silent no-ops, and core mints a vendor
        from a PDF — the one outcome `core_profile.py:49-52` says there is
        *"deliberately NO degraded mode"* for.

        CONTROL: the parameter is asserted to hold a KNOWN profile at the moment
        of the call, which is the whole premise; and a second call with the
        registry recognisable again must return the live answer rather than
        raise, so a method that had simply started raising could not pass.
        """
        params = self.env["ir.config_parameter"].sudo()
        move = self.env["account.move"]
        live, _failures = move._ai_detect_core_profile()
        self.assertIn(live, KNOWN_PROFILES, "CONTROL: this bench IS recognisable")

        params.set_param(CORE_PROFILE_PARAM, live)
        with unknown_core_probe(move):
            self.assertEqual(
                params.get_param(CORE_PROFILE_PARAM),
                live,
                "CONTROL: the cache still holds yesterday's answer",
            )
            with self.assertRaises(UserError):
                move._ai_assert_core_profile()

        self.assertEqual(
            move._ai_assert_core_profile(),
            live,
            "CONTROL: with the registry recognisable again it answers, and the "
            "refusal is about the LIVE core rather than about raising",
        )

    def test_an_unknown_profile_refuses_at_first_use(self):
        """Not at load. A raise in `_register_hook` bricks `-u all` and every
        worker start on an unrecognised nightly — a worse problem than the one
        being solved (the dual-core design)."""
        move = self.env["account.move"]
        with self._unrecognisable_core():
            # CONTROL: the patched registry really is unrecognisable.
            profile, _failures = move._ai_detect_core_profile()
            self.assertEqual(profile, PROFILE_UNKNOWN)
            with self.assertRaises(UserError):
                move._ai_assert_core_profile()

    def test_the_refusal_names_the_unknown_build_and_not_the_other_profile(self):
        """🔴 a review finding, second half. `detail` is built from the failures
        of EVERY profile, so on a build that matches one of them it printed only
        the other one's — naming correct facts as the reason for a refusal and
        sending a support engineer after a core defect that does not exist. It
        is only ever rendered now when NEITHER profile matched, so both must
        appear."""
        move = self.env["account.move"]
        with self._unrecognisable_core():
            with self.assertRaises(UserError) as caught:
                move._ai_assert_core_profile()
        message = str(caught.exception)
        for profile in KNOWN_PROFILES:
            self.assertIn(
                profile,
                message,
                "a refusal must name what BOTH profiles expected and did not "
                "find, or it blames one core for being the other",
            )

    def test_an_unknown_profile_refuses_before_a_bill_is_built(self):
        move = self.env["account.move"].new({"move_type": "in_invoice"})
        with self._unrecognisable_core():
            with self.assertRaises(UserError):
                self.env["account.move"]._ai_decode_ubl_into_move(
                    move, b"<Invoice/>", {}, attachment=None
                )

    def test_an_unknown_profile_refuses_before_claude_is_called(self):
        """🔴 The refusal must come before the paid call, not after it — AND IT
        MUST LAND ON THE RECORD (found in review).

        The refusal itself is unchanged and absolute: no extraction, no bill, no
        degraded mode. What changed in 19.0.3.21.0 is where it is VISIBLE.
        `_ai_assert_core_profile` raised out of `_process_import` with nothing
        around it, so queue_job rolled the transaction back and the record was
        left in `draft` with an empty `error_message`: a person opening it saw a
        draft import and no reason, and `action_retry` — which requires
        `failed` — could not be used once the core was fixed. The SAME refusal
        raised from `_ai_decode_ubl_into_move` has always been caught by
        `_finalize_import` and recorded through `_fail`, so one refusal had two
        outcomes depending on which seam it fired at.

        🔴 THIS IS NOT THE DEGRADED MODE `core_profile.py:49-52` FORBIDS, and
        the distinction is the whole judgement. A degraded mode would IMPORT on
        an unmeasured core. `_fail` imports nothing: it writes `failed` plus the
        refusal text and returns, which is exactly what every other terminal
        refusal in this method already does (no API key, no PDF, a failed
        extraction). `_ai_assert_core_profile`'s own docstring names the reason
        this is the right place for the check — *"there is a human present, A
        RECORD TO WRITE THE ERROR ON, and a bill that has not yet been
        created"* — and until now nothing on this path wrote it.

        CONTROLS, three, and each pins a way this could be got wrong:
          * Claude is never reached. `_NoClaudeMixin` is not in play in this
            file, so the extractor is patched here explicitly and the patch
            asserts rather than records.
          * The refusal TEXT reaches `error_message`, so "state is failed" could
            not be satisfied by a record that failed for some other reason.
          * The record is left retryable: `action_retry` is called and does not
            raise, which is the user-visible consequence the draft state denied.
        """
        record = self.env["goodahead.invoice.import"].create({"name": "probe-refusal.pdf"})

        def _forbidden(*args, **kwargs):
            raise AssertionError(
                "the refusal must come BEFORE the paid call — the extractor was "
                "reached on an unrecognised core"
            )

        guard = patch.object(
            claude_client.ClaudeInvoiceExtractor, "extract_invoice", _forbidden
        )
        guard.start()
        self.addCleanup(guard.stop)

        with self._unrecognisable_core():
            result = record._process_import()

        self.assertTrue(
            result.startswith("FAILED"),
            "the refusal must be a terminal failure result, got %r" % (result,),
        )
        self.assertEqual(record.state, "failed")
        self.assertIn(
            "cannot run on this Odoo core",
            record.error_message or "",
            "the refusal text itself has to reach the record, or support reads "
            "a failed import with no reason on it",
        )
        # CONTROL: and the record is retryable again once the core is fixed,
        # which a `draft` record with no error was not.
        record.action_retry()
        self.assertEqual(record.state, "waiting")

    def test_a_finished_record_is_never_failed_by_the_core_refusal(self):
        """🔴 THE ORDER OF THE TWO GUARDS IS LOAD-BEARING (found in review).

        Recording the refusal through `_fail` is only safe BEHIND the
        `done`/`move_id` guard. Put the assert first and an unrecognised core
        rewrites the state of a record that already produced a vendor bill —
        turning a finished import into a `failed` one, with a message about a
        core profile, for an import that is over and that this method is about
        to decline to touch anyway. Nothing is refused by refusing it: a
        finished record performs no extraction and creates no bill, so there is
        no paid call and no vendor to invent.

        CONTROL: the same unrecognised core, on a record that is NOT finished,
        does fail it — asserted in
        `test_an_unknown_profile_refuses_before_claude_is_called` — so this test
        cannot pass because the refusal stopped working.
        """
        record = self.env["goodahead.invoice.import"].create({"name": "probe-done.pdf"})
        record.write({"state": "imported", "error_message": False})

        with self._unrecognisable_core():
            result = record._process_import()

        self.assertEqual(result, "OK: already processed")
        self.assertEqual(record.state, "imported")
        self.assertFalse(record.error_message)
