# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The fix-it wizard (contract §4, SCR-07, Flow G).

Fixes the e-invoice address and operator of an invoice's **invoice
contact** without leaving the invoice or the mass sending wizard: look them
up at APIX (with the invoice company's connection), or enter them by hand,
then save them to the contact (tracked) and switch the invoice to e-invoice.

Nothing is saved until the Accountant saves; with several addresses found,
none is picked for them. Lookup and delivery-method errors are shown
in the wizard and never touch the connection's status or the sending pause.

Three modes, one target each:

- **invoice** (``move_id``): the invoice's contact, from the invoice or the
  mass sending wizard; Save also switches the invoice to e-invoice.
- **partner** (``target_partner_id``): a contact, from the contact form;
  the lookup uses the APIX connection of the user's current company
  (``lookup_company_id``). Save writes the pair to that contact only.
- **company** (``target_company_id``): the company's own e-invoice address
  and operator, from the company form, accounting administrators only; the
  lookup uses that company's connection.

The delivery-method check is about an invoice and exists in invoice mode
only. Every action first checks the user may change the target, before any
APIX call or save.
"""

import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from odoo.addons.goodahead_apix.lib.apix_client import ApixError

INVOICE_MODE = "invoice"
PARTNER_MODE = "partner"
COMPANY_MODE = "company"
#: The wizard's targets; exactly one is set.
TARGETS = ("move_id", "target_partner_id", "target_company_id")
ADMIN_GROUP = "account.group_account_manager"
BILLING_GROUP = "account.group_account_invoice"
#: A Finnish Business ID (Y-tunnus): seven digits, a hyphen, a check digit.
FINNISH_BUSINESS_ID = re.compile(r"^\d{7}-\d$")

TARGET_REQUIRED = "fix.target_required"
DELIVERY_METHOD_INVOICE_ONLY = "fix.delivery_method_invoice_only"
NOTHING_PICKED = "fix.nothing_picked"
ADDRESS_REQUIRED = "fix.address_required"
OPERATOR_NOT_LISTED = "fix.operator_not_listed"
LOOKUP_INPUT_REQUIRED = "fix.lookup_input_required"
NONE_FOUND = "fix.none_found"
LOOKUP_UNAVAILABLE = "fix.lookup_unavailable"
DELIVERY_METHOD_UNAVAILABLE = "fix.delivery_method_unavailable"
TRANSMIT_NOT_SWITCHED = "fix.transmit_not_switched"
FOUND = "fix.found"
DELIVERY_METHOD_SHOWN = "fix.delivery_method_shown"
SAVED = "fix.saved"

FIX = "goodahead.invoice.send.fix"


class FixError(UserError):
    """A validation of the fix-it wizard; ``code`` is one of the ``fix.*``
    codes of contract §4."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class GoodaheadInvoiceSendFix(models.TransientModel):
    _name = FIX
    _description = "Fix e-invoice details of a contact or a company"

    move_id = fields.Many2one("account.move", ondelete="cascade")
    target_partner_id = fields.Many2one("res.partner", string="Contact", ondelete="cascade")
    target_company_id = fields.Many2one("res.company", string="Company to fix", ondelete="cascade")
    lookup_company_id = fields.Many2one(
        "res.company", string="APIX connection of", ondelete="cascade",
        help="Partner mode: the company whose APIX connection looks the contact up "
        "(the user's current company when opened).",
    )
    mode = fields.Selection(
        [(INVOICE_MODE, "Invoice"), (PARTNER_MODE, "Contact"), (COMPANY_MODE, "Company")],
        compute="_compute_target",
    )
    company_id = fields.Many2one("res.company", compute="_compute_target")
    partner_id = fields.Many2one(
        "res.partner", string="Invoice contact", compute="_compute_target"
    )
    commercial_partner_id = fields.Many2one(related="partner_id.commercial_partner_id")
    return_to = fields.Selection(
        [("invoice", "Invoice"), ("mass_wizard", "Mass sending wizard")],
        default="invoice",
        required=True,
    )
    return_res_model = fields.Char()
    return_res_id = fields.Integer()
    notice = fields.Char(compute="_compute_notice")
    current_edicode = fields.Char(string="Current e-invoice address", compute="_compute_current")
    current_operator = fields.Char(string="Current operator", compute="_compute_current")
    current_from_commercial = fields.Boolean(compute="_compute_current")

    lookup_business_id = fields.Char(
        string="Business ID", default=lambda self: self._default_lookup()[0]
    )
    lookup_name = fields.Char(string="Name", default=lambda self: self._default_lookup()[1])
    lookup_state = fields.Selection([
        (FOUND, "Found"),
        (NONE_FOUND, "None found"),
        (LOOKUP_UNAVAILABLE, "Lookup unavailable"),
    ])
    lookup_message = fields.Char(readonly=True)
    result_ids = fields.One2many("goodahead.invoice.send.fix.result", "wizard_id",
                                 string="Found at APIX")
    picked_result_id = fields.Many2one("goodahead.invoice.send.fix.result")

    delivery_state = fields.Selection([
        (DELIVERY_METHOD_SHOWN, "Shown"),
        (DELIVERY_METHOD_UNAVAILABLE, "Unavailable"),
    ])
    delivery_message = fields.Char(readonly=True)

    manual_edicode = fields.Char(string="E-invoice address")
    manual_operator_id = fields.Many2one(
        "res.partner.operator.einvoice", string="Operator",
        help="An operator from the e-invoice operator list.",
    )
    outcome_code = fields.Char(readonly=True)

    # The Business ID of the picked APIX result: saved to the commercial
    # partner (company mode: the company) only where it has none; the VAT
    # number FI + its digits only when asked (``set_vat``, off by default).
    found_business_id = fields.Char(
        string="Business ID found", related="picked_result_id.business_id")
    registry_fill = fields.Boolean(compute="_compute_identity_offer")
    vat_proposal = fields.Char(string="VAT number", compute="_compute_identity_offer")
    vat_offer = fields.Boolean(compute="_compute_identity_offer")
    set_vat = fields.Boolean(
        string="Also set VAT number", default=False,
        help="Also save this VAT number, made from the Business ID APIX found, to the "
        "customer (or the company), which has none.",
    )

    @api.depends("picked_result_id", "mode", "partner_id", "target_company_id")
    def _compute_identity_offer(self):
        for wizard in self:
            target = wizard._identity_target().sudo()
            business_id = (wizard.picked_result_id.business_id or "").strip()
            wizard.registry_fill = bool(business_id) and not target.company_registry
            wizard.vat_proposal = _finnish_vat(business_id)
            wizard.vat_offer = bool(wizard.vat_proposal) and bool(target) and not target.vat

    def _identity_target(self):
        """Whose Business ID and VAT number a lookup result may fill: the
        company in company mode, else the contact's commercial partner."""
        self.ensure_one()
        if self.mode == COMPANY_MODE:
            return self.target_company_id
        return self.partner_id.commercial_partner_id

    def _identity_values(self):
        """The Business ID (where empty) and, when asked, the VAT number
        (where empty) from the picked result; nothing for entry by hand."""
        self.ensure_one()
        business_id = (self.picked_result_id.business_id or "").strip()
        if not business_id:
            return {}
        target = self._identity_target().sudo()
        values = {}
        if not target.company_registry:
            values["company_registry"] = business_id
        vat = _finnish_vat(business_id)
        if self.set_vat and vat and not target.vat:
            values["vat"] = vat
        return values

    @api.model
    def _default_lookup(self):
        context = self.env.context
        targets = self._browse_targets({
            name: context.get(f"default_{name}") for name in TARGETS
        })
        if not any(target.exists() for target in targets[:3]):
            return False, False
        return _target_lookup_values(*targets[:3])

    @api.model
    def _browse_targets(self, vals):
        """``(move, partner, company, lookup company)`` named in ``vals``."""
        return (
            self.env["account.move"].browse(vals.get("move_id")),
            self.env["res.partner"].browse(vals.get("target_partner_id")),
            self.env["res.company"].browse(vals.get("target_company_id")),
            self.env["res.company"].browse(vals.get("lookup_company_id")),
        )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._prepare_target_values(vals)
        # the user must be able to change the target (AC-54, §6.1): an RPC
        # call cannot open the wizard on another company's invoice, contact
        # or company, nor look up with a company the user is not in
        for vals in vals_list:
            _check_target_access(self.env, *self._browse_targets(vals))
        for vals in vals_list:
            if "lookup_business_id" in vals or "lookup_name" in vals:
                continue
            vals["lookup_business_id"], vals["lookup_name"] = _target_lookup_values(
                *self._browse_targets(vals)[:3]
            )
        return super().create(vals_list)

    @api.model
    def _prepare_target_values(self, vals):
        """Exactly one target, taken from ``vals`` or the context defaults
        (the dialog creates the wizard with them); partner mode looks up
        with the user's current company unless told otherwise."""
        context = self.env.context
        for name in (*TARGETS, "lookup_company_id"):
            if name not in vals and context.get(f"default_{name}"):
                vals[name] = context[f"default_{name}"]
        if sum(bool(vals.get(name)) for name in TARGETS) != 1:
            raise FixError(TARGET_REQUIRED, _(
                "Open the wizard from one invoice, one contact or one company."
            ))
        if vals.get("target_partner_id"):
            vals["lookup_company_id"] = vals.get("lookup_company_id") or self.env.company.id
        else:
            vals.pop("lookup_company_id", None)

    def _check_move_access(self):
        """Every action first: the user can still change the target (the
        invoice, the contact or the company), before any APIX call or save.
       """
        for wizard in self:
            _check_target_access(
                self.env, wizard.move_id, wizard.target_partner_id,
                wizard.target_company_id, wizard.lookup_company_id,
            )

    @api.depends("move_id", "target_partner_id", "target_company_id", "lookup_company_id")
    def _compute_target(self):
        for wizard in self:
            move = wizard.move_id.sudo()
            company = wizard.target_company_id.sudo()
            if move:
                wizard.mode = INVOICE_MODE
                wizard.company_id = move.company_id
                wizard.partner_id = move.partner_id
            elif company:
                wizard.mode = COMPANY_MODE
                wizard.company_id = company
                wizard.partner_id = company.partner_id
            else:
                wizard.mode = PARTNER_MODE
                wizard.company_id = wizard.lookup_company_id
                wizard.partner_id = wizard.target_partner_id

    @api.depends("mode", "partner_id", "company_id")
    def _compute_notice(self):
        for wizard in self:
            if wizard.mode == COMPANY_MODE:
                wizard.notice = _(
                    "These are the own e-invoice address and operator of %(company)s: where "
                    "it receives e-invoices and what its sent e-invoices give as the sender.",
                    company=wizard.company_id.name or "",
                )
                continue
            if wizard.mode == PARTNER_MODE:
                wizard.notice = _(
                    "These details belong to %(contact)s, not to a company, and apply to every "
                    "company that invoices it. The lookup uses the APIX connection of "
                    "%(company)s.",
                    contact=wizard.partner_id.display_name or "",
                    company=wizard.company_id.name or "",
                )
                continue
            wizard.notice = _(
                "These details belong to %(contact)s (invoice contact), not to a company, "
                "and apply to every company that invoices it.",
                contact=wizard.partner_id.display_name or "",
            )

    @api.depends("partner_id")
    def _compute_current(self):
        for wizard in self:
            contact = wizard.partner_id.sudo()
            commercial = contact.commercial_partner_id
            own = bool(contact.edicode)
            source = contact if own else commercial
            wizard.current_edicode = source.edicode or False
            wizard.current_operator = source.einvoice_operator_id.display_name or False
            wizard.current_from_commercial = not own and bool(commercial.edicode)

    # ------------------------------------------------------------------
    # Look up at APIX
    # ------------------------------------------------------------------

    def action_lookup(self):
        self.ensure_one()
        self._check_move_access()
        business_id = (self.lookup_business_id or "").strip()
        name = (self.lookup_name or "").strip()
        if not business_id and not name:
            raise FixError(LOOKUP_INPUT_REQUIRED, _("Enter a Business ID or a name."))
        self.write({"result_ids": [(5, 0, 0)], "picked_result_id": False})
        connection = self._working_connection()
        if not connection:
            return self._lookup_unavailable(self._no_connection_reason())
        try:
            found = connection._client().address_query(
                connection.transfer_id, connection.transfer_key,
                business_id=business_id or None, name=None if business_id else name,
            )
        except ApixError as exc:
            return self._lookup_unavailable(_reason(exc))
        if not found:
            self.write({
                "lookup_state": NONE_FOUND,
                "lookup_message": _(
                    "APIX found no e-invoice address for %s — enter it by hand.",
                    business_id or name,
                ),
            })
            return self._reopen()
        Result = self.env["goodahead.invoice.send.fix.result"]
        Result.create([self._result_values(row) for row in found])
        self.write({
            "lookup_state": FOUND,
            "lookup_message": _("APIX found %s e-invoice address(es). Pick one to use.", len(found)),
        })
        return self._reopen()

    def _result_values(self, row):
        operator = self._match_operator(row.get("operator") or "")
        return {
            "wizard_id": self.id,
            "receiver_name": row.get("receiver_name") or "",
            "business_id": row.get("business_id") or "",
            "edicode": row.get("edicode") or "",
            "operator_identifier": row.get("operator") or "",
            "operator_name": operator.name or row.get("operator") or "",
            "operator_id": operator.id,
        }

    def _match_operator(self, value):
        """The listed operator with this identifier, else with this name."""
        Operator = self.env["res.partner.operator.einvoice"]
        if not value:
            return Operator
        return (
            Operator.search([("identifier", "=", value)], limit=1)
            or Operator.search([("name", "=ilike", value)], limit=1)
        )

    def _lookup_unavailable(self, reason):
        self.write({
            "lookup_state": LOOKUP_UNAVAILABLE,
            "lookup_message": _(
                "Lookup unavailable: %s. You can still enter the details by hand.", reason
            ),
        })
        return self._reopen()

    def _pick(self, result):
        self.ensure_one()
        self._check_move_access()
        self.picked_result_id = result
        return self._reopen()

    # ------------------------------------------------------------------
    # Delivery method (AC-20, Flow G) — on request only, saves nothing
    # ------------------------------------------------------------------

    def action_check_delivery_method(self):
        self.ensure_one()
        self._check_move_access()
        if self.mode != INVOICE_MODE:
            # how APIX would deliver is a question about an invoice
            raise FixError(DELIVERY_METHOD_INVOICE_ONLY, _(
                "The delivery method is checked for an invoice: use Fix e-invoice address "
                "on the invoice."
            ))
        connection = self._working_connection()
        if not connection:
            return self._delivery_unavailable(self._no_connection_reason())
        try:
            answer = connection._client().delivery_method(
                connection.transfer_id, connection.transfer_key, [self._delivery_document()]
            )
        except ApixError as exc:
            return self._delivery_unavailable(_reason(exc))
        if not answer:
            return self._delivery_unavailable(_("APIX gave no delivery method"))
        channel = answer[0]
        self.write({
            "delivery_state": DELIVERY_METHOD_SHOWN,
            "delivery_message": _(
                "APIX would deliver this invoice as %(channel)s — cost %(credits)s credit(s).",
                channel=channel.get("channel_name") or _("unknown"),
                credits=channel.get("cost_in_credits") if channel.get("cost_in_credits") is not None
                else "?",
            ),
        })
        return self._reopen()

    def _delivery_document(self):
        move = self.move_id.sudo()
        recipient = move._apix_recipient_values()
        sender = self.env["res.partner"]._apix_sender_values(move.company_id)
        edicode = self.picked_result_id.edicode or self.manual_edicode or recipient["edicode"]
        return {
            "document_id": move.name,
            "sender_name": sender["name"],
            "sender_business_id": sender["business_id"],
            "receiver_name": recipient["name"],
            "receiver_country_code": recipient["country_code"],
            "receiver_business_id": recipient["business_id"],
            "receiver_edicode": edicode,
        }

    def _delivery_unavailable(self, reason):
        self.write({
            "delivery_state": DELIVERY_METHOD_UNAVAILABLE,
            "delivery_message": _("Delivery check unavailable: %s.", reason),
        })
        return self._reopen()

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def action_save(self):
        self.ensure_one()
        self._check_move_access()
        edicode, operator = self._chosen_details()
        values = {"edicode": edicode, "einvoice_operator_id": operator.id}
        identity = self._identity_values()
        if self.mode == PARTNER_MODE:
            # the user may change this contact (checked above): as the user,
            # and the Business ID / VAT number as the user too (its
            # commercial partner's rights apply)
            self.target_partner_id.write(values)
            if identity:
                self._identity_target().write(identity)
            return self._saved_notification(SAVED, _(
                "E-invoice details saved to %s.", self.partner_id.display_name
            ), "success")
        if self.mode == COMPANY_MODE:
            # an accounting administrator who may change the company (checked
            # above); the pair lives on the company's partner, which the
            # administrator need not be allowed to edit otherwise
            self.target_company_id.sudo().write(dict(values, **identity))
            return self._saved_notification(SAVED, _(
                "E-invoice details of %s saved.", self.company_id.name
            ), "success")
        contact = self.partner_id
        # Accountants fix customers' e-invoice details here
        # without the right to edit contacts otherwise: these two validated
        # fields only, tracked under the Accountant's name.
        contact.sudo().write(values)
        if identity:
            # the same Accountant's fix: only empty fields, only from APIX
            self._identity_target().sudo().write(identity)
        return self._saved_notification(*self._switch_transmit_method())

    def _saved_notification(self, code, message, kind):
        """The notification after Save; ``kind`` is its type."""
        self.outcome_code = code
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": kind,
                "message": message,
                "sticky": False,
                "next": self._return_action(),
            },
        }

    def _chosen_details(self):
        """``(edicode, operator)`` of the picked result or the entry by
        hand; raises the ``fix.*`` validation when incomplete."""
        result = self.picked_result_id
        if result:
            operator = result.operator_id or self.manual_operator_id
            self._check_operator(operator)
            return result.edicode, operator
        if not self.manual_edicode and not self.manual_operator_id:
            raise FixError(NOTHING_PICKED, _("Pick one of the addresses or enter one by hand."))
        if not (self.manual_edicode or "").strip():
            raise FixError(ADDRESS_REQUIRED, _(
                "An e-invoice needs an address and an operator from the operator list."
            ))
        self._check_operator(self.manual_operator_id)
        return self.manual_edicode.strip(), self.manual_operator_id

    def _check_operator(self, operator):
        if operator and operator.active:
            return
        raise FixError(OPERATOR_NOT_LISTED, _(
            "An e-invoice needs an address and an operator from the operator list: "
            "choose the operator from the list."
        ))

    def _switch_transmit_method(self):
        """Switch the invoice to e-invoice unless a send of it is in
        progress; ``(code, message, notification type)``."""
        move = self.move_id
        if move.transmit_method == "einvoice":
            return SAVED, _("E-invoice details saved to %s.", self.partner_id.display_name), "success"
        holder = move._apix_holder()
        if holder:
            return TRANSMIT_NOT_SWITCHED, _(
                "E-invoice details saved to %(contact)s. A send is in progress — the transmit "
                "method was left unchanged (%(holder)s).",
                contact=self.partner_id.display_name,
                holder=holder.message(self.env),
            ), "warning"
        move.write({"transmit_method": "einvoice"})
        return SAVED, _(
            "E-invoice details saved to %s; the invoice now goes as an e-invoice.",
            self.partner_id.display_name,
        ), "success"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _working_connection(self):
        """The invoice company's connection when it can call APIX, else an
        empty recordset (AC-25: never the active company's)."""
        connection = self.env["goodahead.apix.connection"]._apix_for_company(self.company_id)
        if connection.status != "working" or not connection.transfer_id or not connection.transfer_key:
            return connection.browse()
        return connection

    def _no_connection_reason(self):
        return _("%s has no working APIX connection", self.company_id.name)

    def _reopen(self):
        return {
            "type": "ir.actions.act_window",
            "res_model": FIX,
            "res_id": self.id,
            "view_mode": "form",
            "views": [[False, "form"]],
            "target": "new",
        }

    def _return_action(self):
        if self.return_to != "mass_wizard" or not self.return_res_model:
            return {"type": "ir.actions.act_window_close"}
        caller = self.env[self.return_res_model].browse(self.return_res_id).exists()
        if hasattr(caller, "_apix_after_fix"):
            # the mass wizard re-classifies the fixed invoice (Flow G)
            caller._apix_after_fix(self.move_id)
        return {
            "type": "ir.actions.act_window",
            "res_model": self.return_res_model,
            "res_id": self.return_res_id,
            "view_mode": "form",
            "views": [[False, "form"]],
            "target": "new",
        }


