# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Offline contract tests for the APIX client's sending calls.

SendInvoiceZIP, AddressQuery and DeliveryMethod (sending
contracts/public-api.md §10; spec AC-01, AC-02, AC-03, AC-05, AC-06, AC-21,
AC-24). Runs like `test_apix_client.py`: with plain unittest
(`python -m unittest discover -s goodahead_apix/tests -t goodahead_apix`) and
under the ERP's runner. No network: every client gets a fake session. The
answers under `fixtures/` are invented, shaped like the APIX reference pages.
"""

import logging
import os
import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlsplit

import requests
from lxml import etree
from urllib3.exceptions import MaxRetryError, NewConnectionError, ProtocolError

if __name__.startswith("odoo.addons."):
    from odoo.tests import BaseCase, tagged

    from odoo.addons.goodahead_apix.lib import apix_client
else:  # plain unittest, no ERP
    from lib import apix_client

    BaseCase = unittest.TestCase

    def tagged(*_tags):
        return lambda cls: cls


FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
FIXED_NOW = datetime(2026, 2, 16, 12, 50, 30, tzinfo=timezone.utc)
FIXED_TS = "20260216125030"
ZIP_BYTES = b"PK\x03\x04-invented-zip-body-ZZ-0001"


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


def _client(session, environment="production", now=FIXED_NOW):
    return apix_client.ApixClient(environment=environment, session=session, clock=lambda: now)


def _split(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}", parse_qsl(parts.query, keep_blank_values=True)


def _err(status_code, text):
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Response version="1.0">'
        f"<Status>ERR</Status><StatusCode>{status_code}</StatusCode>"
        f'<FreeText language="en">{text}</FreeText></Response>'
    ).encode()


def _connect_refused():
    """What `requests` raises when the TCP connection cannot be opened."""
    reason = NewConnectionError(None, "Failed to establish a new connection: [Errno 61] Connection refused")
    return requests.ConnectionError(MaxRetryError(None, "/invoices", reason=reason))


def _aborted_after_upload():
    """What `requests` raises when the server drops the line after the body."""
    return requests.ConnectionError(ProtocolError("Connection aborted.", ConnectionResetError(54, "reset")))


@tagged("post_install", "-at_install")
class TestSendInvoiceZipRequest(BaseCase):
    def test_documented_digest_example_byte_for_byte(self):
        """sha-256-usage page, "Example (SendInvoiceZip)": the URL the client
        builds for the documented values carries the documented digest."""
        session = _FakeSession(fixture("send_accepted.xml"))
        client = _client(session, environment="test", now=datetime(2010, 6, 21, 10, 38, 0, tzinfo=timezone.utc))
        client.send_invoice_zip("18984859858", "8874926028", "Demosoft", "1.0", ZIP_BYTES)
        self.assertEqual(
            session.calls[0]["url"],
            "https://test-api.apix.fi/invoices?soft=Demosoft&ver=1.0&TraID=18984859858&t=20100621103800"
            "&d=SHA-256:82ba4381cfa3584b2f35f8c27014f711dc87dba5600f7b3df14f65b01287ac94",
        )

    def test_put_octet_stream_with_the_zip_as_body(self):
        session = _FakeSession(fixture("send_accepted.xml"))
        _client(session).send_invoice_zip("TRA-1", "KEY-1", "Standard", "1.0", ZIP_BYTES)
        call = session.calls[0]
        base, params = _split(call["url"])
        self.assertEqual(call["method"], "PUT")
        self.assertEqual(base, "https://api.apix.fi/invoices")
        self.assertEqual([k for k, _v in params], ["soft", "ver", "TraID", "t", "d"])
        self.assertEqual(dict(params)["t"], FIXED_TS)
        self.assertEqual(
            dict(params)["d"],
            apix_client.compute_digest(["Standard", "1.0", "TRA-1", FIXED_TS], "KEY-1"),
        )
        self.assertEqual(call["data"], ZIP_BYTES)
        self.assertEqual(call["headers"]["Content-Type"], "application/octet-stream")
        self.assertEqual(call["timeout"], 60)

    def test_client_never_retries_a_send(self):
        session = _FakeSession(raises=requests.ReadTimeout("read timed out"))
        with self.assertRaises(apix_client.ApixNoAnswer):
            _client(session).send_invoice_zip("T", "K", "Standard", "1.0", ZIP_BYTES)
        self.assertEqual(len(session.calls), 1)


@tagged("post_install", "-at_install")
class TestSendAnswer(BaseCase):
    def _send(self, body):
        return _client(_FakeSession(body)).send_invoice_zip("T", "K", "Standard", "1.0", ZIP_BYTES)

    def test_accepted(self):
        answer = self._send(fixture("send_accepted.xml"))
        self.assertEqual(answer.status, "OK")
        self.assertEqual(answer.status_code, "1000")
        self.assertEqual(answer.free_text, "OK")
        self.assertEqual(answer.batch_id, "00000000-aaaa-bbbb-cccc-000000000001")
        self.assertEqual(answer.saldo, 250)
        self.assertEqual(answer.cost_in_credits, 1)
        self.assertEqual(answer.networked, 1)
        self.assertEqual(answer.letters, 0)
        self.assertEqual(answer.letter_pages, 0)
        self.assertEqual(answer.accepted_count, 1)
        self.assertEqual(answer.rejected_count, 0)
        self.assertEqual(
            answer.documents,
            [{"accepted": True, "document_id": "ZZ-2001", "texts": {"en": "", "fi": "", "sv": ""}}],
        )
        self.assertEqual(answer.raw, fixture("send_accepted.xml"))

    def test_accepted_with_a_warning_keeps_its_texts(self):
        """AC-02: an accepted document's ValidateText is a warning."""
        answer = self._send(fixture("send_accepted_warning.xml"))
        self.assertEqual(answer.accepted_count, 1)
        document = answer.documents[0]
        self.assertTrue(document["accepted"])
        self.assertEqual(document["document_id"], "ZZ-2002")
        self.assertIn("BuyerOrganisationTaxCode is missing", document["texts"]["en"])
        self.assertIn("puuttuu", document["texts"]["fi"])
        self.assertIn("saknas", document["texts"]["sv"])

    def test_rejected_document(self):
        """AC-03: a RejectedDocument group is parsed with its reason."""
        answer = self._send(fixture("send_rejected.xml"))
        self.assertEqual(answer.status, "OK")
        self.assertEqual(answer.rejected_count, 1)
        self.assertEqual(answer.accepted_count, 0)
        document = answer.documents[0]
        self.assertFalse(document["accepted"])
        self.assertEqual(document["document_id"], "ZZ-2003")
        self.assertIn("SellerOrganisationName is missing", document["texts"]["en"])
        self.assertEqual(document["texts"]["sv"], "")

    def test_finnish_only_text_is_kept(self):
        answer = self._send(fixture("send_rejected_fi_only.xml"))
        texts = answer.documents[0]["texts"]
        self.assertEqual(texts["fi"], "Laskun summa puuttuu")
        self.assertEqual(texts["en"], "")
        self.assertIsNone(answer.saldo)
        self.assertEqual(answer.free_text, "")

    def test_err_answers_are_returned_for_the_service_to_map(self):
        """AC-06: 03 / 04 / 02 / 11 surface in `status_code`, not as an
        exception, so the sending service maps them."""
        for status_code in ("2003", "2004", "2002", "2011", "4701", "2012", "9099"):
            with self.subTest(status_code=status_code):
                answer = self._send(_err(status_code, "Refused for a reason"))
                self.assertEqual(answer.status, "ERR")
                self.assertEqual(answer.status_code, status_code)
                self.assertEqual(answer.free_text, "Refused for a reason")
                self.assertEqual(answer.documents, [])
                self.assertIsNone(answer.batch_id)

    def test_err_answer_with_counters(self):
        answer = self._send(fixture("error_4701.xml"))
        self.assertEqual(answer.status_code, "4701")
        self.assertEqual(answer.saldo, 75)
        self.assertEqual(answer.cost_in_credits, 0)


