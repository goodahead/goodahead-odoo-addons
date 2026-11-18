# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Model sync, archiving and default-model resolution.

No test touches the network: `list_models` is replaced by a fixture and `requests` by an
object that raises on any use.
"""

from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_claude.lib import claude_client as _lib


class _NoNetwork:
    """Anything that reaches for `requests` in these tests fails here, on purpose."""

    def __getattr__(self, name):
        raise AssertionError(
            "a goodahead_claude test tried to use requests.%s — no test may call the "
            "Claude API" % name
        )


def _api_model(api_id, created_at="2026-01-01T00:00:00Z", max_input_tokens=1000000,
               pdf=True, display_name=None):
    """One entry shaped like `GET /v1/models` returns them."""
    return {
        "id": api_id,
        "display_name": display_name or api_id,
        "created_at": created_at,
        "max_input_tokens": max_input_tokens,
        "capabilities": {"pdf_input": {"supported": pdf}},
    }


# `OPUS` is the newest big-tier model; `HAIKU` is newer still but small-tier.
OPUS = _api_model("zz-opus-9", created_at="2026-03-01T00:00:00Z")
SONNET = _api_model("zz-sonnet-9", created_at="2026-02-01T00:00:00Z")
HAIKU = _api_model("zz-haiku-9", created_at="2026-04-01T00:00:00Z",
                   max_input_tokens=200000)
OLD = _api_model("zz-sonnet-4-20250514", created_at="2025-05-14T00:00:00Z")
# A big-tier model, newer than everything else, without PDF input support.
NO_PDF = _api_model("zz-opus-10-text", created_at="2026-05-01T00:00:00Z", pdf=False)

FULL = [OPUS, SONNET, HAIKU, OLD]
WITHOUT_OLD = [OPUS, SONNET, HAIKU]
WITH_TEXT_ONLY = [OPUS, SONNET, HAIKU, NO_PDF]


@tagged("post_install", "-at_install")
class TestSyncModelsRetires(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.sudo().claude_api_key = "zz-not-a-real-key"
        cls.ClaudeModel = cls.env["goodahead.claude.model"].with_context(active_test=False)
        # Archive existing rows so the fixture is the whole catalogue, and start with no
        # company default.
        cls.ClaudeModel.search([]).write({"active": False})
        cls.company.claude_default_model_id = False

    def _sync(self, catalogue):
        """Run one sync against a fixture response and return the detail dict."""
        self.calls = []

        def _fake_list_models(api_key):
            self.calls.append(api_key)
            return list(catalogue)

        with patch.object(_lib, "list_models", _fake_list_models), \
                patch.object(_lib, "requests", _NoNetwork()):
            result = self.env["goodahead.claude.client"]._sync_models(company=self.company)
        # Control: the fixture was actually consulted.
        self.assertEqual(len(self.calls), 1, "the sync did not call the models API at all")
        return result

    def _row(self, api_id):
        return self.ClaudeModel.search([("model_api_id", "=", api_id)])

    def _resolve(self, model=None, requires_pdf=False):
        with patch.object(_lib, "list_models", lambda api_key: list(FULL)), \
                patch.object(_lib, "requests", _NoNetwork()):
            return self.env["goodahead.claude.client"]._resolve_model(
                self.company, model, requires_pdf=requires_pdf
            )

    # ---------------------------------------------------------------- archiving

    def test_a_model_absent_from_the_response_is_archived_and_a_present_one_is_not(self):
        self._sync(FULL)
        self.assertTrue(self._row(OLD["id"]).active)      # control: it starts current

        result = self._sync(WITHOUT_OLD)

        self.assertEqual(result["archived"], [OLD["id"]])
        self.assertFalse(self._row(OLD["id"]).active,
                         "a model the API stopped listing is still selectable")
        # Control: models still listed stay active.
        for still_listed in WITHOUT_OLD:
            self.assertTrue(self._row(still_listed["id"]).active,
                            "%s is listed by the API but was archived" % still_listed["id"])

    def test_archiving_does_not_delete_the_row(self):
        self._sync(FULL)
        row_id = self._row(OLD["id"]).id
        self._sync(WITHOUT_OLD)
        self.assertEqual(self._row(OLD["id"]).id, row_id,
                         "the row was replaced or deleted instead of archived")

    def test_an_archived_model_is_no_longer_offered_for_selection(self):
        """Archived models are excluded from `name_search`, i.e. the Settings dropdown."""
        self._sync(FULL)
        self._sync(WITHOUT_OLD)

        offered = dict(self.env["goodahead.claude.model"].name_search(""))
        self.assertNotIn(self._row(OLD["id"]).id, offered,
                         "a retired model is still in the Settings dropdown")
        # Control: the dropdown is not simply empty.
        self.assertIn(self._row(OPUS["id"]).id, offered)

    def test_an_empty_response_archives_nothing(self):
        """A failed or filtered sync is not the retirement of the whole catalogue."""
        self._sync(FULL)
        result = self._sync([])

        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["archived"], [])
        for entry in FULL:
            self.assertTrue(self._row(entry["id"]).active)

    def test_a_model_that_is_listed_again_is_restored_without_duplicating_the_row(self):
        """A re-listed model is restored in place, not re-created against the unique constraint."""
        self._sync(FULL)
        self._sync(WITHOUT_OLD)
        self.assertFalse(self._row(OLD["id"]).active)   # control: it really was archived

        result = self._sync(FULL)

        self.assertEqual(result["restored"], [OLD["id"]])
        self.assertTrue(self._row(OLD["id"]).active)
        self.assertEqual(len(self._row(OLD["id"])), 1, "the sync created a duplicate row")

    def test_the_sync_is_idempotent(self):
        first = self._sync(FULL)
        ids_after_first = sorted(self.ClaudeModel.search([("active", "=", True)]).ids)

        second = self._sync(FULL)

        self.assertEqual(second["synced"], first["synced"])
        self.assertEqual(second["archived"], [])
        self.assertEqual(second["restored"], [])
        self.assertEqual(sorted(self.ClaudeModel.search([("active", "=", True)]).ids),
                         ids_after_first)
        # Control: "archived nothing" above is not because the sync can never archive.
        self.assertEqual(self._sync(WITHOUT_OLD)["archived"], [OLD["id"]])

    # ------------------------------------------------- a company that points at one

    def test_a_company_pointing_at_an_archived_model_still_reads_and_still_renders(self):
        """A company default pointing at an archived model still reads and renders in Settings."""
        self._sync(FULL)
        self.company.claude_default_model_id = self._row(OLD["id"])

        self._sync(WITHOUT_OLD)

        self.assertTrue(self.company.claude_default_model_id,
                        "archiving cleared the company's pointer")
        self.assertEqual(self.company.claude_default_model_id.model_api_id, OLD["id"])
        self.assertFalse(self.company.claude_default_model_id.active)

        # Same web_read the client does when rendering the Settings form.
        settings = self.env["res.config.settings"].with_company(self.company).create({})
        rendered = settings.web_read({
            "claude_default_model_id": {"fields": {"display_name": {}}},
        })
        shown = rendered[0]["claude_default_model_id"]
        self.assertEqual(shown["id"], self._row(OLD["id"]).id)
        self.assertTrue(shown["display_name"])

    def test_the_sync_reports_the_configuration_it_has_just_invalidated(self):
        """The sync reports configuration that points at a model it just archived."""
        self._sync(FULL)
        self.company.claude_default_model_id = self._row(OLD["id"])

        result = self._sync(WITHOUT_OLD)

        pointers = [p for p in result["pointers"] if p["model"] == "res.company"]
        self.assertTrue(pointers, "the sync archived a model in use and said nothing")
        self.assertIn("claude_default_model_id", [p["field"] for p in pointers])
        self.assertIn(OLD["id"], [p["model_api_id"] for p in pointers])

        # Control: a sync that invalidates nothing reports nothing.
        self.assertEqual(self._sync(WITHOUT_OLD)["pointers"], [])

    def test_the_settings_button_says_what_it_archived(self):
        """The Settings button shows a sticky warning naming the archived model in use."""
        self._sync(FULL)
        self.company.claude_default_model_id = self._row(OLD["id"])

        settings = self.env["res.config.settings"].with_company(self.company).create({})
        with patch.object(_lib, "list_models", lambda api_key: list(WITHOUT_OLD)), \
                patch.object(_lib, "requests", _NoNetwork()):
            action = settings.action_claude_sync_models()

        params = action["params"]
        self.assertIn(OLD["id"], params["message"])
        self.assertTrue(params["sticky"],
                        "the model a company is configured to use was archived behind a "
                        "notification that disappears on its own")
        self.assertEqual(params["type"], "warning")

    def test_resolving_an_archived_default_raises_and_names_it(self):
        self._sync(FULL)
        self.company.claude_default_model_id = self._row(OLD["id"])
        self.assertEqual(self._resolve(), OLD["id"])    # control: it resolves while listed

        self._sync(WITHOUT_OLD)

        with self.assertRaises(UserError) as caught:
            self._resolve()
        self.assertIn(OLD["id"], str(caught.exception))

    # ------------------------------------------------------------ the pinned model

    def test_a_pinned_model_still_resolves_exactly_as_before(self):
        """An explicit model is returned untouched, archived or not, whatever the default."""
        self._sync(FULL)
        self.company.claude_default_model_id = self._row(OLD["id"])
        self._sync(WITHOUT_OLD)

        # The company default is archived, so the unpinned path raises...
        with self.assertRaises(UserError):
            self._resolve()
        # ...and the pinned path does not, for a current model or an archived one.
        self.assertEqual(self._resolve(model=OPUS["id"]), OPUS["id"])
        self.assertEqual(self._resolve(model=OLD["id"]), OLD["id"])

    # ---------------------------------------------------------- the fallback model

    def test_the_fallback_is_not_the_small_tier_even_when_it_is_the_newest(self):
        """The fallback skips the small tier even when it is the newest release."""
        self._sync(FULL)
        self.company.claude_default_model_id = False

        self.assertEqual(self._resolve(), OPUS["id"])
        # Control: the small tier really is the newest release.
        newest_overall = self.ClaudeModel.search([("active", "=", True)], limit=1)
        self.assertEqual(newest_overall.model_api_id, HAIKU["id"])

    def test_the_fallback_takes_the_small_tier_rather_than_failing_when_it_is_all_there_is(
            self):
        self._sync([HAIKU])
        self.company.claude_default_model_id = False
        self.assertEqual(self._resolve(), HAIKU["id"])

    def test_the_fallback_ignores_archived_models(self):
        self._sync(FULL)
        self.company.claude_default_model_id = False
        self.assertEqual(self._resolve(), OPUS["id"])    # control: it is the one chosen

        self._sync([SONNET, HAIKU])

        self.assertFalse(self._row(OPUS["id"]).active)
        self.assertEqual(self._resolve(), SONNET["id"])

    # ------------------------------------------------------- the PDF requirement

    def test_the_fallback_skips_a_newer_model_that_cannot_read_a_pdf(self):
        self._sync(WITH_TEXT_ONLY)
        self.company.claude_default_model_id = False

        # Control: without the requirement the newest big-tier (text-only) model wins.
        self.assertEqual(self._resolve(), NO_PDF["id"])

        self.assertEqual(self._resolve(requires_pdf=True), OPUS["id"])

    def test_the_requirement_degrades_rather_than_failing_when_nothing_claims_pdf(self):
        """With no model reporting PDF support, `requires_pdf` falls back instead of failing."""
        text_only = _api_model("zz-sonnet-11-text", created_at="2026-06-01T00:00:00Z",
                               pdf=False)
        self._sync([text_only])
        self.company.claude_default_model_id = False

        self.assertEqual(self._resolve(requires_pdf=True), text_only["id"])

    def test_a_pinned_model_is_still_returned_untouched_when_a_pdf_is_required(self):
        """An explicit model is returned as asked even when it lacks PDF support."""
        self._sync(WITH_TEXT_ONLY)

        self.assertEqual(self._resolve(model=NO_PDF["id"], requires_pdf=True),
                         NO_PDF["id"])

    def test_a_company_default_that_cannot_read_a_pdf_is_named_rather_than_replaced(self):
        """A company default without PDF support raises, naming it, when a PDF is required."""
        self._sync(WITH_TEXT_ONLY)
        self.company.claude_default_model_id = self._row(NO_PDF["id"])

        # Control: a caller that sends text resolves it exactly as before.
        self.assertEqual(self._resolve(), NO_PDF["id"])

        with self.assertRaises(UserError) as caught:
            self._resolve(requires_pdf=True)
        self.assertIn(NO_PDF["id"], str(caught.exception))

    def test_the_requirement_reaches_the_call_through_messages_create(self):
        """`messages_create` passes `requires_pdf` through to the model the transport gets."""
        self._sync(WITH_TEXT_ONLY)
        self.company.claude_default_model_id = False
        sent = []

        def _fake_messages_create(api_key, model, messages, **kwargs):
            sent.append(model)
            return {"content": [], "usage": {}}

        Client = self.env["goodahead.claude.client"]
        with patch.object(_lib, "messages_create", _fake_messages_create), \
                patch.object(_lib, "requests", _NoNetwork()):
            Client.messages_create(messages=[], company=self.company)
            Client.messages_create(messages=[], company=self.company, requires_pdf=True)

        # The plain call gets the newest big-tier model; the PDF call skips it for OPUS.
        self.assertEqual(sent, [NO_PDF["id"], OPUS["id"]])

    def test_an_undated_model_never_outranks_a_dated_one(self):
        """An undated row never becomes the fallback, despite NULLS FIRST under DESC."""
        undated = _api_model("zz-opus-undated", created_at=None)
        self._sync([OPUS, SONNET, undated])
        self.company.claude_default_model_id = False

        self.assertFalse(self._row(undated["id"]).created_at)       # control
        self.assertEqual(self._resolve(), OPUS["id"])
