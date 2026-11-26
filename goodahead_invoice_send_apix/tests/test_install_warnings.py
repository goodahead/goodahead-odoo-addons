# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Installing the add-on logs no warning of its own: no two fields of one
model share a label, and the developer view of an attempt has no access
rights inconsistency (its developer fields sit inside a developer-only page).
"""

import unittest

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from lxml import etree  # noqa: E402
from odoo.tests import TransactionCase, tagged  # noqa: E402

MODULE = "goodahead_invoice_send_apix"
VIEW_LOGGER = "odoo.addons.base.models.ir_ui_view"


@tagged("post_install", "-at_install")
class TestInstallWarnings(TransactionCase):
    def _module_models(self):
        """The models this add-on defines or extends."""
        data = self.env["ir.model.data"].search([("module", "=", MODULE), ("model", "=", "ir.model")])
        return self.env["ir.model"].browse(data.mapped("res_id")).mapped("model")

    def test_no_two_fields_share_a_label(self):
        models = self._module_models()
        self.assertIn("goodahead.invoice.send.attempt", models)
        for model_name in models:
            by_label = {}
            for field in self.env[model_name]._fields.values():
                other = by_label.setdefault(field.string, field)
                if other is field or MODULE not in (field._module, other._module):
                    continue
                with self.subTest(model=model_name, label=field.string):
                    self.fail(f"{other.name} and {field.name} of {model_name} share the label {field.string!r}")

    def test_developer_view_has_no_rights_inconsistency(self):
        view = self.env.ref(f"{MODULE}.goodahead_invoice_send_attempt_view_form_technical")
        arch = etree.fromstring(view.get_combined_arch())
        page = arch.xpath("//page[@name='technical']")[0]
        self.assertEqual(page.get("groups"), "base.group_no_one")
        with self.assertNoLogs(VIEW_LOGGER, "WARNING"):
            view._check_xml()