@tagged("post_install", "-at_install")
class TestSendInvoiceZipErrors(BaseCase):
    """apix.unreachable (never left) vs apix.no_answer (may be at APIX)."""

    def assertSendRaises(self, cls, code, session):
        with self.assertRaises(cls) as ctx:
            _client(session).send_invoice_zip("T", "K", "Standard", "1.0", ZIP_BYTES)
        self.assertEqual(ctx.exception.code, code)
        self.assertIsInstance(ctx.exception, apix_client.ApixError)
        return ctx.exception

    def test_connection_refused_is_unreachable(self):
        for raised in (_connect_refused(), requests.ConnectTimeout("connect timed out"), ConnectionRefusedError()):
            with self.subTest(raised=type(raised).__name__):
                exc = self.assertSendRaises(apix_client.ApixUnreachable, "apix.unreachable", _FakeSession(raises=raised))
                self.assertNotIsInstance(exc, apix_client.ApixNoAnswer)

    def test_read_timeout_after_upload_is_no_answer(self):
        exc = self.assertSendRaises(
            apix_client.ApixNoAnswer, "apix.no_answer", _FakeSession(raises=requests.ReadTimeout("read timed out"))
        )
        self.assertNotIsInstance(exc, apix_client.ApixUnreachable)

    def test_broken_connection_after_upload_is_no_answer(self):
        for raised in (_aborted_after_upload(), requests.exceptions.ChunkedEncodingError("broken"), OSError("reset")):
            with self.subTest(raised=type(raised).__name__):
                self.assertSendRaises(apix_client.ApixNoAnswer, "apix.no_answer", _FakeSession(raises=raised))

    def test_http_200_that_is_not_a_response_is_no_answer(self):
        for body in (b"", b"  ", b"<html>gateway</html>", b"garbage", b"<Response><Content/></Response>"):
            with self.subTest(body=body):
                self.assertSendRaises(apix_client.ApixNoAnswer, "apix.no_answer", _FakeSession(body))

    def test_http_error_status_is_unexpected(self):
        exc = self.assertSendRaises(apix_client.ApixUnexpected, "apix.unexpected", _FakeSession(b"x", status_code=502))
        self.assertEqual(exc.http_status, 502)


