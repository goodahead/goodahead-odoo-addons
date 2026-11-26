# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Offline contract tests for the APIX client.

Runs two ways:
- plain `python -m unittest discover -s goodahead_apix/tests -t goodahead_apix`
  without Odoo (the lib is imported as the top-level package `lib`);
- under Odoo's runner as `odoo.addons.goodahead_apix.tests.test_apix_client`
  (BaseCase + tags; the Odoo import is only attempted in that case).

Nothing here touches the network: every client gets a fake session.
The fixtures are invented responses in the documented APIX message structure,
not copies of the APIX documentation.
"""

import hashlib
import logging
import os
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlsplit

import requests

if __name__.startswith("odoo.addons."):
    from odoo.tests import BaseCase, tagged

    from odoo.addons.goodahead_apix.lib import apix_client
else:  # plain unittest, no Odoo
    from lib import apix_client

    BaseCase = unittest.TestCase

    def tagged(*_tags):
        return lambda cls: cls


FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
FIXED_NOW = datetime(2026, 2, 16, 12, 50, 30, tzinfo=timezone.utc)
FIXED_TS = "20260216125030"


def fixture(name):
    with open(os.path.join(FIXTURES, name), "rb") as handle:
        return handle.read()


class _FakeResponse:
    def __init__(self, status_code=200, content=b""):
        self.status_code = status_code
        self.content = content


class _FakeSession:
    """Records requests; answers with a canned response or raises."""

    def __init__(self, content=b"", status_code=200, raises=None):
        self.content = content
        self.status_code = status_code
        self.raises = raises
        self.calls = []

    def request(self, method, url, timeout=None, **kwargs):
        self.calls.append({"method": method, "url": url, "timeout": timeout, **kwargs})
        if self.raises is not None:
            raise self.raises
        return _FakeResponse(self.status_code, self.content)


def _client(session, environment="production", **kwargs):
    return apix_client.ApixClient(environment=environment, session=session, clock=lambda: FIXED_NOW, **kwargs)


def _split(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}", parse_qsl(parts.query, keep_blank_values=True)


@tagged("post_install", "-at_install")
class TestApixDigest(BaseCase):
    """sha-256-usage-in-apix-webservices-api.html, both worked examples."""

    def test_password_hash_example(self):
        self.assertEqual(
            apix_client.password_hash("badpassword"),
            "3693d93220b28a03d3c70bdc1cab2b890c65a2e6baff3d4a2a651b713c161c5c",
        )

    def test_retrieve_transfer_id_example(self):
        # The page's concatenated example string (anonymised values) is the
        # documented input; its printed digest was computed from different,
        # non-anonymised table values, so the expectation is the SHA-256 of the
        # documented string itself. Real personal data stays out of this repo.
        documented_input = (
            "1234567-8+y-tunnus+test.user@example.com+20100621103800+"
            "3693d93220b28a03d3c70bdc1cab2b890c65a2e6baff3d4a2a651b713c161c5c"
        )
        digest = apix_client.compute_digest(
            ["1234567-8", "y-tunnus", "test.user@example.com", "20100621103800"],
            apix_client.password_hash("badpassword"),
        )
        expected = "SHA-256:" + hashlib.sha256(documented_input.encode()).hexdigest()
        self.assertEqual(digest, expected)

    def test_send_invoice_zip_example(self):
        digest = apix_client.compute_digest(["Demosoft", "1.0", "18984859858", "20100621103800"], "8874926028")
        self.assertEqual(digest, "SHA-256:82ba4381cfa3584b2f35f8c27014f711dc87dba5600f7b3df14f65b01287ac94")

    def test_timestamp_is_utc_ccyymmddhhmmss(self):
        self.assertEqual(apix_client.utc_timestamp(FIXED_NOW), FIXED_TS)
        helsinki = datetime(2026, 2, 16, 14, 50, 30, tzinfo=timezone(timedelta(hours=2)))
        self.assertEqual(apix_client.utc_timestamp(helsinki), FIXED_TS)


@tagged("post_install", "-at_install")
class TestApixRequests(BaseCase):
    """URL, parameters, digest input order and timeout per operation."""

    def test_retrieve_transfer_id_request(self):
        session = _FakeSession(fixture("retrieve_transfer_id.xml"))
        result = _client(session).retrieve_transfer_id("1234567-8", "user@example.com", "badpassword")
        call = session.calls[0]
        base, params = _split(call["url"])
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["timeout"], 60)
        self.assertEqual(base, "https://api.apix.fi/app-transferID")
        expected = apix_client.compute_digest(
            ["1234567-8", "y-tunnus", "user@example.com", FIXED_TS], apix_client.password_hash("badpassword")
        )
        self.assertEqual(
            params,
            [("id", "1234567-8"), ("idq", "y-tunnus"), ("uid", "user@example.com"), ("ts", FIXED_TS), ("d", expected)],
        )
        self.assertNotIn("badpassword", call["url"])
        self.assertIn("d=SHA-256:", call["url"])
        self.assertEqual(
            result,
            {
                "TransferID": "0a0a0a0a-1b1b-4c2c-8d3d-000000000001",
                "TransferKey": "0f0f0f0f-2e2e-4d4d-9c9c-000000000001",
                "UniqueCompanyID": "0c0c0c0c-3b3b-4a4a-8e8e-000000000001",
            },
        )

    def test_company_info_request(self):
        session = _FakeSession(fixture("company_info.xml"))
        _client(session).company_info("TRA-1", "KEY-1")
        base, params = _split(session.calls[0]["url"])
        self.assertEqual(base, "https://api.apix.fi/getcompanyinfo")
        digest = apix_client.compute_digest(["TRA-1", FIXED_TS], "KEY-1")
        self.assertEqual(params, [("TraID", "TRA-1"), ("t", FIXED_TS), ("d", digest)])

    def test_list_packages_request(self):
        session = _FakeSession(fixture("list2.xml"))
        _client(session).list_packages("TRA-1", "KEY-1")
        base, params = _split(session.calls[0]["url"])
        self.assertEqual(base, "https://terminal.apix.fi/list2")
        digest = apix_client.compute_digest(["TRA-1", FIXED_TS], "KEY-1")
        self.assertEqual(params, [("TraID", "TRA-1"), ("t", FIXED_TS), ("d", digest)])

    def test_download_request_mark_received(self):
        for mark, value in ((True, "yes"), (False, "no")):
            session = _FakeSession(b"PK\x03\x04zipbytes")
            _client(session).download("SID-1", "SKEY-1", mark_received=mark)
            base, params = _split(session.calls[0]["url"])
            self.assertEqual(base, "https://terminal.apix.fi/download")
            digest = apix_client.compute_digest([value, "SID-1", FIXED_TS], "SKEY-1")
            self.assertEqual(params, [("markreceived", value), ("SID", "SID-1"), ("t", FIXED_TS), ("d", digest)])

    def test_metadata_request(self):
        session = _FakeSession(fixture("metadata.xml"))
        _client(session).metadata("TRA-1", "KEY-1", "SID-1")
        base, params = _split(session.calls[0]["url"])
        self.assertEqual(base, "https://terminal.apix.fi/metadata")
        digest = apix_client.compute_digest(["SID-1", "TRA-1", FIXED_TS], "KEY-1")
        self.assertEqual(params, [("TraID", "TRA-1"), ("SID", "SID-1"), ("t", FIXED_TS), ("d", digest)])

    def test_test_environment_hosts(self):
        expectations = [
            ("company_info", ("T", "K"), "https://test-api.apix.fi/getcompanyinfo", "company_info.xml"),
            ("list_packages", ("T", "K"), "https://test-terminal.apix.fi/list2", "list2.xml"),
            ("metadata", ("T", "K", "S"), "https://test-terminal.apix.fi/metadata", "metadata.xml"),
            (
                "retrieve_transfer_id",
                ("1234567-8", "u@example.com", "pw"),
                "https://test-api.apix.fi/app-transferID",
                "retrieve_transfer_id.xml",
            ),
        ]
        for method, args, url, fixture_name in expectations:
            session = _FakeSession(fixture(fixture_name))
            getattr(_client(session, environment="test"), method)(*args)
            self.assertEqual(_split(session.calls[0]["url"])[0], url)
        session = _FakeSession(b"PK")
        _client(session, environment="test").download("S", "K", True)
        self.assertEqual(_split(session.calls[0]["url"])[0], "https://test-terminal.apix.fi/download")

    def test_timeout_is_capped(self):
        session = _FakeSession(fixture("list2.xml"))
        _client(session, timeout=15).list_packages("T", "K")
        self.assertEqual(session.calls[0]["timeout"], 15)
        with self.assertRaises(ValueError):
            apix_client.ApixClient(timeout=61, session=session)
        with self.assertRaises(ValueError):
            apix_client.ApixClient(timeout=0, session=session)
        with self.assertRaises(ValueError):
            apix_client.ApixClient(environment="staging", session=session)

    def test_default_clock_is_utc_now(self):
        session = _FakeSession(fixture("list2.xml"))
        apix_client.ApixClient(session=session).list_packages("T", "K")
        ts = dict(_split(session.calls[0]["url"])[1])["t"]
        stamp = datetime.strptime(ts, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        self.assertLess(abs((datetime.now(timezone.utc) - stamp).total_seconds()), 120)


@tagged("post_install", "-at_install")
class TestApixParsing(BaseCase):
    """Example responses (invented, in the documented shape) parse into dicts."""

    def test_list_packages_returns_one_dict_per_group(self):
        packages = _client(_FakeSession(fixture("list2.xml"))).list_packages("T", "K")
        self.assertEqual(len(packages), 1)
        package = packages[0]
        self.assertEqual(package["StorageID"], "0d0d0d0d-4c4c-4b4b-8a8a-000000000001")
        self.assertEqual(package["StorageKey"], "100200300400")
        self.assertEqual(package["StorageStatus"], "UNRECEIVED")
        self.assertEqual(package["TransferStatus"], "NEW")
        self.assertEqual(package["DocumentName"], "invoice.xml")
        self.assertEqual(package["ImageName"], "zz-invoice-0001.pdf")
        self.assertEqual(package["SenderName"], "ZZ Seller Oy")
        self.assertEqual(package["AmountToPay"], "64.50")
        self.assertEqual(len(package), 24)

    def test_valid_listing_with_zero_groups_is_empty_list(self):
        body = b"<Response><Status>OK</Status><StatusCode>2700</StatusCode><Content></Content></Response>"
        self.assertEqual(_client(_FakeSession(body)).list_packages("T", "K"), [])
        body = b"<Response><Status>OK</Status><StatusCode>2700</StatusCode></Response>"
        self.assertEqual(_client(_FakeSession(body)).list_packages("T", "K"), [])

    def test_company_info(self):
        parsed = _client(_FakeSession(fixture("company_info.xml"))).company_info("T", "K")
        self.assertEqual(parsed["status"], "OK")
        self.assertEqual(parsed["status_code"], "5000")
        self.assertEqual(parsed["free_text"], "")
        company, address = parsed["groups"]
        self.assertEqual(company["y-tunnus"], "1234567-8")
        self.assertEqual(company["Name"], "ZZ Example Company Oy")
        self.assertEqual(address["AddressType"], "Billing")
        self.assertEqual(address["Country"], "FI")

    def test_metadata(self):
        parsed = _client(_FakeSession(fixture("metadata.xml"))).metadata("T", "K", "S")
        self.assertEqual(
            parsed["groups"],
            [
                {"email": "zz.intake@example.com", "extraInfo": "zz extra note"},
                {"FileName": "zz-invoice-0042.pdf", "email": "zz.receiver@example.com"},
            ],
        )

    def test_parse_error_example(self):
        parsed = apix_client.parse_response(fixture("error_4701.xml"))
        self.assertEqual(parsed["status"], "ERR")
        self.assertEqual(parsed["status_code"], "4701")
        self.assertEqual(parsed["free_text"], "Error: The invoice datafile fails validation. Reason:")
        self.assertEqual(parsed["free_texts"], {"en": "Error: The invoice datafile fails validation. Reason:"})
        self.assertEqual(parsed["groups"], [{"Saldo": "75", "CostInCredits": "0"}])

    def test_free_text_prefers_english(self):
        body = (
            b'<Response><Status>ERR</Status><StatusCode>5004</StatusCode>'
            b'<FreeText language="fi">Ei oikeutta</FreeText><FreeText language="en">Permission denied</FreeText>'
            b"</Response>"
        )
        parsed = apix_client.parse_response(body)
        self.assertEqual(parsed["free_text"], "Permission denied")
        self.assertEqual(parsed["free_texts"]["fi"], "Ei oikeutta")

    def test_download_returns_body_bytes(self):
        body = b"PK\x03\x04" + b"\x00" * 32
        self.assertEqual(_client(_FakeSession(body)).download("S", "K", False), body)

    def test_parse_response_rejects_non_response(self):
        for body in (b"", b"   ", b"not xml", b"<Other><Status>OK</Status></Other>", b"<Response/>"):
            with self.assertRaises(apix_client.ApixInvalidResponse):
                apix_client.parse_response(body)

    def test_entities_and_external_dtds_are_not_resolved(self):
        """An answer is data: no entity expansion, no DTD, no network."""
        body = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE Response [<!ENTITY secret SYSTEM "file:///etc/hosts">'
            b'<!ENTITY word "expanded">]>'
            b"<Response><Status>OK</Status><StatusCode>5000</StatusCode>"
            b'<FreeText language="en">&word;</FreeText>'
            b'<Content><Group><Value type="Name">&secret;</Value></Group></Content></Response>'
        )
        parsed = apix_client.parse_response(body)
        self.assertNotIn("expanded", parsed["free_text"])
        self.assertNotIn("localhost", str(parsed["groups"]))

    def test_parse_response_reads_text_bodies_with_a_declaration(self):
        body = fixture("company_info.xml").decode("utf-8")
        self.assertEqual(apix_client.parse_response(body)["status"], "OK")


@tagged("post_install", "-at_install")
class TestApixErrors(BaseCase):
    """Every error path maps to its contract code (public-api.md §3)."""

    def assertApixError(self, cls, code, func, *args):
        with self.assertRaises(cls) as ctx:
            func(*args)
        self.assertEqual(ctx.exception.code, code)
        self.assertIsInstance(ctx.exception, apix_client.ApixError)
        return ctx.exception

    CALLS = (
        ("company_info", ("T", "K")),
        ("list_packages", ("T", "K")),
        ("metadata", ("T", "K", "S")),
        ("retrieve_transfer_id", ("1234567-8", "u@example.com", "pw")),
    )

    @staticmethod
    def _err(status_code, text):
        return (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Response version="1.0">'
            f"<Status>ERR</Status><StatusCode>{status_code}</StatusCode>"
            f'<FreeText language="en">{text}</FreeText></Response>'
        ).encode()

    def test_status_err_other_than_permission_is_a_service_error(self):
        """response-message-structure.html: 4701 is error 01, "Data contents
        invalid" — a failed call, not a refused credential."""
        client = _client(_FakeSession(fixture("error_4701.xml")))
        for method, args in self.CALLS:
            exc = self.assertApixError(
                apix_client.ApixServiceError, "apix.service_error", getattr(client, method), *args
            )
            self.assertNotIsInstance(exc, apix_client.ApixRejected)
            self.assertIn("The invoice datafile fails validation", exc.message)
            self.assertIn("4701", exc.message)
            self.assertEqual(exc.status_code, "4701")
        for status_code in ("9099", "1012", "5002", "2003", ""):
            client = _client(_FakeSession(self._err(status_code, "Temporary trouble")))
            self.assertApixError(apix_client.ApixServiceError, "apix.service_error", client.company_info, "T", "K")

    def test_permission_denied_is_rejected(self):
        """Error 04, "Permission denied", is the credential refusal."""
        for status_code in ("5004", "1004", "9004"):
            client = _client(_FakeSession(self._err(status_code, "Permission denied")))
            for method, args in self.CALLS:
                exc = self.assertApixError(apix_client.ApixRejected, "apix.rejected", getattr(client, method), *args)
                self.assertIn("Permission denied", exc.message)
                self.assertEqual(exc.status_code, status_code)

    def test_no_data_available_on_the_listing_is_no_packages(self):
        client = _client(_FakeSession(self._err("1005", "No data available")))
        self.assertEqual(client.list_packages("T", "K"), [])
        # ...but on any other call it is an error, not an empty success.
        self.assertApixError(apix_client.ApixServiceError, "apix.service_error", client.company_info, "T", "K")

    def test_error_code_is_the_last_two_characters(self):
        self.assertEqual(apix_client.error_code("4704"), "04")
        self.assertEqual(apix_client.error_code(" 1005 "), "05")
        self.assertEqual(apix_client.error_code("7"), "")
        self.assertEqual(apix_client.error_code(None), "")

    def test_download_empty_body_is_empty_answer(self):
        for body in (b"", b"  \n"):
            client = _client(_FakeSession(body))
            self.assertApixError(apix_client.ApixEmptyAnswer, "apix.empty_answer", client.download, "S", "K", True)

    def test_download_err_response_is_classified(self):
        client = _client(_FakeSession(fixture("error_4701.xml")))
        self.assertApixError(apix_client.ApixServiceError, "apix.service_error", client.download, "S", "K", True)
        client = _client(_FakeSession(self._err("1004", "Permission denied")))
        self.assertApixError(apix_client.ApixRejected, "apix.rejected", client.download, "S", "K", True)
        client = _client(_FakeSession(self._err("1005", "No data available")))
        self.assertApixError(apix_client.ApixEmptyAnswer, "apix.empty_answer", client.download, "S", "K", True)

    def test_invalid_listing_is_empty_answer_never_no_packages(self):
        for body in (b"", b"<html>gateway</html>", b"garbage", b"<Response><Content/></Response>"):
            client = _client(_FakeSession(body))
            self.assertApixError(apix_client.ApixEmptyAnswer, "apix.empty_answer", client.list_packages, "T", "K")

    def test_invalid_answer_on_other_calls_is_unexpected(self):
        client = _client(_FakeSession(b"<html/>"))
        self.assertApixError(apix_client.ApixUnexpected, "apix.unexpected", client.company_info, "T", "K")
        self.assertApixError(apix_client.ApixUnexpected, "apix.unexpected", client.metadata, "T", "K", "S")

    def test_transfer_id_answer_without_credentials_is_unexpected(self):
        body = b"<Response><Status>OK</Status><StatusCode>5000</StatusCode><Content><Group/></Content></Response>"
        client = _client(_FakeSession(body))
        self.assertApixError(
            apix_client.ApixUnexpected, "apix.unexpected", client.retrieve_transfer_id, "1234567-8", "user@example.test", "pw"
        )

    def test_timeout_is_unreachable(self):
        client = _client(_FakeSession(raises=requests.Timeout("read timed out")))
        self.assertApixError(apix_client.ApixUnreachable, "apix.unreachable", client.list_packages, "T", "K")

    def test_connection_error_is_unreachable(self):
        for raised in (requests.ConnectionError("refused"), OSError("socket")):
            client = _client(_FakeSession(raises=raised))
            self.assertApixError(apix_client.ApixUnreachable, "apix.unreachable", client.download, "S", "K", True)

    def test_other_request_error_is_unexpected(self):
        client = _client(_FakeSession(raises=requests.TooManyRedirects("loop")))
        self.assertApixError(apix_client.ApixUnexpected, "apix.unexpected", client.company_info, "T", "K")

    def test_http_error_status_is_unexpected_with_status(self):
        client = _client(_FakeSession(b"oops", status_code=503))
        exc = self.assertApixError(apix_client.ApixUnexpected, "apix.unexpected", client.list_packages, "T", "K")
        self.assertEqual(exc.http_status, 503)
        self.assertIn("503", exc.message)

    def test_client_never_retries(self):
        session = _FakeSession(raises=requests.Timeout("t"))
        with self.assertRaises(apix_client.ApixUnreachable):
            _client(session).list_packages("T", "K")
        self.assertEqual(len(session.calls), 1)


