# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""No secret ever leaves its field (test-plan edge case "storage key
present in logs — never"; spec §6 security; ADR-0007).

A full cycle — credentials fetched with the APIX web login, connection check,
Scheduler run, package imports, mark-received, daily checks, a technical
failure with retry, a rejected listing — runs with the REAL `ApixClient`
(digests and URLs computed for real) over an in-process HTTP double that
checks every digest. All log output is captured (root logger and every
logger at DEBUG) together with the chatter, tracking values, to-dos, job
results and the readable fields; none of them may contain the web password,
its hash, the transfer key, a storage key, a request digest or `SHA-256:`.

The one exception is `odoo.sql_db`'s DEBUG query log (`--log-level
debug_sql`): it prints every SQL parameter by design, including the values
written to the secret fields themselves; it stays at INFO here.
"""

import logging
import re
from datetime import date
from urllib.parse import parse_qsl, urlsplit
from unittest.mock import patch
from xml.sax.saxutils import escape

import requests

from odoo.tests import Form, new_test_user, tagged

from odoo.addons.goodahead_apix.lib.apix_client import (
    DIGEST_PREFIX,
    ApixClient,
    compute_digest,
    password_hash,
)

from .apix_e2e_common import ApixE2ECase, make_zip, simple_finvoice

PASSWORD = "Web-Pa55word-T17-Never-Logged"
EMAIL = "apix.t17@example.test"
BUSINESS_ID = "0000006-0"
TRANSFER_ID = "f17f17f1-0000-4000-8000-000000000017"
TRANSFER_KEY = "tk-T17-9d2e1c7a-transfer-key-never-logged"
STORAGE_KEYS = {
    "SEC-1": "sk-T17-0001-storage-key-never-logged",
    "SEC-2": "sk-T17-0002-storage-key-never-logged",
    "SEC-3": "sk-T17-0003-storage-key-never-logged",
}


class _Response:
    def __init__(self, content, status_code=200):
        self.content = content
        self.status_code = status_code


def _response_xml(groups, status="OK", code="5000", text="OK"):
    content = "".join(
        "<Group>"
        + "".join(
            f'<Value type="{key}">{escape(str(value))}</Value>' for key, value in group.items()
        )
        + "</Group>"
        for group in groups
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<Response><Status>{status}</Status><StatusCode>{code}</StatusCode>"
        f'<FreeText language="en">{text}</FreeText><Content>{content}</Content></Response>'
    ).encode()


class FakeApixHttp:
    """APIX over HTTP, in process: answers the documented operations and
    rejects any request whose digest is not made with the right secret."""

    def __init__(self):
        self.urls = []
        self.digests = set()
        self.packages = {}
        self.status = {}
        self.rejected = False
        #: (storage id, markreceived "yes"/"no") -> failures served first:
        #: "network" (requests error) or "http500"
        self.failures = {}
        self.marked = []

    def add_package(self, storage_id, number):
        self.packages[storage_id] = {
            "StorageID": storage_id,
            "StorageKey": STORAGE_KEYS[storage_id],
            "DocumentID": number,
            "SenderName": "Example Supplier Oy",
            "DocumentName": "invoice.xml",
            "DocumentDate": date.today().strftime("%Y%m%d"),
            "_body": make_zip(("invoice.xml", simple_finvoice(number))),
        }
        self.status[storage_id] = "UNRECEIVED"

    def _err(self):
        return _Response(_response_xml([], "ERR", "4704", "Authentication failed"))

    def request(self, method, url, timeout=None):
        self.urls.append(url)
        parts = urlsplit(url)
        params = dict(parse_qsl(parts.query))
        self.digests.add(params.get("d", ""))
        operation = parts.path.strip("/")
        return getattr(self, "_op_" + operation.replace("-", "_"))(url, params)

    def _op_app_transferID(self, url, params):
        expected = compute_digest(
            [params["id"], params["idq"], params["uid"], params["ts"]], password_hash(PASSWORD)
        )
        if params["d"] != expected or params["uid"] != EMAIL:
            return self._err()
        group = {"TransferID": TRANSFER_ID, "TransferKey": TRANSFER_KEY, "UniqueCompanyID": "u-17"}
        return _Response(_response_xml([group]))

    def _transfer_ok(self, params):
        expected = compute_digest([params["TraID"], params["t"]], TRANSFER_KEY)
        return not self.rejected and params["TraID"] == TRANSFER_ID and params["d"] == expected

    def _op_getcompanyinfo(self, url, params):
        if not self._transfer_ok(params):
            return self._err()
        return _Response(_response_xml([{"y-tunnus": BUSINESS_ID, "Name": "Example Receiver Oy"}]))

    def _op_list2(self, url, params):
        if not self._transfer_ok(params):
            return self._err()
        groups = [
            dict(
                {key: value for key, value in package.items() if not key.startswith("_")},
                StorageStatus=self.status[storage_id],
            )
            for storage_id, package in self.packages.items()
        ]
        return _Response(_response_xml(groups, code="2700"))

    def _op_download(self, url, params):
        storage_id = params["SID"]
        package = self.packages.get(storage_id)
        expected = package and compute_digest(
            [params["markreceived"], storage_id, params["t"]], package["StorageKey"]
        )
        if not package or params["d"] != expected:
            return _Response(b"")
        failures = self.failures.get((storage_id, params["markreceived"]))
        if failures:
            failure = failures.pop(0)
            if failure == "network":
                # requests puts the whole URL — digest included — in the text.
                raise requests.ConnectionError(f"Max retries exceeded with url: {url}")
            return _Response(b"<html>Internal error</html>", status_code=500)
        if params["markreceived"] == "yes":
            self.status[storage_id] = "RECEIVED"
            self.marked.append(storage_id)
        return _Response(package["_body"])


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


@tagged("post_install", "-at_install", "apix_e2e")
class TestApixSecretsNeverLeak(ApixE2ECase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = new_test_user(
            cls.env,
            login="t17_secret_admin",
            email="t17.secret.admin@example.test",
            groups="base.group_user,account.group_account_manager",
            company_id=cls.company.id,
            company_ids=[(6, 0, cls.company.ids)],
        )

    def setUp(self):
        # The real client: no `FakeApix` at the client boundary, the HTTP
        # double below it instead.
        super(ApixE2ECase, self).setUp()
        if not self.journal:
            self.skipTest("no purchase journal on this database")
        self.http = FakeApixHttp()
        http = self.http

        def client(record, *args, **kwargs):
            environment = args[0] if args else getattr(record, "environment", "test")
            return ApixClient(environment=environment, session=http)

        for model, name in (
            ("goodahead.apix.connection", "_client"),
            ("goodahead.apix.fetch.credentials", "_make_client"),
        ):
            patcher = patch.object(type(self.env[model]), name, autospec=True, side_effect=client)
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- capture -----------------------------------------------------------------

    def _start_capture(self):
        handler = _Capture()
        root = logging.getLogger()
        saved = {"": root.level}
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        for name, logger in list(logging.Logger.manager.loggerDict.items()):
            if isinstance(logger, logging.Logger) and logger.level not in (
                logging.NOTSET,
                logging.DEBUG,
            ):
                saved[name] = logger.level
                logger.setLevel(logging.DEBUG)
        sql = logging.getLogger("odoo.sql_db")
        saved.setdefault("odoo.sql_db", sql.level)
        sql.setLevel(logging.INFO)

        def stop():
            root.removeHandler(handler)
            for name, level in saved.items():
                logging.getLogger(name or None).setLevel(level)

        self.addCleanup(stop)
        return handler

    def _secrets(self):
        secrets = [PASSWORD, password_hash(PASSWORD), TRANSFER_KEY, *STORAGE_KEYS.values()]
        secrets += [digest for digest in self.http.digests if digest]
        secrets += [digest[len(DIGEST_PREFIX):] for digest in self.http.digests if digest]
        return secrets

    def _assert_clean(self, where, text):
        text = str(text or "")
        self.assertNotIn(DIGEST_PREFIX, text, f"a digest in {where}")
        for secret in self._secrets():
            self.assertNotIn(secret, text, f"a secret in {where}")

    # -- the cycle -----------------------------------------------------------------

    def test_full_cycle_leaks_no_secret(self):
        for number, storage_id in enumerate(STORAGE_KEYS, start=1):
            self.http.add_package(storage_id, f"SEC-{number}")
        # Technical failures: an import download cut by the network (its
        # exception text holds the URL), a mark answered with HTTP 500.
        self.http.failures = {("SEC-1", "no"): ["network"], ("SEC-2", "yes"): ["http500"]}
        first_message = self.env["mail.message"].sudo().search([], order="id desc", limit=1).id or 0
        first_job = self.env["queue.job"].sudo().search([], order="id desc", limit=1).id or 0
        handler = self._start_capture()

        # 1. The Administrator fetches the credentials with the web login.
        dialog = Form(
            self.env["goodahead.apix.fetch.credentials"]
            .with_user(self.admin)
            .with_context(default_connection_id=self.connection.id)
        )
        dialog.environment = "test"
        dialog.email = EMAIL
        dialog.password = PASSWORD
        dialog.business_id = BUSINESS_ID
        wizard = dialog.save()
        wizard.action_fetch()
        self.connection.invalidate_recordset()
        self.assertEqual(self.connection.status, "working", self.connection.status_message)
        self.assertEqual(self.connection.transfer_key, TRANSFER_KEY)
        self.assertEqual(self.connection.receiving_state, "live")

        # 2. The Scheduler: run, imports (one interrupted, retried), marks
        # (one failing once, retried).
        timings = []
        self._scheduler_cycle(timings=timings)
        records = self._apix_records()
        self.assertEqual(len(records), 3)
        self.assertEqual(set(records.mapped("state")), {"imported"})
        self.assertTrue(all(records.mapped("apix_mark_confirmed")))
        self.assertEqual(sorted(self.http.marked), sorted(STORAGE_KEYS))
        retried = [result for _k, _s, result in timings if not isinstance(result, str)]
        self.assertEqual(len(retried), 2, "one retried import, one retried mark")

        # 3. The daily checks, then a rejected listing (to-do with the reason).
        self.Connection._cron_apix_daily_checks()
        self._perform(("apix-daily:",))
        self.http.rejected = True
        self.assertEqual(self.Connection._apix_run(self.company.id), "rejected")
        self.assertTrue(self.connection._apix_open_todos("rejected"))
        self.http.rejected = False
        self.connection.action_check_connection()

        # Every request carried a digest, and the double checked each one.
        self.assertTrue(self.http.digests)
        self.assertTrue(all(d.startswith(DIGEST_PREFIX) for d in self.http.digests))

        # -- nothing leaked: logs ----------------------------------------------
        self.assertTrue(handler.lines, "the capture saw the cycle")
        self.assertTrue([line for line in handler.lines if "APIX" in line])
        for line in handler.lines:
            self._assert_clean("the log", line)

        # -- chatter, tracking, to-dos -------------------------------------------
        messages = self.env["mail.message"].sudo().search([("id", ">", first_message)])
        self.assertTrue(messages)
        for message in messages:
            self._assert_clean(f"message {message.id}", f"{message.subject} {message.body}")
            for value in message.tracking_value_ids:
                self._assert_clean(
                    "a tracking value",
                    f"{value.old_value_char} {value.new_value_char} "
                    f"{value.old_value_text} {value.new_value_text}",
                )
        activities = self.env["mail.activity"].sudo().search(
            [("res_model", "=", "goodahead.apix.connection")]
        )
        for activity in activities:
            self._assert_clean("a to-do", f"{activity.summary} {activity.note}")

        # -- job records -------------------------------------------------------------
        jobs = self.env["queue.job"].sudo().search([("id", ">", first_job)])
        self.assertTrue(jobs)
        for job in jobs:
            self._assert_clean(
                f"job {job.name}",
                f"{job.name} {job.result} {job.exc_info} {job.exc_message} {job.exc_name} "
                f"{job.args} {job.kwargs} {job.records}",
            )

        # -- readable fields of the records the cycle wrote ------------------------------
        secret_fields = {
            ("goodahead.apix.connection", "transfer_key"),
            ("goodahead.invoice.import", "apix_storage_key"),
        }
        for record in (self.connection.sudo(), wizard.sudo(), *records.sudo()):
            for name, field in record._fields.items():
                if (record._name, name) in secret_fields:
                    continue
                if field.type in ("char", "text", "html", "json") and field.store:
                    self._assert_clean(f"{record._name}.{name}", record[name])
        self.assertFalse(wizard.sudo().password or wizard.sudo().password_sha256)
        for record in records:
            self.assertNotIn("StorageKey", record.apix_listing_data)

        # The URLs themselves did carry the secrets' digests (the check bites).
        self.assertTrue(any(re.search(r"d=SHA-256:[0-9a-f]{64}", url) for url in self.http.urls))
