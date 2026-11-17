# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Systray counts delivered through `lazy_session_info`.

`lazy_session_info` is fetched once per session after the client is ready (and `account`
already overrides it), so this adds no boot latency and no extra request. The badge is a
snapshot; each row opens the live list.
"""

from odoo import api, models


class IrHttp(models.AbstractModel):
    _inherit = 'ir.http'

    @api.model
    def lazy_session_info(self):
        result = super().lazy_session_info()

        # Also runs for website visitors, who cannot read `account.move`; an AccessError here
        # would break the client boot, so do not rely on the group check alone.
        if not self.env.user._is_internal():
            return result

        counts = self.env['account.move']._bill_approval_systray_counts()
        if counts:
            # The key is absent for users holding neither level; the component hides itself
            # when it is missing.
            result['goodahead_bill_approval'] = counts
        return result
