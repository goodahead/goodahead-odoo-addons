# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The domains that identify nobody, and the one way the list may be wrong.

🆕 19.0.3.16.0 SPLIT THE FLOOR IN TWO AND ADDED A SUFFIX RULE. `_PUBLIC_EMAIL_
DOMAINS` is free providers; `_NEVER_IDENTIFYING_DOMAINS` is import placeholders
and near-misses of a provider; `_PUBLIC_EMAIL_SUFFIXES` covers the consumer
mailboxes under one ISP's regional subdomains. `TestDomainsThatIdentifyNobody`
below holds the measurements that forced each of them.

🔴 WHAT THIS LIST DOES, AND WHY GETTING IT WRONG IS SILENT. `_normalise_domain`
answers None for a host on this list, so no identifier row is ever stored for it
and no printed address is ever looked up against it. Take `gmail.com` off and
the module does not break: it starts, slowly, storing a `gmail.com` row for
every partner whose email happens to be there, and the moment exactly ONE of
them is bill-eligible, every gmail invoice in the inbox is filed against that
one vendor with confidence. In any large contact book `gmail.com` is carried by
a great many distinct commercial partners — so the failure is not
hypothetical, it is only waiting for the list to be short enough.

🔴 THE DESIGN RULE EVERY TEST BELOW EXISTS TO PIN: the shipped constant is a
FLOOR, not a default value. The system parameter ADDS to it, and may subtract
from it only when an operator writes an explicit `-` in front of a domain. An
operator who empties the field gets the shipped list back, not an empty one.
the dual-core design asked for a parameter "seeded from the constant"; that is a different
design and it fails in two directions at once — see the long note in
`models/partner_identifier.py`.
"""

import logging

from lxml import etree

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_import.models.partner_identifier import (
    PUBLIC_EMAIL_DOMAINS_PARAM,
    _NEVER_IDENTIFYING_DOMAINS,
    _PUBLIC_EMAIL_DOMAINS,
)

#: What a database with nothing configured excludes by exact host. Both floors,
#: because 19.0.3.16.0 split them: `_PUBLIC_EMAIL_DOMAINS` is free providers and
#: `_NEVER_IDENTIFYING_DOMAINS` is placeholders and near-misses. The suffix rule
#: is deliberately NOT in here — it is not a set of hosts, and
#: `TestDomainsThatIdentifyNobody` asserts it on its own.
SHIPPED_FLOOR = _PUBLIC_EMAIL_DOMAINS | _NEVER_IDENTIFYING_DOMAINS


@tagged("post_install", "-at_install")
class TestPublicEmailDomains(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Identifier = cls.env["goodahead.partner.identifier"]
        cls.Params = cls.env["ir.config_parameter"].sudo()

    def setUp(self):
        super().setUp()
        # 🔴 THE ORMCACHE DOES NOT ROLL BACK AND THE TRANSACTION DOES. The
        # effective list is cached on the REGISTRY under the `stable` key;
        # `ir.config_parameter.write` clears it, but a test's own rollback does
        # not put the old value back into the cache. Without this cleanup a test
        # that sets the parameter leaves its list in place for every test that
        # runs after it, in this file and in every other — which would show up
        # as an unrelated matching test failing by order of execution.
        self.addCleanup(self.env.registry.clear_cache, "stable")
        self.env.registry.clear_cache("stable")

    def _set(self, value):
        self.Params.set_param(PUBLIC_EMAIL_DOMAINS_PARAM, value)

    # ------------------------------------------------------------------
    # The floor
    # ------------------------------------------------------------------

    def test_with_nothing_configured_the_shipped_list_is_in_force(self):
        """🔴 AND IT ASSERTS SOMETHING THE CONSTANT DOES NOT SUPPLY (found in
        review). `SHIPPED_FLOOR` is `_PUBLIC_EMAIL_DOMAINS | _NEVER_IDENTIFYING_
        DOMAINS`, so the equality below reduces to `floor == floor` and cannot
        fail for any edit to the shipped list — deleting `windstream.net` or
        `roadrunner.com` left it green. The named sentinels are written out HERE,
        by hand, so a removal fails a test instead of passing one. They are
        deliberately the two the review named plus one of each kind, not the
        whole list: a copy of 106 entries is a second place to maintain, and the
        failure being guarded is a DELETION, which one member catches.
        """
        self.assertFalse(self.Params.get_param(PUBLIC_EMAIL_DOMAINS_PARAM))
        self.assertEqual(self.Identifier._non_identifying_domains(), SHIPPED_FLOOR)
        for host in (
            "gmail.com",          # the free provider the feature started with
            "windstream.net",     # a US ISP — a review finding names this one
            "roadrunner.com",     # and this one
            "emaildomain.com",    # an import placeholder on many unrelated partners
            "example.com",        # the RFC 2606 reserved documentation name
        ):
            self.assertIn(
                host,
                self.Identifier._non_identifying_domains(),
                "%s must be excluded with nothing configured" % host,
            )
            self.assertIsNone(
                self.Identifier._normalise_domain("ap@" + host),
                "%s must not normalise to an identifier" % host,
            )
        self.assertIsNone(self.Identifier._normalise_domain("zzexamplevendor@gmail.com"))
        # CONTROL: a host that is NOT on the floor still normalises, so an
        # exclusion that had swallowed everything could not pass.
        self.assertEqual(
            self.Identifier._normalise_domain("ap@zzrealvendorhost.com"),
            "zzrealvendorhost.com",
        )

    def test_clearing_the_setting_does_not_turn_the_exclusion_off(self):
        """🔴 THE ONE FAILURE THE HELP TEXT WARNS ABOUT, MADE UNREACHABLE.

        An operator opens the setting, decides the list is not theirs, selects
        it all and deletes it. Under "the parameter IS the list" that empties
        the exclusion and every free provider becomes an identifier. Here it
        restores the shipped list, which is the only answer that cannot be a
        catastrophe.

        All three spellings of "empty" are asserted, because `res.config.settings`
        turns a blank Char into `False`, which `set_param` turns into an
        `unlink`, and a parser that handled only one of them would be correct in
        the UI and wrong against a hand-edited parameter, or the reverse.
        """
        for emptied in ("", "   ", "\n \n"):
            self._set(emptied)
            self.env.registry.clear_cache("stable")
            self.assertEqual(
                self.Identifier._non_identifying_domains(),
                SHIPPED_FLOOR,
                "clearing the setting emptied the exclusion list (%r)" % emptied,
            )
            self.assertIsNone(self.Identifier._normalise_domain("x@gmail.com"))

    def test_a_bare_list_ADDS_and_never_replaces(self):
        """The control for the test above: a value that LOOKS like a complete
        replacement list still leaves the shipped one standing. If the setting
        were a replacement, `gmail.com` would normalise here."""
        self._set("zz-provider.example")
        self.assertIsNone(self.Identifier._normalise_domain("a@zz-provider.example"))
        self.assertIsNone(self.Identifier._normalise_domain("a@gmail.com"))
        self.assertEqual(
            self.Identifier._normalise_domain("a@zzvendor.example"),
            "zzvendor.example",
            "the control: a domain nobody listed must still be an identifier",
        )

    # ------------------------------------------------------------------
    # Subtraction, which must be deliberate and must say so
    # ------------------------------------------------------------------

    def test_removing_a_shipped_domain_needs_an_explicit_minus(self):
        self._set("gmail.com")
        self.assertIsNone(
            self.Identifier._normalise_domain("a@gmail.com"),
            "naming a shipped domain without a '-' must not remove it",
        )
        self._set("-gmail.com")
        self.assertEqual(self.Identifier._normalise_domain("a@gmail.com"), "gmail.com")

    def test_removing_a_shipped_domain_is_logged_as_a_warning(self):
        """A subtraction is the one edit that can turn a vendor book into a
        single wrong vendor, so it is never silent. `assertLogs` takes an
        INTEGER level."""
        with self.assertLogs(
            "odoo.addons.goodahead_invoice_import.models.partner_identifier",
            level=logging.WARNING,
        ) as captured:
            self._set("-yahoo.com")
            self.Identifier._non_identifying_domains()
        self.assertTrue(
            any("yahoo.com" in line for line in captured.output),
            "the removal was not named in the log: %r" % captured.output,
        )

    def test_subtracting_something_that_was_never_shipped_changes_nothing(self):
        self._set("-zzvendor.example")
        self.assertEqual(self.Identifier._non_identifying_domains(), SHIPPED_FLOOR)
        self.assertEqual(
            self.Identifier._normalise_domain("a@zzvendor.example"), "zzvendor.example"
        )

    # ------------------------------------------------------------------
    # Junk
    # ------------------------------------------------------------------

    def test_junk_is_logged_and_dropped_and_never_empties_the_list(self):
        """🔴 THE SHAPE THIS PROJECT KEEPS GETTING BITTEN BY: a parse failure
        that fails in the direction that looks like success. An unreadable
        setting must cost the entries nobody could read and nothing else."""
        with self.assertLogs(
            "odoo.addons.goodahead_invoice_import.models.partner_identifier",
            level=logging.WARNING,
        ) as captured:
            self._set("not_a_domain!, zz-nordic.example, @@@")
            effective = self.Identifier._non_identifying_domains()
        self.assertTrue(_PUBLIC_EMAIL_DOMAINS.issubset(effective))
        self.assertTrue(
            any("not_a_domain!" in line for line in captured.output),
            "the unreadable entry was dropped without saying so: %r"
            % captured.output,
        )
        self.assertIsNone(
            self.Identifier._normalise_domain("a@zz-nordic.example"),
            "the readable entry beside the junk was lost too",
        )

    def test_comments_newlines_and_semicolons_all_separate_entries(self):
        self._set(
            "# the providers our Finnish vendors use\n"
            "zz-one.example, zz-two.example; zz-three.example\n"
            "zz-four.example   # trailing comment\n"
        )
        for host in ("zz-one", "zz-two", "zz-three", "zz-four"):
            self.assertIsNone(
                self.Identifier._normalise_domain("a@%s.example" % host),
                "%s.example was not read out of the setting" % host,
            )

    def test_the_setting_is_re_read_when_it_changes(self):
        """The effective list is cached on the registry. Writing the parameter
        has to invalidate that cache or the setting is a one-shot that only
        takes effect after a restart — which is how a settings field becomes a
        thing nobody trusts."""
        self.assertEqual(
            self.Identifier._normalise_domain("a@zz-late.example"), "zz-late.example"
        )
        self._set("zz-late.example")
        self.assertIsNone(self.Identifier._normalise_domain("a@zz-late.example"))
        self._set("")
        self.assertEqual(
            self.Identifier._normalise_domain("a@zz-late.example"), "zz-late.example"
        )

    # ------------------------------------------------------------------
    # The shipped Nordic providers
    # ------------------------------------------------------------------

    def test_the_nordic_providers_ship_in_the_constant_not_in_a_setting(self):
        """A Nordic deployment must not have to configure anything to get its own locale's free
        providers excluded — the module is meant to run UNCHANGED on both
        deployments (the dual-core design). So these are in the shipped floor, and this test
        reads the floor rather than the effective list, so that a Nordic domain
        quietly moved into a data file would fail here."""
        for host in (
            "suomi24.fi",
            "elisanet.fi",
            "saunalahti.fi",
            "luukku.com",
            "hotmail.fi",
            "spray.se",
            "home.se",
            "online.no",
            "live.no",
            "mail.dk",
            "sol.dk",
            "simnet.is",
        ):
            self.assertIn(host, _PUBLIC_EMAIL_DOMAINS, "%s is not shipped" % host)
            self.assertIsNone(self.Identifier._normalise_domain("myynti@%s" % host))

    def test_a_real_nordic_vendor_domain_is_still_an_identifier(self):
        """The control for the row above, and it is the one that matters: the
        additions must not have swallowed ordinary company domains in those
        countries.

        🔴 THE HOSTS HAVE THE SHAPE OF A COMPANY'S OWN DOMAIN, AND THAT IS THE
        WHOLE POINT OF THE TEST (found in review). Every host below is shaped
        like the domain of a Nordic company in or near this module's own trade
        — foodservice equipment, professional appliances, industrial controls,
        grocery wholesale, food processing machinery, one per Nordic country
        code plus a `.com` — i.e. exactly the shape of domain a vendor bill on
        a Nordic ledger prints. The companies are invented; the shape is not.
        """
        for host in (
            "zz-keittiotekniikka.fi",
            "zz-appliancepro.com",
            "zz-styringsteknik.dk",
            "zz-dagligvaregrossist.no",
            "zz-matvinnsluvelar.is",
        ):
            self.assertNotIn(
                host,
                SHIPPED_FLOOR,
                "%s — a real company's own domain — is on the shipped list" % host,
            )
            self.assertEqual(
                self.Identifier._normalise_domain("sales@%s" % host),
                host,
                "%s stopped being an identifier" % host,
            )

    def test_the_domains_measured_as_not_live_were_NOT_added(self):
        """🔴 THE ONES A PLAUSIBLE LIST WOULD HAVE CONTAINED AND THIS ONE DOES
        NOT, because they were measured rather than assumed (see the provenance
        note on `_PUBLIC_EMAIL_DOMAINS`). `outlook.fi` and `outlook.no` publish
        no MX record at all; `outlook.se` publishes one that is NOT Microsoft's,
        so it is somebody's own domain; `comhem.se` and `tele2.se` now answer on
        a corporate Microsoft 365 tenant, i.e. they are company domains and not
        consumer mail any more. Excluding any of them would make a real
        company's domain unmatchable for ever."""
        for host in ("outlook.fi", "outlook.no", "outlook.se", "comhem.se", "tele2.se"):
            self.assertNotIn(host, _PUBLIC_EMAIL_DOMAINS)
            self.assertEqual(self.Identifier._normalise_domain("a@%s" % host), host)


