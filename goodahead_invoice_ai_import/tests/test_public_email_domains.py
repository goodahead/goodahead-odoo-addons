# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Email domains that identify nobody.

`_PUBLIC_EMAIL_DOMAINS` holds free providers, `_NEVER_IDENTIFYING_DOMAINS` import
placeholders and near-misses of a provider, and `_PUBLIC_EMAIL_SUFFIXES` consumer
mailboxes under an ISP's regional subdomains. `_normalise_domain` answers None for
these, so they are never stored or looked up: otherwise a single bill-eligible
partner on `gmail.com` would receive every gmail invoice.

The shipped lists are a floor. The system parameter adds to them and removes an
entry only when it is prefixed with `-`; an emptied parameter restores the floor.
"""

import logging

from lxml import etree

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_ai_import.models.partner_identifier import (
    PUBLIC_EMAIL_DOMAINS_PARAM,
    _NEVER_IDENTIFYING_DOMAINS,
    _PUBLIC_EMAIL_DOMAINS,
)

#: What a database with nothing configured excludes by exact host. The suffix
#: rule is not a set of hosts and is asserted separately.
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
        # The effective list is cached on the registry ("stable") and the cache is
        # not rolled back with the test transaction, so clear it on both ends.
        self.addCleanup(self.env.registry.clear_cache, "stable")
        self.env.registry.clear_cache("stable")

    def _set(self, value):
        self.Params.set_param(PUBLIC_EMAIL_DOMAINS_PARAM, value)

    # ------------------------------------------------------------------
    # The floor
    # ------------------------------------------------------------------

    def test_with_nothing_configured_the_shipped_list_is_in_force(self):
        """With nothing configured the shipped floor is in force.

        A few sentinels are named by hand so that deleting one from the shipped
        list fails here rather than passing a `floor == floor` comparison.
        """
        self.assertFalse(self.Params.get_param(PUBLIC_EMAIL_DOMAINS_PARAM))
        self.assertEqual(self.Identifier._non_identifying_domains(), SHIPPED_FLOOR)
        for host in (
            "gmail.com",          # a free provider
            "windstream.net",     # a US ISP
            "roadrunner.com",     # another US ISP
            "emaildomain.com",    # an import placeholder
            "example.com",        # a reserved name
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
        self.assertIsNone(self.Identifier._normalise_domain("zzvendorparts@gmail.com"))
        # Control: a host not on the floor still normalises.
        self.assertEqual(
            self.Identifier._normalise_domain("ap@zzrealvendorhost.com"),
            "zzrealvendorhost.com",
        )

    def test_clearing_the_setting_does_not_turn_the_exclusion_off(self):
        """Emptying the setting restores the shipped list instead of excluding nothing.

        Several spellings of "empty" are checked, since the settings form and a
        hand-edited parameter produce different ones.
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
        """A configured list adds to the shipped one and never replaces it."""
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
        """A removal from the shipped list is always logged as a warning."""
        with self.assertLogs(
            "odoo.addons.goodahead_invoice_ai_import.models.partner_identifier",
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
        """Unreadable entries are logged and dropped; the rest of the list survives."""
        with self.assertLogs(
            "odoo.addons.goodahead_invoice_ai_import.models.partner_identifier",
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
        """Writing the parameter invalidates the cached effective list."""
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
        """Nordic free providers are excluded without any configuration.

        The test reads the shipped constant, not the effective list.
        """
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

    def test_a_nordic_vendor_domain_is_still_an_identifier(self):
        """Control: ordinary Nordic company domains are still identifiers.

        The hosts are invented (no real company is named), but they sit on the
        same country-code TLDs as the providers above, so a rule that dropped a
        whole Nordic TLD would fail here.
        """
        for host in (
            "zzexamplefoodservice.fi",
            "zzexampleprofessional.com",
            "zzexamplevalves.dk",
            "zzexamplegrocery.no",
            "zzexamplefisheries.is",
        ):
            self.assertNotIn(
                host,
                SHIPPED_FLOOR,
                "%s — a company's own domain — is on the shipped list" % host,
            )
            self.assertEqual(
                self.Identifier._normalise_domain("sales@%s" % host),
                host,
                "%s stopped being an identifier" % host,
            )

    def test_domains_without_live_mail_were_not_added(self):
        """Plausible-looking hosts that are not consumer mail are not excluded.

        These have no consumer MX records or now serve a corporate tenant, so
        excluding them would make a real company's domain unmatchable.
        """
        for host in ("outlook.fi", "outlook.no", "outlook.se", "comhem.se", "tele2.se"):
            self.assertNotIn(host, _PUBLIC_EMAIL_DOMAINS)
            self.assertEqual(self.Identifier._normalise_domain("a@%s" % host), host)


@tagged("post_install", "-at_install")
class TestDomainsThatIdentifyNobody(TransactionCase):
    """The second floor: hosts that are not free providers but still identify nobody.

    A host is unsafe when many unrelated parties carry it and exactly one of them
    is bill-eligible, because `_commercial_partner_for` would then decide on it.
    Import placeholders such as `<digits>@emaildomain.com`, typos of providers and
    ISP regional subdomains are of this kind.
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
        """An import placeholder address normalises to nothing."""
        self.assertIsNone(
            self.Identifier._normalise_domain("1000000001@emaildomain.com")
        )
        self.assertIsNone(self.Identifier._normalise_domain("emaildomain.com"))

    def test_a_near_miss_of_a_shipped_provider_identifies_nobody(self):
        """Typos of a shipped provider identify nobody."""
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
        """Control: hosts one character away from shipped entries still identify."""
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
    # Subdomains
    # ------------------------------------------------------------------

    def test_a_regional_host_of_a_consumer_isp_identifies_nobody(self):
        """Excluding a domain does not exclude its subdomains (`_host_of` keeps the
        whole host), so ISP regional hosts are covered by the suffix rule."""
        for host in ("nc.rr.com", "nycap.rr.com", "tampabay.rr.com", "rr.com"):
            self.assertIsNone(
                self.Identifier._normalise_domain("owner@%s" % host),
                "%s is still an identifier" % host,
            )

    def test_the_suffix_rule_matches_a_LABEL_and_not_a_substring(self):
        """Control: the suffix rule matches whole labels, not a character suffix."""
        for host in ("bigrr.com", "rr.com.vendor.example", "notrr.com", "rrr.com"):
            self.assertEqual(
                self.Identifier._normalise_domain("sales@%s" % host),
                host,
                "%s stopped being an identifier" % host,
            )

    def test_every_shipped_rule_is_still_subtractable_by_an_operator(self):
        """Every shipped rule, suffixes included, can be removed with `-`."""
        self.Params = self.env["ir.config_parameter"].sudo()
        self.Params.set_param(PUBLIC_EMAIL_DOMAINS_PARAM, "-emaildomain.com, -rr.com")
        self.env.registry.clear_cache("stable")
        self.assertEqual(
            self.Identifier._normalise_domain("1000000001@emaildomain.com"),
            "emaildomain.com",
        )
        self.assertEqual(
            self.Identifier._normalise_domain("owner@nc.rr.com"), "nc.rr.com"
        )

    def test_subtracting_a_shipped_rule_is_logged_by_name(self):
        """Removing a second-floor entry is logged as a removal, not as a no-op."""
        with self.assertLogs(
            "odoo.addons.goodahead_invoice_ai_import.models.partner_identifier",
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
    """Rows stored before a domain joined the list can no longer match.

    Every write and lookup passes through `_normalise_domain`, so no migration is
    needed. The stale row is created directly, as in an upgraded database.
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
        """Control: the stale row exists."""
        self.assertTrue(
            self.Identifier.search(
                [("kind", "=", "domain"), ("value", "=", "suomi24.fi")]
            )
        )

    def test_a_stale_row_for_a_newly_excluded_domain_can_no_longer_decide(self):
        self.assertFalse(
            self.env["res.partner"]._ai_vendor_by_domains(["laskutus@suomi24.fi"])
        )

    def test_and_it_can_no_longer_be_offered_as_a_candidate_either(self):
        """The candidate path, which reads the stored table directly, skips it too."""
        candidates = self.env["account.move"]._ai_vendor_candidates(
            {"name": "ZZ Nothing Like It", "email": "laskutus@suomi24.fi"}
        )
        self.assertNotIn(self.partner, [c["partner"] for c in candidates])

    def test_a_row_whose_domain_is_NOT_excluded_still_decides(self):
        """Control: the same kind of row on a non-excluded domain still decides."""
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
    """The list can be changed through `res.config.settings`."""

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
        """The field help names the removal syntax, its effect and the risk.

        The risk is exactly one bill-eligible carrier, not many.
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
        """The always-visible `<setting help>` text carries the same warning.

        `fields.Char(help=…)` is only a tooltip; the setting's help is shown at all times.
        """
        view = self.env.ref(
            "goodahead_invoice_ai_import.res_config_settings_view_form"
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
