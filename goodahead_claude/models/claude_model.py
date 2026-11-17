# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
from odoo import fields, models


class ClaudeModel(models.Model):
    _name = "goodahead.claude.model"
    _description = "Claude AI Model"
    # Newest first; the fallback default is picked from this order. PostgreSQL sorts NULLs
    # first under DESC, so `_fallback_model()` prefers dated rows explicitly.
    _order = "created_at desc, id desc"

    name = fields.Char(
        string="Name",
        required=True,
        help="Human-readable model display name (e.g. 'Claude Opus 5').",
    )
    model_api_id = fields.Char(
        string="Model ID",
        required=True,
        help="API model identifier passed to the Anthropic API (e.g. 'claude-opus-5').",
    )
    pdf_supported = fields.Boolean(
        string="PDF Support",
        help="Whether this model accepts PDF document inputs.",
    )
    max_input_tokens = fields.Integer(string="Max Input Tokens")
    # Ordering uses the API's release timestamp; model ids carry no reliable date.
    created_at = fields.Datetime(
        string="Released",
        help="Release timestamp reported by the Anthropic models API. Used to pick the "
             "newest model when no default is configured.",
    )
    # Owned by "Sync Models", which archives models the API stops listing (configuration
    # still points at them) and restores them when listed again. Archiving by hand is
    # therefore undone by the next sync.
    active = fields.Boolean(
        default=True,
        help="Maintained by \"Sync Models\": set when the Anthropic models API lists this "
             "model, cleared when it stops listing it. Archived models stay in the database "
             "so existing configuration keeps working, but are no longer offered for "
             "selection.",
    )

    _model_api_id_uniq = models.Constraint(
        "UNIQUE(model_api_id)",
        "Model ID must be unique.",
    )
