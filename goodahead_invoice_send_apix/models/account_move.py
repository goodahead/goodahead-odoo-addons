# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""How a customer invoice goes out: its own transmit method.

Set once, when the invoice is created, from its customer; changed
later by hand, but never while a send of it is in progress.

🔴 NOTHING UNTIL SEND: creating or posting an invoice only stores this
field. No message, attachment, job, history entry or APIX call is made here.
"""

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.osv import expression
from odoo.tools import format_datetime, split_every

from odoo.addons.goodahead_einvoice_base.lib.finvoice import vat as finvoice_vat

from ..lib import readiness

SEND_IN_PROGRESS = "send.in_progress"
NOT_CUSTOMER_DOCUMENT = "send.not_customer_document"
NOT_POSTED = "send.not_posted"
SENDING_OFF = "send.sending_off"
NOT_READY = "send.not_ready"
ALREADY_SENT = "send.already_sent"
#: Context key: open the invoice form on its Delivery tab.
DELIVERY_TAB_KEY = "goodahead_delivery_tab"
#: Channels that go through APIX and need the company's sending switch.
APIX_CHANNELS = ("einvoice", "letter")
CUSTOMER_TYPES = ("out_invoice", "out_refund")
BUYER_REFERENCE = "einvoice_buyer_reference"
TRANSMIT_METHODS = [
    ("einvoice", "E-invoice"),
    ("letter", "Printed letter"),
    ("email", "Email"),
]
READINESS_LEVELS = [
    ("ready", "Ready"),
    ("warning", "Ready with warnings"),
    ("not_ready", "Not ready"),
    ("off", "Sending off"),
]
SEND_STATES = [
    ("ready", "Ready"),
    ("not_ready", "Not ready"),
    ("in_progress", "In progress"),
    ("sent", "Sent"),
    ("refused", "Refused"),
    ("failed", "Failed"),
]
#: Where a readiness item is fixed (readiness ``fix_at``), as shown on SCR-01.
FIX_AT_LABELS = {
    "fix_it": "Fix e-invoice address",
    "contact": "the invoice contact",
    "company": "your company",
    "unit_codes": "Unit codes",
    "taxes": "Taxes — an Administrator sets the VAT category",
    "invoice": "this invoice",
}
CHANNEL_LABELS = {"einvoice": "e-invoice", "letter": "printed letter", "email": "email"}
#: Invoices whose readiness a list filter works out at once (prefetch size).
READINESS_BATCH = 500


def search_values(operator, value, keys):
    """The selection keys a search on a computed selection asks for. Odoo
    18.0 hands a field's ``search`` method ``=``, ``!=``, ``in`` and
    ``not in`` as written; 19.0 normalises them to ``in``. A negation is the
    complement of the keys (never ``['!', ...]``, which would also drop rows
    with no value)."""
    if operator in ("=", "!="):
        chosen = {value}
    elif operator in ("in", "not in"):
        chosen = set(value or ())
    else:
        raise ValueError(f"Unsupported search operator {operator}")
    if operator in ("!=", "not in"):
        return set(keys) - chosen
    return chosen


class SendInProgress(UserError):
    """A transmit-method change (or a second send) while a send of the
    invoice is queued or running."""

    code = SEND_IN_PROGRESS


class AccountMove(models.Model):
    _inherit = "account.move"

    # AC-12: the standing value the last change of customer filled in, kept
    # in the form only (not stored), so a later change of customer can tell
    # a filled value from a typed one.
    einvoice_buyer_reference_filled = fields.Char(store=False, copy=False)
    transmit_method = fields.Selection(
        TRANSMIT_METHODS,
        string="Transmit method",
        copy=False,
        tracking=True,
        help="How this invoice goes out when it is sent: as an e-invoice or a printed "
        "letter through APIX, or by email. Set from the customer when the invoice is "
        "created; it can be changed by hand unless a send is in progress.",
    )
    sent_before_install = fields.Boolean(
        string="Sent before sending was installed",
        readonly=True,
        copy=False,
        default=False,
        help="The invoice was already sent before this add-on was installed.",
    )
    # Not stored, worked out from stored data only — no APIX call, no write
    #.
    send_readiness = fields.Json(string="Sending readiness", compute="_compute_send_readiness")
    send_ready = fields.Boolean(string="Ready to send", compute="_compute_send_readiness")
    send_in_progress = fields.Boolean(compute="_compute_send_in_progress")
    send_in_progress_info = fields.Char(compute="_compute_send_in_progress")
    already_sent = fields.Boolean(compute="_compute_already_sent", search="_search_already_sent")
    send_copy_warning = fields.Char(compute="_compute_send_copy_warning")
    apix_send_available = fields.Boolean(compute="_compute_apix_send_available")
    # SCR-01: the readiness line of a posted customer document, and the
    # sending badge (header, SCR-05 list).
    send_readiness_level = fields.Selection(
        READINESS_LEVELS, string="Readiness", compute="_compute_send_readiness_display"
    )
    send_readiness_html = fields.Html(
        string="Readiness details", compute="_compute_send_readiness_display", sanitize=True
    )
    send_state = fields.Selection(
        SEND_STATES, string="Sending", compute="_compute_send_state", search="_search_send_state"
    )
    # SCR-02: the delivery tab — every attempt, read-only.
    send_attempt_ids = fields.One2many(
        "goodahead.invoice.send.attempt", "move_id", string="Delivery history", readonly=True
    )
    delivery_empty = fields.Boolean(compute="_compute_delivery_empty")

    @api.depends(
        "transmit_method", "move_type", "partner_id", "company_id", "partner_bank_id",
        "invoice_date_due", "invoice_line_ids.product_uom_id",
    )
    def _compute_send_readiness(self):
        # the switch and the sender details once per company (and account),
        # not once per invoice: a list filter computes hundreds at once
        enabled = {}
        senders = {}
        Connection = self.env["goodahead.apix.connection"]
        for move in self:
            if move.move_type not in CUSTOMER_TYPES or not move.partner_id:
                move.send_readiness = False
                move.send_ready = False
                continue
            company = move.company_id
            if company not in enabled:
                enabled[company] = Connection._apix_sending_enabled(company)
            key = (company, move.partner_bank_id)
            if key not in senders:
                senders[key] = move._apix_sender_values()
            result = move._apix_readiness(
                move._apix_channel(), sending_enabled=enabled[company], sender=senders[key]
            )
            move.send_readiness = result
            move.send_ready = result["ready"]

    @api.depends("state")
    def _compute_send_in_progress(self):
        for move in self:
            holder = move._apix_holder()
            move.send_in_progress = bool(holder)
            move.send_in_progress_info = bool(holder) and _(
                "Sending since %(since)s, started by %(user)s",
                since=format_datetime(self.env, holder.since, dt_format="HH:mm"),
                user=holder.user_name,
            )

    @api.depends("sent_before_install")
    def _compute_already_sent(self):
        for move in self:
            move.already_sent = bool(move._apix_already_sent_info())

    @api.depends("company_id")
    def _compute_send_copy_warning(self):
        Connection = self.env["goodahead.apix.connection"]
        for move in self:
            connection = Connection._apix_for_company(move.company_id)
            move.send_copy_warning = connection._apix_copy_warning() if connection else False

    @api.depends("move_type", "state", "company_id", "transmit_method", "partner_id")
    def _compute_apix_send_available(self):
        """"Send via APIX" is offered for a posted customer document going
        through APIX (not by email: that is Send & Print's, AC-26) in a
        company whose sending is on."""
        Connection = self.env["goodahead.apix.connection"]
        for move in self:
            move.apix_send_available = (
                move.move_type in CUSTOMER_TYPES
                and move.state == "posted"
                and move._apix_channel() in APIX_CHANNELS
                and Connection._apix_sending_enabled(move.company_id)
            )

    @api.depends("send_readiness", "state")
    def _compute_send_readiness_display(self):
        for move in self:
            move.send_readiness_level = False
            move.send_readiness_html = False
            if move.state != "posted" or not move.send_readiness:
                continue
            move.send_readiness_level, move.send_readiness_html = move._apix_readiness_display()

    def _apix_readiness_display(self):
        """``(level, html)`` of the SCR-01 readiness line."""
        channel = self.env._(CHANNEL_LABELS.get(self._apix_channel(), ""))  # pylint: disable=translation-not-lazy
        return readiness_display(
            self.env, self.send_readiness, self.company_id,
            ready=_("Ready to send as %s", channel),
            not_ready=_("Not ready to send as %s", channel),
        )

    @api.depends("send_attempt_ids", "sent_before_install")
    def _compute_delivery_empty(self):
        for move in self:
            move.delivery_empty = not move.sudo().send_attempt_ids and not move.sent_before_install

    @api.depends("state", "send_ready", "already_sent", "send_in_progress")
    def _compute_send_state(self):
        Attempt = self.env["goodahead.invoice.send.attempt"].sudo()
        latest = {
            attempt.move_id.id: attempt.state
            for attempt in Attempt.search([("move_id", "in", self.ids), ("is_latest", "=", True)])
        }
        for move in self:
            move.send_state = move._apix_send_state(latest.get(move.id))

    def _apix_send_state(self, latest_state):
        """The sending badge: in progress, the latest refused / failed
        outcome, sent, else ready or not ready."""
        if self.move_type not in CUSTOMER_TYPES or self.state != "posted":
            return False
        if self.send_in_progress:
            return "in_progress"
        if latest_state in ("refused", "failed"):
            return latest_state
        if self.already_sent:
            return "sent"
        return "ready" if self.send_ready else "not_ready"

    # ------------------------------------------------------------------
    # List filters: searching the non-stored badge
    # ------------------------------------------------------------------

    def _search_send_state(self, operator, value):
        # Odoo 18.0 passes `=`, `!=`, `in`, `not in` as written (19.0 hands
        # over `in` only): "not X" is "one of the other badges".
        wanted = search_values(operator, value, [key for key, _label in SEND_STATES])
        return [("id", "in", self._apix_send_state_ids(wanted))]

    def _apix_send_state_ids(self, wanted):
        """Ids of the posted customer documents (of the user's companies)
        whose badge is in ``wanted``. In progress, refused / failed and sent
        are SQL subqueries on the history (one search, nothing loaded); ready
        / not ready need the readiness rules, worked out in batches only for
        the invoices with none of those."""
        badge = self._apix_stored_badge_domains()
        domains = [badge[state] for state in wanted if state in badge]
        found = []
        if domains:
            found = self.search(expression.AND([
                self._apix_posted_customer_domain(), expression.OR(domains),
            ])).ids
        if not wanted & {"ready", "not_ready"}:
            return found
        rest = self.search(self._apix_posted_customer_domain() + badge["none"])
        for batch in split_every(READINESS_BATCH, rest.ids, self.browse):
            found += [move.id for move in batch
                      if ("ready" if move.send_ready else "not_ready") in wanted]
        return found

    @api.model
    def _apix_posted_customer_domain(self):
        return [("move_type", "in", CUSTOMER_TYPES), ("state", "=", "posted")]

    @api.model
    def _apix_stored_badge_domains(self):
        """``{badge: domain}`` of the badges read from the history, in the
        badge's priority (in progress > latest refused / failed > sent), and
        ``none`` for an invoice with none of them."""
        Attempt = self.env["goodahead.invoice.send.attempt"].sudo()

        def moves_of(domain):
            return Attempt._search(domain).subselect("move_id")

        in_progress = moves_of([("state", "in", ("queued", "running"))])
        refused = moves_of([("is_latest", "=", True), ("state", "=", "refused")])
        failed = moves_of([("is_latest", "=", True), ("state", "=", "failed")])
        counted = moves_of([("counts_as_sent", "=", True)])
        not_in_progress = [("id", "not in", in_progress)]
        not_failing = [("id", "not in", refused), ("id", "not in", failed)]
        sent = ["|", ("sent_before_install", "=", True), ("id", "in", counted)]
        not_sent = [("sent_before_install", "=", False), ("id", "not in", counted)]
        return {
            "in_progress": [("id", "in", in_progress)],
            "refused": not_in_progress + [("id", "in", refused)],
            "failed": not_in_progress + [("id", "in", failed)],
            "sent": not_in_progress + not_failing + sent,
            "none": not_in_progress + not_failing + not_sent,
        }

    def _search_already_sent(self, operator, value):
        if operator not in ("=", "!=") or not isinstance(value, bool):
            # 18.0: no `return NotImplemented` fallback
            raise UserError(_("Unsupported search operator %s", operator))
        Attempt = self.env["goodahead.invoice.send.attempt"].sudo()
        sent_ids = Attempt._search([("counts_as_sent", "=", True)]).subselect("move_id")
        domain = ["|", ("sent_before_install", "=", True), ("id", "in", sent_ids)]
        positive = (operator == "=") == value
        return domain if positive else ["!", *domain]

    @api.constrains("transmit_method", "move_type")
    def _check_transmit_method_customer_only(self):
        for move in self:
            if move.transmit_method and move.move_type not in CUSTOMER_TYPES:
                raise ValidationError(
                    _("Only customer invoices and credit notes have a transmit method.")
                )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._einvoice_buyer_reference_default_for_vals(vals)
            # the invoice form sends the empty field too: empty = not chosen
            if vals.get("transmit_method"):
                continue
            method = self._apix_transmit_default_for_vals(vals)
            if method:
                vals["transmit_method"] = method
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # The buyer reference starts with the customer's standing one
    # ------------------------------------------------------------------

    @api.model
    def _einvoice_buyer_reference_default_for_vals(self, vals):
        """Fill the buyer reference of a new customer invoice or credit note
        whose vals carry none, from its customer's standing value. A value
        given by the caller (an empty one too) and a reversal (which copies
        the reversed invoice's) are kept."""
        if BUYER_REFERENCE in vals or vals.get("reversed_entry_id"):
            return
        move_type = vals.get("move_type") or self.env.context.get("default_move_type")
        if move_type not in CUSTOMER_TYPES or not vals.get("partner_id"):
            return
        partner = self.env["res.partner"].browse(vals["partner_id"])
        standing = partner._einvoice_standing_buyer_reference()
        if standing:
            vals[BUYER_REFERENCE] = standing

    @api.onchange("partner_id")
    def _onchange_partner_id_einvoice_buyer_reference(self):
        """A change of customer replaces the buyer reference only while it is
        empty or still the previous customer's standing value; a typed value
        stays."""
        for move in self:
            if move.move_type not in CUSTOMER_TYPES or move.state != "draft":
                continue
            current = move.einvoice_buyer_reference or False
            previous = move.einvoice_buyer_reference_filled or (
                move._origin.partner_id._einvoice_standing_buyer_reference()
                if move._origin.partner_id else None)
            if current and current != previous:
                continue
            standing = (move.partner_id._einvoice_standing_buyer_reference()
                        if move.partner_id else None)
            move.einvoice_buyer_reference = standing or False
            move.einvoice_buyer_reference_filled = standing or False

    def write(self, vals):
        if "transmit_method" in vals:
            self._apix_check_no_send_in_progress(vals["transmit_method"])
        return super().write(vals)

    # ------------------------------------------------------------------
    # Opened from a failed send: the Delivery tab first
    # ------------------------------------------------------------------

    @api.model
    def _get_view(self, view_id=None, view_type="form", **options):
        arch, view = super()._get_view(view_id, view_type, **options)
        if view_type != "form" or not self.env.context.get(DELIVERY_TAB_KEY):
            return arch, view
        for page in arch.xpath("//page[@name='apix_delivery']"):
            page.set("autofocus", "autofocus")
        return arch, view

    @api.model
    def _get_view_cache_key(self, view_id=None, view_type="form", **options):
        key = super()._get_view_cache_key(view_id, view_type, **options)
        return (*key, bool(self.env.context.get(DELIVERY_TAB_KEY)))

    # ------------------------------------------------------------------
    # The creation default
    # ------------------------------------------------------------------

    def _apix_default_transmit_method(self):
        """The rule for this invoice: from its customer in its company, or
        False for anything but a customer invoice or credit note."""
        self.ensure_one()
        if self.move_type not in CUSTOMER_TYPES or not self.partner_id:
            return False
        return self.partner_id._apix_transmit_default(self.company_id)

    @api.model
    def _apix_transmit_default_for_vals(self, vals):
        """The default for an invoice about to be created from ``vals``."""
        move_type = vals.get("move_type") or self.env.context.get("default_move_type")
        partner_id = vals.get("partner_id") or self.env.context.get("default_partner_id")
        if move_type not in CUSTOMER_TYPES or not partner_id:
            return False
        company = self._apix_company_for_vals(vals)
        return self.env["res.partner"].browse(partner_id)._apix_transmit_default(company)

    @api.model
    def _apix_company_for_vals(self, vals):
        if vals.get("company_id"):
            return self.env["res.company"].browse(vals["company_id"])
        if vals.get("journal_id"):
            return self.env["account.journal"].browse(vals["journal_id"]).company_id
        return self.env.company

    # ------------------------------------------------------------------
    # One send at a time
    # ------------------------------------------------------------------

    def _apix_check_no_send_in_progress(self, new_method):
        """Refuse changing the transmit method of an invoice whose send is
        queued or running, naming who started it and when."""
        Attempt = self.env["goodahead.invoice.send.attempt"].sudo()
        for move in self.filtered(lambda m: m.transmit_method != new_method):
            holder = Attempt._ledger_holder(move.id)
            if holder:
                raise SendInProgress(holder.message(self.env))

    def _apix_holder(self):
        """The ``Holder`` of this invoice's send in progress, or None."""
        self.ensure_one()
        if not self.id:
            return None
        return self.env["goodahead.invoice.send.attempt"].sudo()._ledger_holder(self.id)

    # ------------------------------------------------------------------
    # Readiness (AC-17 – AC-20, contract §5) — stored data only
    # ------------------------------------------------------------------

    def _apix_channel(self):
        self.ensure_one()
        return self.transmit_method or self._apix_default_transmit_method() or "email"

    def _apix_recipient_values(self):
        self.ensure_one()
        return self.partner_id._apix_recipient_values()

    def _apix_sender_values(self):
        self.ensure_one()
        return self.env["res.partner"]._apix_sender_values(self.company_id, self.partner_bank_id)

    def _apix_line_values(self):
        """Each line's unit as the send codes it (``_line_unit``): a unit
        sent through its coded reference unit is no warning."""
        # imported here: a module-level import would change the class order
        from .account_move_finvoice import _line_unit

        self.ensure_one()
        lines = self.sudo().invoice_line_ids.filtered(lambda line: line.display_type == "product")
        values = []
        for line in lines.filtered("product_uom_id"):
            _quantity, code, unit_name, _base = _line_unit(line)
            values.append({"unit_name": unit_name or "", "rec20_code": code})
        return values

    def _apix_readiness(self, channel, sending_enabled=None, sender=None):
        """``Readiness`` of this invoice for ``channel``;
        ``sending_enabled`` / ``sender``: already read for its company."""
        self.ensure_one()
        if sending_enabled is None:
            sending_enabled = self.env["goodahead.apix.connection"]._apix_sending_enabled(
                self.company_id
            )
        return readiness.evaluate(
            channel,
            self._apix_recipient_values(),
            sender if sender is not None else self._apix_sender_values(),
            sending_enabled=sending_enabled,
            lines=self._apix_line_values(),
            due_date=self.invoice_date_due,
            **self._apix_content_readiness_inputs(channel),
        )

    def _apix_content_readiness_inputs(self, channel):
        """The finvoice content inputs of the readiness rules,
        worked out by the same builders as the message, so a warning and
        what the message states never disagree. Stored data only."""
        if channel not in APIX_CHANNELS or self.move_type not in CUSTOMER_TYPES:
            return {}
        move = self.sudo()
        vat_info = move._apix_vat_info()
        vat_lines = []
        lines = []
        for index, line in enumerate(move._apix_finvoice_product_lines(), start=1):
            taxes = move._apix_line_vat_taxes(line, vat_info)
            resolved = finvoice_vat.resolve_line(taxes)
            label = _("Line %(number)s (%(name)s)", number=index,
                      name=(line.name or line.product_id.name or "").split("\n")[0][:40])
            vat_lines.append(dict(resolved, line=label, name=label, taxes=taxes))
            lines.append({"untaxed": line.price_subtotal, "taxes": taxes})
        reasons = {
            tax_id: {"reason_lines": info["reason_lines"],
                     "exemption_code": info["exemption_code"]}
            for tax_id, info in vat_info.items()
        }
        partner = move.partner_id
        return {
            "vat_lines": vat_lines,
            "vat_entries": finvoice_vat.group_breakdown(lines, {}, reasons),
            "tax_names": {tax.id: tax.name for tax in
                          move._apix_finvoice_product_lines().tax_ids.flatten_taxes_hierarchy()},
            "references": move._apix_finvoice_references(),
            "delivery": move._apix_finvoice_delivery(),
            "buyer_vat_present": bool(partner.vat or partner.commercial_partner_id.vat),
        }

    # ------------------------------------------------------------------
    # Already sent
    # ------------------------------------------------------------------

    def _apix_already_sent_info(self):
        """``{when, channel, by, origin, before_install}`` of the newest
        attempt that counts as sent; ``{before_install: True, channel:
        "unknown"}`` when only the pre-install mark is set; False when never
        sent. A download, a refusal or a failure never counts."""
        self.ensure_one()
        if not self.id:
            return False
        attempt = self.env["goodahead.invoice.send.attempt"].sudo().search(
            [("move_id", "=", self.id), ("counts_as_sent", "=", True)],
            order="end_date desc, id desc",
            limit=1,
        )
        if attempt:
            return {
                "when": attempt.end_date,
                "channel": attempt.channel,
                "by": attempt.requested_by_id.name,
                "origin": attempt.origin,
                "before_install": False,
            }
        if self.sent_before_install:
            return {
                "when": False,
                "channel": "unknown",
                "by": False,
                "origin": False,
                "before_install": True,
            }
        return False

    def _apix_already_sent_message(self, info):
        if info["before_install"]:
            return _(
                "This invoice was already sent before APIX sending was installed (channel "
                "unknown). Send it again?"
            )
        channels = dict(self.env["goodahead.invoice.send.attempt"]._fields["channel"]
                        ._description_selection(self.env))
        return _(
            "This invoice was already sent on %(when)s as %(channel)s by %(by)s. Send it again?",
            when=format_datetime(self.env, info["when"]) if info["when"] else "",
            channel=channels.get(info["channel"], info["channel"]),
            by=info["by"] or "",
        )

    # ------------------------------------------------------------------
    # The fix-it wizard
    # ------------------------------------------------------------------

    def action_apix_open_fix(self):
        """Open the fix-it wizard for this invoice's invoice contact."""
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "goodahead_invoice_send_apix.goodahead_invoice_send_fix_action"
        )
        action["context"] = {"default_move_id": self.id, "default_return_to": "invoice"}
        return action

    # ------------------------------------------------------------------
    # The one check every send door runs first
    # ------------------------------------------------------------------

    def _apix_check_sendable(self, channel=None, resend_confirmed=False):
        """Refusals (``{code, message, items}``) that stop sending this
        invoice on ``channel``; empty = sendable. Pure checks over stored
        data: no write, no APIX call. Never picks another channel."""
        self.ensure_one()
        if self.move_type not in CUSTOMER_TYPES:
            return [_refusal(NOT_CUSTOMER_DOCUMENT, _(
                "Only customer invoices and credit notes can be sent."
            ))]
        if self.state != "posted":
            return [_refusal(NOT_POSTED, _(
                "Not sent: only posted, not cancelled invoices and credit notes can be sent."
            ))]
        channel = channel or self._apix_channel()
        refusals = []
        enabled = self.env["goodahead.apix.connection"]._apix_sending_enabled(self.company_id)
        if channel in APIX_CHANNELS and not enabled:
            refusals.append(_refusal(SENDING_OFF, _(
                "Sending through APIX is not switched on for %s.", self.company_id.name
            )))
        else:
            blocking = [
                item for item in self._apix_readiness(channel)["items"]
                if item["severity"] == readiness.BLOCK
            ]
            if blocking:
                refusals.append(_refusal(NOT_READY, _(
                    "Not sent: the invoice cannot be sent until these details are fixed: %s",
                    " ".join(item["message"] for item in blocking),
                ), blocking))
        info = not resend_confirmed and self._apix_already_sent_info()
        if info:
            refusals.append(_refusal(ALREADY_SENT, self._apix_already_sent_message(info), info))
        holder = self._apix_holder()
        if holder:
            refusals.append(_refusal(
                SEND_IN_PROGRESS, holder.message(self.env),
                {"user": holder.user_name, "since": holder.since},
            ))
        return refusals


def _refusal(code, message, items=None):
    return {"code": code, "message": message, "items": items}


def readiness_display(env, result, company, ready, not_ready):
    """``(level, html)`` of a readiness line: ``off``
    while the company's sending is off, else ready / warning / not_ready
    with each item and where it is fixed."""
    items = result["items"]
    if any(item["key"] == "sending_off" for item in items):
        return "off", Markup("<p>%s</p>") % env._(
            "Sending is not switched on for %s.", company.name
        )
    blocking = [item for item in items if item["severity"] == readiness.BLOCK]
    warnings = [item for item in items if item["severity"] != readiness.BLOCK]
    if blocking:
        level, title = "not_ready", not_ready
    else:
        level, title = ("warning" if warnings else "ready"), ready
    body = Markup("<p class='mb-1'><strong>%s</strong></p>") % title
    lines = [_readiness_item_html(env, item) for item in blocking + warnings]
    if lines:
        body += Markup("<ul class='mb-0'>%s</ul>") % Markup().join(lines)
    return level, body


def _readiness_item_html(env, item):
    text = env._(item["message"])  # pylint: disable=translation-not-lazy
    where = FIX_AT_LABELS.get(item.get("fix_at"))
    if where:
        text = env._("%(text)s (fix in: %(where)s)", text=text, where=env._(where))  # pylint: disable=translation-not-lazy
    if item["severity"] != readiness.BLOCK:
        return Markup("<li class='text-muted'>%s</li>") % text
    return Markup("<li>%s</li>") % text
