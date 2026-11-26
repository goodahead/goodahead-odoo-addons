# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from odoo import api, models


class ResPartnerBank(models.Model):
    _inherit = "res.partner.bank"

    @api.model
    def _ai_bank_lookup_only(self):
        """True while an import runs under a policy whose bank accounts are
        looked up only (`MatchingPolicy.bank_account_lookup_only`)."""
        policy = self.env["res.partner"]._ai_matching_policy()
        return bool(policy.bank_account_lookup_only)

    def _find_or_create_bank_account(self, account_number, partner, company, **kwargs):
        """🔴 A STRICT IMPORT NEVER CREATES A BANK ACCOUNT.

        The ERP's e-invoice decoder records the account printed on the
        document on the vendor (and makes it the bill's account) when the
        vendor does not hold it yet. Invoice fraud is precisely a changed bank
        account, so under a policy with ``bank_account_lookup_only`` (every
        structured source: APIX, Finvoice by upload or mail) the account is
        only LOOKED UP on the vendor: an account the vendor already has is
        used, anything else is left to the Accountant as a sensitive vendor
        data suggestion — or, when it is registered to another partner, only
        named in the chatter (`account.move._ai_sensitive_suggestions`).
        """
        if not self._ai_bank_lookup_only():
            return super()._find_or_create_bank_account(
                account_number, partner, company, **kwargs
            )
        Bank = self.env["res.partner.bank"]
        if not account_number or not partner:
            return Bank
        found = Bank.sudo().with_context(active_test=False).search(
            [
                ("acc_number", "=", account_number),
                ("partner_id", "child_of", partner.commercial_partner_id.id),
            ]
        )
        return found.filtered_domain(
            [*Bank._check_company_domain(company), ("active", "=", True)]
        ).sorted(lambda bank: bank.partner_id != partner).sudo(False)[:1]