@tagged("post_install", "-at_install")
class TestDomainsThatIdentifyNobody(TransactionCase):
    """🔴 THE SECOND FLOOR, AND WHY ONE FLOOR WAS THE WRONG SHAPE (found in
    review).

    The first floor is called "free email providers" and is extended by a rule —
    *named by `willwhite/freemail` AND carried by 5+ partners*. That rule is
    structurally blind to an import placeholder, because `freemail` will never
    name one.

    The property that actually makes a host unsafe is not *"is it a free
    provider"*. It is **"is it carried by many unrelated parties and by exactly
    one party a bill may be filed against"** — because that is the arithmetic
    `_commercial_partner_for` does. These tests pin the shapes of host that
    property catches, judged by the module's own eligibility rule:

    | host | carriers | bill-eligible | decides |
    |---|---|---|---|
    | `emaildomain.com` | many unrelated partners | one | 🔴 yes |
    | `gamil.com` | several unrelated partners | none | one `supplier_rank` away |
    | `gmai.com` | several unrelated partners | none | one `supplier_rank` away |
    | `gmail.con` | several unrelated partners | none | one `supplier_rank` away |
    | a regional `rr.com` host | several partners | one | 🔴 yes |

    An import placeholder such as `emaildomain.com` appears as many distinct
    printed addresses of the shape `<digits>@emaildomain.com`, spread over many
    unrelated partners.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Identifier = cls.env["goodahead.partner.identifier"]

    def setUp(self):
        super().setUp()
        self.addCleanup(self.env.registry.clear_cache, "stable")
        self.env.registry.clear_cache("stable")

    def test_an_import_placeholder_identifies_nobody(self):
        """🔴 Without this floor `1000000001@emaildomain.com` normalises to
        `emaildomain.com`, which can resolve to exactly one bill-eligible
        partner, so every bill printing any of those addresses is filed
        against that one partner as a DECISION, with no candidate card."""
        self.assertIsNone(
            self.Identifier._normalise_domain("1000000001@emaildomain.com")
        )
        self.assertIsNone(self.Identifier._normalise_domain("emaildomain.com"))

    def test_a_near_miss_of_a_shipped_provider_identifies_nobody(self):
        """They decline today, but not for the reason the changelog gave: not
        because they are ambiguous (≥2 carriers) but because **nobody
        bill-eligible carries them at all** (0). One `supplier_rank` increment
        on any of those partners flips each of them to deciding, so the
        reason is one bill away from being false."""
        for host in ("gamil.com", "gmai.com", "gmail.con"):
            self.assertIsNone(
                self.Identifier._normalise_domain("purchasing@%s" % host),
                "%s is still an identifier" % host,
            )

    def test_a_reserved_example_domain_identifies_nobody(self):
        """RFC 2606 §3 reserves these for documentation; they can never be a
        company's own domain, and they are what a half-filled import writes."""
        for host in ("example.com", "example.net", "example.org"):
            self.assertIsNone(
                self.Identifier._normalise_domain("ap@%s" % host),
                "%s is still an identifier" % host,
            )

    def test_a_host_that_merely_LOOKS_like_a_placeholder_still_identifies(self):
        """🔴 THE CONTROL, and without it every test above passes on a module
        whose domain matching is simply broken. One character away from each
        shipped entry, and each of these must still be an identifier."""
        for host in (
            "emaildomains.com",
            "emaildomain.co",
            "gmail.com.vendor.example",
            "examples.com",
        ):
            self.assertEqual(
                self.Identifier._normalise_domain("sales@%s" % host),
                host,
                "%s stopped being an identifier" % host,
            )

    # ------------------------------------------------------------------
    # The subdomain asymmetry — a review finding
    # ------------------------------------------------------------------

    def test_a_regional_host_of_a_consumer_isp_identifies_nobody(self):
        """🔴 EXCLUDING A DOMAIN DOES NOT EXCLUDE ITS SUBDOMAINS, because
        `_host_of` keeps the whole host on purpose. So `roadrunner.com` being on
        the list said nothing about `nc.rr.com`, which is a consumer mailbox at
        the same ISP and which can decide a partner when exactly one of its
        carriers is bill-eligible."""
        for host in ("nc.rr.com", "nycap.rr.com", "tampabay.rr.com", "rr.com"):
            self.assertIsNone(
                self.Identifier._normalise_domain("owner@%s" % host),
                "%s is still an identifier" % host,
            )

    def test_the_suffix_rule_matches_a_LABEL_and_not_a_substring(self):
        """The control for the row above. A suffix rule written as `endswith`
        over characters rather than over labels would swallow `bigrr.com` and
        `rr.com.vendor.example`, i.e. would make real companies permanently
        unmatchable — the error `comhem.se` and `tele2.se` were kept off the
        list to avoid."""
        for host in ("bigrr.com", "rr.com.vendor.example", "notrr.com", "rrr.com"):
            self.assertEqual(
                self.Identifier._normalise_domain("sales@%s" % host),
                host,
                "%s stopped being an identifier" % host,
            )

    def test_every_shipped_rule_is_still_subtractable_by_an_operator(self):
        """The escape hatch has to be complete, or a shipped exclusion that
        turns out to be wrong for one database cannot be undone there. A suffix
        is subtracted by naming the suffix."""
        self.Params = self.env["ir.config_parameter"].sudo()
        self.Params.set_param(PUBLIC_EMAIL_DOMAINS_PARAM, "-emaildomain.com, -rr.com")
        self.env.registry.clear_cache("stable")
        self.assertEqual(
            self.Identifier._normalise_domain("1000000001@emaildomain.com"),
            "emaildomain.com",
        )
        self.assertEqual(
            self.Identifier._normalise_domain("zz-owner@nc.rr.com"), "nc.rr.com"
        )

    def test_subtracting_a_shipped_rule_is_logged_by_name(self):
        """The whole point of the WARNING is that it fires for the entries that
        BITE. A subtraction intersected only with the free-provider floor would
        log *"it was never on the list; the entry does nothing"* while silently
        doing something."""
        with self.assertLogs(
            "odoo.addons.goodahead_invoice_import.models.partner_identifier",
            level=logging.WARNING,
        ) as captured:
            self.env["ir.config_parameter"].sudo().set_param(
                PUBLIC_EMAIL_DOMAINS_PARAM, "-emaildomain.com"
            )
            self.Identifier._normalise_domain("1@emaildomain.com")
        self.assertTrue(
            any(
                "emaildomain.com" in line and "REMOVED" in line
                for line in captured.output
            ),
            "the removal was logged as a no-op, or not at all: %r" % captured.output,
        )