@tagged("post_install", "-at_install")
class TestApixNoSecretsInLogs(BaseCase):
    """No key, password, digest or URL reaches logs or exception text."""

    PASSWORD = "S3cretWebPassw0rd"
    TRANSFER_KEY = "TKEY-7f3a9c"
    STORAGE_KEY = "SKEY-42d1b0"

    def _secrets(self, session):
        secrets = {self.PASSWORD, self.TRANSFER_KEY, self.STORAGE_KEY, apix_client.password_hash(self.PASSWORD)}
        for call in session.calls:
            secrets.add(call["url"])
            secrets.add(dict(_split(call["url"])[1])["d"])
            secrets.add(dict(_split(call["url"])[1])["d"].split(":", 1)[1])
        return secrets

    def test_no_secret_in_logs_or_errors(self):
        logger_name = apix_client.__name__
        session_ok = _FakeSession()
        texts = []
        with self.assertLogs(logger_name, level=logging.DEBUG) as captured:
            client = _client(session_ok)
            session_ok.content = fixture("retrieve_transfer_id.xml")
            client.retrieve_transfer_id("1234567-8", "user@example.com", self.PASSWORD)
            session_ok.content = fixture("company_info.xml")
            client.company_info("TRA-1", self.TRANSFER_KEY)
            session_ok.content = fixture("list2.xml")
            client.list_packages("TRA-1", self.TRANSFER_KEY)
            session_ok.content = b"PK\x03\x04"
            client.download("SID-1", self.STORAGE_KEY, True)
            session_ok.content = fixture("metadata.xml")
            client.metadata("TRA-1", self.TRANSFER_KEY, "SID-1")
            failing = [
                _FakeSession(fixture("error_4701.xml")),
                _FakeSession(b""),
                _FakeSession(b"x", status_code=500),
                _FakeSession(raises=requests.Timeout("timeout on https://terminal.apix.fi/list2?d=SHA-256:x")),
                _FakeSession(raises=requests.ConnectionError("https://terminal.apix.fi/download?SID=1")),
            ]
            for session in failing:
                bad = _client(session)
                for call in (
                    lambda: bad.list_packages("TRA-1", self.TRANSFER_KEY),
                    lambda: bad.download("SID-1", self.STORAGE_KEY, False),
                    lambda: bad.retrieve_transfer_id("1234567-8", "user@example.com", self.PASSWORD),
                ):
                    try:
                        call()
                    except apix_client.ApixError as exc:
                        texts.append(str(exc))
                        self.assertIsNone(exc.__cause__)
                        # no transport exception (whose text embeds the URL) rides along
                        self.assertTrue(exc.__context__ is None or exc.__suppress_context__)
        texts.extend(captured.output)
        self.assertTrue(any("SID-1" in line for line in captured.output))
        secrets = self._secrets(session_ok)
        for session in failing:
            secrets |= self._secrets(session)
        blob = "\n".join(texts)
        for secret in secrets:
            self.assertNotIn(secret, blob)
        self.assertNotIn("SHA-256:", blob)
        self.assertNotIn("apix.fi", blob)

    def test_transport_debug_log_never_carries_the_url(self):
        """urllib3 logs request lines (the URL, digest and keys included) at
        DEBUG; the client keeps that logger at INFO, even when somebody
        lowered it after the client was imported."""
        transport = logging.getLogger(apix_client.TRANSPORT_LOGGER)
        previous = transport.level
        self.addCleanup(transport.setLevel, previous)
        transport.setLevel(logging.DEBUG)
        session = _FakeSession(fixture("list2.xml"))
        _client(session).list_packages("TRA-1", self.TRANSFER_KEY)
        self.assertGreaterEqual(transport.level, logging.INFO)
        self.assertFalse(transport.isEnabledFor(logging.DEBUG))
        # A stricter level set by the deployment is left alone.
        transport.setLevel(logging.WARNING)
        apix_client.quiet_transport_logger()
        self.assertEqual(transport.level, logging.WARNING)


if __name__ == "__main__":
    unittest.main()
