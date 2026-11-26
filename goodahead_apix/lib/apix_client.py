# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Pure-Python client for the APIX REST API (receiving and sending).

Clean-room implementation written only from the APIX wiki pages listed in
`goodahead_invoice_import_apix/docs/reference/README.md`:

- `rest-api-for-external-usage.html` — operations, hosts, parameters
  (RetrieveTransferID, RetrieveCompanyInformation, ListInvoiceZIPs, Download,
  GetMetadata; for sending SendInvoiceZIP, AddressQuery, DeliveryMethod);
- `sha-256-usage-in-apix-webservices-api.html` — the digest rule;
- `response-message-structure.html` — the common Response XML;
- `request-message-structure.html` — the common Request XML (PUT bodies);
- `list-value-types.html` — the `Value/@type` vocabulary.

No Odoo import here. The transport (a `requests.Session`-like object with a
`request(method, url, timeout=...)` method) is injectable so the tests never
touch the network. The client never retries: it only classifies the outcome
into one of the `apix.*` error codes and lets the caller decide.

🔴 SECRETS. Digests, TransferKey, StorageKey and passwords travel in the query
string, so the full URL is a secret too. Nothing here logs or raises a URL,
a digest, a key, a password or a `requests` exception text (which embeds the
URL). Logs carry the operation name, the storage id (when there is one) and
the APIX status only.

🔴 AND NOTHING BELOW US LOGS IT EITHER. `urllib3.connectionpool` logs every
request line — the full URL with its query string — at DEBUG. A server run
with `--log-level=debug` (or a `--log-handler` for urllib3) would write every
digest and StorageKey to the log. `quiet_transport_logger` keeps that logger
at INFO or above; it runs when this module is imported and before every
request, so a log level set later does not reopen the hole.
"""

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
from lxml import etree
from urllib3.exceptions import (
    ConnectTimeoutError,
    MaxRetryError,
    NewConnectionError,
    ProxyError,
)
from urllib3.exceptions import SSLError as Urllib3SSLError

_logger = logging.getLogger(__name__)

#: The transport logger that would print request URLs at DEBUG.
TRANSPORT_LOGGER = "urllib3.connectionpool"


def quiet_transport_logger():
    """Keep `TRANSPORT_LOGGER` at INFO or above (see the module docstring).
    A stricter level (WARNING...) set by the deployment is left alone."""
    transport = logging.getLogger(TRANSPORT_LOGGER)
    if transport.level < logging.INFO:
        transport.setLevel(logging.INFO)


quiet_transport_logger()

MAX_TIMEOUT = 60

# rest-api-for-external-usage.html: every operation lists a PRODUCTION URL and
# a TEST URL; administrative calls live on the `api` host, receiving calls
# (list2, download, metadata) on the `terminal` host.
HOSTS = {
    "production": {
        "api": "https://api.apix.fi",
        "terminal": "https://terminal.apix.fi",
    },
    "test": {
        "api": "https://test-api.apix.fi",
        "terminal": "https://test-terminal.apix.fi",
    },
}

DIGEST_PREFIX = "SHA-256:"
DEFAULT_ID_QUALIFIER = "y-tunnus"


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class ApixError(Exception):
    """Base error. `code` is one of the `apix.*` codes of the subclasses."""

    code = "apix.error"

    def __init__(self, message, status_code=None, http_status=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.http_status = http_status

    def __str__(self):
        return f"{self.code}: {self.message}"


class ApixRejected(ApixError):
    """APIX refused the credentials: an ERR Response whose StatusCode says
    the permission was denied (a wrong TransferKey, digest or login). The
    connection stops being trusted until somebody checks it."""

    code = "apix.rejected"


class ApixServiceError(ApixError):
    """APIX answered with an ERR Response that is NOT a credential refusal —
    invalid data, a misformed request, no saldo, an internal error. A failed
    run to report and try again; never a reason to distrust the credentials."""

    code = "apix.service_error"


class ApixEmptyAnswer(ApixError):
    """Empty download body, or a listing that is not a valid Response."""

    code = "apix.empty_answer"


class ApixUnreachable(ApixError):
    """Network error or timeout."""

    code = "apix.unreachable"


class ApixUnexpected(ApixError):
    """Anything else (HTTP status other than 200, malformed answer)."""

    code = "apix.unexpected"


class ApixNoAnswer(ApixError):
    """Sending only: the request was
    sent, or the connection broke after it, and no complete answer came within
    the timeout, or the answer is not a valid Response. The invoice MAY have
    reached APIX. Raised only by `send_invoice_zip`, where `ApixUnreachable`
    means the connection could not be opened at all (nothing left)."""

    code = "apix.no_answer"


class ApixInvalidResponse(ValueError):
    """`parse_response` could not read a Response document."""


# --------------------------------------------------------------------------
# Digest and timestamp (sha-256-usage-in-apix-webservices-api.html)
# --------------------------------------------------------------------------


def utc_timestamp(now=None):
    """UTC timestamp in the documented `CCYYMMDDHHMMSS` form."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc)
    return now.strftime("%Y%m%d%H%M%S")


