# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import logging
from datetime import datetime, timezone

from odoo import _, api, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import config

from ..lib import claude_client as _api

_logger = logging.getLogger(__name__)

# Server-side ceilings for what one request may ask for. Every request is
# billed to the company's account, so neither value is left to the caller.
# Both are system parameters; the defaults are above anything a shipped
# caller asks for (AI invoice import: 16,000 tokens, 300 s).
MAX_OUTPUT_TOKENS_CEILING_PARAM = "goodahead_claude.max_output_tokens_ceiling"
MAX_OUTPUT_TOKENS_CEILING_DEFAULT = 64000
TIMEOUT_CEILING_PARAM = "goodahead_claude.timeout_ceiling"
TIMEOUT_CEILING_DEFAULT = 300
#: Seconds left to a worker after a request has timed out, so the job can still
#: record its failure before Odoo's ``limit_time_real`` kills the worker.
WORKER_TIME_MARGIN = 15
#: The shortest timeout the worker limit may cut a request down to.
WORKER_TIMEOUT_FLOOR = 30

# 🔴 19.0.1.3.0 — the ONLY place this module judges a model by its name, and it is a deliberate,
# bounded exception to `claude_model.py`'s "do not parse the id" rule.
#
# That rule exists because the API hands us `created_at`, so parsing a date out of an id is
# both unnecessary and wrong. Capability tier is different: `GET /v1/models` reports `id`,
# `display_name`, `created_at`, `max_input_tokens` and `capabilities.pdf_input` — and nothing
# that says "this is the small model". There is no non-name signal to use, so the choice is
# between naming the small tier and silently defaulting to it.
#
# It is an EXCLUSION, not a ranking: the resolver does not claim to know whether Opus beats
# Sonnet. It only refuses to make the small tier the default for callers who never chose one.
# An unknown future small tier simply is not matched, which lands on the old behaviour rather
# than on an error — add its token here when one appears.
_SMALL_TIER_TOKENS = ("haiku",)