@tagged("post_install", "-at_install")
class TestRowsSeededUnderTheOldList(TransactionCase):
    """🔴 WHAT HAPPENS TO THE ROWS THAT WERE STORED BEFORE A DOMAIN JOINED THE
    LIST — the dual-core design, answered as a RULE rather than as a count.

    The exclusion is applied in `_normalise_domain`, which is the ONE function
    every write and every lookup passes through. So a row stored under the old
    list is not reachable: nothing can produce its value as a lookup key any
    more. No migration, and that is a property of where the check sits, not a
    hopeful statement about data.

    The test writes the stale row through `create()` directly, deliberately
    bypassing `_derive_from_contact`, because that is exactly the state an
    upgraded database is in.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Identifier = cls.env["goodahead.partner.identifier"]
        cls.partner = cls.env["res.partner"].create(
            {
                "name": "ZZ Suomi Vendor Oy",
                "is_company": True,
                "supplier_rank": 3,
            }
        )
        cls.Identifier.create(
            {
                "partner_id": cls.partner.id,
                "kind": "domain",
                "value": "suomi24.fi",
                "source": "seeded before suomi24.fi joined the list",
            }
        )

    def test_the_stale_row_is_still_there(self):
        """The control. If this fails the test below proves nothing, because it
        would be asserting that a row which does not exist cannot match."""
        self.assertTrue(
            self.Identifier.search(
                [("kind", "=", "domain"), ("value", "=", "suomi24.fi")]
            )
        )

    def test_a_stale_row_for_a_newly_excluded_domain_can_no_longer_decide(self):
        self.assertFalse(
            self.env["res.partner"]._ai_vendor_by_domains(["zz-laskutus@suomi24.fi"])
        )

    def test_and_it_can_no_longer_be_offered_as_a_candidate_either(self):
        """`_partners_for` reads the stored table directly, so this is a second,
        independent path to the same row — and it is normalised by its callers
        for the same reason."""
        candidates = self.env["account.move"]._ai_vendor_candidates(
            {"name": "ZZ Nothing Like It", "email": "zz-laskutus@suomi24.fi"}
        )
        self.assertNotIn(self.partner, [c["partner"] for c in candidates])

    def test_a_row_whose_domain_is_NOT_excluded_still_decides(self):
        """The control for both of the above: the same shape of stale row, on a
        domain nobody excluded, must still work — otherwise the two tests above
        would pass on a module whose domain matching was simply broken."""
        other = self.env["res.partner"].create(
            {"name": "ZZ Live Vendor Oy", "is_company": True, "supplier_rank": 3}
        )
        self.Identifier.create(
            {
                "partner_id": other.id,
                "kind": "domain",
                "value": "zzlivevendor.example",
                "source": "test",
            }
        )
        self.assertEqual(
            self.env["res.partner"]._ai_vendor_by_domains(
                ["laskutus@zzlivevendor.example"]
            ),
            other,
        )


@tagged("post_install", "-at_install")
class TestTheSettingIsReachableFromTheUI(TransactionCase):
    """A rule that only a developer can change is not a setting. This asserts
    the round trip through `res.config.settings` — not the wording of any
    label, which would pin a spelling rather than a rule."""

    def setUp(self):
        super().setUp()
        self.addCleanup(self.env.registry.clear_cache, "stable")

    def test_saving_the_settings_form_changes_what_the_matcher_answers(self):
        Identifier = self.env["goodahead.partner.identifier"]
        self.assertEqual(
            Identifier._normalise_domain("a@zz-ui.example"), "zz-ui.example"
        )
        settings = self.env["res.config.settings"].create(
            {"ai_public_email_domains": "zz-ui.example"}
        )
        settings.execute()
        self.assertIsNone(Identifier._normalise_domain("a@zz-ui.example"))

    def test_the_field_carries_a_help_text_that_names_the_consequence(self):
        """§8 asks for the warning to live on the field rather than only in a
        changelog, and a field with `help=False` would satisfy every other test
        in this file.

        🔴 IT USED TO ASSERT `assertIn("-", field.help)`, WHICH PASSES FOR ANY
        HYPHENATED ENGLISH (found in review) — including the retracted
        wording it was meant to pin. What is asserted now is the RULE, in three
        parts a reader can check: the syntax (`-gmail.com`), the verb
        (`REMOVES`), and the consequence that correction 2 established — the
        danger is **exactly one** bill-eligible carrier, not many.
        """
        field = self.env["res.config.settings"]._fields["ai_public_email_domains"]
        self.assertTrue(field.help)
        for phrase in ("-gmail.com", "REMOVES", "exactly one"):
            self.assertIn(
                phrase,
                field.help,
                "the field help no longer names %r" % phrase,
            )

    def test_the_ALWAYS_VISIBLE_setting_text_carries_the_same_warning(self):
        """🔴 THE CORRECTION LANDED WHERE YOU HAVE TO HOVER (found in
        review).

        Odoo renders `<setting help="…">` as grey text under the label, on
        screen at all times; `fields.Char(help=…)` is the TOOLTIP. The commit's
        own framing is *"the warning on this setting is the setting"*, so the
        sentence a reader sees without hovering is the one that has to be right.
        It said a removal *"makes every contact on it matchable by domain"* —
        the wording correction 2 exists to retract, and the one that makes a
        removal sound survivable.
        """
        view = self.env.ref(
            "goodahead_invoice_import.res_config_settings_view_form"
        )
        arch = etree.fromstring(view.arch_db)
        settings = arch.xpath(
            "//setting[.//field[@name='ai_public_email_domains']]"
        )
        self.assertEqual(
            len(settings), 1, "the provider setting is not in the settings view"
        )
        help_text = settings[0].get("help") or ""
        self.assertIn("exactly one", help_text)
        self.assertNotIn("every contact on it matchable", help_text)