def sha256_hex(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def password_hash(password):
    """SHA-256 of the user's web password, used as the shared secret.

    Double hashing applies only to the web password, never to a TransferKey
    (sha-256 page, NOTE under the RetrieveTransferID example).
    """
    return sha256_hex(password)


def compute_digest(parts, secret):
    """`SHA-256:` + sha256 of the parameters joined with "+" plus the secret."""
    joined = "+".join([str(part) for part in parts] + [str(secret)])
    return DIGEST_PREFIX + sha256_hex(joined)


# --------------------------------------------------------------------------
# Response XML (response-message-structure.html)
# --------------------------------------------------------------------------


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(element, name):
    for child in element:
        if _local(child.tag) == name:
            return child
    return None


def _text(element):
    if element is None or element.text is None:
        return ""
    return element.text.strip()


# response-message-structure.html, StatusCode: "Four character alphanumeric
# representation of the status. The first character defines the system ...
# the last two characters are the actual error code." Of the documented error
# codes only "04 ERROR Permission denied" says the credentials were refused;
# "05 ERROR No data available" on a listing means there is nothing to list.
# Everything else (01 data invalid, 02/11 saldo, 03 no contract, 12 request
# misformed, 99 internal error...) is a service error, not a rejection.
REJECTION_ERROR_CODES = frozenset({"04"})
NO_DATA_ERROR_CODE = "05"


def error_code(status_code):
    """The documented two-character error code of a StatusCode ("4704" ->
    "04"), or "" when the StatusCode is too short to carry one."""
    status_code = (status_code or "").strip()
    return status_code[-2:] if len(status_code) >= 2 else ""


def _xml_parser():
    """No DTD, no entity expansion, no network: an APIX answer is data."""
    return etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        huge_tree=False,
        remove_comments=True,
        remove_pis=True,
    )


def parse_response(body):
    """Parse a Response document into a dict.

    Returns `{"status", "status_code", "free_text", "free_texts", "groups"}`
    where `groups` is a list of `{Value/@type: text}` dicts, one per Group.
    Within a group the first Value of a given type wins. `free_text` is the
    English FreeText (sent always, per the docs), else the first one.

    Raises `ApixInvalidResponse` when the body is empty, not XML, not a
    `Response` root, or has no `Status`.
    """
    if not body or not body.strip():
        raise ApixInvalidResponse("empty body")
    if isinstance(body, str):
        body = body.encode("utf-8")
    try:
        root = etree.fromstring(body, parser=_xml_parser())
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise ApixInvalidResponse("not XML") from exc
    if root is None:
        raise ApixInvalidResponse("not XML")
    if _local(root.tag) != "Response":
        raise ApixInvalidResponse("root element is not Response")
    status_el = _child(root, "Status")
    if status_el is None:
        raise ApixInvalidResponse("Response has no Status")

    free_texts = {}
    first_text = ""
    for child in root:
        if _local(child.tag) != "FreeText":
            continue
        text = _text(child)
        lang = child.get("language") or ""
        free_texts.setdefault(lang, text)
        first_text = first_text or text

    groups = []
    content = _child(root, "Content")
    for group_el in content if content is not None else ():
        if _local(group_el.tag) != "Group":
            continue
        group = {}
        for value_el in group_el:
            if _local(value_el.tag) != "Value":
                continue
            value_type = value_el.get("type")
            if not value_type:
                continue
            group.setdefault(value_type, _text(value_el))
        groups.append(group)

    return {
        "status": _text(status_el),
        "status_code": _text(_child(root, "StatusCode")),
        "free_text": free_texts.get("en", first_text),
        "free_texts": free_texts,
        "groups": groups,
    }