class GoodaheadInvoiceSendFixResult(models.TransientModel):
    _name = "goodahead.invoice.send.fix.result"
    _description = "E-invoice address found at APIX"
    _order = "id"

    wizard_id = fields.Many2one(FIX, required=True, ondelete="cascade")
    receiver_name = fields.Char(readonly=True)
    business_id = fields.Char(string="Business ID", readonly=True)
    edicode = fields.Char(string="E-invoice address", readonly=True)
    operator_identifier = fields.Char(readonly=True)
    operator_name = fields.Char(string="Operator", readonly=True)
    operator_id = fields.Many2one("res.partner.operator.einvoice", string="Listed operator", readonly=True)
    operator_listed = fields.Boolean(compute="_compute_operator_listed")
    operator_note = fields.Char(string="Note", compute="_compute_operator_listed")
    picked = fields.Boolean(compute="_compute_picked")

    @api.depends("operator_id")
    def _compute_operator_listed(self):
        for result in self:
            result.operator_listed = bool(result.operator_id)
            result.operator_note = not result.operator_id and _(
                "Operator not on the list — choose the operator below"
            )

    @api.depends("wizard_id.picked_result_id")
    def _compute_picked(self):
        for result in self:
            result.picked = result.wizard_id.picked_result_id == result

    def action_use(self):
        self.ensure_one()
        return self.wizard_id._pick(self)


