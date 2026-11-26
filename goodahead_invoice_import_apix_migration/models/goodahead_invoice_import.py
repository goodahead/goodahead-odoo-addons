# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The recognition asks the earlier connector first."""

from odoo import models


class GoodaheadInvoiceImport(models.Model):
    _inherit = "goodahead.invoice.import"

    def _apix_bill_from_earlier_receiver(self):
        """The bill the migration source recorded for this package
        (`_migration_<code>_find_bill`): the shadow classification and the
        live recognition take it before matching attachments and the
        duplicate rule."""
        move = super()._apix_bill_from_earlier_receiver()
        if move:
            return move
        record = self.sudo()
        return self.env["goodahead.apix.connection"]._migration_find_bill(
            record.company_id, record.apix_storage_id
        )
