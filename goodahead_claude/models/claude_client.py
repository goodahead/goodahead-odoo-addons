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

# The only place a model is judged by its id: the models API exposes no capability tier.
# This is an exclusion, not a ranking - it keeps the small tier from becoming the default
# for callers that never chose a model.
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

    # The default model is resolved, never hardcoded: the company's configured default, then
    # the fallback among synced models, then one automatic sync, then a UserError naming the
    # fix. Raising beats a stale literal, which would fail later as an opaque 404.

    @api.model
    def _parse_api_datetime(self, value):
        """Parse the models API `created_at` into a naive UTC datetime, or False."""
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
        """The model to use when no one has chosen one.

        Prefers non-small-tier models, then the most recently released, then dated rows over
        undated ones; if every model is small-tier, the newest of those. Archived models are
        excluded by `active_test`. `requires_pdf` restricts to PDF-capable models, but falls
        back with a warning when none reports PDF support, since that more likely means the
        capability data is missing.

        Each preference is a separate search on purpose: PostgreSQL sorts NULLs first under
        DESC, so relying on `_order` would make an undated row the default.
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
        """The model id to send. See the comment at the top of the class for the order."""
        if model:
            # An explicit model is returned untouched, archived or not: it is the caller's
            # configuration, and the API reports a retired id at call time.
            return model

        default = company.claude_default_model_id
        if default and not default.active:
            # Raise rather than send an id the API may reject, or silently fall back to
            # a model the company did not choose.
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
            # Raise only when a PDF-capable alternative exists; otherwise the capability
            # data is probably missing, so warn and proceed like `_fallback_model`.
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

        # Nothing synced yet (fresh database): try one sync before giving up. The private
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
            "Settings, or use \"Sync Models\" to fetch the current list from the API."
        ) % company.name)

    # Who may spend the company's key: every public method of every model is callable by
    # any logged-in user through `/web/dataset/call_kw` - a portal user included. The
    # methods below call the API with the COMPANY's key, so they are `@api.private`:
    # callable from Python (other add-ons' server code), never over RPC. The two an
    # administrator triggers from Settings also check the caller.

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

        ``timeout`` (seconds) and ``requires_pdf`` are per-call on purpose, so one caller's
        needs never change the behaviour of other callers sharing this client. ``None``
        keeps the transport default of 120 s; ``requires_pdf`` limits model resolution to
        models that accept PDF input.
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

        Walks `ir.model.fields` so that Many2one fields added by dependent modules are found
        without this module depending on them.
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
        """`sync_models` with the detail the UI needs.

        Returns a dict: `synced` (int), `archived` / `restored` (model api ids) and
        `pointers` (from `_pointers_at`, configuration now pointing at an archived model).
        """
        company = company or self.env.company
        api_key = company.sudo().claude_api_key
        if not api_key:
            raise UserError(
                _("Claude API key is not configured for company %s.") % company.name
            )

        models_data = _api.list_models(api_key)
        # active_test=False: otherwise the upsert misses archived rows and violates the
        # unique constraint on `model_api_id`.
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
                "created_at": self._parse_api_datetime(m.get("created_at")),
            }
            existing = ClaudeModel.search([("model_api_id", "=", model_id)], limit=1)
            if existing:
                if not existing.active:
                    # Listed again: restore it, so a partial response heals on the next sync.
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
            # Guarded by `if count:` - an empty response is a failed sync, not the
            # retirement of the whole catalogue.
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

        Returns the count of models stored (details are in `_sync_models`). Models the API
        no longer lists are archived, never deleted, because configuration points at them.
        Administrators only; server code that needs a catalogue (`_resolve_model`) calls
        `_sync_models`.
        """
        self._check_claude_admin()
        return self._sync_models(company=company)["synced"]