class ClaudeClient(models.AbstractModel):
    """Entry point for other addons to call the Claude API.

    Usage:
        result = env['goodahead.claude.client'].messages_create(
            messages=[{'role': 'user', 'content': 'Hi'}],
            system='You are a helpful assistant.',
        )
        text = env['goodahead.claude.client'].extract_text(result)
    """

    _name = "goodahead.claude.client"
    _description = "Claude API Client"

    # ------------------------------------------------------------------ #
    # Q3 — the default model is RESOLVED, never hardcoded.                #
    # ------------------------------------------------------------------ #
    #
    # 🔴 It used to fall back to `lib.claude_client.DEFAULT_MODEL`, a literal that had gone
    # stale: `claude-sonnet-4-20250514` is retired, so every call that did not name a model
    # explicitly was aimed at a model id the API no longer serves. Nothing in the tree caught
    # it, because the failure is a 404 from Anthropic at call time rather than anything a gate
    # can see -- and a constant is only correct until the vendor ships again.
    #
    # The fix: make the default dynamic, chosen from the fetched models.
    #
    # The module already had the machinery -- `sync_models()` reads GET /v1/models into
    # `goodahead.claude.model`. What was missing was using it. `_resolve_model` now walks:
    #
    #   1. the company's configured `claude_default_model_id`  — an explicit choice wins;
    #   2. the newest row in `goodahead.claude.model`          — ordered by the API's own
    #                                                            `created_at`, not by parsing
    #                                                            a date out of the id;
    #   3. one automatic `sync_models()`, then 2 again         — so a fresh database that has
    #                                                            never synced still works;
    #   4. a UserError naming the fix.
    #
    # 🔴 Step 4 raises rather than falling back to a literal, and that is deliberate. A stale
    # constant fails anyway -- as a 404 from Anthropic, at call time, in whatever job happened
    # to make the call. Raising here fails in the same place but says WHY and what to do about
    # it, which is the whole difference between the old behaviour and this one.

    @api.model
    def _parse_api_datetime(self, value):
        """`created_at` from the models API ('2025-02-19T00:00:00Z') -> a naive UTC datetime.

        Returns False on anything unparseable, so one odd row can never abort a sync of the
        rest. A model with no `created_at` simply sorts last, which is the safe direction.
        """
        if not value:
            return False
        try:
            return datetime.strptime(
                str(value).replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z"
            ).astimezone(timezone.utc).replace(tzinfo=None)
        except (ValueError, TypeError):
            _logger.warning("Claude models API returned an unparseable created_at: %r", value)
            return False

    @api.model
    def _fallback_model(self, ClaudeModel, requires_pdf=False):
        """The model to use when NOBODY has chosen one. Renamed from `_newest_model` (19.0.1.3.0).

        🔴 The old name was the bug. "Newest" meant newest by RELEASE DATE, and after a sync
        the newest release is whatever Anthropic shipped last — which was `claude-haiku-5-5`,
        the SMALL tier. A client that extracts invoices and scores orders for fraud silently
        got the cheapest model, because it shipped most recently. Recency is not capability.

        The rule now, stated exactly:

          1. prefer models whose id does NOT name the small tier (`_SMALL_TIER_TOKENS`);
          2. within that preference, the most recently released one;
          3. preferring rows that carry a `created_at` over rows that do not;
          4. if EVERY listed model is small-tier, take the newest of those rather than fail —
             a wrong-sized default is recoverable, no default at all is an outage.

        Archived models are excluded for free: `search` applies `active_test`, so a model the
        last sync did not see can never become the fallback. That is half of 19.0.1.3.0's fix.

        🔴 Do NOT collapse this into `search([], limit=1)` and lean on `_order`. PostgreSQL's
        `DESC` implies **NULLS FIRST**, so a row with no `created_at` sorts to the TOP of a
        `created_at desc` ordering — the exact opposite of what is wanted, and it would make
        an undated row the silent default. Found by the fixture test, which is the only
        reason it is not still in here: the first version of this method had that bug and
        looked completely reasonable.

        Odoo's `_order` parser will not take a raw `NULLS LAST` fragment, so each preference
        is expressed as its own search instead of one clever one.

        🔴 `requires_pdf` (2026-10-09) is the SAME BUG AS THE SMALL TIER, in the other
        column. `sync_models` has always recorded `capabilities.pdf_input.supported` into
        `goodahead.claude.model.pdf_supported`, and until now NOTHING READ THAT FIELD —
        not this resolver, not the settings dropdown, nothing but a search filter on the
        list view. So the next sync that brought in a newer model without PDF support
        would have silently retargeted `goodahead_invoice_ai_import` — which sends a PDF
        and nothing else — at a model that cannot read the document. Recency is not
        capability, and neither is tier.

        It is a per-call REQUIREMENT, never a global preference: a caller that sends text
        passes nothing and gets exactly the model it got before. `goodahead_fraud_detection`
        is such a caller, and it also pins its own model, so it is twice removed from this.

        ⚠️ The requirement DEGRADES rather than fails, for the same reason as rule 4 above:
        if not one listed model claims PDF support the likeliest explanation is that the
        capability data is missing, not that Anthropic has withdrawn PDF input from the
        whole catalogue. A wrong model is recoverable and says so in the log; no model at
        all is an outage.
        """
        big_tier = [("model_api_id", "not ilike", token) for token in _SMALL_TIER_TOKENS]
        capability = [[("pdf_supported", "=", True)], []] if requires_pdf else [[]]
        for cap in capability:
            if requires_pdf and not cap:
                _logger.warning(
                    "Claude: a caller asked for a model that accepts PDF input and not "
                    "one synced model reports `capabilities.pdf_input.supported`. "
                    "Falling back to the ordinary default — run \"Sync Models\" and "
                    "check the PDF Support column, because the call that follows sends "
                    "a document."
                )
            for tier in (big_tier, []):
                dated = ClaudeModel.search(cap + tier + [("created_at", "!=", False)],
                                           limit=1)
                if dated:
                    return dated
                undated = ClaudeModel.search(cap + tier, limit=1)
                if undated:
                    return undated
        return ClaudeModel.browse()

    @api.model
    def _resolve_model(self, company, model=None, requires_pdf=False):
        """The model id to send. See the block comment above for the order and why."""
        if model:
            # 🔴 An EXPLICIT model is returned untouched, archived or not. Fraud detection
            # pins `res.company.fraud_model_id` and passes it here; 19.0.1.3.0 must not change what
            # a pinned model does. If a pinned model really has been retired, the API says so
            # at call time — that is the caller's configuration to fix, not ours to override.
            return model

        default = company.claude_default_model_id
        if default and not default.active:
            # 19.0.1.3.0: the configured default is no longer in the catalogue. Say so, loudly,
            # instead of either sending a model id the API will 404 or silently substituting
            # a different model than the one this company chose. Same reasoning as step 4.
            raise UserError(_(
                "Company %(company)s is configured to use the Claude model "
                "\"%(model)s\" (%(api_id)s), which the Anthropic models API no longer "
                "lists — the last \"Sync Models\" archived it. Pick a current model as the "
                "Default Model in Settings."
            ) % {
                "company": company.name,
                "model": default.name,
                "api_id": default.model_api_id,
            })
        ClaudeModel = self.env["goodahead.claude.model"].sudo()
        if requires_pdf and default and not default.pdf_supported:
            # The company's chosen default cannot read a document and the caller is
            # about to send one. Raise for the same reason the archived branch above
            # raises: the alternative is a 400 from the API with no explanation, or
            # silently sending a different model than this company chose.
            #
            # ⚠️ Only when a PDF-capable model actually exists to point at. If none
            # does, the capability column is far more likely to be empty than the whole
            # catalogue to have lost PDF input, and an outage over missing metadata is
            # the worse trade. That case warns and proceeds, like `_fallback_model`.
            if ClaudeModel.search_count([("pdf_supported", "=", True)]):
                raise UserError(_(
                    "Company %(company)s is configured to use the Claude model "
                    "\"%(model)s\" (%(api_id)s), which does not accept PDF documents — "
                    "and this feature sends one. Pick a model with PDF Support as the "
                    "Default Model in Settings, or set a PDF-capable model for this "
                    "feature only."
                ) % {
                    "company": company.name,
                    "model": default.name,
                    "api_id": default.model_api_id,
                })
            _logger.warning(
                "Claude: company %s defaults to %s, which does not report PDF support, "
                "and no synced model does either. Sending the document to it anyway — "
                "run \"Sync Models\" and check the PDF Support column.",
                company.name, default.model_api_id,
            )
        if default and default.model_api_id:
            return default.model_api_id

        newest = self._fallback_model(ClaudeModel, requires_pdf=requires_pdf)
        if newest and newest.model_api_id:
            return newest.model_api_id

        # Nothing synced yet. Try once -- a fresh database has an API key but an empty table,
        # and failing there would be a setup papercut rather than a real error. The private
        # sync, not `sync_models`: the caller here is server code running as whoever queued
        # the work (an accountant), not an administrator pressing the button.
        try:
            self._sync_models(company=company)
        except Exception as e:                            # noqa: BLE001 - reported below
            _logger.warning("Claude: automatic model sync failed while resolving a "
                            "default model: %s", e)
        else:
            newest = self._fallback_model(ClaudeModel, requires_pdf=requires_pdf)
            if newest and newest.model_api_id:
                _logger.info("Claude: synced models on demand; using %s as the default.",
                             newest.model_api_id)
                return newest.model_api_id

        raise UserError(_(
            "No Claude model is available for company %s. Set a default model in "
            "Settings, or use \"Sync Models\" to fetch the current list from the API. "
            "(This used to fall back to a hardcoded model id, which had gone stale — "
            "the call would have failed at the API instead, with a less useful message.)"
        ) % company.name)

    # ------------------------------------------------------------------ #
    # Who may spend the company's key                                     #
    # ------------------------------------------------------------------ #
    #
    # 🔴 Every public method of every model is callable by any logged-in user through
    # `/web/dataset/call_kw` — a portal user included. The methods below call the API with
    # the COMPANY's key, so they are `@api.private`: callable from Python (other add-ons'
    # server code), never over RPC. The two an administrator triggers from Settings also
    # check the caller, because they are reached from a button.

    @api.model
    def _check_claude_admin(self):
        """Only an administrator may test the connection or sync the catalogue."""
        if self.env.su or self.env.user.has_group("base.group_system"):
            return
        raise AccessError(_("Only an administrator can test the Claude connection or "
                            "sync the Claude models."))

    @api.model
    def _int_param(self, key, default):
        try:
            value = int(self.env["ir.config_parameter"].sudo().get_param(key, default))
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    @api.model
    def _max_output_tokens_ceiling(self):
        """The most output tokens one request may ask for (system parameter
        ``goodahead_claude.max_output_tokens_ceiling``, default 64,000)."""
        return self._int_param(MAX_OUTPUT_TOKENS_CEILING_PARAM, MAX_OUTPUT_TOKENS_CEILING_DEFAULT)

    @api.model
    def _timeout_ceiling(self):
        """The longest one request may wait, in seconds (system parameter
        ``goodahead_claude.timeout_ceiling``, default 300)."""
        return self._int_param(TIMEOUT_CEILING_PARAM, TIMEOUT_CEILING_DEFAULT)

    @api.model
    def _worker_timeout_ceiling(self):
        """The longest a request may wait in THIS server's workers, or None.

        🔴 With worker processes Odoo kills a request that runs longer than
        ``limit_time_real``. A Claude call still waiting then has been billed, its
        answer is lost and queue_job runs the job again. So the timeout stays
        ``WORKER_TIME_MARGIN`` seconds below that limit. Without workers
        (``workers = 0``) the limit is not enforced and nothing is cut.
        """
        try:
            workers = int(config["workers"] or 0)
            limit = int(config["limit_time_real"] or 0)
        except (KeyError, TypeError, ValueError):
            return None
        if workers <= 0 or limit <= 0:
            return None
        return max(WORKER_TIMEOUT_FLOOR, limit - WORKER_TIME_MARGIN)

    @api.model
    def _clamp(self, value, ceiling):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return ceiling
        return max(1, min(value, ceiling))

    @api.private
    @api.model
    def messages_create(
        self,
        messages,
        system=None,
        model=None,
        max_tokens=None,
        cache_static_system=True,
        company=None,
        timeout=None,
        requires_pdf=False,
    ):
        """Call Claude's messages API. See lib.claude_client.messages_create.

        ``timeout`` is a PER-CALL override of the transport's
        ``DEFAULT_TIMEOUT`` (120 s), in seconds. It was added for AI invoice
        import, whose calls send a whole PDF and ask a thinking model for a
        long JSON answer — ~300 s rather than ~10 s.

        🔴 It is per-call ON PURPOSE, and must stay that way. The obvious
        alternative — raising ``DEFAULT_TIMEOUT``, or adding a company-level
        setting the way ``claude_max_output_tokens`` works — would change the
        behaviour of EVERY caller of this client, and fraud detection is one of
        them. A fraud check that currently gives up after 120 s would start
        holding its worker for five minutes instead, which is a different
        product decision made by accident. ``None`` keeps the transport default,
        so no existing caller moves.

        ``requires_pdf`` says that this call sends a document, so the model must
        be one that can read one. It is per-call for exactly the same reason:
        it constrains what ``_resolve_model`` may choose for THIS caller, and a
        caller that sends text passes nothing and resolves exactly as before.
        See ``_fallback_model`` — the capability was synced and never read, which
        is how an invoice prompt could end up aimed at a model that cannot see
        the invoice.
        """
        company = company or self.env.company
        api_key = company.sudo().claude_api_key
        if not api_key:
            raise UserError(
                _("Claude API key is not configured for company %s.") % company.name
            )

        model = self._resolve_model(company, model, requires_pdf=requires_pdf)

        if max_tokens is None:
            max_tokens = company.claude_max_output_tokens or _api.DEFAULT_MAX_OUTPUT_TOKENS
        # Clamped on the server whatever the caller asked for: the request is billed to
        # the company, and a long timeout holds a worker.
        max_tokens = self._clamp(max_tokens, self._max_output_tokens_ceiling())
        timeout = self._clamp(_api.DEFAULT_TIMEOUT if timeout is None else timeout,
                              self._timeout_ceiling())
        worker_ceiling = self._worker_timeout_ceiling()
        if worker_ceiling and timeout > worker_ceiling:
            _logger.warning(
                "Claude: a request asked for a %s s timeout but workers are killed after "
                "limit_time_real; waiting %s s instead. Raise limit_time_real for long "
                "requests (AI invoice import asks for 300 s).", timeout, worker_ceiling,
            )
            timeout = worker_ceiling

        try:
            return _api.messages_create(
                api_key=api_key,
                model=model,
                messages=messages,
                system=system,
                max_tokens=max_tokens,
                cache_static_system=cache_static_system,
                timeout=timeout,
            )
        except _api.ClaudeAPIError as e:
            _logger.warning("Claude API call failed: %s", e)
            raise

    @api.private
    @api.model
    def extract_text(self, response):
        return _api.extract_text(response)

    @api.private
    @api.model
    def extract_usage(self, response):
        return _api.extract_usage(response)

    @api.private
    @api.model
    def test_connection(self, company=None):
        """Send a minimal call to verify the API key. Returns True or raises.
        Administrators only."""
        self._check_claude_admin()
        company = company or self.env.company
        api_key = company.sudo().claude_api_key
        if not api_key:
            raise UserError(
                _("Claude API key is not configured for company %s.") % company.name
            )
        model = self._resolve_model(company)
        _api.test_connection(api_key, model)
        return True

    @api.model
    def _pointers_at(self, claude_models):
        """Every stored record that points at one of `claude_models`, as plain dicts.

        🔴 19.0.1.3.0 asked whether archiving should be silent. It should not: a sync that quietly
        takes away the model a company is configured to use is the same shape of silent
        failure this change is about. So the sync names what it broke.

        This walks `ir.model.fields` rather than importing anything, ON PURPOSE.
        `goodahead_claude` does not depend on `goodahead_fraud_detection` and must not — but
        `res.company.fraud_model_id` points here, and so will the next module that adds a
        field. Asking the registry finds all of them, including fields that do not exist yet.
        """
        if not claude_models:
            return []
        found = []
        fields_pointing_here = self.env["ir.model.fields"].sudo().search([
            ("ttype", "=", "many2one"),
            ("relation", "=", "goodahead.claude.model"),
            ("store", "=", True),
        ])
        for field in fields_pointing_here:
            Model = self.env.get(field.model)
            if Model is None or Model._abstract or Model._transient:
                continue
            if field.name not in Model._fields:
                continue
            holders = Model.sudo().with_context(active_test=False).search(
                [(field.name, "in", claude_models.ids)]
            )
            for holder in holders:
                found.append({
                    "model": field.model,
                    "field": field.name,
                    "record": holder.display_name,
                    "model_api_id": holder[field.name].model_api_id,
                })
        return found

    @api.model
    def _sync_models(self, company=None):
        """`sync_models` with the detail the UI needs. Returns a dict; see `sync_models`.

        Keys: `synced` (int), `archived` / `restored` (lists of model api ids),
        `pointers` (list of dicts from `_pointers_at`, describing configuration that now
        points at an archived model).
        """
        company = company or self.env.company
        api_key = company.sudo().claude_api_key
        if not api_key:
            raise UserError(
                _("Claude API key is not configured for company %s.") % company.name
            )

        models_data = _api.list_models(api_key)
        # 🔴 `active_test=False`, and this is not cosmetic. Once a row can be archived, an
        # upsert that searches with the default active filter cannot SEE the archived row —
        # it would take the `create` branch and hit the `model_api_id` UNIQUE constraint. The
        # sync would then fail on exactly the models it was taught to handle.
        ClaudeModel = self.env["goodahead.claude.model"].sudo().with_context(active_test=False)
        seen = ClaudeModel.browse()
        restored = []
        count = 0
        for m in models_data:
            capabilities = m.get("capabilities", {}) or {}
            pdf_input = capabilities.get("pdf_input", {}) or {}
            model_id = m.get("id")
            if not model_id:
                continue
            vals = {
                "name": m.get("display_name", model_id),
                "model_api_id": model_id,
                "pdf_supported": bool(pdf_input.get("supported")),
                "max_input_tokens": m.get("max_input_tokens", 0) or 0,
                # Q3: stored so `_resolve_model` can order by recency rather than guess.
                "created_at": self._parse_api_datetime(m.get("created_at")),
            }
            existing = ClaudeModel.search([("model_api_id", "=", model_id)], limit=1)
            if existing:
                if not existing.active:
                    # The catalogue lists it again. Restore it, so one bad sync — a partial
                    # response, a key scoped to fewer models — heals on the next run instead
                    # of permanently costing the dropdown a model.
                    vals["active"] = True
                    restored.append(model_id)
                existing.write(vals)
                row = existing
            else:
                row = ClaudeModel.create(vals)
            seen |= row
            count += 1

        archived = []
        pointers = []
        if count:
            unlisted = ClaudeModel.search([("id", "not in", seen.ids)]).filtered("active")
            # 🔴 `if count:` is the guard that matters. An empty or wholly unparseable
            # response is a FAILED sync, not the retirement of the entire catalogue — without
            # this, one bad response archives every model and takes the dropdown with it.
            if unlisted:
                pointers = self._pointers_at(unlisted)
                archived = unlisted.mapped("model_api_id")
                unlisted.write({"active": False})
                _logger.info(
                    "Claude: %s model(s) are no longer listed by the API and have been "
                    "archived: %s", len(archived), ", ".join(archived),
                )
                for pointer in pointers:
                    _logger.warning(
                        "Claude: %s.%s on %r still points at the archived model %s. It will "
                        "keep reading, but the API no longer lists it — choose another.",
                        pointer["model"], pointer["field"], pointer["record"],
                        pointer["model_api_id"],
                    )
        if restored:
            _logger.info("Claude: %s model(s) are listed again and have been restored: %s",
                         len(restored), ", ".join(restored))

        return {
            "synced": count,
            "archived": archived,
            "restored": restored,
            "pointers": pointers,
        }

    @api.private
    @api.model
    def sync_models(self, company=None):
        """Fetch models from the API and mirror them onto goodahead.claude.model.

        Returns the count of models stored. Administrators only; server code that
        needs a catalogue (`_resolve_model`) calls `_sync_models`.

        🔴 The return value is an int ON PURPOSE, unchanged from before 19.0.1.3.0. `_resolve_model`
        and any caller outside this module only ever asked "did anything arrive"; widening
        this to a dict would have been a silent contract change for them. The detail lives in
        `_sync_models`, which this wraps.

        19.0.1.3.0: the sync now RETIRES as well as adds. A model the API has stopped listing is
        ARCHIVED, never deleted — `res.company.claude_default_model_id` and
        `goodahead_fraud_detection`'s `fraud_model_id` point at these rows, and the foreign
        key has to survive. See `goodahead.claude.model.active`.
        """
        self._check_claude_admin()
        return self._sync_models(company=company)["synced"]
