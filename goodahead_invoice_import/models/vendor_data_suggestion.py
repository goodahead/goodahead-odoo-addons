# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Invoice details that contradict the vendor record, awaiting a decision.

Filling an empty field loses nothing, so ``account.move._ai_apply_vendor_data``
does it automatically; replacing an existing value becomes a row here. A bank
account or tax id always waits for a person, even when the field is empty,
because invoice fraud typically works by changing the bank account. The vendor
name is never suggested: the curated name in the ledger is usually the useful
one.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

# Single definition of "sensitive", shared by the apply and the queue's guard.
SENSITIVE_FIELDS = ("vat", "bank_account")


class GoodaheadVendorDataSuggestion(models.Model):
    _name = "goodahead.vendor.data.suggestion"
    _description = "Vendor detail from an invoice awaiting a decision"
    _order = "is_sensitive desc, partner_id, field_name, id"

    move_id = fields.Many2one(
        "account.move",
        required=True,
        ondelete="cascade",
        index=True,
        string="Bill",
    )
    # The bill's company, stored so record rules can scope the rows: a user
    # of another company must not list it. Filled by the ORM for existing
    # rows on upgrade.
    company_id = fields.Many2one(
        related="move_id.company_id",
        store=True,
        index=True,
        readonly=True,
    )
    partner_id = fields.Many2one(
        "res.partner",
        required=True,
        ondelete="cascade",
        index=True,
        string="Vendor",
    )
    field_name = fields.Char(
        required=True,
        help="The `res.partner` field this is about — or `bank_account`, which "
        "is not a partner field but a `res.partner.bank` row.",
    )
    field_label = fields.Char(
        string="Detail",
        help="What the field is called on the contact form, stored at the time "
        "the row was made so the queue reads the same words the form does.",
    )
    invoice_value = fields.Char(string="On this invoice")
    record_value = fields.Char(string="On the vendor record")
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("applied", "Applied"),
            ("dismissed", "Dismissed"),
        ],
        required=True,
        default="pending",
        index=True,
    )
    is_sensitive = fields.Boolean(
        string="Money detail",
        help="A bank account or a tax id. Never filled in automatically, never "
        "pre-ticked, and never applied by the bulk action — invoice fraud is "
        "precisely a changed bank account.",
    )
    note = fields.Char(
        help="Free text for whoever owns the vendor data — 'keep both: agency "
        "and factory'. Read by a person, never by code.",
    )

    def action_apply(self):
        """Write this detail onto the vendor.

        A phone, email or website is added as a ``goodahead.partner.identifier``
        row and the contact field is left alone, since a vendor may use several.
        """
        for row in self:
            if row.state != "pending":
                continue
            row._apply_one()
            row.state = "applied"
        return True

    def action_dismiss(self):
        """Dismiss the suggestion; the row is kept as a record of the decision."""
        self.filtered(lambda row: row.state == "pending").state = "dismissed"
        return True

    def action_apply_selected(self):
        """Apply every ticked row that is not a money detail.

        Sensitive rows are skipped even when ticked: they must be applied one
        at a time with the row's own button.
        """
        rows = self.filtered(lambda row: row.state == "pending")
        sensitive = rows.filtered("is_sensitive")
        (rows - sensitive).action_apply()
        if not sensitive:
            return True
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "warning",
                "sticky": False,
                "message": _(
                    "%(count)s bank or tax-id detail(s) were left pending. A "
                    "money detail is applied one at a time, by somebody who "
                    "looked at it — invoice fraud is a changed bank account.",
                    count=len(sensitive),
                ),
            },
        }

    def action_open_review(self):
        """Open the review dialog with every pending detail of this vendor.

        Used as the row-click action of the queue list view.
        """
        self.ensure_one()
        siblings = self.search(
            [("partner_id", "=", self.partner_id.id), ("state", "=", "pending")]
        )
        return self.env["goodahead.vendor.data.review"]._open_for(siblings)

    def _apply_one(self):
        self.ensure_one()
        partner = self.partner_id
        value = self.invoice_value
        if not partner or not value:
            raise UserError(
                _("This suggestion has nothing to apply to.")
            )

        Identifier = self.env["goodahead.partner.identifier"]
        source = _("applied from bill %(bill)s", bill=self.move_id.display_name)

        if self.field_name in ("phone", "email", "website"):
            pairs = Identifier._derive_from_contact(
                **{self.field_name: value}
            )
            Identifier._add_pairs({partner.id: pairs}, source)
            return

        if self.field_name == "vat":
            partner.vat = value
            return

        if self.field_name == "bank_account":
            self._apply_bank_account()
            return

        if self.field_name in ("state_id", "country_id"):
            record = self.move_id._ai_resolve_location(
                self.field_name, value, partner.country_id
            )
            if not record:
                # Never create a state or country: an invented state code is
                # sent to carriers, which reject it.
                raise UserError(
                    _(
                        "“%(value)s” is not a state or country Odoo knows for "
                        "this vendor, so it was not applied. Nothing is created "
                        "here on purpose: an import that invents a state puts a "
                        "code on the vendor that the carrier rejects.",
                        value=value,
                    )
                )
            partner[self.field_name] = record.id
            return

        partner[self.field_name] = value

    def _apply_bank_account(self):
        """Record the invoice's bank account on the vendor (manual action only).

        The routing number is taken from the stored extraction and written to
        the routing field (``aba_routing`` with ``l10n_us``), without which a
        US account cannot be paid.
        """
        self.ensure_one()
        Bank = self.env["res.partner.bank"]
        partner = self.partner_id
        existing = Bank.search(
            [
                ("acc_number", "=", self.invoice_value),
                ("partner_id", "child_of", partner.id),
            ],
            limit=1,
        )
        if not existing:
            Bank.create(
                {"acc_number": self.invoice_value, "partner_id": partner.id}
            )
        extracted = self.move_id._ai_stored_extraction()
        if extracted:
            self.env["account.move"]._ai_record_routing_number(
                self.move_id, extracted
            )

    @api.model
    def _replace_pending(self, move, partner, rows):
        """Replace this bill's pending suggestions for ``partner`` with ``rows``.

        Applied and dismissed rows are kept. Written with ``sudo()`` so the
        import never fails on an ACL for this table.
        """
        stale = self.sudo().search(
            [
                ("move_id", "=", move.id),
                ("partner_id", "=", partner.id),
                ("state", "=", "pending"),
            ]
        )
        if stale:
            stale.unlink()
        if not rows:
            return self.browse()
        return self.sudo().create(
            [dict(row, move_id=move.id, partner_id=partner.id) for row in rows]
        )
