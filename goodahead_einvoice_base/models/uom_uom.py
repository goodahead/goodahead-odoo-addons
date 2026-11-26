# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The one UN/ECE Rec 20 code of a unit, for both directions (send ADR-0013).

Sending writes ``rec20_code`` and the readable name on every invoice row
(``_einvoice_outgoing``); receiving reads an incoming code as the unit that
carries it (``_einvoice_unit_for_code``), falling back to the company's
default unit with a note on the bill. Standard units are filled at install
(``hooks.fill_unit_codes``). Only accounting administrators change a code or
the default unit.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..lib.finvoice import units

ADMIN_GROUP = "account.group_account_manager"
UNITS_NOT_ALLOWED = "units.not_allowed"
UNITS_INVALID_CODE = "units.invalid_code"
UNIT_NAME_MAX = 14
UNIT_CODES_VIEW = "goodahead_einvoice_base.uom_rec20_view_list"


class UnitsAccessError(AccessError):
    """A non-administrator changed a unit code or the default unit."""

    code = UNITS_NOT_ALLOWED


class UnitsValidationError(ValidationError):
    """A unit code that is not 2-3 upper-case letters or digits."""

    code = UNITS_INVALID_CODE


def check_units_admin(env):
    """Raise ``units.not_allowed`` unless the user is an accounting
    administrator (or the code runs as superuser: install, upgrade)."""
    if env.su or env.user.has_group(ADMIN_GROUP):
        return
    raise UnitsAccessError(
        _("Only accounting administrators can change unit codes and the default unit.")
    )


class UomUom(models.Model):
    _inherit = "uom.uom"

    rec20_code = fields.Char(
        string="Unit code (UN/ECE Rec 20)",
        help="The UN/ECE Recommendation 20 code of the unit, written on e-invoice rows "
        "and used to read the unit of incoming e-invoices. Standard units are filled "
        "at install; only accounting administrators change it.",
    )
    einvoice_readable_name = fields.Char(
        string="Name (fi)",
        compute="_compute_einvoice_readable_name",
        help="The readable unit name e-invoice rows carry next to the code: the "
        "standard Finnish name of the code, else the unit's own name.",
    )

    @api.depends("rec20_code", "name")
    def _compute_einvoice_readable_name(self):
        for unit in self:
            unit.einvoice_readable_name = unit._einvoice_outgoing()[1]

    @api.model
    def _get_view_cache_key(self, view_id=None, view_type="form", **options):
        key = super()._get_view_cache_key(view_id, view_type, **options)
        return key + (self.env.user.has_group(ADMIN_GROUP),)

    @api.model
    def _get_view(self, view_id=None, view_type="form", **options):
        """SCR-15 is editable for accounting administrators only: anyone else
        gets the unit codes list read-only."""
        arch, view = super()._get_view(view_id, view_type, **options)
        if view.xml_id != UNIT_CODES_VIEW or self.env.user.has_group(ADMIN_GROUP):
            return arch, view
        arch.attrib.pop("editable", None)
        arch.set("edit", "0")
        return arch, view

    def action_einvoice_default_unit(self):
        """SCR-15: open the current company's default unit for unmapped
        incoming codes in a dialog (administrators only, see the list view).

        A list header button: the web client calls it on the selected units
        (often none), so it is a record method that ignores the selection,
        not an ``api.model`` one."""
        check_units_admin(self.env)
        view = self.env.ref("goodahead_einvoice_base.res_company_view_form_einvoice_default_uom")
        return {
            "type": "ir.actions.act_window",
            "name": _("Default unit for unmapped incoming codes"),
            "res_model": "res.company",
            "res_id": self.env.company.id,
            "views": [[view.id, "form"]],
            "target": "new",
        }

    @api.constrains("rec20_code")
    def _check_rec20_code(self):
        for unit in self:
            if unit.rec20_code and not units.is_valid_rec20(unit.rec20_code):
                raise UnitsValidationError(
                    _("A UN/ECE Rec 20 code has 2–3 upper-case letters or digits.")
                )

    @api.model_create_multi
    def create(self, vals_list):
        if any(vals.get("rec20_code") for vals in vals_list):
            check_units_admin(self.env)
        return super().create(vals_list)

    def write(self, vals):
        if "rec20_code" not in vals:
            return super().write(vals)
        check_units_admin(self.env)
        # The unit model is writable by system administrators only; an
        # accounting administrator changes this one field through sudo.
        own = {"rec20_code": vals["rec20_code"]}
        rest = {key: value for key, value in vals.items() if key != "rec20_code"}
        result = super(UomUom, self.sudo()).write(own)
        if rest:
            result = super().write(rest)
        return result

    def _einvoice_outgoing(self):
        """``(code or None, readable name)`` for an invoice row's unit.

        The readable name is the shipped table's Finnish name of the code,
        else the unit's own name, cut to the 14 characters Finvoice allows.
        No unit gives ``(None, "")``.
        """
        if not self:
            return None, ""
        self.ensure_one()
        code = self.rec20_code or None
        name = units.readable_name(code) or self.name or ""
        return code, name[:UNIT_NAME_MAX].strip()

    @api.model
    def _einvoice_unit_for_code(self, code, company):
        """``(unit or empty, note or None)`` for an incoming Rec 20 code.

        The active units with that code, a shipped standard unit preferred
        when several share it. A code no unit carries gives the company's
        default unit (or no unit) and a note for the bill - never a
        guessed unit. No code at all gives no unit and no note.
        """
        Uom = self.env["uom.uom"]
        if not code:
            return Uom, None
        found = Uom.search([("rec20_code", "=", code)], order="id")
        if found:
            return self._einvoice_prefer_shipped(found, code), None
        default = company.einvoice_default_uom_id
        note = _(
            "Unit code %(code)s is not mapped — %(unit)s used.",
            code=code,
            unit=default.name if default else _("no unit"),
        )
        return default, note

    @api.model
    def _einvoice_prefer_shipped(self, found, code):
        for row in units.TABLE:
            if row.code != code or not row.xmlid:
                continue
            shipped = self.env.ref(row.xmlid, raise_if_not_found=False)
            if shipped and shipped in found:
                return shipped
        return found[0]
