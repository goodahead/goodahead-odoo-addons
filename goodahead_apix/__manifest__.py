# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
{
    "name": "Goodahead APIX Connection",
    "summary": "Per-company APIX connection: transfer credentials, environment and connection check",
    # A short description stops Odoo from rendering README.md as RST.
    "description": "Per-company APIX connection used by the APIX receiving and "
    "sending add-ons. See README.md.",
    "version": "18.0.1.7.3",
    "license": "LGPL-3",
    "author": "Goodahead Oy",
    "website": "https://www.goodahead.com/",
    "category": "Accounting",
    # `account` (not just `base`) because the access rights and the settings
    # block hang off the accounting groups and the Invoicing settings app.
    # `queue_job` for the shared `root.apix` job channel (receiving and
    # sending jobs run on it).
    "depends": [
        "base",
        "mail",
        "account",
        "queue_job",
        # the shared e-invoicing attention bubble (connection trouble section)
        "goodahead_einvoice_base",
    ],
    "external_dependencies": {
        "python": ["lxml", "requests"],
    },
    "data": [
        "security/ir.model.access.csv",
        "security/apix_security.xml",
        "data/queue_job_data.xml",
        "views/apix_connection_views.xml",
        "wizard/apix_fetch_credentials_views.xml",
        "views/res_config_settings_views.xml",
        "views/menus.xml",
    ],
    "installable": True,
    "application": False,
}