# --------------------------------------------------------------------------
# Sending answers
# --------------------------------------------------------------------------

#: SendInvoiceZIP's summary counters: Value/@type -> SendAnswer field.
_SEND_COUNTERS = {
    "Saldo": "saldo",
    "CostInCredits": "cost_in_credits",
    "NetworkedInvoices": "networked",
    "Letters": "letters",
    "LetterPages": "letter_pages",
    "AcceptedDocument": "accepted_count",
    "RejectedDocument": "rejected_count",
}

#: ValidateText per language: Value/@type -> key in a document's `texts`.
_VALIDATE_TEXTS = {"en": "ValidateText", "fi": "ValidateText:fi", "sv": "ValidateText:sv"}


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class SendAnswer:
    """APIX's answer to SendInvoiceZIP, OK or ERR alike.

    An ERR Response is an answer too: its `status_code` (03 no contract,
    04 permission denied, 02/11 no credit, ...) is mapped by the sending
    service, not raised here. `documents` holds one entry per
    detail group: `{accepted, document_id, texts: {en, fi, sv}}`, where the
    texts of an accepted document are warnings. `raw` is the answer body, for
    the masked exchange record.
    """

    status: str = ""
    status_code: str = ""
    free_text: str = ""
    batch_id: str = None
    saldo: int = None
    cost_in_credits: int = None
    networked: int = None
    letters: int = None
    letter_pages: int = None
    accepted_count: int = 0
    rejected_count: int = 0
    documents: list = field(default_factory=list)
    raw: bytes = field(default=b"", repr=False)


def parse_send_answer(body):
    """`SendAnswer` from a SendInvoiceZIP Response body.

    Raises `ApixInvalidResponse` when the body is not a Response.
    """
    parsed = parse_response(body)
    answer = SendAnswer(
        status=parsed["status"],
        status_code=parsed["status_code"],
        free_text=parsed["free_text"],
        raw=body if isinstance(body, bytes) else body.encode("utf-8"),
    )
    summary_read = False
    for group in parsed["groups"]:
        document_id = group.get("AcceptedDocumentID") or group.get("RejectedDocumentID")
        if document_id:
            answer.documents.append({
                "accepted": bool(group.get("AcceptedDocumentID")),
                "document_id": document_id,
                "texts": {lang: group.get(value_type, "") for lang, value_type in _VALIDATE_TEXTS.items()},
            })
            continue
        # The first group without a document id is the batch summary.
        if summary_read:
            continue
        summary_read = True
        answer.batch_id = group.get("BatchID") or None
        for value_type, name in _SEND_COUNTERS.items():
            setattr(answer, name, _int_or_none(group.get(value_type)))
    answer.accepted_count = answer.accepted_count or 0
    answer.rejected_count = answer.rejected_count or 0
    return answer


def _address(group):
    return {
        "receiver_name": group.get("ReceiverName", ""),
        "business_id": group.get("ReceiverYtunnus", ""),
        "edicode": group.get("ReceivereInvoiceAddress", ""),
        "operator": group.get("ReceiverOperator", ""),
    }


def _delivery_channel(group):
    return {
        "document_id": group.get("DocumentID", ""),
        "channel_name": group.get("ChannelName", ""),
        "cost_in_credits": _int_or_none(group.get("CostInCredits")),
        "receiver_name": group.get("ReceiverName", ""),
        "letter_pages": _int_or_none(group.get("LetterPages")),
        "letter_class": group.get("LetterClass", ""),
    }


# --------------------------------------------------------------------------
# Request XML (request-message-structure.html)
# --------------------------------------------------------------------------

#: DeliveryMethod request: document key -> Value/@type, in the documented order.
_DELIVERY_METHOD_VALUES = (
    ("document_id", "DocumentID"),
    ("sender_name", "SenderName"),
    ("sender_business_id", "SenderYtunnus"),
    ("receiver_name", "ReceiverName"),
    ("receiver_country_code", "ReceiverCountryCode"),
    ("receiver_business_id", "ReceiverYtunnus"),
    ("receiver_edicode", "ReceivereInvoiceAddress"),
    ("message_type", "MessageType"),
    ("letter_pages", "LetterPages"),
)


