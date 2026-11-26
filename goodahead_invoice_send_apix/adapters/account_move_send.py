# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""APIX as Send & Print methods (19.0 adapter; contract §2.1, SCR-04,
ADR-0003, ADR-0008).

"E-invoice via APIX" (``apix``) and "Printed letter via APIX"
(``apix_letter``) sit next to email and download in the ERP's dialog:

- offered for a company whose sending is on; preselected by the invoice's
  transmit method (``einvoice`` → ``apix``, ``letter`` → ``apix_letter``,
  ``email`` → email). The choice never changes the transmit method.
- usable only when the invoice is ready for that channel; a not-ready
  invoice keeps the method listed but unticked, with what is missing as an
  alert (blocking once ticked).
- every APIX state is one of the dialog's alerts (keys are stable):
  ``apix_not_ready``, ``apix_in_progress``, ``apix_already_sent`` (blocking
  until "Send again anyway" is ticked), ``apix_copy_warning`` (never
  blocking), ``apix_sending_off`` (info).
- sending calls ``_apix_send_now(origin="send_print")`` with the PDF the
  dialog just rendered: the same outcome and history entry as the button.
  Only when a person presses the dialog's Send button (its context
  ``goodahead_person_send``): the ERP's batch cron and code calling the ERP's
  sending or the wizard's action never reach APIX; the invoice gets the error
  "Not sent through APIX" instead.
- every email the ERP's sending sends (dialog, batch cron, or code outside
  this add-on: ``erp_send``) is an ``email`` attempt, recorded once the
  caller's transaction commits — refused while an APIX send runs (the
  invoice row is locked meanwhile), ``send.email_failed`` when it raises; a
  send that only produced the PDF is a ``download`` attempt that never
  counts. The background job records its own
  email (context ``goodahead_send_recorded``).
