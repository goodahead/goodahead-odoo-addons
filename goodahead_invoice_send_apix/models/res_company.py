# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The company's own e-invoice address lookup (company form).

Opens the fix-it wizard in company mode for the company's own e-invoice
address and operator (``l10n_fi_edicode``), looked up with that company's
APIX connection. Accounting administrators of the company only.
"""

from odoo import models
from odoo.exceptions import AccessError

from .apix_connection import ADMIN_GROUP
from .res_partner import _einvoice_lookup_action


class ResCompany(models.Model):
    _inherit = "res.company"

    def action_apix_open_einvoice_lookup(self):
        """Open the fix-it wizard for this company's own e-invoice address."""
        self.ensure_one()
        if not self.env.user.has_group(ADMIN_GROUP):
            raise AccessError(self.env._(
                "Only an accounting administrator can change the company's e-invoice address."
            ))
        if self.id not in self.env.user.company_ids.ids:
            raise AccessError(self.env._(
                "You are not allowed to work in the company %s.", self.sudo().name
            ))
        self.check_access("write")
        return _einvoice_lookup_action(self.env, {"default_target_company_id": self.id})
