# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Detection of which core import pipeline this module is running on.

Odoo has two UBL vendor-bill import pipelines in three builds: `legacy` (Odoo
18.0 before April 2026 and early 19.0 builds), `search_plan` (newer 19.0
builds) and `search_plan_18` (18.0 from April 2026 on). The model code carries
hooks for all three; on 18.0 `search_plan` is never detected. A plain `hasattr` check is
useless because this module itself defines the methods being probed, and some
core methods exist on both pipelines but are dead stubs on one.

Two things are tested:

1. `core_defines_in_mro` can return False when only our own class defines a
   name.
2. `classify_core_profile` can return all four answers (`search_plan`,
   `search_plan_18`, `legacy`, `unknown`) on fixture cores.

The live-build tests assert the rule (exactly one known profile, recorded and
acted on), not which profile a given build is; that is asserted by the gated
classes in `test_legacy_name_slot`.
"""

import logging
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import BaseCase, TransactionCase, tagged

from odoo.addons.goodahead_invoice_ai_import.lib import claude_client
from odoo.addons.goodahead_invoice_import.models.core_profile import (
    CORE_PROFILE_PARAM,
    KNOWN_PROFILES,
    OUR_MODULE,
    PROFILE_LEGACY,
    PROFILE_SEARCH_PLAN,
    PROFILE_SEARCH_PLAN_18,
    PROFILE_UNKNOWN,
    classify_core_profile,
    core_defines_in_mro,
)

# ---------------------------------------------------------------------------
# Three fixture cores. Each is the set of (model, method) pairs that core — not
# this module — defines on that build.
# ---------------------------------------------------------------------------

# Newer 19.0 build (search-plan pipeline).
SEARCH_PLAN_CORE = {
    ("account.edi.ubl", "_ubl_import_invoice"),
    ("account.edi.ubl", "_import_ubl_retrieve_customer_search_plan"),
    ("account.edi.ubl", "_import_ubl_create_missing_customer"),
    ("res.partner", "_import_retrieve_customer"),
    ("res.partner", "_import_retrieve_customer_from_name"),
    ("account.edi.ubl", "_import_ubl_retrieve_products_search_plan"),
    ("product.product", "_import_retrieve_product_from_supplierinfo"),
    # It also has every name the legacy profile checks, so a profile built
    # only from "is this name present" would match `legacy` too.
    ("account.edi.common", "_import_partner"),
    ("account.edi.common", "_import_invoice_lines"),
    ("account.edi.common", "_import_product"),
    ("res.partner", "_retrieve_partner"),
    ("res.partner", "_retrieve_partner_with_name"),
}

# Odoo 18.0 from April 2026 on: 19.0's partner pipeline, 18.0's product plan
# (built by `account.edi.common`), and no core supplierinfo step.
SEARCH_PLAN_18_CORE = (
    SEARCH_PLAN_CORE
    - {
        ("account.edi.ubl", "_import_ubl_retrieve_products_search_plan"),
        ("product.product", "_import_retrieve_product_from_supplierinfo"),
    }
) | {("account.edi.common", "_import_retrieve_products_search_plan")}

# Odoo 18.0 before April 2026 and early 19.0 builds (legacy pipeline).
LEGACY_CORE = {
    ("account.edi.common", "_import_partner"),
    ("account.edi.common", "_import_invoice_lines"),
    ("account.edi.common", "_import_product"),
    ("res.partner", "_retrieve_partner"),
    ("res.partner", "_retrieve_partner_with_name"),
}

# A 17.0-shaped core: a pipeline this module must refuse rather than guess at.
NEITHER_CORE = {
    ("res.partner", "_retrieve_partner"),
    ("product.product", "_retrieve_product"),
}


def probe_for(core):
    """A `_ai_core_defines`-shaped callable answering from a fixture core."""
    return lambda model_name, method_name: (model_name, method_name) in core


def unknown_core_probe(move):
    """Patch the live probe on `account.move` so detection answers `unknown`.

    The refusal reads the live registry, not the stored parameter, so the
    probe itself is patched.
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
        """A name defined only by this module's class is not core's."""
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
        """The `in_module` argument tells which file defines a method.

        `_inherit` puts `account.edi.common`'s classes into `account.edi.ubl`'s
        MRO, so only the defining module separates the two builds for
        `_import_attachments` (used by `_ai_core_reads_substitute_pdf_param`).
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
        """`in_module` is matched exactly, not as a prefix."""
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
        """A name this module defines never counts as core, even with
        `in_module`."""
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
        """`goodahead_invoice_ai_import_extra` is somebody else's module, not
        ours.
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

    def test_a_legacy_core_is_recognised(self):
        profile, _failures = classify_core_profile(probe_for(LEGACY_CORE))
        self.assertEqual(profile, PROFILE_LEGACY)

    def test_an_18_search_plan_core_is_recognised(self):
        """The 18.0 build from April 2026 on is its own profile, not
        `unknown`."""
        profile, failures = classify_core_profile(probe_for(SEARCH_PLAN_18_CORE))
        self.assertEqual(profile, PROFILE_SEARCH_PLAN_18, failures)

    def test_an_18_search_plan_core_is_neither_of_the_others(self):
        """It has the search-plan entry point, so it is not legacy; it lacks
        core's supplierinfo step, so it is not 19.0's search plan."""
        _profile, failures = classify_core_profile(probe_for(SEARCH_PLAN_18_CORE))
        self.assertTrue(failures[PROFILE_LEGACY])
        self.assertIn(
            "product.product._import_retrieve_product_from_supplierinfo "
            "(expected present)",
            failures[PROFILE_SEARCH_PLAN],
        )

    def test_a_19_search_plan_core_is_not_the_18_one(self):
        _profile, failures = classify_core_profile(probe_for(SEARCH_PLAN_CORE))
        self.assertIn(
            "product.product._import_retrieve_product_from_supplierinfo "
            "(expected absent)",
            failures[PROFILE_SEARCH_PLAN_18],
        )

    def test_an_18_search_plan_core_without_its_product_hook_is_unknown(self):
        """A build whose product plan this module cannot hook is refused
        rather than imported with the vendor-code guard silently off."""
        core = SEARCH_PLAN_18_CORE - {
            ("account.edi.common", "_import_retrieve_products_search_plan")
        }
        profile, _failures = classify_core_profile(probe_for(core))
        self.assertEqual(profile, PROFILE_UNKNOWN)

    def test_an_unrecognised_core_is_unknown(self):
        """An unrecognised core is `unknown`, with failures listed per
        profile."""
        profile, failures = classify_core_profile(probe_for(NEITHER_CORE))
        self.assertEqual(profile, PROFILE_UNKNOWN)
        # The failures name what was expected, for the ERROR log.
        for known in KNOWN_PROFILES:
            self.assertTrue(failures[known])

    def test_a_search_plan_core_is_not_also_legacy(self):
        """`legacy` requires the search-plan entry point to be absent, so a
        search-plan core does not also match it."""
        _profile, failures = classify_core_profile(probe_for(SEARCH_PLAN_CORE))
        self.assertTrue(failures[PROFILE_LEGACY])

    def test_removing_only_the_discriminator_stops_search_plan_matching(self):
        """Without `_ubl_import_invoice` the core is no longer search-plan."""
        core = SEARCH_PLAN_CORE - {("account.edi.ubl", "_ubl_import_invoice")}
        profile, _failures = classify_core_profile(probe_for(core))
        self.assertNotEqual(profile, PROFILE_SEARCH_PLAN)

    def test_two_matching_profiles_is_unknown(self):
        """A core matching two profiles is `unknown`."""
        always = (("res.partner", "_retrieve_partner", True),)
        profile, _failures = classify_core_profile(
            probe_for(LEGACY_CORE), markers={"a": always, "b": always}
        )
        self.assertEqual(profile, PROFILE_UNKNOWN)