"""

from functools import partial

from odoo import _, api, models
from odoo.tools import SQL

from ..models.account_move import (
    APIX_CHANNELS,
    CUSTOMER_TYPES,
    NOT_READY,
    SENDING_OFF,
    SendInProgress,
)
from ..models.account_move_queue import EMAIL_FAILED

#: Send & Print method -> APIX channel.
METHOD_CHANNEL = {"apix": "einvoice", "apix_letter": "letter"}
#: Transmit method -> the Send & Print method preselected for it.
TRANSMIT_METHOD = {"einvoice": "apix", "letter": "apix_letter", "email": "email"}
#: Context key: a list the dialog collects ``SendResult`` objects in.
RESULTS_KEY = "apix_send_results"
#: Sending setting (and batch ``sending_data`` key): the attempt origin.
ORIGIN_KEY = "goodahead_origin"
#: Context: the background job records its own email attempt.
RECORDED_KEY = "goodahead_send_recorded"
#: Context: ``{move_id: (origin, user_id, single)}`` of the emails being sent.
EMAILS_KEY = "goodahead_send_emails"
#: Sending setting: a person sends from the dialog now (not the batch cron,
#: not code calling the ERP's sending) — the only case APIX is called here.
PERSON_KEY = "goodahead_person_sends"
#: Context of the Send & Print dialog's Send buttons (the view): a person
#: pressed Send. Code creating the wizard and calling its action has none.
PERSON_CONTEXT = "goodahead_person_send"


class AccountMoveSend(models.AbstractModel):
    _inherit = "account.move.send"

    # ------------------------------------------------------------------
    # Methods, defaults and settings
    # ------------------------------------------------------------------

    @api.model
    def _is_applicable_to_company(self, method, company):
        if method not in METHOD_CHANNEL:
            return super()._is_applicable_to_company(method, company)
        return self.env["goodahead.apix.connection"]._apix_sending_enabled(company)

    @api.model
    def _is_applicable_to_move(self, method, move, **move_data):
        if method not in METHOD_CHANNEL:
            return super()._is_applicable_to_move(method, move, **move_data)
        if move.move_type not in CUSTOMER_TYPES or move.state != "posted":
            return False
        codes = {
            refusal["code"]
            for refusal in move._apix_check_sendable(METHOD_CHANNEL[method], resend_confirmed=True)
        }
        return not codes & {NOT_READY, SENDING_OFF}

    @api.model
    def _get_default_sending_methods(self, move) -> set:
        """The invoice's transmit method picks the method in place of the
        customer's ERP setting; APIX only while the company sends."""
        methods = super()._get_default_sending_methods(move)
        method = move.move_type in CUSTOMER_TYPES and TRANSMIT_METHOD.get(move.transmit_method)
        if not method:
            return methods
        if method in METHOD_CHANNEL and not self._is_applicable_to_company(method, move.company_id):
            return methods
        partner = move.commercial_partner_id.with_company(move.company_id)
        return (set(methods) - {partner.invoice_sending_method or "email"}) | {method}

    @api.model
    def _get_default_sending_settings(self, move, from_cron=False, **custom_settings):
        vals = super()._get_default_sending_settings(move, from_cron=from_cron, **custom_settings)
        vals["apix_resend_confirmed"] = bool(custom_settings.get("apix_resend_confirmed"))
        # who started it: the dialog (now, or deferred to the batch cron) or
        # the ERP's sending outside it
        origin = custom_settings.get(ORIGIN_KEY) or self.env.context.get(ORIGIN_KEY)
        if not origin and from_cron:
            origin = (move.sending_data or {}).get(ORIGIN_KEY)
        vals[ORIGIN_KEY] = origin or "erp_send"
        # APIX only when a person presses the dialog's Send button (its
        # context), never later and never from code (AC-10, AC-55; review F3)
        vals[PERSON_KEY] = bool(self.env.context.get(PERSON_CONTEXT)) and not from_cron
        return vals

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    @api.model
    def _get_alerts(self, moves, moves_data):
        alerts = super()._get_alerts(moves, moves_data)
        for move in moves:
            if move.move_type not in CUSTOMER_TYPES:
                continue
            for key, alert in move._apix_send_print_alerts(self._apix_move_data(move, moves_data)):
                _merge_alert(alerts, key, alert)
        return alerts

    @api.model
    def _apix_move_data(self, move, moves_data):
        data = dict(moves_data.get(move) or {})
        data.setdefault("apix_resend_confirmed", self.env.context.get("apix_resend_confirmed"))
        data["single"] = len(moves_data) == 1
        return data

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    @api.model
    def _call_web_service_after_invoice_pdf_render(self, invoices_data):
        super()._call_web_service_after_invoice_pdf_render(invoices_data)
        for invoice, invoice_data in invoices_data.items():
            methods = [m for m in METHOD_CHANNEL if m in (invoice_data.get("sending_methods") or [])]
            if not methods:
                continue
            if not invoice_data.get(PERSON_KEY):
                _apix_not_started_by_a_person(invoice, invoice_data)
                continue
            self._apix_send_print_one(invoice, invoice_data, methods[0])

    @api.model
    def _apix_send_print_one(self, invoice, invoice_data, method):
        """Send one invoice from the dialog; a refusal or failure becomes
        the dialog's error for it."""
        result = invoice.with_env(self.env)._apix_send_now(
            "send_print",
            channel=METHOD_CHANNEL[method],
            resend_confirmed=invoice_data.get("apix_resend_confirmed"),
            pdf=_rendered_pdf(invoice, invoice_data),
        )
        collector = self.env.context.get(RESULTS_KEY)
        if collector is not None:
            collector.append((invoice, result))
        if result.outcome == "accepted":
            return
        _kind, message = invoice.with_env(self.env)._apix_outcome_notice(result)
        invoice_data["error"] = {"error_title": _("Not sent through APIX"), "errors": [message]}

    # ------------------------------------------------------------------
    # Email and download recording
    # ------------------------------------------------------------------

    @api.model
    def _hook_if_success(self, moves_data, from_cron=False):
        super()._hook_if_success(moves_data, from_cron=from_cron)
        if self.env.context.get(RECORDED_KEY):
            return
        Attempt = self.env["goodahead.invoice.send.attempt"]
        for move, move_data in moves_data.items():
            if set(move_data.get("sending_methods") or []) != {"manual"}:
                continue
            # only the PDF was produced: listed once the caller commits, never counts
            Attempt._ledger_after_transaction(move, on_commit=("downloaded", partial(
                Attempt._ledger_record, move, "download", "downloaded", move_data[ORIGIN_KEY],
                self._apix_author(move_data), {},
            )))

    @api.model
    def _send_mails(self, moves_data):
        if self.env.context.get(RECORDED_KEY):
            return super()._send_mails(moves_data)
        emails = {
            move.id: (move_data[ORIGIN_KEY], self._apix_author(move_data).id, len(moves_data) == 1)
            for move, move_data in moves_data.items()
        }
        return super(AccountMoveSend, self.with_context(**{EMAILS_KEY: emails}))._send_mails(moves_data)

    @api.model
    def _send_mail(self, move, mail_template, **kwargs):
        """Each email the ERP sends is one ``email`` attempt.

        One send at a time: the invoice row is locked for this transaction
        (``FOR UPDATE``, as the ERP's batch cron already locks it), so an APIX
        claim meanwhile waits and answers "in progress"; an APIX send already
        running refuses the email. The email exists only if this transaction
        commits, so the attempt is recorded then: ``sent`` on commit, nothing
        on rollback; a raising email step is ``failed`` either way (R5)."""
        if self.env.context.get(RECORDED_KEY):
            return super()._send_mail(move, mail_template, **kwargs)
        origin, user_id, single = (self.env.context.get(EMAILS_KEY) or {}).get(
            move.id, ("erp_send", self.env.uid, True)
        )
        Attempt = self.env["goodahead.invoice.send.attempt"]
        holder = self._apix_email_guard(move)
        if holder:
            message = _("Email not sent: %s", holder.message(self.env))
            if single:
                raise SendInProgress(message)
            move.message_post(body=message)
            return None
        user = self.env["res.users"].browse(user_id)
        try:
            with self.env.cr.savepoint():
                result = super()._send_mail(move, mail_template, **kwargs)
        except Exception as exc:
            text = _("The email could not be sent: %s", exc)
            failed = partial(Attempt._ledger_record, move, "email", "failed", origin, user,
                             {"reason": f"[{EMAIL_FAILED}] {text}"})
            Attempt._ledger_after_transaction(move, on_commit=("failed", failed),
                                              on_rollback=("failed", failed))
            raise
        Attempt._ledger_after_transaction(move, on_commit=("sent", partial(
            Attempt._ledger_record, move, "email", "sent", origin, user, {},
        )))
        return result

    @api.model
    def _apix_email_guard(self, move):
        """Lock the invoice row for this transaction and return the
        ``Holder`` of a send of it already in progress, or None."""
        self.env.cr.execute(SQL("SELECT id FROM account_move WHERE id = %s FOR UPDATE", move.id))
        return self.env["goodahead.invoice.send.attempt"]._ledger_holder_now(move)

    @api.model
    def _apix_author(self, move_data):
        return self.env["res.users"].browse(move_data.get("author_user_id") or self.env.uid)


