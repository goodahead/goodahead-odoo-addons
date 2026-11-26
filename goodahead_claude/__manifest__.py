# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
{
    "name": "Goodahead Claude",
    "summary": "Generic Claude (Anthropic) API client for Odoo addons",
    "description": "Shared Claude API client for Goodahead add-ons: per-company API key, "
    "default model and output limit, a model catalogue synced from the API, and "
    "messages_create() for other add-ons. See README.md.",
    "version": "18.0.1.4.1",
    "license": "LGPL-3",
    "author": "Goodahead Oy",
    "website": "https://www.goodahead.com/",
    "category": "Technical",
    "depends": [
        "base",
        "mail",
        "queue_job",
    ],
    "external_dependencies": {
        "python": ["requests"],
    },
    "data": [
        "security/ir.model.access.csv",
        "data/queue_job_channel.xml",
        "views/claude_model_views.xml",
        "views/res_config_settings_views.xml",
        "views/menus.xml",
    ],
    "installable": True,
    "application": False,
}
