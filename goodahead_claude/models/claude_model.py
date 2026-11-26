# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
from odoo import fields, models


class ClaudeModel(models.Model):
    _name = "goodahead.claude.model"
    _description = "Claude AI Model"
    # 🔴 Q3, 2026-08-28: ordered NEWEST FIRST, because this order is now load-bearing —
    # `goodahead.claude.client._resolve_model()` picks the fallback default from it.
    # `created_at` comes straight from GET /v1/models; `id desc` breaks ties for rows synced
    # before that field existed.
    #
    # ⚠️ THIS ORDER ALONE IS NOT ENOUGH, and the reason is easy to miss: PostgreSQL's `DESC`
    # implies **NULLS FIRST**, so a row with no `created_at` sorts to the TOP here, not the
    # bottom. Reading the first row would therefore make an UNDATED model the default. The
    # resolver compensates in `_newest_model()` by preferring dated rows explicitly; Odoo's
    # `_order` parser will not accept a raw `NULLS LAST` fragment, so it cannot be fixed here.
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
    # 🔴 Q3: the API's own release timestamp, and the ONLY sound way to order these.
    # The obvious alternative -- parsing the date suffix out of an id like
    # `claude-sonnet-4-20250514` -- is wrong twice over: current ids carry no date suffix at
    # all (`claude-opus-5`), and a string sort puts `claude-sonnet-5` above `claude-opus-5`
    # alphabetically while meaning nothing about recency.
    created_at = fields.Datetime(
        string="Released",
        help="Release timestamp reported by the Anthropic models API. Used to pick the "
             "newest model when no default is configured.",
    )
    # 🔴 19.0.1.3.0: `active` is OWNED BY `Sync Models`, and that is the whole mechanism by which a
    # retired model leaves the Settings dropdown.
    #
    # `sync_models()` mirrors the catalogue onto this flag: a model the API still lists is
    # active, a model it has stopped listing is archived, and a model that comes back is
    # restored. Archiving — rather than deleting — is the point:
    # `res.company.claude_default_model_id` and `goodahead_fraud_detection`'s
    # `res.company.fraud_model_id` both point here, and the foreign key has to survive. An archived row still READS through
    # those pointers (`active` filters `search`, never `read`), so nothing that is already
    # configured breaks; it simply stops being offered to the next person.
    #
    # ⚠️ Consequence, stated because it is the cost of the choice: archiving a row by hand is
    # NOT a way to stop people using a model. The next sync will un-archive it, because the
    # sync cannot tell a human's archive from its own. If a model must be blocked, that needs
    # a separate flag and a `domain=` on every pointer field — which this module does not have
    # and 19.0.1.3.0 did not add.
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