class AccountMove(models.Model):
    _inherit = "account.move"

    def _apix_send_print_alerts(self, data):
        """``[(key, alert)]`` of this invoice in the Send & Print dialog."""
        self.ensure_one()
        alerts = []
        if self.send_copy_warning:
            alerts.append(("apix_copy_warning", {"level": "warning",
                                                  "message": self.send_copy_warning}))
        holder = self._apix_holder()
        if holder:
            alerts.append(("apix_in_progress", {"level": "danger",
                                                 "message": holder.message(self.env)}))
        enabled = self.env["goodahead.apix.connection"]._apix_sending_enabled(self.company_id)
        if self.transmit_method in APIX_CHANNELS and not enabled:
            alerts.append(("apix_sending_off", {"level": "info", "message": _(
                "Sending through APIX is not switched on for %s.", self.company_id.name
            )}))
        if not enabled or self.state != "posted":
            return alerts
        ticked = [m for m in METHOD_CHANNEL if m in (data.get("sending_methods") or [])]
        alerts += self._apix_not_ready_alerts(ticked, data)
        if ticked:
            alerts += self._apix_already_sent_alerts(data)
        return alerts

    def _apix_not_ready_alerts(self, ticked, data):
        """Not ready for the ticked APIX method (blocking), or for the
        transmit method's APIX channel (a warning while not ticked)."""
        channel = METHOD_CHANNEL[ticked[0]] if ticked else self.transmit_method
        if channel not in APIX_CHANNELS:
            return []
        refusals = [r for r in self._apix_check_sendable(channel, resend_confirmed=True)
                    if r["code"] == NOT_READY]
        if not refusals:
            return []
        alert = {
            "level": "danger" if ticked else "warning",
            "message": _("Not ready for %(channel)s: %(missing)s", channel=_channel_label(self, channel),
                         missing=" ".join(item["message"] for item in refusals[0]["items"])),
        }
        if data.get("single"):
            alert.update(action_text=_("Fix"), action=self.action_apix_open_fix())
        return [("apix_not_ready", alert)]

    def _apix_already_sent_alerts(self, data):
        info = self._apix_already_sent_info()
        if not info:
            return []
        confirmed = bool(data.get("apix_resend_confirmed"))
        message = self._apix_already_sent_message(info)
        if not confirmed:
            message = _("%s Tick \"Send again anyway\" to send it again.", message)
        return [("apix_already_sent", {"level": "warning" if confirmed else "danger",
                                        "message": message})]


def _channel_label(move, channel):
    labels = {"einvoice": move.env._("e-invoice"), "letter": move.env._("printed letter")}
    return labels.get(channel, channel)


def _merge_alert(alerts, key, alert):
    """One alert per key; with several invoices the messages add up and the
    most severe level wins."""
    if key not in alerts:
        alerts[key] = alert
        return
    order = ("info", "warning", "danger")
    current = alerts[key]
    if order.index(alert["level"]) > order.index(current["level"]):
        current["level"] = alert["level"]
    if alert["message"] not in current["message"]:
        current["message"] = f"{current['message']}\n{alert['message']}"


def _apix_not_started_by_a_person(invoice, invoice_data):
    """The invoice's error when the ERP's sending reaches an APIX method
    without a person's send (batch cron, other code): nothing goes to APIX,
    and the ERP shows or posts why."""
    env = invoice.env
    invoice_data["error"] = {
        "error_title": env._("Not sent through APIX"),
        "errors": [env._(
            "Sending through APIX starts only when a user sends the invoice: the Send via "
            "APIX button, Send & Print, or the mass sending wizard."
        )],
    }


def _rendered_pdf(invoice, invoice_data):
    """``(filename, bytes)`` the dialog rendered (or the invoice's stored
    PDF); None makes the service render it."""
    values = invoice_data.get("pdf_attachment_values")
    if values:
        return values["name"], values["raw"]
    report = invoice.invoice_pdf_report_id
    if report:
        return report.name, report.raw
    return None
