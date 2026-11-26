# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The VAT settings of a tax (finvoice content ADR-0002 – ADR-0004).

The set VAT category and exemption reason code are the ERP's own e-invoicing
fields of the tax (``account_edi_ubl_cii``), read through
``_einvoice_set_category`` — the one version-specific accessor. The default
category is worked out when shown and when a message is built, never stored.
The VAT reason is this add-on's own text; empty means the
category's default wording. Every change is tracked; sales taxes are
checked for consistency.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from odoo.addons.goodahead_einvoice_base.lib.finvoice import vat

from .account_move import search_values

UNCLASSIFIED = "unclassified"
CATEGORY_SELECTION = [
    ("S", "Standard rate"),
    ("Z", "Zero rated"),
    ("E", "Exempt"),
    ("AE", "Reverse charge"),
    ("K", "Intra-EU supply"),
    ("G", "Export outside the EU"),
    ("O", "Outside the scope of VAT"),
    ("L", "L - Canary Islands general indirect tax"),
    ("M", "M - Ceuta and Melilla"),
    ("B", "B - Transferred (VAT), Italy"),
    (UNCLASSIFIED, "Unclassified"),
]
#: Exemption reason codes Odoo 19.0 lists and 18.0 does not (18.0 already
#: has VATEX-EU-AE, -IC, -G, -O, -D, -F, -I, -J and two French codes, under
#: keys with underscores: ``_einvoice_set_exemption_code`` sends the code).
EXEMPTION_CODES_FROM_19 = [
    ('VATEX-EU-135-1', 'VATEX-EU-135-1 - Exempt based on article 135, section 1 of Council Directive 2006/112/EC'),
    ('VATEX-EU-144', 'VATEX-EU-144 - Exempt based on article 144 of Council Directive 2006/112/EC'),
    ('VATEX-EU-146-1E', 'VATEX-EU-146-1E - Exempt based on article 146 section 1 (e) of Council Directive 2006/112/EC'),
    ('VATEX-EU-153', 'VATEX-EU-153 - Exempt based on article 153 of Council Directive 2006/112/EC'),
    ('VATEX-EU-159', 'VATEX-EU-159 - Exempt based on article 159 of Council Directive 2006/112/EC'),
    ('VATEX-FR-CGI261-1', 'VATEX-FR-CGI261-1 - Exempt based on 1 of article 261 of the Code Général des Impôts (CGI ; General tax code)'),
    ('VATEX-FR-CGI261-2', 'VATEX-FR-CGI261-2 - Exempt based on 2 of article 261 of the Code Général des Impôts (CGI ; General tax code)'),
    ('VATEX-FR-CGI261-3', 'VATEX-FR-CGI261-3 - Exempt based on 3 of article 261 of the Code Général des Impôts (CGI ; General tax code)'),
    ('VATEX-FR-CGI261-4', 'VATEX-FR-CGI261-4 - Exempt based on 4 of article 261 of the Code Général des Impôts (CGI ; General tax code)'),
    ('VATEX-FR-CGI261-5', 'VATEX-FR-CGI261-5 - Exempt based on 5 of article 261 of the Code Général des Impôts (CGI ; General tax code)'),
    ('VATEX-FR-CGI261-7', 'VATEX-FR-CGI261-7 - Exempt based on 7 of article 261 of the Code Général des Impôts (CGI ; General tax code)'),
    ('VATEX-FR-CGI261-8', 'VATEX-FR-CGI261-8 - Exempt based on 8 of article 261 of the Code Général des Impôts (CGI ; General tax code)'),
    ('VATEX-FR-CGI261A', 'VATEX-FR-CGI261A - Exempt based on article 261 A of the Code Général des Impôts (CGI ; General tax code)'),
    ('VATEX-FR-CGI261B', 'VATEX-FR-CGI261B - Exempt based on article 261 B of the Code Général des Impôts (CGI ; General tax code)'),
    ('VATEX-FR-CGI261C-1', 'VATEX-FR-CGI261C-1 - Exempt based on 1° of article 261 C of the Code Général des Impôts (CGI ; General tax code)'),
    ('VATEX-FR-CGI261C-2', 'VATEX-FR-CGI261C-2 - Exempt based on 2° of article 261 C of the Code Général des Impôts (CGI ; General tax code)'),
    ('VATEX-FR-CGI261C-3', 'VATEX-FR-CGI261C-3 - Exempt based on 3° of article 261 C of the Code Général des Impôts (CGI ; General tax code)'),
    ('VATEX-FR-CGI261D-1', 'VATEX-FR-CGI261D-1 - Exempt based on 1° of article 261 D of the Code Général des Impôts (CGI ; General tax code)'),
    ('VATEX-FR-CGI261D-1BIS', 'VATEX-FR-CGI261D-1BIS - Exempt based on 1°bis of article 261 D of the Code Général des Impôts (CGI ; General tax code)'),
    ('VATEX-FR-CGI261D-2', 'VATEX-FR-CGI261D-2 - Exempt based on 2° of article 261 D of the Code Général des Impôts (CGI ; General tax code)'),
    ('VATEX-FR-CGI261D-3', 'VATEX-FR-CGI261D-3 - Exempt based on 3° of article 261 D of the Code Général des Impôts (CGI ; General tax code) Exonération de TVA - Article 261 D-3° du Code Général des Impôts'),
    ('VATEX-FR-CGI261D-4', 'VATEX-FR-CGI261D-4 - Exempt based on 4° of article 261 D of the Code Général des Impôts (CGI ; General tax code)'),
    ('VATEX-FR-CGI261E-1', 'VATEX-FR-CGI261E-1 - Exempt based on 1° of article 261 E of the Code Général des Impôts (CGI ; General tax code)'),
    ('VATEX-FR-CGI261E-2', 'VATEX-FR-CGI261E-2 - Exempt based on 2° of article 261 E of the Code Général des Impôts (CGI ; General tax code)'),
    ('VATEX-FR-CGI277A', 'VATEX-FR-CGI277A - Exempt based on article 277 A of the Code Général des Impôts (CGI ; General tax code)'),
    ('VATEX-FR-CGI275', 'VATEX-FR-CGI275 - Exempt based on article 275 of the Code Général des Impôts (CGI ; General tax code)'),
    ('VATEX-FR-298SEXDECIESA', 'VATEX-FR-298SEXDECIESA - Exempt based on article 298 sexdecies A of the Code Général des Impôts (CGI ; General tax code)'),
    ('VATEX-FR-CGI295', 'VATEX-FR-CGI295 - Exempt based on article 295 of the Code Général des Impôts (CGI ; General tax code)'),
    ('VATEX-FR-AE', 'VATEX-FR-AE - Exempt based on 2 of article 283 of the Code Général des Impôts (CGI ; General tax code)'),
    ('VATEX-FR-F', 'VATEX-FR-F - Second-hand sales'),
    ('VATEX-FR-I', 'VATEX-FR-I - Sales of works of art'),
    ('VATEX-FR-J', 'VATEX-FR-J - Sales of antiques'),
]
VAT_SETTING_FIELDS = (
    "ubl_cii_tax_category_code", "ubl_cii_tax_exemption_reason_code", "einvoice_vat_reason",
)
CHECKED_FIELDS = (
    "amount", "amount_type", "type_tax_use", "ubl_cii_tax_category_code",
    "ubl_cii_tax_exemption_reason_code", "einvoice_vat_reason",
)