@tagged("post_install", "-at_install")
class TestAddressQuery(BaseCase):
    def test_request(self):
        session = _FakeSession(fixture("address_one.xml"))
        _client(session).address_query("TRA-1", "KEY-1", business_id="0000003-5", name="Example Receiver Oy")
        call = session.calls[0]
        base, params = _split(call["url"])
        self.assertEqual(call["method"], "PUT")
        self.assertEqual(base, "https://api.apix.fi/addressquery")
        self.assertEqual([k for k, _v in params], ["uid", "t", "d"])
        self.assertEqual(dict(params)["uid"], "TRA-1")
        self.assertEqual(dict(params)["d"], apix_client.compute_digest(["TRA-1", FIXED_TS], "KEY-1"))
        self.assertEqual(call["headers"]["Content-Type"], "text/xml")
        root = etree.fromstring(call["data"])
        self.assertEqual(root.tag, "Request")
        groups = root.findall("Content/Group")
        self.assertEqual(len(groups), 1)
        values = {v.get("type"): v.text for v in groups[0].findall("Value")}
        self.assertEqual(values, {"ReceiverYtunnus": "0000003-5", "ReceiverName": "Example Receiver Oy"})

    def test_business_id_only(self):
        session = _FakeSession(fixture("address_one.xml"))
        _client(session).address_query("TRA-1", "KEY-1", business_id="0000003-5")
        root = etree.fromstring(session.calls[0]["data"])
        self.assertEqual([v.get("type") for v in root.iter("Value")], ["ReceiverYtunnus"])

    def test_needs_a_business_id_or_a_name(self):
        with self.assertRaises(ValueError):
            _client(_FakeSession(fixture("address_one.xml"))).address_query("T", "K")

    def test_one_address(self):
        found = _client(_FakeSession(fixture("address_one.xml"))).address_query("T", "K", business_id="0000003-5")
        self.assertEqual(
            found,
            [{
                "receiver_name": "Example Receiver Oy",
                "business_id": "0000003-5",
                "edicode": "003700000035",
                "operator": "Example Operator Oy",
            }],
        )

    def test_several_addresses_all_listed(self):
        """AC-21: every address APIX found is offered."""
        found = _client(_FakeSession(fixture("address_several.xml"))).address_query("T", "K", business_id="0000003-5")
        self.assertEqual([a["edicode"] for a in found], ["003700000035", "FI0000000000000000"])
        self.assertEqual([a["operator"] for a in found], ["Example Operator Oy", "Example Bank Oyj"])

    def test_no_data_is_an_empty_list(self):
        found = _client(_FakeSession(fixture("address_none.xml"))).address_query("T", "K", business_id="0000003-5")
        self.assertEqual(found, [])

    def test_lookup_errors_raise_the_receiving_classes(self):
        """AC-24: unavailable lookup carries APIX's text and status code."""
        client = _client(_FakeSession(_err("2004", "Permission denied")))
        with self.assertRaises(apix_client.ApixRejected) as ctx:
            client.address_query("T", "K", business_id="0000003-5")
        self.assertIn("Permission denied", ctx.exception.message)
        self.assertEqual(ctx.exception.status_code, "2004")
        client = _client(_FakeSession(_err("2003", "No valid contract")))
        with self.assertRaises(apix_client.ApixServiceError) as ctx:
            client.address_query("T", "K", business_id="0000003-5")
        self.assertIn("No valid contract", ctx.exception.message)
        with self.assertRaises(apix_client.ApixUnreachable):
            _client(_FakeSession(raises=_connect_refused())).address_query("T", "K", business_id="0000003-5")
        with self.assertRaises(apix_client.ApixUnexpected):
            _client(_FakeSession(b"<html/>")).address_query("T", "K", business_id="0000003-5")


