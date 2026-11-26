# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The shared e-invoicing attention bubble.

The frame: a super-chained section registry, a group filter per section, a
section that raises is dropped (others still returned), empty sections are
dropped, and one route returns ``{total, sections}``. The sections themselves
are contributed by other add-ons; here invented sections stand in for them.
"""

from unittest.mock import patch

from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged

from odoo.addons.web.tests.test_js import unit_test_error_checker

ROUTE = "/goodahead_einvoice/attention"


def _section(count, title="ZZ item"):
    return {
        "label": "ZZ section",
        "count": count,
        "items": [{"id": n + 1, "title": f"{title} {n + 1}", "subtitle": "", "date": False} for n in range(min(count, 10))],
        "action": {"type": "ir.actions.act_window", "res_model": "res.partner", "views": [[False, "list"]]},
        "item_action": None,
    }


class _AttentionCase:
    """Replace the registry chain with invented sections built by the frame."""

    def _patch_sections(self, specs):
        Attention = type(self.env["goodahead.einvoice.attention"])

        def _sections(model):
            return [model._attention_section(key, groups, builder) for key, groups, builder in specs]

        return patch.object(Attention, "_attention_sections", _sections)


@tagged("post_install", "-at_install")
class TestAttentionFrame(_AttentionCase, TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.billing = new_test_user(
            cls.env, login="zz_att_billing", groups="base.group_user,account.group_account_invoice"
        )
        cls.manager = new_test_user(
            cls.env, login="zz_att_manager", groups="base.group_user,account.group_account_manager"
        )
        cls.plain = new_test_user(cls.env, login="zz_att_plain", groups="base.group_user")

    def _data(self, user):
        return self.env["goodahead.einvoice.attention"].with_user(user)._attention_data()

    def test_registry_answers_a_list(self):
        # The base contributes no section; installed add-ons append theirs.
        sections = self.env["goodahead.einvoice.attention"]._attention_sections()
        self.assertIsInstance(sections, list)
        data = self.env["goodahead.einvoice.attention"]._attention_data()
        self.assertEqual(set(data), {"total", "sections"})

    def test_total_is_the_sum_of_visible_sections(self):
        specs = [
            ("zz_one", "account.group_account_invoice", lambda: _section(3)),
            ("zz_two", "account.group_account_invoice", lambda: _section(2)),
        ]
        with self._patch_sections(specs):
            data = self._data(self.billing)
        self.assertEqual(data["total"], 5)
        self.assertEqual([s["key"] for s in data["sections"]], ["zz_one", "zz_two"])
        section = data["sections"][0]
        self.assertEqual(
            set(section),
            {"key", "label", "groups", "count", "items", "action", "item_action"},
        )
        self.assertEqual(section["groups"], "account.group_account_invoice")
        self.assertEqual(len(section["items"]), 3)

    def test_section_dropped_for_users_in_none_of_its_groups(self):
        called = []

        def admin_only():
            called.append(True)
            return _section(1)

        specs = [
            ("zz_billing", "account.group_account_invoice", lambda: _section(2)),
            ("zz_admin", "account.group_account_manager,base.group_system", admin_only),
        ]
        with self._patch_sections(specs):
            billing = self._data(self.billing)
            manager = self._data(self.manager)
        self.assertEqual([s["key"] for s in billing["sections"]], ["zz_billing"])
        self.assertEqual(billing["total"], 2)
        # account.group_account_manager implies group_account_invoice in core.
        self.assertEqual([s["key"] for s in manager["sections"]], ["zz_billing", "zz_admin"])
        self.assertEqual(manager["total"], 3)
        self.assertEqual(len(called), 1, "the builder is not run for a user outside its groups")

    def test_user_in_no_group_gets_nothing(self):
        specs = [("zz_billing", "account.group_account_invoice", lambda: _section(2))]
        with self._patch_sections(specs):
            self.assertEqual(self._data(self.plain), {"total": 0, "sections": []})

    def test_empty_sections_are_dropped(self):
        specs = [
            ("zz_empty", "account.group_account_invoice", lambda: _section(0)),
            ("zz_none", "account.group_account_invoice", lambda: None),
            ("zz_full", "account.group_account_invoice", lambda: _section(1)),
        ]
        with self._patch_sections(specs):
            data = self._data(self.billing)
        self.assertEqual([s["key"] for s in data["sections"]], ["zz_full"])
        self.assertEqual(data["total"], 1)

    def test_a_section_that_raises_is_dropped_and_logged(self):
        def broken():
            self.env.cr.execute("SELECT no_such_column FROM res_partner")
            return _section(9)

        specs = [
            ("zz_broken", "account.group_account_invoice", broken),
            ("zz_fine", "account.group_account_invoice", lambda: _section(2)),
        ]
        with self._patch_sections(specs), self.assertLogs(
            "odoo.addons.goodahead_einvoice_base.models.einvoice_attention", level="ERROR"
        ) as logs:
            data = self._data(self.billing)
        self.assertEqual([s["key"] for s in data["sections"]], ["zz_fine"])
        self.assertEqual(data["total"], 2)
        self.assertIn("zz_broken", logs.output[0])
        # The transaction is still usable after the failed section.
        self.env.cr.execute("SELECT 1")


@tagged("post_install", "-at_install")
class TestAttentionRoute(_AttentionCase, HttpCase):
    def test_route_returns_the_visible_sections(self):
        new_test_user(
            self.env,
            login="zz_att_route",
            password="zz_att_route_pw",
            groups="base.group_user,account.group_account_invoice",
        )
        specs = [
            ("zz_billing", "account.group_account_invoice", lambda: _section(2)),
            ("zz_admin", "base.group_system", lambda: _section(4)),
        ]
        self.authenticate("zz_att_route", "zz_att_route_pw")
        with self._patch_sections(specs):
            data = self.make_jsonrpc_request(ROUTE)
        self.assertEqual(data["total"], 2)
        self.assertEqual([s["key"] for s in data["sections"]], ["zz_billing"])

    def test_route_needs_a_login(self):
        response = self.url_open(
            ROUTE,
            data='{"jsonrpc": "2.0", "method": "call", "params": {}}',
            headers={"Content-Type": "application/json"},
        )
        self.assertIn("SessionExpired", response.text)


@tagged("post_install", "-at_install")
class TestAttentionMenuComponent(HttpCase):
    """The systray component's unit tests (SCR-10 states), run in the browser."""

    def test_attention_menu_component(self):
        self.browser_js(
            "/web/tests?headless&loglevel=2&preset=desktop&timeout=60000"
            "&filter=EinvoiceAttentionMenu",
            "",
            "",
            login="admin",
            timeout=900,
            success_signal="[HOOT] Test suite succeeded",
            error_checker=unit_test_error_checker,
        )
