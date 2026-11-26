# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..lib.claude_client import LOCALE_BLOCKS, LOCALE_EU, SLOT_NAMES, build_system_prompt

# The JSON keys a schema slot declares, one per line: `    "vat": "…",`.
_SCHEMA_KEY_RE = re.compile(r'^\s*"([a-z_]+)"\s*:', re.MULTILINE)

# Fields a built-in profile takes from the module's code and nowhere else.
_CODE_OWNED = SLOT_NAMES + ("code", "builtin")


class InvoicePromptProfile(models.Model):
    """One set of locale wording for the extraction prompt.

    The prompt is the module's country-neutral procedures with thirteen slots
    filled from a profile: tax, identifier and payment prose, the schema's
    field descriptions, and a closing rule. A company picks its profile in the
    settings; with none picked, the block is chosen from the company's country
    (`account.move._ai_prompt_locale`).

    🔴 A BUILT-IN PROFILE IS NOT READ FROM ITS OWN FIELDS. US and EU ship as
    records so they can be chosen and duplicated, but the prompt they send is
    composed from `LOCALE_BLOCKS` by code, which is what keeps the composed US
    prompt byte-identical to the one the US deployment runs. Their fields are a copy,
    refreshed on every upgrade by `_sync_builtin_profiles`, and read-only.

    A custom profile — a duplicate, edited — is sent as its fields say. The
    schema slots must keep the JSON keys the shipped blocks declare: the
    wording is free, the shape of the extraction is not.
    """

    _name = "goodahead.invoice.prompt.profile"
    _description = "AI Invoice Import Prompt Profile"
    _order = "sequence, name"

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    code = fields.Char(
        readonly=True,
        copy=False,
        help="The shipped locale block a built-in profile is composed from.",
    )
    builtin = fields.Boolean(
        string="Built-in",
        readonly=True,
        copy=False,
        help="Shipped with the module. Its text follows the module's code and "
        "cannot be edited; duplicate it to make a profile of your own.",
    )
    note = fields.Text(help="What this profile is for. Not sent to the model.")

    locale_origin = fields.Text(
        string="Where Documents Come From",
        help="Closes the opening paragraph: which vendors most documents are from.",
    )
    locale_tax = fields.Text(
        string="Tax",
        help="How tax is charged and reported, and the tax category codes.",
    )
    locale_identifiers = fields.Text(
        string="Identifiers",
        help="What goes in the vendor's vat and business_id.",
    )
    locale_payment = fields.Text(
        string="Bank and Payment",
        help="Bank details, remittance address and payment references.",
    )
    locale_seller_identifiers = fields.Text(
        string="Schema: Vendor Identifiers",
        help="The vat and business_id lines of the seller object.",
    )
    locale_seller_region = fields.Text(
        string="Schema: Vendor Region",
        help="The state_code line of the seller object.",
    )
    locale_seller_bank = fields.Text(
        string="Schema: Vendor Bank",
        help="The bank lines of the seller object.",
    )
    locale_buyer_identifier = fields.Text(
        string="Schema: Buyer Identifier",
        help="The vat line of the buyer object.",
    )
    locale_buyer_region = fields.Text(
        string="Schema: Buyer Region",
        help="The state_code line of the buyer object.",
    )
    locale_payment_means = fields.Text(
        string="Schema: Payment Means",
        help="The payment_means_code line.",
    )
    locale_unit_code = fields.Text(
        string="Schema: Unit Codes",
        help="The unit_code line of an invoice line.",
    )
    locale_tax_fields = fields.Text(
        string="Schema: Line Tax",
        help="The tax lines of an invoice line.",
    )
    locale_rules = fields.Text(
        string="Extra Rules",
        help="Rules added at the end of the important-rules list.",
    )

    prompt_preview = fields.Text(
        string="Composed Prompt",
        compute="_compute_prompt_preview",
        help="The system prompt this profile sends, with the expense "
        "categories left as a placeholder.",
    )

    _code_uniq = models.Constraint(
        "UNIQUE(code)",
        "Only one profile can be built from each shipped locale block.",
    )

    @api.depends("builtin", "code", *SLOT_NAMES)
    def _compute_prompt_preview(self):
        placeholder = _("(the expense categories are listed here)")
        for profile in self:
            try:
                prompt = build_system_prompt(profile._ai_prompt_locale())
                profile.prompt_preview = prompt.format(categories_block=placeholder)
            except (ValueError, KeyError, IndexError) as exc:
                profile.prompt_preview = str(exc)

    def _ai_prompt_locale(self):
        """What `ClaudeInvoiceExtractor(locale=…)` gets for this profile: the
        shipped block's name for a built-in, the profile's own block otherwise.
        """
        self.ensure_one()
        if self.builtin:
            return self.code
        return {name: self[name] or "" for name in SLOT_NAMES}

    @api.constrains(*SLOT_NAMES)
    def _check_slots(self):
        reference = LOCALE_BLOCKS[LOCALE_EU]
        for profile in self.filtered(lambda p: not p.builtin):
            labels = []
            for name in SLOT_NAMES:
                if not (profile[name] or "").strip():
                    labels.append(profile._fields[name].string)
            if labels:
                raise ValidationError(_(
                    "Prompt profile %(profile)s: every part of the prompt needs "
                    "text. Empty: %(parts)s.",
                    profile=profile.name,
                    parts=", ".join(labels),
                ))
            for name in SLOT_NAMES:
                text = profile[name]
                label = profile._fields[name].string
                if "@@" in text:
                    raise ValidationError(_(
                        "Prompt profile %(profile)s: %(part)s contains \"@@\", "
                        "which the prompt template uses for its own slots.",
                        profile=profile.name,
                        part=label,
                    ))
                expected = set(_SCHEMA_KEY_RE.findall(reference[name]))
                found = set(_SCHEMA_KEY_RE.findall(text))
                if expected != found:
                    raise ValidationError(_(
                        "Prompt profile %(profile)s: %(part)s must declare "
                        "exactly the JSON keys %(expected)s, one per line "
                        "(found: %(found)s). Change the descriptions, not the "
                        "keys — the import reads those keys.",
                        profile=profile.name,
                        part=label,
                        expected=", ".join(sorted(expected)) or _("none"),
                        found=", ".join(sorted(found)) or _("none"),
                    ))

    def write(self, vals):
        if not self.env.context.get("prompt_profile_sync"):
            for profile in self.filtered("builtin"):
                if any(
                    name in vals and (vals[name] or False) != (profile[name] or False)
                    for name in _CODE_OWNED
                ):
                    raise UserError(_(
                        "%(profile)s is a built-in prompt profile and follows "
                        "the module's code. Duplicate it to change its text.",
                        profile=profile.name,
                    ))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_builtin(self):
        if self.filtered("builtin"):
            raise UserError(_(
                "Built-in prompt profiles cannot be deleted. Archive one to "
                "hide it."
            ))

    def copy_data(self, default=None):
        vals_list = super().copy_data(default=default)
        if default and "name" in default:
            return vals_list
        return [
            dict(vals, name=_("%s (copy)", profile.name))
            for profile, vals in zip(self, vals_list)
        ]

    @api.model
    def _sync_builtin_profiles(self):
        """Copy the shipped blocks into the built-in profiles' fields.

        Run from the module's data on every install and upgrade, so the
        read-only text a user sees — and copies when duplicating — is the text
        that is sent.
        """
        profiles = self.with_context(active_test=False, prompt_profile_sync=True)
        for profile in profiles.search([("builtin", "=", True)]):
            block = LOCALE_BLOCKS.get(profile.code)
            if block:
                profile.write({name: block[name] for name in SLOT_NAMES})