@tagged("post_install", "-at_install")
class TestDeliveryMethod(BaseCase):
    DOCUMENTS = (
        {
            "document_id": "ZZ-1",
            "sender_name": "Example Seller Oy",
            "sender_business_id": "0000001-9",
            "receiver_name": "Example Receiver Oy",
            "receiver_country_code": "FI",
            "receiver_business_id": "0000003-5",
            "receiver_edicode": "003700000035",
        },
        {
            "document_id": "ZZ-2",
            "sender_name": "Example Seller Oy",
            "sender_business_id": "0000001-9",
            "receiver_name": "Another Example Oy",
            "letter_pages": 2,
        },
    )

    def test_request(self):
        session = _FakeSession(fixture("delivery_method.xml"))
        _client(session).delivery_method("TRA-1", "KEY-1", self.DOCUMENTS)
        call = session.calls[0]
        base, params = _split(call["url"])
        self.assertEqual(call["method"], "PUT")
        self.assertEqual(base, "https://api.apix.fi/method")
        self.assertEqual(dict(params)["d"], apix_client.compute_digest(["TRA-1", FIXED_TS], "KEY-1"))
        root = etree.fromstring(call["data"])
        groups = root.findall("Content/Group")
        self.assertEqual(len(groups), 2)
        first = [(v.get("type"), v.text) for v in groups[0].findall("Value")]
        self.assertEqual(
            first,
            [
                ("DocumentID", "ZZ-1"),
                ("SenderName", "Example Seller Oy"),
                ("SenderYtunnus", "0000001-9"),
                ("ReceiverName", "Example Receiver Oy"),
                ("ReceiverCountryCode", "FI"),
                ("ReceiverYtunnus", "0000003-5"),
                ("ReceivereInvoiceAddress", "003700000035"),
                ("MessageType", "eInvoice"),
            ],
        )
        second = {v.get("type"): v.text for v in groups[1].findall("Value")}
        self.assertEqual(second["LetterPages"], "2")
        self.assertNotIn("ReceiverYtunnus", second)

    def test_answer(self):
        found = _client(_FakeSession(fixture("delivery_method.xml"))).delivery_method("T", "K", self.DOCUMENTS)
        self.assertEqual(
            found,
            [
                {
                    "document_id": "ZZ-1", "channel_name": "eInvoice", "cost_in_credits": 1,
                    "receiver_name": "Example Receiver Oy", "letter_pages": None, "letter_class": "",
                },
                {
                    "document_id": "ZZ-2", "channel_name": "Paper", "cost_in_credits": 3,
                    "receiver_name": "Another Example Oy", "letter_pages": 2, "letter_class": "2",
                },
            ],
        )

    def test_errors_raise_the_receiving_classes(self):
        with self.assertRaises(apix_client.ApixServiceError):
            _client(_FakeSession(fixture("error_4701.xml"))).delivery_method("T", "K", self.DOCUMENTS)
        with self.assertRaises(apix_client.ApixRejected):
            _client(_FakeSession(_err("2004", "Permission denied"))).delivery_method("T", "K", self.DOCUMENTS)


