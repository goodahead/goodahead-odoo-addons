# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""SCR-06 — fetch the transfer credentials once with the APIX web login.

🔴 The clear web password never reaches the database. The client saves a
dialog record before its button runs (a separate request and transaction), so
`create`/`write` replace the typed password with its SHA-256 hash before the
row is written — APIX's RetrieveTransferID digest only ever needs that hash.
The hash is cleared as the first step of `action_fetch`, and that clear always
commits: the method reports failures on the dialog instead of raising. Only
the returned transfer id and key (plus the Business ID and environment) are
stored, on the company's connection.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from ..lib.apix_client import ApixClient, ApixError, ApixRejected, password_hash

_logger = logging.getLogger(__name__)

ADMIN_GROUP = "account.group_account_manager"


class GoodaheadApixFetchCredentials(models.TransientModel):
    _name = "goodahead.apix.fetch.credentials"
    _description = "Fetch APIX transfer credentials"

    connection_id = fields.Many2one("goodahead.apix.connection", ondelete="cascade")
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
    )
    environment = fields.Selection(
        [("production", "Production"), ("test", "Test")],
        required=True,
        default="test",
    )
    email = fields.Char(string="APIX email")
    # Always empty in the database: `create`/`write` turn it into the hash.
    password = fields.Char(string="APIX password", copy=False, groups=ADMIN_GROUP)
    password_sha256 = fields.Char(copy=False, groups=ADMIN_GROUP)
    business_id = fields.Char(string="Business ID")
    error_message = fields.Char(readonly=True)

    @api.model
    def _hash_password_vals(self, vals):
        if vals.get("password"):
            vals["password_sha256"] = password_hash(vals["password"])
        vals.pop("password", None)
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        return super().create([self._hash_password_vals(dict(vals)) for vals in vals_list])

    def write(self, vals):
        return super().write(self._hash_password_vals(dict(vals)))

    @api.model
    def _make_client(self, environment):
        return ApixClient(environment=environment)

    def _reopen(self, message):
        self.error_message = message
        return {
            "type": "ir.actions.act_window",
            "name": _("Fetch from APIX login"),
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_fetch(self):
        self.ensure_one()
        if not self.env.user.has_group(ADMIN_GROUP):
            raise AccessError(_("Only accounting administrators can fetch APIX credentials."))
        password_sha256 = self.password_sha256
        # 🔴 Clear first, and make sure that clear is COMMITTED: nothing after
        # it may raise out of this method, or the request's rollback would
        # restore the hash. Everything else runs in a savepoint and any
        # exception — expected or not — becomes a reason on the dialog.
        self.password_sha256 = False
        self.error_message = False
        try:
            with self.env.cr.savepoint():
                return self._fetch(password_sha256)
        except Exception as exc:  # noqa: BLE001 - see above: the clear must commit
            _logger.warning(
                "APIX credential fetch for company %s failed unexpectedly (%s)",
                self.company_id.id,
                type(exc).__name__,
            )
            return self._reopen(
                _("The credentials could not be fetched because of an unexpected error. "
                  "Try again, or enter the transfer ID and key by hand.")
            )
        finally:
            password_sha256 = None

    def _fetch(self, password_sha256):
        """The body of `action_fetch` once the hash is cleared. May raise:
        the caller turns anything unexpected into a plain reason."""
        if not (self.email and password_sha256 and self.business_id):
            return self._reopen(_("Enter the APIX email, password and Business ID."))

        try:
            answer = self._make_client(self.environment).retrieve_transfer_id(
                self.business_id, self.email, password_sha256=password_sha256
            )
        except ApixRejected as exc:
            return self._reopen(_("APIX rejected the login: %s", exc.message))
        except ApixError as exc:
            return self._reopen(_("The credentials could not be fetched: %s", exc.message))

        transfer_id = answer.get("TransferID")
        Connection = self.env["goodahead.apix.connection"]
        owner = Connection._find_credentials_owner(transfer_id, self.environment, exclude_company=self.company_id)
        if owner:
            return self._reopen(Connection._credentials_in_use_message(owner))

        vals = {
            "transfer_id": transfer_id,
            "transfer_key": answer.get("TransferKey"),
            "business_id": self.business_id,
            "environment": self.environment,
        }
        connection = self.connection_id or Connection.search([("company_id", "=", self.company_id.id)], limit=1)
        try:
            with self.env.cr.savepoint():
                if connection:
                    connection.write(vals)
                else:
                    connection = Connection.create(dict(vals, company_id=self.company_id.id))
        except (ValidationError, UserError) as exc:
            return self._reopen(str(exc))
        self.connection_id = connection
        _logger.info(
            "APIX transfer credentials fetched for company %s (%s)", self.company_id.id, self.environment
        )
        # AC-12: show the company APIX reports right away.
        connection.action_check_connection()
        return {
            "type": "ir.actions.act_window",
            "name": _("APIX connection"),
            "res_model": "goodahead.apix.connection",
            "res_id": connection.id,
            "view_mode": "form",
            "target": "current",
        }
