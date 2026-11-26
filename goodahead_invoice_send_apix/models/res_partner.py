# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The customer's sending channels, its e-invoice address and its readiness.


The ERP's company-dependent ``invoice_sending_method`` gets the two APIX
channels; ``edicode`` and ``einvoice_operator_id`` (``l10n_fi_edicode``) are
tracked so a changed e-invoice address shows in the customer's history.

Readiness is worked out from stored details only — no APIX call: the
plain dicts the pure rules (``lib/readiness.py``) read are built here, the
invoice contact first and the commercial customer as fallback per field (the
e-invoice address and operator as one pair).
"""

from odoo import api, fields, models
from odoo.exceptions import AccessError

from odoo.addons.goodahead_einvoice_base.models.account_move import check_buyer_reference

from ..lib import readiness
from .account_move import READINESS_LEVELS, readiness_display

#: Customer channel -> invoice transmit method. Any other ERP value counts as
#: not set.
CHANNEL_TO_METHOD = {"apix": "einvoice", "apix_letter": "letter", "email": "email"}
#: Who uses the fix-it wizard.
BILLING_GROUP = "account.group_account_invoice"
FIX_ACTION = "goodahead_invoice_send_apix.goodahead_invoice_send_fix_action"
#: Transmit method -> what the contact receives.
RECEIVE_LABELS = {"einvoice": "e-invoices", "letter": "printed letters", "email": "email"}


class ResPartner(models.Model):
    _inherit = "res.partner"

    invoice_sending_method = fields.Selection(
        selection_add=[
            ("apix", "E-invoice via APIX"),
            ("apix_letter", "Printed letter via APIX"),
        ],
        # The ERP field has no default: on uninstall the channel is cleared
        # (counts as not set) rather than "set default".
        ondelete={"apix": "set null", "apix_letter": "set null"},
    )
    edicode = fields.Char(tracking=True)
    # finvoice content ADR-0006: shared by every company (not company-dependent).
    einvoice_standing_buyer_reference = fields.Char(
        string="Standing Buyer Reference",
        tracking=True,
        help="The buyer reference new invoices to this customer start with, such as an "
        "orderer's name or a cost centre (at most 70 characters). Changeable on each "
        "invoice while it is a draft.",
    )
    einvoice_operator_id = fields.Many2one(tracking=True)
    send_readiness = fields.Json(
        string="Sending readiness",
        compute="_compute_send_readiness",
        help="What an invoice to this contact still needs before it can be sent "
        "by its default transmit method in the current company.",
    )
    send_ready = fields.Boolean(string="Ready to send", compute="_compute_send_readiness")
    send_readiness_level = fields.Selection(
        READINESS_LEVELS, string="Readiness", compute="_compute_send_readiness_display"
    )
    send_readiness_html = fields.Html(
        string="Readiness details", compute="_compute_send_readiness_display", sanitize=True
    )
    einvoice_lookup_allowed = fields.Boolean(
        compute="_compute_einvoice_lookup_allowed",
        help="Whether the user may look up and change this contact's e-invoice address "
        "with the fix-it wizard (a billing user who may change the contact).",
    )

    @api.depends_context("uid")
    def _compute_einvoice_lookup_allowed(self):
        billing = self.env.user.has_group(BILLING_GROUP)
        for partner in self:
            record = partner._origin
            # an unsaved contact has nothing to save to yet
            partner.einvoice_lookup_allowed = (
                billing and bool(record.id) and record.has_access("write")
            )

    def action_apix_open_einvoice_lookup(self):
        """Open the fix-it wizard for this contact (partner mode): looked up
        with the APIX connection of the user's current company."""
        self.ensure_one()
        if not self.env.user.has_group(BILLING_GROUP):
            raise AccessError(self.env._("Only billing users can look up e-invoice addresses."))
        self.check_access("write")
        return _einvoice_lookup_action(self.env, {
            "default_target_partner_id": self.id,
            "default_lookup_company_id": self.env.company.id,
        })

    @api.depends_context("company")
    @api.depends(
        "invoice_sending_method", "edicode", "einvoice_operator_id", "name", "email",
        "street", "zip", "city", "country_id", "vat", "company_registry",
        "commercial_partner_id",
    )
    def _compute_send_readiness(self):
        """SCR-08: the contact's own channel (or e-invoice with an e-invoice
        address), without invoice-level items."""
        company = self.env.company
        enabled = self.env["goodahead.apix.connection"]._apix_sending_enabled(company)
        sender = self._apix_sender_values(company)
        for partner in self:
            if not partner.id:
                partner.send_readiness = False
                partner.send_ready = False
                continue
            result = readiness.evaluate(
                partner._apix_transmit_default(company),
                partner._apix_recipient_values(),
                sender,
                sending_enabled=enabled,
                lines=[],
                invoice_level=False,
            )
            partner.send_readiness = result
            partner.send_ready = result["ready"]

    @api.depends_context("company")
    @api.depends("send_readiness")
    def _compute_send_readiness_display(self):
        """SCR-08: "Ready to receive e-invoices", or what is missing."""
        company = self.env.company
        for partner in self:
            partner.send_readiness_level = False
            partner.send_readiness_html = False
            if not partner.send_readiness:
                continue
            what = self.env._(RECEIVE_LABELS[partner._apix_transmit_default(company)])  # pylint: disable=translation-not-lazy
            partner.send_readiness_level, partner.send_readiness_html = readiness_display(
                self.env, partner.send_readiness, company,
                ready=self.env._("Ready to receive %s", what),
                not_ready=self.env._("Not ready to receive %s", what),
            )

    # The standing buyer reference is customer data: written under the ERP's
    # contact rights like every other customer field, never through sudo
    # (spec §6.1, review F7). Billing users without those rights set the
    # buyer reference on the invoice itself.

    @api.constrains("einvoice_standing_buyer_reference")
    def _check_einvoice_standing_buyer_reference(self):
        for partner in self:
            check_buyer_reference(partner.einvoice_standing_buyer_reference)

    def _einvoice_standing_buyer_reference(self):
        """The buyer reference new invoices to this contact start with: its
        own standing value, else its commercial customer's."""
        self.ensure_one()
        return (self.einvoice_standing_buyer_reference
                or self.commercial_partner_id.einvoice_standing_buyer_reference or None)

    def _apix_transmit_default(self, company):
        """How an invoice to this contact goes out by default in ``company``
       : the contact's channel, else its commercial customer's; with
        neither set, e-invoice when one of them has an e-invoice address,
        else email."""
        self.ensure_one()
        contact = self.with_company(company)
        commercial = contact.commercial_partner_id
        for partner in (contact, commercial):
            method = CHANNEL_TO_METHOD.get(partner.invoice_sending_method)
            if method:
                return method
        if contact.edicode or commercial.edicode:
            return "einvoice"
        return "email"

    # ------------------------------------------------------------------
    # Plain values for the readiness rules
    # ------------------------------------------------------------------

    def _apix_recipient_values(self):
        """The recipient dict: each field from this contact, else from its
        commercial customer — except the e-invoice address and its operator,
        which come as a pair from the first of the two that has an address
       , so an address never goes with another's operator."""
        self.ensure_one()
        contact = self.sudo()
        commercial = contact.commercial_partner_id

        def pick(getter):
            return getter(contact) or getter(commercial) or ""

        einvoice = contact if contact.edicode else commercial
        operator = einvoice.einvoice_operator_id
        return {
            "name": pick(lambda p: p.name),
            "edicode": einvoice.edicode or "",
            "operator_identifier": operator.identifier or "",
            "operator_listed": bool(operator) and operator.active,
            "business_id": pick(lambda p: p.company_registry),
            "vat": pick(lambda p: p.vat),
            "street": pick(lambda p: p.street),
            "zip": pick(lambda p: p.zip),
            "city": pick(lambda p: p.city),
            "country_code": pick(lambda p: p.country_id.code),
            "email": pick(lambda p: p.email),
        }

    @api.model
    def _apix_sender_values(self, company, bank_account=None):
        """The sender dict of ``company``; the payment account is
        ``bank_account`` (the invoice's) or else the company's first one."""
        company = company.sudo()
        partner = company.partner_id
        bank = (bank_account or partner.bank_ids[:1]).sudo()
        if bank and not bank.active:
            bank = partner.bank_ids[:1]
        return {
            "name": company.name or "",
            "business_id": company.company_registry or company.vat or "",
            "edicode": partner.edicode or "",
            "iban": bank.sanitized_acc_number or "",
            "bic": bank.bank_id.bic or "",
            "street": company.street or "",
            "zip": company.zip or "",
            "city": company.city or "",
            "country_code": company.country_id.code or "",
        }


def _einvoice_lookup_action(env, context):
    """The fix-it wizard's dialog action for a contact or a company."""
    action = env["ir.actions.act_window"]._for_xml_id(FIX_ACTION)
    action["name"] = env._("Look up e-invoice address")
    action["context"] = context
    return action