@tagged("post_install", "-at_install")
class TestSendingCallsLogNoSecret(BaseCase):
    TRANSFER_ID = "TRA-5e1d-0001"
    TRANSFER_KEY = "TKEY-9b2c-secret"

    def test_no_key_digest_parameter_or_body_in_logs_or_errors(self):
        sessions = []
        texts = []

        def run(session, call):
            sessions.append(session)
            try:
                call(_client(session))
            except apix_client.ApixError as exc:
                texts.append(str(exc))
                self.assertIsNone(exc.__cause__)
                self.assertTrue(exc.__context__ is None or exc.__suppress_context__)

        tid, key = self.TRANSFER_ID, self.TRANSFER_KEY
        with self.assertLogs(apix_client.__name__, level=logging.DEBUG) as captured:
            for body in (fixture("send_accepted.xml"), fixture("send_rejected.xml"), _err("2004", "x"), b"junk"):
                run(_FakeSession(body), lambda c: c.send_invoice_zip(tid, key, "Standard", "1.0", ZIP_BYTES))
            for raised in (
                _connect_refused(),
                requests.ReadTimeout("https://api.apix.fi/invoices?TraID=TRA-5e1d-0001&d=SHA-256:x"),
                _aborted_after_upload(),
            ):
                run(_FakeSession(raises=raised), lambda c: c.send_invoice_zip(tid, key, "Standard", "1.0", ZIP_BYTES))
            for body in (fixture("address_one.xml"), fixture("address_none.xml"), _err("2004", "x")):
                run(_FakeSession(body), lambda c: c.address_query(tid, key, business_id="0000003-5"))
            for body in (fixture("delivery_method.xml"), _err("2003", "x")):
                run(_FakeSession(body), lambda c: c.delivery_method(tid, key, TestDeliveryMethod.DOCUMENTS))
        blob = "\n".join(texts + captured.output)
        self.assertIn("SendInvoiceZIP", blob)
        secrets = {tid, key, ZIP_BYTES.decode("latin-1"), "invented-zip-body"}
        for session in sessions:
            for call in session.calls:
                secrets.add(call["url"])
                digest = dict(_split(call["url"])[1])["d"]
                secrets.add(digest)
                secrets.add(digest.split(":", 1)[1])
        for secret in secrets:
            self.assertNotIn(secret, blob)
        self.assertNotIn("SHA-256:", blob)
        self.assertNotIn("apix.fi", blob)


if __name__ == "__main__":
    unittest.main()
