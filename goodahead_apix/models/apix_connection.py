# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Per-company APIX connection.

One record per company holds the environment, the transfer credentials, the
Business ID the company is known by at APIX and the outcome of the last
connection check. Receiving and (later) sending share it.

`receiving_state` (off / shadow / live) is deliberately NOT defined here: it is
added by `goodahead_invoice_import_apix`, so this connection stays usable
without the receiving add-on.

🔴 SECRETS. `transfer_key` (and, by extension, every request digest) grants
read access to the company's invoice inbox. It is restricted to accounting
administrators, never copied, never tracked in chatter and never logged. Log
lines and status messages carry the company, the environment and the APIX
outcome only.
"""

import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.osv import expression

from ..lib.apix_client import ApixClient, ApixError, ApixRejected

_logger = logging.getLogger(__name__)

ADMIN_GROUP = "account.group_account_manager"
CREDENTIAL_FIELDS = ("transfer_id", "transfer_key", "environment", "business_id")
#: Never exported (see the field definitions).
SECRET_FIELDS = ("transfer_id", "transfer_key")
#: Source text of the copy warning (`_apix_copy_warning_text` translates it).
COPY_WARNING = (
    "This is a mirror environment holding production credentials — "
    "sending reaches real customers."
)
NEUTRALIZED_PARAM = "database.is_neutralized"


def normalize_business_id(value):
    """Digits of a Finnish Business ID, so `1234567-8`, `FI12345678` and
    ` 1234567 8 ` compare equal."""
    return re.sub(r"\D", "", value or "")


class GoodaheadApixConnection(models.Model):
    _name = "goodahead.apix.connection"
    _description = "APIX connection"
    _inherit = ["mail.thread"]
    _order = "company_id"

    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        ondelete="cascade",
        default=lambda self: self.env.company,
        tracking=True,
    )
    environment = fields.Selection(
        [("production", "Production"), ("test", "Test")],
        required=True,
        default="test",
        tracking=True,
    )
    # 🔴 NEVER EXPORTED, NOT EVEN BY AN ADMINISTRATOR: an export is a file
    # that leaves the system and outlives every access rule. `exportable`
    # hides the fields from the export dialog; `_export_rows` refuses a
    # request that names them anyway (a saved export list, an RPC call, a
    # relational path from another model).
    transfer_id = fields.Char(
        string="Transfer ID",
        groups=ADMIN_GROUP,
        copy=False,
        exportable=False,
    )
    transfer_key = fields.Char(
        string="Transfer key",
        groups=ADMIN_GROUP,
        copy=False,
        exportable=False,
    )
    business_id = fields.Char(
        string="Business ID",
        required=True,
        tracking=True,
        help="The company's Business ID (y-tunnus). The connection check fails "
        "when APIX reports a different one.",
    )
    status = fields.Selection(
        [("unchecked", "Not checked"), ("working", "Working"), ("rejected", "Rejected")],
        required=True,
        default="unchecked",
        readonly=True,
        copy=False,
        tracking=True,
    )
    status_message = fields.Char(readonly=True, copy=False)
    last_check_date = fields.Datetime(readonly=True, copy=False)
    # The `soft` / `ver` parameters of APIX's sending interface (spec §8):
    # settings, so a changed value needs no release.
    interface_name = fields.Char(
        string="Sending interface name",
        required=True,
        default="Standard",
        groups=ADMIN_GROUP,
        help="Sent to APIX as the name of the sending software (soft).",
    )
    interface_version = fields.Char(
        string="Sending interface version",
        required=True,
        default="1.0",
        groups=ADMIN_GROUP,
        help="Sent to APIX as the version of the sending software (ver).",
    )

    _sql_constraints = [
        (
            "company_uniq",
            "UNIQUE(company_id)",
            "Each company has exactly one APIX connection.",
        ),
        # Backstop only: `_check_credentials_free` refuses first, naming the
        # company that uses the credentials. NULLs are distinct, so
        # connections without credentials do not collide.
        (
            "transfer_environment_uniq",
            "UNIQUE(transfer_id, environment)",
            "These APIX transfer credentials are already used by another company.",
        ),
    ]

    @api.depends("company_id")
    def _compute_display_name(self):
        for connection in self:
            connection.display_name = _("APIX — %s", connection.company_id.name or "")

    # ------------------------------------------------------------------
    # Uniqueness of the APIX account across companies
    # ------------------------------------------------------------------

    @api.model
    def _find_credentials_owner(self, transfer_id, environment, exclude_company=None):
        """The connection of ANOTHER company using these credentials, or an
        empty recordset. Same transfer id in test and production is allowed."""
        if not transfer_id or not environment:
            return self.browse()
        domain = [("transfer_id", "=", transfer_id), ("environment", "=", environment)]
        if exclude_company:
            domain.append(("company_id", "!=", exclude_company.id))
        return self.sudo().with_context(active_test=False).search(domain, limit=1)

    @api.model
    def _credentials_in_use_message(self, owner):
        return _(
            "These APIX transfer credentials are already used by the company %(company)s. "
            "One APIX account can be connected to one company only.",
            company=owner.company_id.name,
        )

    def _check_credentials_free(self, vals):
        """Refuse BEFORE the write, so the message names the other company
        instead of surfacing the database constraint."""
        if "transfer_id" not in vals and "environment" not in vals:
            return
        for connection in self.sudo():
            transfer_id = vals["transfer_id"] if "transfer_id" in vals else connection.transfer_id
            environment = vals.get("environment") or connection.environment
            owner = self._find_credentials_owner(transfer_id, environment, exclude_company=connection.company_id)
            if owner:
                raise ValidationError(self._credentials_in_use_message(owner))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            company = self.env["res.company"].browse(vals.get("company_id") or self.env.company.id)
            owner = self._find_credentials_owner(
                vals.get("transfer_id"), vals.get("environment") or "test", exclude_company=company
            )
            if owner:
                raise ValidationError(self._credentials_in_use_message(owner))
        return super().create(vals_list)

    def _export_rows(self, fields, *, _is_toplevel_call=True):
        secret = sorted({path[0] for path in fields if path and path[0] in SECRET_FIELDS})
        if secret:
            raise AccessError(
                _(
                    "The APIX transfer credentials (%(fields)s) are never exported.",
                    fields=", ".join(secret),
                )
            )
        return super()._export_rows(fields, _is_toplevel_call=_is_toplevel_call)

    def write(self, vals):
        self._check_credentials_free(vals)
        if "status" not in vals and any(name in vals for name in CREDENTIAL_FIELDS):
            # New credentials are unproven: stop fetching until checked.
            vals = dict(
                vals,
                status="unchecked",
                status_message=_("Connection details changed — check the connection."),
            )
        return super().write(vals)

    # ------------------------------------------------------------------
    # Copy safety
    # ------------------------------------------------------------------

    def _apix_copy_warning(self):
        """The warning every sending surface shows, or False. Never blocks.

        Super-chained: receiving answers when it receives in mirror mode;
        here the connection answers on a neutralized database copy whose
        production credentials were entered again (neutralization clears
        them, `data/neutralize.sql`). One text either way.
        """
        if not self:
            return False
        self.ensure_one()
        if not self._apix_is_neutralized_copy():
            return False
        connection = self.sudo()
        if connection.environment != "production":
            return False
        if not (connection.transfer_id and connection.transfer_key):
            return False
        return self._apix_copy_warning_text()

    @api.model
    def _apix_is_neutralized_copy(self):
        return bool(self.env["ir.config_parameter"].sudo().get_param(NEUTRALIZED_PARAM))

    @api.model
    def _apix_copy_warning_text(self):
        return _(
            "This is a mirror environment holding production credentials — "
            "sending reaches real customers."
        )

    # ------------------------------------------------------------------
    # APIX calls
    # ------------------------------------------------------------------

    def _client(self):
        """An `ApixClient` for this connection's environment."""
        self.ensure_one()
        return ApixClient(environment=self.environment)

    def action_check_connection(self):
        """Ask APIX for the company behind the transfer credentials (AC-12/13).

        Working only when APIX accepts the credentials AND reports the
        connection's Business ID — and the company's own Business ID
        (`company_registry`) when the company has one. Anything else leaves
        the connection not working, with a plain-language reason in
        `status_message`. A service error at APIX (`apix.service_error`)
        leaves it unchecked, not rejected: the credentials were not refused.
        """
        if not self.env.user.has_group(ADMIN_GROUP) and not self.env.su:
            raise UserError(_("Only accounting administrators can check the APIX connection."))
        for connection in self:
            connection._check_connection()
        return True

    def _check_connection(self):
        self.ensure_one()
        now = fields.Datetime.now()
        if not self.transfer_id or not self.transfer_key:
            self._set_status("unchecked", _("Enter the transfer ID and transfer key first."), now)
            return
        try:
            answer = self._client().company_info(self.transfer_id, self.transfer_key)
        except ApixRejected as exc:
            self._set_status(
                "rejected",
                _("APIX rejected the transfer credentials: %s", exc.message),
                now,
            )
            return
        except ApixError as exc:
            self._set_status(
                "unchecked",
                _("The connection could not be checked: %s", exc.message),
                now,
            )
            return

        company = (answer.get("groups") or [{}])[0]
        reported_id = company.get("y-tunnus", "")
        reported_name = company.get("Name", "")
        if normalize_business_id(reported_id) != normalize_business_id(self.business_id):
            self._set_status(
                "rejected",
                _(
                    "APIX reports these credentials for %(name)s (Business ID %(reported)s), "
                    "not for Business ID %(expected)s. Fetching stays off.",
                    name=reported_name or _("an unnamed company"),
                    reported=reported_id or _("none"),
                    expected=self.business_id,
                ),
                now,
            )
            return
        # AC-12, N5: the APIX account must be THIS company's. The Business
        # ID typed on the connection could be a typo of the same wrong
        # number; the company's own registry number, when it is set, is the
        # independent check, so credentials of another company never fetch
        # invoices into these books.
        registry = self.company_id.company_registry
        if registry and normalize_business_id(registry) != normalize_business_id(reported_id):
            self._set_status(
                "rejected",
                _(
                    "APIX reports these credentials for %(name)s (Business ID %(reported)s), "
                    "but the company %(company)s has the Business ID %(registry)s. "
                    "Fetching stays off.",
                    name=reported_name or _("an unnamed company"),
                    reported=reported_id or _("none"),
                    company=self.company_id.name,
                    registry=registry,
                ),
                now,
            )
            return
        self._set_status(
            "working",
            _("Working — %(name)s (%(business_id)s)", name=reported_name, business_id=reported_id),
            now,
        )

    def _set_status(self, status, message, when):
        # `status` in vals → `write` does not reset it back to unchecked.
        self.write({"status": status, "status_message": message, "last_check_date": when})
        _logger.info(
            "APIX connection check for company %s (%s): %s",
            self.company_id.id,
            self.environment,
            status,
        )

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def action_open_fetch_credentials(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Fetch from APIX login"),
            "res_model": "goodahead.apix.fetch.credentials",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_connection_id": self.id,
                "default_company_id": self.company_id.id,
                "default_environment": self.environment,
                "default_business_id": self.business_id,
            },
        }

    # ------------------------------------------------------------------
    # Connection trouble in the attention bubble
    # ------------------------------------------------------------------

    @api.model
    def _attention_connection_domain(self):
        """Connections that are "not working". Here: refused credentials or
        contract (`status = rejected`); the sending add-on ORs in an open
        sending pause. `unchecked` is not trouble."""
        return [("status", "=", "rejected")]

    @api.model
    def _attention_connection_trouble_section(self):
        """The `connection_trouble` section: troubled connections of the
        user's allowed companies, each opening that company's connection."""
        domain = expression.AND([
            self._attention_connection_domain(),
            [("company_id", "in", self.env.companies.ids)],
        ])
        connections = self.search(domain, order="last_check_date desc, id desc")
        return {
            "label": _("APIX connection not working"),
            "count": len(connections),
            "items": [
                {
                    "id": connection.id,
                    "title": connection.company_id.name,
                    "subtitle": connection.status_message or "",
                    "date": fields.Datetime.to_string(connection.last_check_date),
                }
                for connection in connections
            ],
            "action": {
                "type": "ir.actions.act_window",
                "name": _("APIX connections not working"),
                "res_model": self._name,
                "views": [[False, "list"], [False, "form"]],
                "domain": domain,
            },
            "item_action": {
                "type": "ir.actions.act_window",
                "res_model": self._name,
                "views": [[False, "form"]],
            },
        }

    # ------------------------------------------------------------------
    # Public API for the receiving / sending add-ons
    # ------------------------------------------------------------------

    @api.model
    def _get_working_connection(self, company):
        """The company's connection when its last check succeeded, else an
        empty recordset. Callers (the Scheduler) must not fetch without one.
       The record is returned in the caller's environment: reading
        the credentials still needs administrator rights or sudo."""
        connection = self.sudo().search(
            [("company_id", "=", company.id), ("status", "=", "working")],
            limit=1,
        )
        if not connection.transfer_id or not connection.transfer_key:
            return self.browse()
        return connection.with_env(self.env)