def _finnish_vat(business_id):
    """``FI`` + the digits of a Finnish Business ID (``1234567-8`` ->
    ``FI12345678``), else ``False``."""
    if not FINNISH_BUSINESS_ID.match(business_id or ""):
        return False
    return "FI" + business_id.replace("-", "")


def _lookup_values(contact):
    """``(Business ID, name)`` to look ``contact`` up with: the contact's
    Business ID, else its commercial customer's; the organisation's name."""
    if not contact:
        return False, False
    commercial = contact.commercial_partner_id
    business_id = contact.company_registry or commercial.company_registry or False
    return business_id, commercial.name or contact.name or False


def _target_lookup_values(move, partner, company):
    """``(Business ID, name)`` to look the wizard's target up with."""
    if move:
        return _lookup_values(move.partner_id)
    if partner:
        return _lookup_values(partner)
    if company:
        return company.company_registry or False, company.name or False
    return False, False


def _check_target_access(env, move, partner, company, lookup_company):
    """Raise ``AccessError`` unless the user may change the wizard's target:
    the invoice; the contact, looking up with a company the user is in; or,
    for an accounting administrator, a company the user is in. Superuser
    code passes, as with every access check."""
    if move:
        move.check_access("write")
        return
    if env.su:
        return
    if partner:
        partner.check_access("write")
        _check_company_allowed(env, lookup_company)
        return
    if not env.user.has_group(ADMIN_GROUP):
        raise AccessError(env._(
            "Only an accounting administrator can change the company's e-invoice address."
        ))
    _check_company_allowed(env, company)
    company.check_access("write")


def _check_company_allowed(env, company):
    if company and company.id in env.user.company_ids.ids:
        return
    raise AccessError(env._(
        "You are not allowed to work in the company %s.", company.sudo().name or ""
    ))


def _reason(exc):
    """APIX's reason in plain words, with its status code when it has one."""
    message = getattr(exc, "message", None) or str(exc)
    if getattr(exc, "status_code", None):
        return f"{message} (code {exc.status_code})"
    return message
