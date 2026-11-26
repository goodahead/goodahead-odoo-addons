# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Shared test set-up for the invoice import add-ons.

🔴 THE BILL TESTS NEVER SKIP FOR WANT OF A JOURNAL. They used to skip with
"no purchase journal on this database", so a database without a chart of
accounts passed the gate while running none of the tests that build a bill.
`ensure_purchase_journal` loads the generic chart of accounts for a company
that has none, and falls back to a bare purchase journal only when that chart
cannot be loaded.
"""


def ensure_purchase_journal(env, company=None):
    """A purchase journal of ``company`` (default: the current one),
    loading the generic chart of accounts first when the company has none."""
    company = company or env.company
    Journal = env["account.journal"]
    domain = [("type", "=", "purchase"), ("company_id", "=", company.id)]
    journal = Journal.search(domain, limit=1)
    if journal:
        return journal
    if not company.chart_template:
        env["account.chart.template"].try_loading(
            "generic_coa", company=company, install_demo=False
        )
        journal = Journal.search(domain, limit=1)
        if journal:
            return journal
    return Journal.create(
        {
            "name": "ZZ Test Purchases",
            "code": "ZZPUR",
            "type": "purchase",
            "company_id": company.id,
        }
    )