@tagged("post_install", "-at_install")
class TestCoreProfileOnThisBuild(TransactionCase):
    """Against the real registry, on either build, without naming a profile."""

    def _unrecognisable_core(self):
        """Clear the cached parameter and make the live registry
        unrecognisable; the refusal reads both."""
        self.env["ir.config_parameter"].sudo().search(
            [("key", "=", CORE_PROFILE_PARAM)]
        ).unlink()
        return unknown_core_probe(self.env["account.move"])

    def test_the_naive_probe_cannot_fail_here(self):
        """`hasattr` is true here because this module defines the method, which
        is why it cannot serve as the probe."""
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
        """The walk sees core's `_retrieve_partner_with_name` beneath this
        module's override; core defines it on both builds."""
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
        """The live registry matches exactly one known profile, and the other
        profile fails at least one marker.
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
        """The stored parameter equals a live re-detection."""
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
        """An unrecognised core logs an ERROR naming the expectations and
        records `unknown`."""
        with self.assertLogs(
            "odoo.addons.goodahead_invoice_import.models.core_profile",
            logging.ERROR,
        ) as logs:
            profile = self.env["account.move"]._ai_record_core_profile(
                probe=probe_for(NEITHER_CORE)
            )
        self.assertEqual(profile, PROFILE_UNKNOWN)
        self.assertIn("_ubl_import_invoice", "".join(logs.output))
        self.assertEqual(
            self.env["ir.config_parameter"].sudo().get_param(CORE_PROFILE_PARAM),
            PROFILE_UNKNOWN,
        )

    def test_a_recognised_core_logs_the_profile_it_chose(self):
        """A recognised core logs the chosen profile at INFO.

        Use `logging.INFO`, not the string "INFO": Odoo registers level 25
        (RUNBOT) under the name "INFO", so `assertLogs(..., "INFO")` could
        filter out real INFO records.
        """
        with self.assertLogs(
            "odoo.addons.goodahead_invoice_import.models.core_profile",
            logging.INFO,
        ) as logs:
            recorded = self.env["account.move"]._ai_record_core_profile()
        self.assertIn(recorded, KNOWN_PROFILES)
        self.assertIn(recorded, "".join(logs.output))

    def test_a_good_build_with_no_recorded_profile_still_imports(self):
        """A missing parameter does not refuse a recognisable build.

        The parameter write may fail (e.g. on a read-only cursor), so the
        refusal relies on the live detection and repairs the cache.
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
        # The cache is repaired on the way through.
        self.assertEqual(params.get_param(CORE_PROFILE_PARAM), live)

    def test_a_stale_parameter_does_not_outvote_a_recognisable_registry(self):
        """A stored `unknown` does not refuse a build the registry recognises;
        the parameter is only a cache."""
        self.env["ir.config_parameter"].sudo().set_param(
            CORE_PROFILE_PARAM, PROFILE_UNKNOWN
        )
        live, _failures = self.env["account.move"]._ai_detect_core_profile()

        self.assertEqual(self.env["account.move"]._ai_assert_core_profile(), live)

    def test_a_recognised_cache_does_not_outvote_an_unrecognisable_registry(self):
        """A stored known profile does not let an unrecognisable registry pass.

        After a core upgrade the parameter write may fail and keep the old
        answer; the guard must still refuse on the live answer. With the
        registry recognisable again, it answers normally.
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
        """An unknown profile refuses at first use, not at load, so module
        updates and worker starts still work."""
        move = self.env["account.move"]
        with self._unrecognisable_core():
            # Control: the patched registry really is unrecognisable.
            profile, _failures = move._ai_detect_core_profile()
            self.assertEqual(profile, PROFILE_UNKNOWN)
            with self.assertRaises(UserError):
                move._ai_assert_core_profile()

    def test_the_refusal_names_the_unknown_build_and_not_the_other_profile(self):
        """The refusal message names the expectations of both profiles."""
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
        """On an unknown core the import fails before the paid call, and the
        failure is recorded on the import record.

        `_fail` imports nothing; it marks the record `failed` with the refusal
        text so it can be retried once the core is fixed.
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
        # The record can be retried.
        record.action_retry()
        self.assertEqual(record.state, "waiting")

    def test_a_finished_record_is_never_failed_by_the_core_refusal(self):
        """A finished import is not marked failed by the core refusal; the
        `done` guard runs before the profile check.
        """
        record = self.env["goodahead.invoice.import"].create({"name": "probe-done.pdf"})
        record.write({"state": "imported", "error_message": False})

        with self._unrecognisable_core():
            result = record._process_import()

        self.assertEqual(result, "OK: already processed")
        self.assertEqual(record.state, "imported")
        self.assertFalse(record.error_message)
