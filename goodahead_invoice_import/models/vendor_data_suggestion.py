# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""One row per detail on an invoice that CONTRADICTS the vendor record — plus
the two kinds of detail that wait for a person even when the record is empty.

🔴 THE SPLIT IS BY RISK, AND IT IS THE WHOLE DESIGN. Filling a field that is
EMPTY cannot lose anything, so `account.move._ai_apply_vendor_data` writes it
with nobody asked and logs it. Replacing something a person typed CAN lose
something, so it becomes a row here and waits. The automatic half exists
because asking permission for the safe half means nobody answers: the
accountant just searches for a vendor and the data never improves.

🔴 TWO FIELDS ARE NEVER AUTOMATIC, EMPTY OR NOT: a BANK ACCOUNT and a TAX ID.
That is not fussiness about money. INVOICE FRAUD IS A CHANGED BANK ACCOUNT —
the fraudster sends a real-looking invoice from a real-looking vendor with their
own account on it — and vendor invoices do carry printed warnings about
exactly that scam. An import that writes a bank account
off a PDF is the attack, automated. So both arrive here as `is_sensitive` rows,
marked, never pre-ticked, and applied only by a person who looked.

🔴 AND THE VENDOR'S NAME IS NEVER SUGGESTED AT ALL — not automatically, not as
a conflict, not in this table. It is the one field somebody curated on purpose.
The ledger says `NORTHWIND - Smith reps` (the rep agency that sells for Northwind)
and the invoice says `Northwind`; `Contoso` against `Fabrikam, LLC`; `Tailwind Fleet -
Woodgrove Bank` against `Woodgrove Bank`. In every such case the LEDGER name is the
useful one and the printed name is the one that would overwrite it.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

# The fields whose value is a bank account or a tax identity. Named once, read
# by the apply and by the queue's own guard, so "sensitive" cannot come to mean
# two different things in two places.
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
    # 🔴 STORED, FOR THE COMPANY RULE (security.xml): a suggestion carries
    # one company's invoice values — a bank account among them — and a user
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

    # ------------------------------------------------------------------
    # The decisions
    # ------------------------------------------------------------------

    def action_apply(self):
        """Write this one detail onto the vendor.

        🔴 A PHONE, AN EMAIL OR A WEBSITE IS ADDED BESIDE, NOT OVER. Applying
        one of those stores the value as a `goodahead.partner.identifier` row
        and LEAVES the field on the contact alone, because a vendor is reached
        on several numbers and addresses and the newest is not the only true
        one. Replacing `orders@contoso-us.test` with whatever the latest invoice
        happened to print would be a regression dressed up as an improvement —
        F27 says so in those words. Everything else is an ordinary write.
        """
        for row in self:
            if row.state != "pending":
                continue
            row._apply_one()
            row.state = "applied"
        return True

    def action_dismiss(self):
        """Say no, once, for everybody. The row stays as the record of the
        decision rather than disappearing — otherwise the next bill from the
        same vendor puts the same question in front of the same person, which
        is the thing the queue exists to stop."""
        self.filtered(lambda row: row.state == "pending").state = "dismissed"
        return True

    def action_apply_selected(self):
        """Apply every ticked row that is not a money detail.

        🔴 A SENSITIVE ROW IS DELIBERATELY SKIPPED HERE, AND THIS IS STRICTER
        THAN "a person ticked it". Select-all is one click, and the whole reason
        a bank account waits for a person is that it should get somebody's
        individual attention rather than ride along in a batch of nine address
        corrections. The row's own Apply button still applies it, which is the
        deliberate act the rule is actually asking for.
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
        """Open this row's VENDOR in the review dialog — every pending detail
        that vendor has, not this one row.

        🔴 THIS IS WHAT A ROW CLICK IN THE QUEUE DOES (`<list action="…"
        type="object">` in `views/vendor_data_suggestion.xml`), and the scope is
        the point. The default row click opened a form on ONE suggestion, which
        is the least useful thing to look at: the question is never "is this
        phone number right" on its own, it is "what does this invoice claim
        about this vendor". The queue is already grouped by vendor for the same
        reason — one person does a vendor at a time, which is how the same
        correction stops arriving seven times.
        """
        self.ensure_one()
        siblings = self.search(
            [("partner_id", "=", self.partner_id.id), ("state", "=", "pending")]
        )
        return self.env["goodahead.vendor.data.review"]._open_for(siblings)

    # ------------------------------------------------------------------
    # The writes themselves
    # ------------------------------------------------------------------

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

        if self.field_name in ("phone", "email", "website") and not partner[self.field_name]:
            # A detail that waited for a person although the field was empty
            # (a document anybody can send): applying it fills the field. The
            # contact's own write hook derives the identifiers.
            partner[self.field_name] = value
            return
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
                # 🔴 LOOK IT UP, NEVER CREATE IT. An import that creates them
                # invents US "states" — say a SECOND "New Jersey" whose code is
                # `1` — and the state code is what Odoo sends to a carrier,
                # which refuses a booking over it.
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
        """Record the account printed on the invoice against the vendor.

        Never on its own initiative — only from this button, pressed by a
        person. The routing number comes from the stored extraction rather than
        from this row, because `account.move._ai_record_routing_number` already
        knows how to put it in `clearing_number` (Odoo's own home for it, which
        `l10n_us` labels ABA/Routing) and a US account without one cannot be
        paid into at all.
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

    # ------------------------------------------------------------------
    # Building the rows
    # ------------------------------------------------------------------

    @api.model
    def _replace_pending(self, move, partner, rows):
        """Store ``rows`` as this bill's pending suggestions for ``partner``.

        Pending rows from an earlier run are dropped first, so re-importing the
        same bill restates the question rather than asking it twice. Rows
        already APPLIED or DISMISSED are left alone — they are the record of a
        decision somebody made.

        `sudo()` on the write for the same reason as the candidates: the import
        runs in a queue job as whoever uploaded the PDF, and an import must
        never fail on an ACL for a suggestion table.
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
