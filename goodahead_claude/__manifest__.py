# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
{
    "name": "Goodahead Claude",
    "summary": "Generic Claude (Anthropic) API client for Odoo addons",
    "description": """
Generic Claude API client used by other Goodahead modules. Provides:

* Per-company API key + default model configuration
* Catalog of available Claude models (synced from the API)
* AbstractModel `goodahead.claude.client` exposing `messages_create()` to other
  addons, with native prompt caching support
* "Test Connection" and "Sync Models" actions in General Settings
""",
    "version": "18.0.1.4.0",
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