def build_request(groups):
    """A Request document with one Group per list of `(type, value)` pairs.

    Empty values are left out (every documented value but the names is
    optional).
    """
    root = etree.Element("Request", version="1.0")
    content = etree.SubElement(root, "Content")
    for pairs in groups:
        group = etree.SubElement(content, "Group")
        for value_type, value in pairs:
            if value in (None, ""):
                continue
            etree.SubElement(group, "Value", type=value_type).text = str(value)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


class ApixClient:
    """One APIX environment (production or test).

    :param environment: "production" or "test".
    :param timeout: seconds per request, 0 < timeout <= 60 (default 60).
    :param session: object with `request(method, url, timeout=...)` returning
        something with `.status_code` and `.content` (default: a new
        `requests.Session`).
    :param clock: callable returning an aware or UTC-naive `datetime`; used
        for the request timestamp (default: now in UTC).
    """

    def __init__(self, environment="production", timeout=MAX_TIMEOUT, session=None, clock=None):
        if environment not in HOSTS:
            raise ValueError(f"unknown APIX environment {environment!r}")
        if not timeout or timeout <= 0 or timeout > MAX_TIMEOUT:
            raise ValueError(f"timeout must be within (0, {MAX_TIMEOUT}] seconds")
        self.environment = environment
        self.timeout = timeout
        self.session = session if session is not None else requests.Session()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # -- helpers ---------------------------------------------------------

    def _timestamp(self):
        return utc_timestamp(self.clock())

    def _url(self, host, path, params):
        # Keep ':' and '@' literal so the URL reads as the documented example
        # (`d=SHA-256:<digest>`); both are legal in a query string.
        query = urlencode(params, safe=":@")
        return f"{HOSTS[self.environment][host]}/{path}?{query}"

    def _get(self, operation, host, path, params, storage_id=None, method="GET", **body):
        """One request; transport failures classified for the receiving calls
        (and the sending lookups, which reuse them): any network failure or
        timeout is `apix.unreachable`. `body` is `data=` / `headers=` for a
        PUT."""
        quiet_transport_logger()
        url = self._url(host, path, params)
        try:
            response = self.session.request(method, url, timeout=self.timeout, **body)
        # `from None` everywhere: a requests exception's text embeds the URL,
        # i.e. the digest — it must not ride along as __cause__/__context__.
        except requests.Timeout:
            self._log(operation, storage_id, "timeout")
            raise ApixUnreachable(f"{operation}: APIX did not answer within {self.timeout} s") from None
        except requests.ConnectionError as exc:
            self._log(operation, storage_id, f"network error ({type(exc).__name__})")
            raise ApixUnreachable(f"{operation}: APIX could not be reached ({type(exc).__name__})") from None
        except requests.RequestException as exc:
            self._log(operation, storage_id, f"request error ({type(exc).__name__})")
            raise ApixUnexpected(f"{operation}: request failed ({type(exc).__name__})") from None
        except OSError as exc:
            self._log(operation, storage_id, f"network error ({type(exc).__name__})")
            raise ApixUnreachable(f"{operation}: APIX could not be reached ({type(exc).__name__})") from None
        if response.status_code != 200:
            self._log(operation, storage_id, f"HTTP {response.status_code}")
            raise ApixUnexpected(
                f"{operation}: APIX answered HTTP {response.status_code}",
                http_status=response.status_code,
            )
        return response.content or b""

    @staticmethod
    def _log(operation, storage_id, status):
        if storage_id:
            _logger.info("APIX %s storage %s: %s", operation, storage_id, status)
            return
        _logger.info("APIX %s: %s", operation, status)

    def _call_xml(self, operation, host, path, params, storage_id=None, invalid_error=ApixUnexpected,
                  no_data_is_empty=False, request_xml=None):
        if request_xml is None:
            body = self._get(operation, host, path, params, storage_id=storage_id)
        else:
            body = self._get(
                operation, host, path, params, method="PUT",
                data=request_xml, headers={"Content-Type": "text/xml"},
            )
        try:
            parsed = parse_response(body)
        except ApixInvalidResponse as exc:
            self._log(operation, storage_id, f"invalid answer ({exc})")
            raise invalid_error(f"{operation}: APIX answer is not a valid Response ({exc})", http_status=200) from None
        self._log(operation, storage_id, f"{parsed['status']} {parsed['status_code']}".strip())
        if parsed["status"] == "OK":
            return parsed
        if no_data_is_empty and error_code(parsed["status_code"]) == NO_DATA_ERROR_CODE:
            # "05 ERROR No data available": nothing to list is not a failure.
            return dict(parsed, groups=[])
        self._raise_for_status(operation, parsed)
        return parsed

    @staticmethod
    def _raise_for_status(operation, parsed):
        """Raise the error an ERR Response stands for (see
        `REJECTION_ERROR_CODES`): `ApixRejected` for a refused credential,
        `ApixServiceError` for anything else."""
        status_code = parsed["status_code"]
        message = f"{operation}: {parsed['free_text'] or 'error'} (StatusCode {status_code or '?'})"
        if error_code(status_code) in REJECTION_ERROR_CODES:
            raise ApixRejected(message, status_code=status_code, http_status=200)
        raise ApixServiceError(message, status_code=status_code, http_status=200)

    # -- operations ------------------------------------------------------

    def retrieve_transfer_id(
        self, business_id, user_email, password=None, id_qualifier=DEFAULT_ID_QUALIFIER, password_sha256=None
    ):
        """RetrieveTransferID: fetch TransferID, TransferKey, UniqueCompanyID.

        GET https://api.apix.fi/app-transferID?id=&idq=&uid=&ts=&d=
        Digest: id+idq+uid+ts+sha256(password). The password itself is used
        only to compute the hash and is never stored or sent. Callers that
        must not hold the clear password (a dialog saved to the database
        before its button runs) pass ``password_sha256`` instead.
        """
        if password_sha256 is None:
            if password is None:
                raise ValueError("retrieve_transfer_id needs password or password_sha256")
            password_sha256 = password_hash(password)
        ts = self._timestamp()
        digest = compute_digest([business_id, id_qualifier, user_email, ts], password_sha256)
        params = [("id", business_id), ("idq", id_qualifier), ("uid", user_email), ("ts", ts), ("d", digest)]
        parsed = self._call_xml("RetrieveTransferID", "api", "app-transferID", params)
        group = parsed["groups"][0] if parsed["groups"] else {}
        if not group.get("TransferID") or not group.get("TransferKey"):
            raise ApixUnexpected("RetrieveTransferID: answer has no TransferID/TransferKey", http_status=200)
        return group

    def company_info(self, transfer_id, transfer_key):
        """RetrieveCompanyInformation.

        GET https://api.apix.fi/getcompanyinfo?TraID=&t=&d=
        Digest: TraID+t+TransferKey. Returns the parsed Response dict; the
        first group is the company, following groups are addresses.
        """
        ts = self._timestamp()
        digest = compute_digest([transfer_id, ts], transfer_key)
        params = [("TraID", transfer_id), ("t", ts), ("d", digest)]
        return self._call_xml("RetrieveCompanyInformation", "api", "getcompanyinfo", params)

    def list_packages(self, transfer_id, transfer_key):
        """ListInvoiceZIPs: one dict per Group (StorageID, StorageKey, ...).

        GET https://terminal.apix.fi/list2?TraID=&t=&d=
        Digest: TraID+t+TransferKey. A valid Response with no groups, or an
        ERR Response with error code 05 ("No data available"), is an empty
        list; an answer that is not a valid Response is `apix.empty_answer`,
        never "no packages".
        """
        ts = self._timestamp()
        digest = compute_digest([transfer_id, ts], transfer_key)
        params = [("TraID", transfer_id), ("t", ts), ("d", digest)]
        parsed = self._call_xml(
            "ListInvoiceZIPs", "terminal", "list2", params, invalid_error=ApixEmptyAnswer,
            no_data_is_empty=True,
        )
        return parsed["groups"]

    def download(self, storage_id, storage_key, mark_received):
        """Download: the storage contents (bytes, a ZIP).

        GET https://terminal.apix.fi/download?markreceived=&SID=&t=&d=
        Digest: markreceived-value+SID+t+StorageKey. `markreceived` is always
        sent explicitly because omitting it marks the storage received.
        Wrong id or key returns nothing → `apix.empty_answer`.
        """
        mark = "yes" if mark_received else "no"
        ts = self._timestamp()
        digest = compute_digest([mark, storage_id, ts], storage_key)
        params = [("markreceived", mark), ("SID", storage_id), ("t", ts), ("d", digest)]
        body = self._get("Download", "terminal", "download", params, storage_id=storage_id)
        if not body or not body.strip():
            self._log("Download", storage_id, "empty answer")
            raise ApixEmptyAnswer("Download: APIX returned nothing (wrong storage id or key)", http_status=200)
        self._raise_if_error_response(body, storage_id)
        self._log("Download", storage_id, f"OK ({len(body)} bytes)")
        return body

    def _raise_if_error_response(self, body, storage_id):
        """A download is a file, but if APIX sends an ERR Response, say so."""
        head = body.lstrip()[:200]
        if not (head.startswith(b"<?xml") or head.startswith(b"<Response")):
            return
        try:
            parsed = parse_response(body)
        except ApixInvalidResponse:
            return
        if parsed["status"] == "OK":
            return
        self._log("Download", storage_id, f"{parsed['status']} {parsed['status_code']}")
        if error_code(parsed["status_code"]) == NO_DATA_ERROR_CODE:
            # Nothing stored under that id and key: the same as an empty body.
            raise ApixEmptyAnswer(
                f"Download: {parsed['free_text'] or 'no data'} (StatusCode {parsed['status_code']})",
                status_code=parsed["status_code"],
                http_status=200,
            )
        self._raise_for_status("Download", parsed)

    def metadata(self, transfer_id, transfer_key, storage_id):
        """GetMetadata: parsed Response dict for the storage and its files.

        GET https://terminal.apix.fi/metadata?TraID=&SID=&t=&d=
        Digest: SID+TraID+t+TransferKey.
        """
        ts = self._timestamp()
        digest = compute_digest([storage_id, transfer_id, ts], transfer_key)
        params = [("TraID", transfer_id), ("SID", storage_id), ("t", ts), ("d", digest)]
        return self._call_xml("GetMetadata", "terminal", "metadata", params, storage_id=storage_id)

    # -- sending -------------------

    def send_invoice_zip(self, transfer_id, transfer_key, soft, ver, zip_bytes):
        """SendInvoiceZIP: send one package; returns a `SendAnswer`.

        PUT https://api.apix.fi/invoices?soft=&ver=&TraID=&t=&d=
        (application/octet-stream, the zip as body).
        Digest: soft+ver+TraID+t+TransferKey.

        Every valid Response is returned, ERR included, for the service to
        map. Raises `ApixUnreachable` when the connection
        could not be opened (nothing left), `ApixNoAnswer` when the body may
        have reached APIX but no valid answer came, `ApixUnexpected` on an
        HTTP status other than 200. Never retried here.
        """
        operation = "SendInvoiceZIP"
        ts = self._timestamp()
        digest = compute_digest([soft, ver, transfer_id, ts], transfer_key)
        params = [("soft", soft), ("ver", ver), ("TraID", transfer_id), ("t", ts), ("d", digest)]
        body = self._send(operation, self._url("api", "invoices", params), zip_bytes)
        try:
            answer = parse_send_answer(body)
        except ApixInvalidResponse as exc:
            self._log(operation, None, f"no valid answer ({exc})")
            raise ApixNoAnswer(
                f"{operation}: APIX's answer is not a valid Response ({exc}); the invoice may have reached APIX",
                http_status=200,
            ) from None
        self._log(
            operation, None,
            f"{answer.status} {answer.status_code} "
            f"(accepted {answer.accepted_count}, rejected {answer.rejected_count})",
        )
        return answer

    def _send(self, operation, url, payload):
        """PUT the package. Transport failures split by whether the body can
        have left: see `_never_left`."""
        quiet_transport_logger()
        try:
            response = self.session.request(
                "PUT", url, timeout=self.timeout, data=payload,
                headers={"Content-Type": "application/octet-stream"},
            )
        # `from None`: a requests exception's text embeds the URL (the digest).
        except OSError as exc:  # requests' exceptions are OSErrors too
            raise self._send_transport_error(operation, exc) from None
        if response.status_code != 200:
            self._log(operation, None, f"HTTP {response.status_code}")
            raise ApixUnexpected(
                f"{operation}: APIX answered HTTP {response.status_code}",
                http_status=response.status_code,
            )
        return response.content or b""

    def _send_transport_error(self, operation, exc):
        name = type(exc).__name__
        if _never_left(exc):
            self._log(operation, None, f"could not connect ({name})")
            return ApixUnreachable(f"{operation}: APIX could not be reached ({name})")
        if isinstance(exc, requests.RequestException) and not isinstance(exc, _POSSIBLY_SENT):
            # InvalidURL, TooManyRedirects...: no answer to classify.
            self._log(operation, None, f"request error ({name})")
            return ApixUnexpected(f"{operation}: request failed ({name})")
        self._log(operation, None, f"no answer ({name})")
        return ApixNoAnswer(
            f"{operation}: APIX did not answer within {self.timeout} s or the connection broke "
            f"({name}); the invoice may have reached APIX"
        )

    def address_query(self, transfer_id, transfer_key, business_id=None, name=None):
        """AddressQuery: every e-invoice address APIX knows for a receiver.

        PUT https://api.apix.fi/addressquery?uid=&t=&d= with a Request XML
        holding one Group: ReceiverYtunnus (preferred) and/or ReceiverName.
        Digest: TransferID+t+TransferKey.

        Returns `[{receiver_name, business_id, edicode, operator}]`, `[]` on
        error 05 "No data available". Other errors raise the receiving
        classes, carrying APIX's text and status code.
        """
        if not business_id and not name:
            raise ValueError("address_query needs a business_id or a name")
        request_xml = build_request([[("ReceiverYtunnus", business_id), ("ReceiverName", name)]])
        parsed = self._call_xml(
            "AddressQuery", "api", "addressquery", self._uid_params(transfer_id, transfer_key),
            no_data_is_empty=True, request_xml=request_xml,
        )
        return [_address(group) for group in parsed["groups"]]

    def delivery_method(self, transfer_id, transfer_key, documents):
        """DeliveryMethod: the channel and price APIX would use per document.

        PUT https://api.apix.fi/method?uid=&t=&d= with a Request XML holding
        one Group per document. `documents` are dicts with the keys of
        `_DELIVERY_METHOD_VALUES` (`message_type` defaults to `eInvoice`).
        Digest: TransferID+t+TransferKey.

        Returns `[{document_id, channel_name, cost_in_credits, receiver_name,
        letter_pages, letter_class}]`; errors raise the receiving classes.
        """
        groups = []
        for document in documents:
            values = dict({"message_type": "eInvoice"}, **document)
            groups.append([(value_type, values.get(key)) for key, value_type in _DELIVERY_METHOD_VALUES])
        parsed = self._call_xml(
            "DeliveryMethod", "api", "method", self._uid_params(transfer_id, transfer_key),
            request_xml=build_request(groups),
        )
        return [_delivery_channel(group) for group in parsed["groups"]]

    def _uid_params(self, transfer_id, transfer_key):
        ts = self._timestamp()
        digest = compute_digest([transfer_id, ts], transfer_key)
        return [("uid", transfer_id), ("t", ts), ("d", digest)]


#: urllib3 reasons that mean the connection was never opened, so nothing of
#: the request can have reached APIX.
_CONNECT_REASONS = (NewConnectionError, ConnectTimeoutError, ProxyError, Urllib3SSLError)

#: Failures raised before anything was sent: no connection, TLS or proxy.
_NEVER_LEFT = (
    requests.ConnectTimeout,
    requests.exceptions.SSLError,
    requests.exceptions.ProxyError,
    ConnectionRefusedError,
)

#: requests failures after which the body may have reached APIX.
_POSSIBLY_SENT = (
    requests.Timeout,
    requests.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
)


def _never_left(exc):
    """True when `exc` proves the request body never left this machine.

    Anything not proven (a read timeout, a connection reset or aborted after
    the upload, a broken chunked answer) may have reached APIX — sending must
    then say so rather than report connection trouble.
    """
    if isinstance(exc, _NEVER_LEFT):
        return True
    if not isinstance(exc, requests.ConnectionError):
        return False
    reason = exc.args[0] if exc.args else None
    if isinstance(reason, MaxRetryError):
        reason = reason.reason
    return isinstance(reason, _CONNECT_REASONS)
