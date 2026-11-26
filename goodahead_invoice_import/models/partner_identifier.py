# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Every way a partner is known, beside the one field that holds the latest value.

The name a vendor prints on its invoice is often not the name in the ledger,
and many vendor books carry phones and emails but rarely a VAT or bank account.
A partner field holds one value; a vendor is reached on several numbers and
addresses, so identifier rows are only ever added, never replaced or removed.
"""

import logging
import re

from psycopg2.errors import UniqueViolation
from psycopg2.extras import execute_values

from odoo import api, fields, models
from odoo.tools import ormcache

_logger = logging.getLogger(__name__)

_NON_DIGITS = re.compile(r"\D+")

# A hostname whose last label is alphabetic and at least two characters. Not a
# validator for the internet — a filter that keeps obvious non-domains ("n/a",
# "see above", a bare phone number) out of the table.
_HOST_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*\.[a-z]{2,}$"
)

#: System parameter that extends (never replaces) the shipped domain rules
#: below. The key keeps its historical name: renaming a config parameter would
#: silently drop existing values.
PUBLIC_EMAIL_DOMAINS_PARAM = "goodahead_invoice_ai_import.public_email_domains"

#: Commas, semicolons and any whitespace all separate entries, so an operator
#: can paste a list in whichever shape they have it.
_DOMAIN_SETTING_SPLIT = re.compile(r"[\s,;]+")

# Free-mail provider domains are never identifiers: otherwise any two vendors
# using the same provider would match each other. This set is a floor that the
# system parameter can only extend or explicitly subtract from.
#
# Credit: part of this list comes from https://github.com/willwhite/freemail
# (`data/free.txt`, ISC licence, copyright (c) 2015 Will White).
#
# Sources: common US/global providers; providers named by the public
# `willwhite/freemail` list that are widespread in contact books; and
# Nordic consumer providers named by that list or confirmed by their MX
# records. A few entries (e.g. telia.com, comcast.com, visir.is) are also
# companies' own domains; they are kept because consumer mailboxes on them are
# common, so bills from those companies must match on another key.
_PUBLIC_EMAIL_DOMAINS = frozenset(
    {
        # US/global providers
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "yahoo.co.uk",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "aol.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "msn.com",
        "comcast.net",
        "verizon.net",
        "att.net",
        "sbcglobal.net",
        "bellsouth.net",
        "protonmail.com",
        "proton.me",
        "gmx.com",
        "mail.com",
        "yandex.com",
        "qq.com",
        "163.com",
        "126.com",
        "sina.com",
        "foxmail.com",
        # named by the freemail list and common in contact books
        "aim.com",
        "charter.net",
        "comcast.com",
        "cox.net",
        "cs.com",
        "earthlink.net",
        "embarqmail.com",
        "frontier.com",
        "frontiernet.net",
        "hotmail.fr",
        "hotmail.it",
        "juno.com",
        "mail.ru",
        "naver.com",
        "netscape.net",
        "optonline.net",
        "prodigy.net",
        "ptd.net",
        "roadrunner.com",
        "rocketmail.com",
        "windstream.net",
        "yahoo.es",
        "yahoo.it",
        "ymail.com",
        "zoominternet.net",
        # Nordic — named by the freemail list and still carrying a live MX
        "alskens.dk",
        "bluemail.dk",
        "e-mail.dk",
        "fastmail.se",
        "forum.dk",
        "home.se",
        "hotmail.no",
        "iobox.fi",
        "jippii.fi",
        "jubiimail.dk",
        "live.dk",
        "live.no",
        "live.se",
        "luukku.com",
        "mailme.dk",
        "netby.dk",
        "outlook.dk",
        "sci.fi",
        "sol.dk",
        "spray.no",
        "spray.se",
        "yahoo.dk",
        "yahoo.fi",
        "yahoo.no",
        "yahoo.se",
        # Nordic — confirmed by MX: Microsoft consumer mail
        "hotmail.dk",
        "hotmail.fi",
        "hotmail.se",
        "live.fi",
        # Nordic — confirmed by MX: Finnish operators and portals
        "dnainternet.net",
        "elisanet.fi",
        "kolumbus.fi",
        "saunalahti.fi",
        "suomi24.fi",
        # Nordic — confirmed by MX: Swedish
        "passagen.se",
        "telia.com",
        # Nordic — confirmed by MX: Norwegian
        "broadpark.no",
        "frisurf.no",
        "online.no",
        "start.no",
        # Nordic — confirmed by MX: Danish
        "mail.dk",
        "post.tele.dk",
        "stofanet.dk",
        "youmail.dk",
        # Nordic — confirmed by MX: Icelandic
        "internet.is",
        "simnet.is",
        "visir.is",
    }
)

# Hosts that identify nobody although no freemail list names them: import
# placeholders, misspellings of a provider, and documentation domains.
_NEVER_IDENTIFYING_DOMAINS = frozenset(
    {
        # an import placeholder seen on many unrelated partners
        "emaildomain.com",
        # near-misses of a shipped provider — gmail, typed badly
        "gamil.com",
        "gmai.com",
        "gmail.con",
        # RFC 2606 §3 reserves these for documentation, so they can never be a
        # company's own domain. They are what a half-filled import writes.
        "example.com",
        "example.net",
        "example.org",
    }
)

# Excluding a domain does not exclude its subdomains (see `_host_of`). Every
# host under these suffixes is a consumer mailbox (regional `rr.com` hosts).
_PUBLIC_EMAIL_SUFFIXES = frozenset({"rr.com"})

#: Everything an operator may subtract with a leading `-`, so any shipped rule
#: that is wrong for one database can be undone there.
_SHIPPED_DOMAIN_RULES = (
    _PUBLIC_EMAIL_DOMAINS | _NEVER_IDENTIFYING_DOMAINS | _PUBLIC_EMAIL_SUFFIXES
)


class GoodaheadPartnerIdentifier(models.Model):
    _name = "goodahead.partner.identifier"
    _description = "Another way a partner is known"
    _order = "partner_id, kind, value"

    partner_id = fields.Many2one(
        "res.partner",
        required=True,
        ondelete="cascade",
        index=True,
        string="Partner",
    )
    kind = fields.Selection(
        [("phone", "Phone"), ("domain", "Domain")],
        required=True,
        index=True,
    )
    value = fields.Char(
        required=True,
        index=True,
        help="Always stored NORMALISED: the last ten digits of a phone, or a "
        "lower-case host name. A value stored in any other shape matches "
        "nothing, because the import normalises what it reads off the invoice "
        "before it looks.",
    )
    source = fields.Char(
        help="Where this row came from, in words — 'AI import INV0001', "
        "'seeded from res.partner.phone'. Free text on purpose: it is read by "
        "a person deciding whether to trust the row, not by code.",
    )
    active = fields.Boolean(default=True)

    _sql_constraints = [
        (
            "partner_kind_value_uniq",
            "UNIQUE(partner_id, kind, value)",
            "This partner already carries that identifier.",
        ),
    ]

    @api.model
    def _normalise_phone(self, value):
        """The last ten digits of a phone number, or None when there are fewer.

        This assumes North American numbers (ten digits plus an optional
        ``1``); numbers from other countries lose their country code and could
        in principle collide. Extensions are not stripped and produce values
        that simply match nothing. ``float`` and ``bool`` are rejected because
        their string forms would yield a plausible but wrong number.
        """
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return None
        digits = _NON_DIGITS.sub("", str(value))
        if len(digits) < 10:
            return None
        return digits[-10:]

    @api.model
    def _host_of(self, value):
        """The lower-case host out of an email address, a URL or a bare domain.

        This is the whole host, not the registrable domain (there is no
        public-suffix list available), so ``mail.vendor.com`` and
        ``vendor.com`` are different identifiers. Over-separating is the safe
        direction. Strips the scheme, path, query, fragment, port, a leading
        ``www.`` and a trailing dot; returns None if the rest is not host-shaped.
        """
        if isinstance(value, bool) or not isinstance(value, str):
            return None
        text = value.strip().lower()
        if not text:
            return None

        if "@" in text:
            text = text.rsplit("@", 1)[1]
        elif "//" in text:
            text = text.split("//", 1)[1]

        for separator in ("/", "?", "#"):
            text = text.split(separator, 1)[0]
        text = text.split(":", 1)[0]
        text = text.strip().rstrip(".")
        if text.startswith("www."):
            text = text[4:]

        if not _HOST_RE.match(text):
            return None
        return text

    @api.model
    def _normalise_domain(self, value):
        """The host an email address, URL or bare domain names, or None.

        Non-identifying domains (free providers etc.) return None here, in the
        one function every caller goes through, so they are never stored or
        matched on.
        """
        host = self._host_of(value)
        if not host or self._is_non_identifying_domain(host):
            return None
        return host

    @api.model
    def _is_non_identifying_domain(self, host):
        """Whether the host identifies nobody: a free provider, a placeholder,
        or a mailbox under a listed suffix.

        The suffix test matches whole labels: ``nc.rr.com`` is under ``rr.com``,
        ``bigrr.com`` is not.
        """
        if not host:
            return False
        if host in self._non_identifying_domains():
            return True
        return any(
            host == suffix or host.endswith("." + suffix)
            for suffix in self._non_identifying_suffixes()
        )

    @api.model
    @ormcache()
    def _non_identifying_domains(self):
        """Every host that identifies nobody: the shipped floors, plus the
        operator's additions, minus explicit subtractions.

        The shipped constants are a floor and the parameter is a delta, rather
        than a parameter seeded from the constants: clearing the parameter
        cannot empty the list, and newly shipped domains reach existing
        databases on upgrade. The list is system-wide because the identifier
        table is not company-scoped. Cached in the default registry cache,
        which ``ir.config_parameter`` clears on every change.
        """
        setting = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param(PUBLIC_EMAIL_DOMAINS_PARAM, "")
        )
        added, removed = self._parse_domain_setting(setting)
        floor = _PUBLIC_EMAIL_DOMAINS | _NEVER_IDENTIFYING_DOMAINS
        return frozenset(floor | added) - removed

    @api.model
    @ormcache()
    def _non_identifying_suffixes(self):
        """The shipped suffixes, minus whatever the operator subtracted.

        Operators cannot add suffixes, only exact hosts. ``log=False`` avoids
        logging the same parse warnings twice.
        """
        setting = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param(PUBLIC_EMAIL_DOMAINS_PARAM, "")
        )
        _added, removed = self._parse_domain_setting(setting, log=False)
        return frozenset(_PUBLIC_EMAIL_SUFFIXES) - removed

    @api.model
    def _parse_domain_setting(self, setting, log=True):
        """``(added, removed)`` out of the configured text.

        Entries are separated by commas, semicolons or whitespace; ``#`` starts
        a comment; a leading ``-`` subtracts a shipped rule. Unreadable entries
        are dropped and logged, and every effective subtraction is logged,
        since removing a provider can make many partners matchable.
        """
        added, removed, rejected = set(), set(), []
        for line in str(setting or "").splitlines():
            line = line.split("#", 1)[0]
            for raw in _DOMAIN_SETTING_SPLIT.split(line):
                entry = raw.strip().lower()
                if not entry:
                    continue
                subtract = entry.startswith("-")
                host = entry[1:] if subtract else entry
                host = host.strip().rstrip(".")
                if host.startswith("www."):
                    host = host[4:]
                if not _HOST_RE.match(host):
                    rejected.append(raw.strip())
                    continue
                (removed if subtract else added).add(host)

        unknown = removed - _SHIPPED_DOMAIN_RULES
        effective = removed & _SHIPPED_DOMAIN_RULES

        if not log:
            return frozenset(added), frozenset(effective)

        if rejected:
            _logger.warning(
                "AI invoice import: %s is not a host name and was ignored in "
                "%s. The shipped list is unaffected.",
                ", ".join(repr(entry) for entry in sorted(rejected)),
                PUBLIC_EMAIL_DOMAINS_PARAM,
            )

        if unknown:
            _logger.warning(
                "AI invoice import: %s cannot be removed from the list of "
                "domains that identify nobody because it was never on it; the "
                "entry does nothing. (%s)",
                ", ".join(sorted(unknown)),
                PUBLIC_EMAIL_DOMAINS_PARAM,
            )

        if effective:
            _logger.warning(
                "AI invoice import: %s has been REMOVED from the list of "
                "domains that identify nobody by %s. Every partner on it can "
                "now be matched by its email domain, and if exactly one of "
                "them can receive bills, every invoice printing an address "
                "there will be filed against that one company.",
                ", ".join(sorted(effective)),
                PUBLIC_EMAIL_DOMAINS_PARAM,
            )
        return frozenset(added), frozenset(effective)

    @api.model
    def _derive_from_contact(self, phone=None, email=None, website=None):
        """The ``(kind, value)`` pairs derivable from one partner's contact
        fields, de-duplicated and in a stable order. An email and a website on
        the same domain produce ONE row, not two."""
        pairs = []
        normalised_phone = self._normalise_phone(phone)
        if normalised_phone:
            pairs.append(("phone", normalised_phone))
        for raw in (email, website):
            domain = self._normalise_domain(raw)
            if domain and ("domain", domain) not in pairs:
                pairs.append(("domain", domain))
        return pairs

    @api.model
    def _add_pairs(self, pairs_by_partner, source):
        """Add the ``(kind, value)`` pairs that are not already stored.

        ``pairs_by_partner`` maps a partner id to an iterable of pairs. Returns
        the ``(partner_id, kind, value)`` triples actually created. Runs as
        ``sudo()``: the rows are a system-maintained derivation, and editing a
        contact must not fail on this table's ACLs.
        """
        wanted = {
            partner_id: set(pairs) for partner_id, pairs in pairs_by_partner.items()
            if pairs
        }
        if not wanted:
            return []

        existing = self.sudo().with_context(active_test=False).search(
            [("partner_id", "in", list(wanted))]
        )
        for row in existing:
            wanted.get(row.partner_id.id, set()).discard((row.kind, row.value))

        missing = [
            (partner_id, kind, value)
            for partner_id, pairs in wanted.items()
            for kind, value in sorted(pairs)
        ]
        if not missing:
            return []

        def vals(triple):
            partner_id, kind, value = triple
            return {
                "partner_id": partner_id,
                "kind": kind,
                "value": value,
                "source": source,
            }

        # Parallel imports for the same vendor can race on the same row. A
        # unique violation would roll back the caller's savepoint, so insert
        # the batch once and, on conflict, row by row, skipping rows another
        # transaction already stored. Not `ON CONFLICT DO NOTHING`: under
        # REPEATABLE READ that raises a serialization failure instead, and a
        # queue_job retry would repeat the paid extraction.
        Identifier = self.sudo()
        try:
            with self.env.cr.savepoint():
                Identifier.create([vals(t) for t in missing])
            return missing
        except UniqueViolation:
            _logger.info(
                "AI invoice import: identifier rows were stored concurrently by "
                "another transaction; adding the rest one by one (%s).",
                source,
            )

        created = []
        for triple in missing:
            try:
                with self.env.cr.savepoint():
                    Identifier.create(vals(triple))
            except UniqueViolation:
                continue
            created.append(triple)
        return created

    @api.model
    def _commercial_partner_for(self, kind, value):
        """The single active commercial partner this identifier resolves to.

        If it resolves to more than one, an empty recordset is returned: an
        ambiguous match would file the bill against the wrong vendor. Rows on
        child contacts resolve to their commercial partner. Archived partners
        are excluded here and offered as candidates instead.
        """
        Partner = self.env["res.partner"]
        if not value:
            return Partner
        rows = self.search([("kind", "=", kind), ("value", "=", value)])
        if not rows:
            return Partner
        partners = rows.partner_id.commercial_partner_id.filtered("active")
        partners = self._drop_partners_that_are_never_a_vendor(partners)
        if len(partners) != 1:
            return Partner
        return partners

    @api.model
    def _drop_partners_that_are_never_a_vendor(self, partners):
        """Remove partners a vendor bill can never be filed against (deciding path only).

        Identifiers are seeded from the whole contact book, so a uniquely
        resolving phone or domain often belongs to a customer, or to one of
        our own companies, whose details also appear on every invoice as the
        buyer. Two rules:

        1. the company's own partner and users (``_ai_drop_impossible_vendors``);
        2. customer-only partners (``supplier_rank = 0`` and
           ``customer_rank > 0``). A new vendor has both at zero and still matches.

        Candidates shown to a person are filtered by rule 1 only (``_partners_for``).
        """
        if not partners:
            return partners
        # Rule 1 is shared with core's own resolution steps. No company is
        # passed because the identifier steps are not company-scoped. Note that
        # rule 2 can also refuse a sister company that is mostly a customer.
        partners = self.env["res.partner"]._ai_drop_impossible_vendors(partners)
        return partners.filtered(
            lambda p: not (p.supplier_rank == 0 and p.customer_rank > 0)
        )

    @api.model
    def _partners_for(self, kind, values):
        """Every commercial partner carrying any of ``values`` for ``kind``,
        archived ones included. For offering candidates, never for deciding —
        `_commercial_partner_for` is the one that decides."""
        Partner = self.env["res.partner"]
        values = [v for v in values if v]
        if not values:
            return Partner
        rows = self.with_context(active_test=False).search(
            [("kind", "=", kind), ("value", "in", values)]
        )
        partners = rows.partner_id.commercial_partner_id
        # Our own companies are never offered; customers still are, since an
        # offer is not a decision.
        return self.env["res.partner"]._ai_drop_impossible_vendors(partners)

    @api.model
    def _seed_from_partners(self, batch_size=5000, source="seeded from res.partner"):
        """Derive identifiers from every existing partner's phone/email/website.

        Without a seed the phone and domain steps would match nothing after
        install or upgrade, so this is called from both ``post_init_hook`` and
        a migration script. ``ON CONFLICT DO NOTHING`` makes it idempotent.
        Raw SQL because the rows are a pure derivation and the ORM is too slow
        on large contact books.
        """
        cr = self.env.cr
        cr.execute(
            """
            SELECT id, phone, email, website
              FROM res_partner
             WHERE phone IS NOT NULL
                OR email IS NOT NULL
                OR website IS NOT NULL
             ORDER BY id
            """
        )
        rows = []
        for partner_id, phone, email, website in cr.fetchall():
            for kind, value in self._derive_from_contact(phone, email, website):
                rows.append((partner_id, kind, value, source))

        uid = self.env.uid
        for start in range(0, len(rows), batch_size):
            batch = [
                (partner_id, kind, value, source, True, uid, uid)
                for partner_id, kind, value, source in rows[start:start + batch_size]
            ]
            execute_values(
                cr,
                """
                INSERT INTO goodahead_partner_identifier
                    (partner_id, kind, value, source, active,
                     create_uid, write_uid, create_date, write_date)
                VALUES %s
                ON CONFLICT (partner_id, kind, value) DO NOTHING
                """,
                batch,
                template="(%s, %s, %s, %s, %s, %s, %s, now() at time zone 'UTC',"
                " now() at time zone 'UTC')",
                page_size=1000,
            )

        # Count from the table: `execute_values` pages the batch, so
        # `cr.rowcount` only reflects the last page.
        cr.execute(
            "SELECT count(*) FROM goodahead_partner_identifier WHERE source = %s",
            (source,),
        )
        present = cr.fetchone()[0]

        self.env.invalidate_all()
        _logger.info(
            "AI invoice import: %s derivable phone/domain pairs across the "
            "contact book; %s identifier rows now carry source %r.",
            len(rows),
            present,
            source,
        )
        return present