class AccountTax(models.Model):
    _inherit = "account.tax"

    # ADR-0002: the ERP's own fields, redefined only to track every change.
    # Odoo 18.0: they come from `account_edi_ubl_cii_tax_extension`; its
    # code list lacks codes 19.0 offers, added here with their 19.0 keys.
    ubl_cii_tax_category_code = fields.Selection(tracking=True)
    ubl_cii_tax_exemption_reason_code = fields.Selection(
        selection_add=EXEMPTION_CODES_FROM_19, tracking=True,
    )

    einvoice_vat_reason = fields.Text(
        string="VAT Reason",
        tracking=True,
        help="Why no VAT is charged, at most 3 lines of 70 characters, each line one "
        "reason text in the e-invoice. Empty: the VAT category's default wording.",
    )
    einvoice_vat_category = fields.Selection(
        CATEGORY_SELECTION,
        string="E-invoice VAT Category",
        compute="_compute_einvoice_vat_category",
        search="_search_einvoice_vat_category",
        help="The VAT category this tax is sent with: the one set on the tax, else "
        "standard rate above 0 %, else the one category its VAT report lines name.",
    )
    einvoice_vat_category_origin = fields.Selection(
        [(vat.ORIGIN_SET, "Set"), (vat.ORIGIN_DEFAULT, "Default"),
         (vat.ORIGIN_UNCLASSIFIED, "Unclassified")],
        string="Category Origin",
        compute="_compute_einvoice_vat_category",
    )
    einvoice_vat_reason_default = fields.Char(
        string="Default VAT Reason",
        compute="_compute_einvoice_vat_category",
    )

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def _einvoice_set_category(self):
        """The set VAT category (18.0: the e-invoicing category of
        ``account_edi_ubl_cii_tax_extension``, the same field as 19.0's)."""
        self.ensure_one()
        return self.ubl_cii_tax_category_code or None

    def _einvoice_set_exemption_code(self):
        """The set exemption reason code as sent (CEF code list). Odoo 18.0
        keys some codes with underscores (``VATEX_EU_AE``, ``VATEX_FR-CNWVAT``):
        the code itself uses hyphens only."""
        self.ensure_one()
        code = self.ubl_cii_tax_exemption_reason_code
        return code.replace("_", "-") if code else None

    def _einvoice_report_formulas(self):
        """``(country code, formulas)`` of the VAT report lines the tax's
        invoice-side base repartition tags feed (a tag's name is its report
        expression's formula without the sign)."""
        self.ensure_one()
        lines = self.invoice_repartition_line_ids.filtered(
            lambda line: line.repartition_type == "base")
        tags = lines.tag_ids
        country = self.country_id.code or (tags[:1].country_id.code or None)
        return country, {name.lstrip("+-") for name in tags.mapped("name") if name}

    def _einvoice_category(self):
        """``(category | None, origin)``."""
        self.ensure_one()
        country, formulas = self._einvoice_report_formulas()
        return vat.effective_category(
            self._einvoice_set_category(), self._einvoice_rate(), country, formulas)

    def _einvoice_rate(self):
        return self.amount if self.amount_type == "percent" else 0.0

    def _einvoice_vat(self):
        """``{category, origin, reason_lines, exemption_code}`` a send uses."""
        self.ensure_one()
        category, origin = self._einvoice_category()
        code = self._einvoice_set_exemption_code() or vat.DEFAULT_EXEMPTION_CODES.get(category)
        if category in (None, vat.STANDARD):
            code = None
        return {
            "category": category,
            "origin": origin,
            "reason_lines": vat.reason_lines(self.einvoice_vat_reason, category),
            "exemption_code": code,
        }

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------

    @api.depends("amount", "amount_type", "ubl_cii_tax_category_code", "country_id",
                 "invoice_repartition_line_ids.tag_ids", "einvoice_vat_reason")
    def _compute_einvoice_vat_category(self):
        for tax in self:
            category, origin = tax._einvoice_category()
            tax.einvoice_vat_category = category or UNCLASSIFIED
            tax.einvoice_vat_category_origin = origin
            tax.einvoice_vat_reason_default = vat.default_reason_text(category)

    def _search_einvoice_vat_category(self, operator, value):
        # Odoo 18.0 passes the operator as written (19.0: `in` only)
        wanted = search_values(operator, value, [key for key, _label in CATEGORY_SELECTION])
        taxes = self.with_context(active_test=False).search([])
        return [("id", "in", taxes.filtered(
            lambda tax: tax.einvoice_vat_category in wanted).ids)]

    # ------------------------------------------------------------------
    # AC-03 — every change of the VAT settings is recorded
    # ------------------------------------------------------------------

    def _message_log(self, **kwargs):
        """The ERP logs a tax's tracked changes only once the tax is used;
        a change of its VAT settings is always recorded, the other
        fields keep the ERP's rule."""
        self.ensure_one()
        values = kwargs.get("tracking_value_ids")
        if self.is_used or not values:
            return super()._message_log(**kwargs)
        ours = {
            self.env["ir.model.fields"]._get(self._name, name).id
            for name in VAT_SETTING_FIELDS
        }
        kept = [value for value in values if value[2].get("field_id") in ours]
        if not kept:
            return super()._message_log(**kwargs)
        kwargs["tracking_value_ids"] = kept
        mail_thread = self.env.registry["mail.thread"]
        return mail_thread._message_log(self, **kwargs)

    # ------------------------------------------------------------------
    # AC-06
    # ------------------------------------------------------------------

    @api.constrains(*CHECKED_FIELDS)
    def _check_einvoice_vat_settings(self):
        if self.env.context.get("install_mode"):
            return
        for tax in self:
            if tax.type_tax_use != "sale" or tax.amount_type != "percent":
                continue
            problems = vat.check_tax(
                tax.amount, tax._einvoice_set_category(),
                tax._einvoice_set_exemption_code(), tax.einvoice_vat_reason)
            if problems:
                raise ValidationError(_("%(tax)s: %(problems)s", tax=tax.name,
                                        problems=" ".join(problems)))
