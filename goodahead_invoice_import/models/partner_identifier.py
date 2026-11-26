# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Every way a partner is known, beside the one field that holds the latest value.

🔴 WHY THIS TABLE EXISTS. A sample set of seven vendor bills was put through the
AI import. All seven extracted correctly and SIX of the seven created a
duplicate vendor beside an active, frequently billed one.
The only matching key that fires on an ordinary US invoice is core's exact name
step, and the name a vendor prints on its own paper is almost never the name the
ledger holds: the invoice says `Fabrikam, LLC` and the ledger says `Contoso`; the
invoice says `Northwind` and the ledger says `NORTHWIND - Smith reps`.

On a typical US vendor book, most suppliers carry a phone and about half an
email, while **almost none carry a bank account or a VAT**. So phone and email domain
are the only identifiers that exist at any scale here, and the European approach
— registration number, VAT, IBAN — does not transfer at all.

⚠️ A PARTNER FIELD CANNOT DO THIS JOB, which is why the rows live here rather
than in `res.partner.phone`. `phone`, `email` and `website` each hold ONE value,
the latest one somebody typed. A vendor is known by several: the sales line and
the accounts-receivable line, `orders@contoso-us.test` and `contoso-us.test`.
Overwriting `orders@contoso-us.test` with whatever the newest invoice happened to
print would be a regression dressed up as an improvement. So rows are only ever
ADDED, never replaced and never removed because a field changed.
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

#: The system parameter that EXTENDS the shipped rules below. See
#: `_non_identifying_domains` for the relationship between the two, which is the
#: whole of the dual-core design.
#:
#: ⚠️ THE KEY IS NOT RENAMED, AND THE NAME IS NOW NARROWER THAN WHAT IT HOLDS.
#: 19.0.3.16.0 widened the seam from "free email providers" to "domains that
#: identify nobody", but the string below is where an operator's own edits are
#: stored. Renaming an `ir.config_parameter` key silently abandons its value —
#: the old row stays, nothing reads it, and the setting page comes up empty with
#: no error anywhere. That is this project's signature failure shape, so the key
#: stays and the label, the help and the method names carry the correction.
PUBLIC_EMAIL_DOMAINS_PARAM = "goodahead_invoice_ai_import.public_email_domains"

#: Commas, semicolons and any whitespace all separate entries, so an operator
#: can paste a list in whichever shape they have it.
_DOMAIN_SETTING_SPLIT = re.compile(r"[\s,;]+")

# 🔴 A FREE-PROVIDER DOMAIN IS NEVER AN IDENTIFIER. This is not a theoretical
# guard: a vendor whose only address is a gmail mailbox is an ordinary case.
# Without this list that one address would make every gmail vendor a match for
# every other gmail vendor — and the uniqueness rule below would then silently
# turn all of them into "ambiguous", i.e. into no match at all, which is the
# polite way this would fail. The impolite way is a two-vendor collision filing
# a bill against the wrong company with confidence.
#
# 🔴 THIS IS A FLOOR, NOT A DEFAULT VALUE, AND THE DIFFERENCE IS THE WHOLE OF
# STEP 8b. `_non_identifying_domains()` returns this set, PLUS the second floor
# below it, PLUS whatever `PUBLIC_EMAIL_DOMAINS_PARAM` adds; the parameter never
# replaces any of them. the dual-core design asked for "an `ir.config_parameter`
# (comma-separated) seeded from the constant", and seeding is a different design
# that fails in two directions at once — see the long note on
# `_non_identifying_domains`.
#
# ---------------------------------------------------------------------------
# WHERE EVERY ENTRY CAME FROM, because a hand-written list of other people's
# mail providers is exactly the shape of thing that gets guessed.
#
# Credit: part of this list comes from https://github.com/willwhite/freemail
# (`data/free.txt`, ISC licence, copyright (c) 2015 Will White).
#
# (a) The 27 US/global entries this module shipped with since 19.0.3.0.0.
#
# (b) 🆕 25 added by a RULE, not by taste: a domain that
#     `willwhite/freemail`'s `data/free.txt` names AND that a typical large
#     contact book carries on **five or more distinct commercial partners**
#     — `optonline.net`, `ymail.com`,
#     `cox.net`, `earthlink.net`, `charter.net`, `frontier.com`, `aim.com`,
#     `embarqmail.com`, `windstream.net`, … Both clauses bind: `optimum.net` and
#     `rcn.com` are NOT here, because the published list does not name them, and
#     `rr.com` is not here because the book does not carry it — only regional
#     hosts such as `nycap.rr.com`.
#
#     🔴 THAT LAST CLAUSE USED TO READ "…which are ambiguous and therefore
#     already decline", AND IT WAS CHECKED ON ONE MEMBER OF THE FAMILY AND
#     GENERALISED TO THE WHOLE FAMILY (found in review). A regional host can
#     be carried by several partners with none bill-eligible (it declines),
#     or by several partners with exactly one bill-eligible (it DECIDES).
#     The regional hosts are covered by `_PUBLIC_EMAIL_SUFFIXES` below now, not
#     by an assumption about them.
#
# (c) 🆕 47 Nordic entries, for Nordic deployments — in the SHIPPED constant and
#     not in a data file or a locale pack, because the dual-core design says the
#     module must run unchanged on both deployments, and a provider list that a
#     deployment has to configure is a provider list it will be running without. Two sources, both checkable:
#     25 that `free.txt` names under a Nordic ccTLD (plus `luukku.com`) and that
#     still publish a live MX record, and 22 more confirmed by their MX alone.
#
#     ⚠️ READING AN EARLIER FORK FIRST WAS THE INSTRUCTION AND IT RETURNED
#     NOTHING. *(measured on the earlier fork `19.0.1.1.3`, read-only)*
#     that copy has no public-domain list and no domain key at all — it matches
#     on the business id — so there was nothing to copy and no bug of theirs to
#     inherit. The list below is ours.
#
#     🔴 FOUR DOMAINS A PLAUSIBLE NORDIC LIST WOULD HAVE CONTAINED ARE ABSENT,
#     AND THAT IS THE MEASUREMENT EARNING ITS KEEP. *(measured — `dig MX`)*
#     `outlook.fi` and `outlook.no` publish NO MX at all, so nobody receives
#     mail there; `outlook.se` publishes `mailin1.outlook.se`, which is not
#     Microsoft, so it is somebody's own domain; `comhem.se` and `tele2.se` now
#     answer on a corporate Microsoft 365 tenant, i.e. they became company
#     domains when the consumer service closed. Excluding any of those four
#     would make a real company's domain permanently unmatchable, which is the
#     error this list cannot detect in itself.
#
#     ⚠️ FIVE ENTRIES ARE ALSO LIVE COMPANIES' OWN DOMAINS, AND THE MX TEST
#     CANNOT SEE THAT. *(found in review)* The signal that caught all
#     four exclusions above is **"the MX is Microsoft 365, or absent"** — which
#     says "a corporate tenant answers here", not "this is a company". It stays
#     silent on an ISP or a portal whose mail is on its own gateway, and five of
#     the shipped entries are exactly that shape:
#
#         telia.com      mail.telia.com                 its own gateway
#         comcast.com    mxa-*.gslb.pphosted.com        Proofpoint, corporate
#         broadpark.no   mx1/mx2.broadpark.no           its own gateway
#         internet.is    spamvorn.internet.is           its own gateway
#         visir.is       spamvorn.internet.is           the SAME gateway, and
#                                                       visir.is is a live
#                                                       commercial news site
#
#     All five are kept, and it is a trade stated rather than discovered later:
#     consumer mailboxes on each are common, so the domain is not an identifier;
#     the cost is that a bill from Telia, Comcast, Get/Broadpark, Vodafone
#     Iceland or Sýn itself will not match on its domain and must match on
#     phone, business id or name. Declining is the safe direction and it is the
#     direction this module takes everywhere else — the opposite error, leaving
#     a consumer provider OFF the list, is the one that files a bill against a
#     stranger. 🔴 The next person extending this list must not reuse the MX
#     test believing it answers "is this a company".
#
#     🔴 AND THE `dig` IS WRITTEN DOWN NOW, WHICH IT WAS NOT. 22 of these 47
#     rested on an MX lookup that lived only in a terminal. All 47, plus the
#     five rejected, are recorded with their answers and the exact command in
#     `goodahead_invoice_ai_import/docs/nordic-mail-providers-evidence.md`,
#     re-runnable by anybody.
_PUBLIC_EMAIL_DOMAINS = frozenset(
    {
        # (a) shipped since 19.0.3.0.0
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
        # (b) named by free.txt AND on 5+ unrelated partners in a typical book
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
        # (c) Nordic — named by free.txt and still carrying a live MX
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
        # (c) Nordic — confirmed by MX: Microsoft consumer mail
        "hotmail.dk",
        "hotmail.fi",
        "hotmail.se",
        "live.fi",
        # (c) Nordic — confirmed by MX: Finnish operators and portals
        "dnainternet.net",
        "elisanet.fi",
        "kolumbus.fi",
        "saunalahti.fi",
        "suomi24.fi",
        # (c) Nordic — confirmed by MX: Swedish
        "passagen.se",
        "telia.com",
        # (c) Nordic — confirmed by MX: Norwegian
        "broadpark.no",
        "frisurf.no",
        "online.no",
        "start.no",
        # (c) Nordic — confirmed by MX: Danish
        "mail.dk",
        "post.tele.dk",
        "stofanet.dk",
        "youmail.dk",
        # (c) Nordic — confirmed by MX: Icelandic
        "internet.is",
        "simnet.is",
        "visir.is",
    }
)

# ---------------------------------------------------------------------------
# 🔴 THE SECOND FLOOR, AND WHY ONE FLOOR WAS THE WRONG SHAPE (19.0.3.16.0,
# a review finding).
#
# The list above is called "free email providers" and is extended by a rule:
# *named by `willwhite/freemail` AND carried by five or more partners*. That
# rule cannot ever reach an import placeholder, because `freemail` will
# never name one — so an import placeholder is structurally invisible to the
# list that exists to catch this defect.
#
# The property that makes a host unsafe is not "is it a free provider". It is
# **carried by many unrelated parties and by exactly ONE party a bill may be
# filed against**, because that is the arithmetic `_commercial_partner_for`
# does. Checking that property over every host in a contact book (with this
# module's own eligibility rule — `cp.active`, not a `res_company.partner_id`,
# and not `supplier_rank = 0 AND customer_rank > 0`) finds hosts of these
# shapes, and this constant is where they land:
#
#     host              carriers                     bill-eligible   decides
#     emaildomain.com   many unrelated partners      one             🔴 yes
#     gamil.com         several unrelated partners   none
#     gmai.com          several unrelated partners   none
#     gmail.con         several unrelated partners   none
#
# An import placeholder such as `emaildomain.com` shows up as many distinct
# printed addresses of the shape `<digits>@emaildomain.com`, spread over many
# unrelated partners, and as soon as one of them is bill-eligible the domain
# decides a vendor for all of them.
#
# ⚠️ THE THREE MISSPELLINGS ARE HERE FOR A DIFFERENT REASON THAN THEY WERE
# CLEARED FOR. 19.0.3.15.0 recorded all four as *"ambiguous and therefore
# already decline"*. They are not ambiguous: typically nobody bill-eligible
# carries them AT ALL (none, not two or more), which is a far more fragile reason — one `supplier_rank`
# increment on any of those partners flips each of them to deciding. A
# near-miss of a free provider belongs to whoever typed it badly, which is to
# say to nobody.
#
# ⚠️ WHAT IS **NOT** HERE, AND IS A DATA-CLEANING ITEM RATHER THAN A
# SHIPPED RULE. The same sweep can name a web agency's own domain, carried by
# dozens of unrelated partners that are test checkouts placed by the agency's
# own staff (`staff1@`, `staff2+123@`). It is a real company's domain with
# nobody bill-eligible, and if that agency ever does invoice the company then
# the one eligible carrier IS that agency, so the domain would decide
# correctly. It is junk data to clean, not a domain to exclude.
_NEVER_IDENTIFYING_DOMAINS = frozenset(
    {
        # an import placeholder, described above
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

# ---------------------------------------------------------------------------
# 🔴 EXCLUDING A DOMAIN DOES NOT EXCLUDE ITS SUBDOMAINS (found in
# review), because `_host_of` keeps the whole host on purpose — see its docstring
# for why, and that reasoning still stands. The consequence was never written
# down: `naver.com` is excluded and `mail.naver.com` is not, and the 19.0.3.15.0
# source comment went further and asserted that the regional `rr.com` hosts
# "are ambiguous and therefore already decline".
#
# That was checked on ONE member of the family and generalised to all of them.
# A regional host carried by several partners with none eligible declines; one
# carried by several partners with exactly ONE eligible DECIDES — a vendor
# reached through printed addresses most of which belong to unrelated parties.
#
# So a suffix rule, with exactly one entry and a narrow justification: Spectrum
# hands out consumer mailboxes under regional subdomains of `rr.com` and sells
# nobody a company domain there, so every host under it is a consumer mailbox.
# ⚠️ IT MATCHES LABELS, NOT CHARACTERS. A plain `endswith` would swallow
# `bigrr.com` and make a real company permanently unmatchable, which is the
# error `comhem.se` and `tele2.se` were kept OFF the list to avoid.
_PUBLIC_EMAIL_SUFFIXES = frozenset({"rr.com"})

#: Everything an operator may subtract with a leading `-`. The escape hatch has
#: to cover every shipped rule or a rule that is wrong for one database cannot
#: be undone there; and a subtraction checked against only the first floor would
#: log "it was never on the list, the entry does nothing" while silently doing
#: something.
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
        help="Where this row came from, in words — 'AI import INV100001', "
        "'seeded from res.partner.phone'. Free text on purpose: it is read by "
        "a person deciding whether to trust the row, not by code.",
    )
    active = fields.Boolean(default=True)

    # 🔴 `models.Constraint`, NOT `_sql_constraints`. In 19.0 the old attribute
    # is read by `odoo/orm/model_classes.py:162-164`, which LOGS a warning and
    # does nothing else — the index is never created and nothing fails. This
    # module has already been bitten by it once (see the 19.0.1.3.0 block in
    # `__manifest__.py`).
    _partner_kind_value_uniq = models.Constraint(
        "UNIQUE(partner_id, kind, value)",
        "This partner already carries that identifier.",
    )

    # ------------------------------------------------------------------
    # Normalisation — the part the matching actually depends on
    # ------------------------------------------------------------------

    @api.model
    def _normalise_phone(self, value):
        """The LAST TEN DIGITS of a phone number, or None when there are fewer.

        🔴 THIS IS US-SHAPED, AND THAT IS A NAMED ASSUMPTION RATHER THAN A
        HIDDEN ONE. A North American number is ten digits plus an optional `1`
        country code, so the last ten identify it whatever decoration is printed
        around them: `212.555.0111`, `(212) 555-0130`, `+1 212-555-0199` and
        `1-833-555-0131` all reduce correctly. A Finnish or Chinese number does
        NOT survive this — `+358 50 1234567` keeps only its last ten digits and
        loses the country code, so in principle it could collide with a US
        number ending the same way. A US vendor book is American; when
        that stops being true this method is the one place to change, and
        F27's open question 3 is where the decision belongs.

        🔴 A COUNTRY-AWARE E.164 VERSION OF THIS METHOD WAS WRITTEN, MEASURED
        AND WITHDRAWN IN 19.0.3.22.0, AND THE WITHDRAWAL IS NOT A VERDICT ON IT.
        The phone and email cleanup is moving to a separate data-cleaning step, because
        the real problem is dirtier than any normaliser can fix: several numbers
        in one field, contact names typed into phone fields, no distinction
        between a landline and a mobile, and the same for emails. ⚠️ THE KEY AND
        ITS MIGRATION LEAVE TOGETHER OR NEITHER LEAVES. A country-aware writer
        over last-ten-digits rows matches NOTHING AT ALL, and so does the
        reverse; see the 19.0.3.22.0 entry in `CHANGELOG.md`, and
        a sweep over a contact book for the instrument that proves
        which shape this function is in.

        ⚠️ KNOWN LIMITATION, NOT HANDLED HERE: an extension. `212-555-0199 x205`
        yields `5550199205`, which is nonsense. It fails in the safe direction —
        a stored nonsense value matches nothing and the import falls through to
        the next step — so it is recorded rather than guessed at. Stripping
        extensions needs real examples of how this business's vendors print
        them, and there are none yet.

        Only `str` and `int` are accepted. A `float` is rejected because
        `str(2125550199.0)` is `"2125550199.0"`, whose last ten digits are
        `1243191120` — the window shifted by one, silently, into a value that
        looks exactly like a phone number. `bool` is rejected for the reason
        recorded in `ubl_generator.peppol_endpoint_for_party`: `str(True)` is
        `"True"`, and a stringified bool that reaches a matching key invents
        data rather than failing.
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

        ⚠️ THIS IS NOT THE REGISTRABLE DOMAIN AND DOES NOT CLAIM TO BE. Working
        out that `vendor.co.uk` is registrable while `co.uk` is not needs a
        public-suffix list, and there is none in this tree. So the host is kept
        whole, with only `www.` removed: `mail.vendor.com` and `vendor.com` are
        two different identifiers here.

        🔴 SO EXCLUDING A DOMAIN DOES NOT EXCLUDE ITS SUBDOMAINS, AND THAT HAD
        NEVER BEEN WRITTEN DOWN (found in review). `naver.com` is on
        the free-provider list; `mail.naver.com` is not, and nothing here would
        notice. A regional `rr.com` host, for example, could decide a vendor
        while an earlier comment claimed those hosts declined. Where
        that asymmetry is known to bite, `_PUBLIC_EMAIL_SUFFIXES` closes it
        explicitly, per family, with the carriers measured first.

        That OVER-separates — two addresses of
        the same company may fail to match — and over-separating is the safe
        direction. The other one, guessing a suffix, merges unrelated vendors.

        Strips the scheme, any path, query or fragment, a port, a leading
        `www.` and a trailing dot. Returns None when what is left is not
        host-shaped.
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
        """The host an email address, URL or bare domain names — or None.

        🔴 A PUBLIC PROVIDER IS None, NOT A DOMAIN. The rejection lives HERE,
        in the one function every caller goes through, rather than beside each
        caller, because a guard a caller has to remember is a guard somebody
        will forget. `zzexamplevendor@gmail.com` — an ordinary vendor mailbox at a
        public provider — returns None, and therefore is never stored and never
        matched on.
        """
        host = self._host_of(value)
        if not host or self._is_non_identifying_domain(host):
            return None
        return host

    @api.model
    def _is_non_identifying_domain(self, host):
        """Is this host one that identifies nobody — a free provider, a
        placeholder, or a consumer mailbox under a listed suffix?

        ⚠️ THE SUFFIX TEST MATCHES LABELS, NOT CHARACTERS. `nc.rr.com` is under
        `rr.com`; `bigrr.com` and `rr.com.vendor.example` are not. A character
        `endswith` would make real companies permanently unmatchable, which is
        the opposite error and the one this list cannot detect in itself.
        """
        if not host:
            return False
        if host in self._non_identifying_domains():
            return True
        return any(
            host == suffix or host.endswith("." + suffix)
            for suffix in self._non_identifying_suffixes()
        )

    # ------------------------------------------------------------------
    # The domains that identify nobody, and the one way the list may be edited
    # ------------------------------------------------------------------

    @api.model
    @ormcache(cache="stable")
    def _non_identifying_domains(self):
        """Every host that identifies nobody HERE: the two shipped floors, plus
        whatever this database's operator has added, minus whatever they have
        explicitly subtracted.

        🔴 IT WAS `_public_email_domains` UNTIL 19.0.3.16.0 AND THE NAME WAS A
        DEFECT, NOT A LABEL (found in review). A list called "free
        email providers", extended by a rule that reads a published list of free
        email providers, cannot see an import placeholder carried by 210
        unrelated parties and exactly one vendor — so it did not, and the
        placeholder decided a vendor on this database the whole time. The seam
        is "does this host identify a company", and it is named that now.

        🔴 WHY THIS IS NOT THE `ir.config_parameter` the dual-core design ASKED FOR, AND THE
        DIFFERENCE IS NOT A DETAIL. §8 says *"move to an `ir.config_parameter`
        (comma-separated) seeded from the constant"*. Seeding means writing the
        shipped domains INTO the parameter once, at install, and reading only
        the parameter afterwards. That is the eleventh instance of this file's
        own standing pattern — **a claim true in one direction only** — and it
        fails in both of the directions that matter:

        * **downwards.** The exclusion list becomes a thing an operator can
          empty. Select-all, delete, Save: no error, no warning, nothing in the
          log, and `gmail.com` is an identifier again. `gmail.com`
          is carried by **thousands** of distinct commercial partners on a
          large database, so the next gmail invoice is filed against whichever of them
          happens to be the only bill-eligible one — or, if none is, the feature
          simply stops working and nobody is told. The help text §8 asks for
          documents this; it does not prevent it.
        * **upwards.** A seeded parameter is a SNAPSHOT. The day this module
          ships a new provider — which is literally what step 8b does, 72 of
          them — every database that already has the parameter keeps the old
          list, for ever, because seeding only happens when the value is
          absent. The upgrade that adds a domain would reach a fresh install and
          silently miss every existing one. That is the bug shape this project
          keeps writing down and keeps re-finding.

        So the constant is a FLOOR and the parameter is a DELTA. Clearing the
        parameter restores the shipped list rather than emptying it, which makes
        the catastrophic edit unreachable instead of merely documented; and a
        new shipped domain reaches every installation on upgrade, because
        nothing was ever copied out of the code.

        🔴 AND IT IS SYSTEM-WIDE, NOT PER COMPANY, BECAUSE THE TABLE IT GUARDS
        IS. `goodahead.partner.identifier` has no `company_id` and
        `_commercial_partner_for` does not scope by company — the contact book
        is shared, so the identifier rows derived from it are too. A per-company
        list would mean company A storing a `suomi24.fi` row that company B's
        lookup then decides on, i.e. one shared table governed by two different
        rules, which is worse than either rule. The matcher IS company-aware
        where a company-aware fact is involved (`_ai_posted_bill_counts` counts
        the bill's company's bills); "is this host a free provider" is not one
        of those facts. It is also deliberately not a `res.company` field for
        the same reason: a field per company invites exactly the divergence the
        shared table cannot express.

        Cached on the registry under `stable`, which `ir.config_parameter`
        clears on every create, write and unlink — so an edit takes effect at
        once, and `_seed_from_partners` does not re-parse the setting once per
        partner.
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
    @ormcache(cache="stable")
    def _non_identifying_suffixes(self):
        """The shipped suffixes, minus whatever the operator subtracted.

        A separate cached method rather than a second return value, so that
        `_is_non_identifying_domain` reads as two questions and either can be
        asserted on its own. An operator cannot ADD a suffix: a suffix rule
        covers every host under it for ever, which is too large a thing to hand
        to a free-text field — the setting adds exact hosts only.

        ⚠️ `log=False`, because this is the SECOND parse of the same setting in
        one cache miss and a warning an operator sees twice is a warning they
        learn to scroll past.
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

        One entry per comma, semicolon, space or newline; `#` starts a comment
        to end of line; a leading `-` subtracts a SHIPPED domain instead of
        adding one.

        🔴 SUBTRACTION HAS TO BE TYPED AND IT IS NEVER SILENT. Taking a provider
        off the list is the single edit that can turn a contact book into one
        confident wrong vendor, so it costs a character that cannot be produced
        by emptying a field or by pasting a list, and every subtraction that
        actually bites is logged by name. Naming a domain that was never
        shipped is a no-op and says so — otherwise `-ourvendor.com` would read
        like it did something.

        🔴 AN UNREADABLE ENTRY IS DROPPED AND REPORTED, NEVER SILENTLY
        SWALLOWED, AND IT NEVER EMPTIES THE LIST. This project's recurring
        defect is a parse that fails in the direction that looks like success;
        here the direction that looks like success is "the setting is empty, so
        nothing is excluded", which is the catastrophe. So a junk entry costs
        itself and nothing else.
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

        # 🔴 BOTH FLOORS AND THE SUFFIXES, NOT JUST THE FREE PROVIDERS. While
        # this read `removed - _PUBLIC_EMAIL_DOMAINS`, a `-emaildomain.com`
        # logged *"it was never on the list; the entry does nothing"* and then
        # did something. A warning that fires for the wrong set is worse than
        # none, because it is read as a clean bill of health.
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

    # ------------------------------------------------------------------
    # Writing rows
    # ------------------------------------------------------------------

    @api.model
    def _add_pairs(self, pairs_by_partner, source):
        """ADD the ``(kind, value)`` pairs that are not already stored.

        🔴 ADD, NEVER REPLACE, AND NEVER DELETE — the rule the whole table
        exists for, stated in this module's docstring. It lives in ONE method
        because there are now two callers that must not drift apart:
        `res.partner._ai_add_contact_identifiers` (the contact book keeping
        itself current) and `account.move._ai_apply_vendor_data` (an invoice
        teaching the ledger a new way the vendor is reached). A second copy of
        "work out what is missing, then create it" is how one of them quietly
        starts replacing instead of adding.

        ``pairs_by_partner`` maps a partner id to an iterable of pairs.
        Returns the list of ``(partner_id, kind, value)`` actually created, so
        a caller can say what was learned rather than guess.

        `sudo()` because the rows are a derivation the system maintains, not
        user data: a salesperson editing a customer's phone must not fail on an
        ACL for a table they have never heard of.
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

        # 🔴 CHECK-THEN-INSERT RACES, AND A BATCH OF BILLS FROM ONE VENDOR IS
        # EXACTLY THE RACE. Two import jobs for the same vendor run in parallel;
        # both search above, neither sees the other's uncommitted row, both
        # insert the same phone. The loser got `UniqueViolation`, and because it
        # is raised inside the import's post-processing savepoint, EVERYTHING
        # post-processing had done to the bill was rolled back with it and the
        # import was marked failed (a batch of bills from one vendor, two of
        # them imported a second apart, is enough).
        #
        # The row the loser wanted now exists — committed by the other job — so
        # skipping it IS the correct end state. Try the whole batch once (the
        # common case, one savepoint); on a conflict, insert row by row, each in
        # its own savepoint, and leave out the ones somebody else stored first.
        #
        # ⚠️ NOT `INSERT … ON CONFLICT DO NOTHING`. Odoo's cursors are
        # REPEATABLE READ, and there a conflict with a row the snapshot cannot
        # see raises a SERIALIZATION failure instead of doing nothing — queue_job
        # retries that, and a retry of an AI import repeats the paid extraction.
        # A plain INSERT waits for the other transaction and raises a unique
        # violation, which a savepoint can absorb.
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

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    @api.model
    def _commercial_partner_for(self, kind, value):
        """The ONE active commercial partner this identifier resolves to.

        🔴 A MATCH THAT IS NOT UNIQUE IS NOT A MATCH. If a normalised phone or
        domain resolves to more than one commercial partner, this returns an
        empty recordset and the caller falls through to the next step. An
        ambiguous match is worse than none: it files the bill against one of
        several possible vendors, with confidence, and nothing downstream
        disagrees. F27 states the rule in those words — a shared domain
        "becomes a candidate, never a decision".

        Rows on child contacts resolve to the contact's `commercial_partner_id`,
        because that is the party a bill is filed against.

        Archived partners are excluded. An archived record is one somebody
        retired on purpose, so it neither wins the match nor blocks it — it is
        offered as a CANDIDATE instead, by
        `account.move._ai_vendor_candidates`, which marks it as archived.
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
        """Remove the partners a VENDOR BILL can never legitimately be filed
        against. Applied to the deciding path only.

        🔴 THIS EXISTS BECAUSE THE IDENTIFIERS ARE SEEDED FROM THE WHOLE
        CONTACT BOOK, NOT FROM THE VENDORS. That is deliberate — a brand-new
        vendor has `supplier_rank = 0` and seeding only vendors would make the
        feature useless for exactly the bills it is meant to catch — but it
        means a uniquely-resolving phone or domain is far more likely to belong
        to a customer than to a vendor. In a typical contact book, among the keys
        that resolve to exactly one partner: **non-vendor keys outnumber vendor
        keys by a wide margin, for phones and for domains alike.** The uniqueness guard is weakest
        precisely where the risk is highest — a switchboard shared by five
        vendors declines safely, while a number held by one customer decides
        confidently and wrongly.

        🔴 THE CASE THAT FORCED THIS. **Both of
        our own companies' partners carry a phone, an email and a website**:

            Alpha LLC  → partner 1     (212) 555-0101  orders@alpha.example.test
            Beta Inc. → partner <id> (212) 555-0102  sales@beta.example.test

        Every invoice prints the BUYER as well as the seller, and the only thing
        standing between the two is prompt text this module's own manifest says
        is unverified. One misread and a vendor bill is filed against
        the buying company itself, or against Beta — which in this two-company
        estate is the sister company the whole intercompany flow runs through.

        Two exclusions, both narrow:

        1. **A `res.company.partner_id`.** We are never a vendor on our own
           bill. This is not a heuristic.
        2. **A partner that is only ever a customer** — `supplier_rank = 0` and
           `customer_rank > 0`. ⚠️ A genuinely new vendor has BOTH at zero, so
           it is still matched; this drops only records the business has
           repeatedly SOLD to and never bought from. Beta fails this one
           as well (a high `customer_rank`), so the intercompany case is
           excluded twice over.

        Candidates are NOT filtered this way beyond rule 1 — an offer a human
        reads is not a decision, and a customer who starts invoicing us is a
        real thing. See `_partners_for`.
        """
        if not partners:
            return partners
        # 🔴 RULE 1 IS NO LONGER WRITTEN OUT HERE. It moved to
        # `res.partner._ai_drop_impossible_vendors` in 19.0.3.23.0, because
        # CORE's own resolution steps bypass this method entirely and needed the
        # same rule — see that method's docstring and bill A. One body
        # per rule; this is the caller that also wants the customer-only rule.
        #
        # ⚠️ TWO THINGS CHANGED HERE AND BOTH ARE DELIBERATE. (1) The hoisted
        # rule is now scoped to `self.env.company` instead of refusing EVERY
        # company's partner, because the agreed rule is that another company's
        # partner is a legitimate vendor. (2) No `company` is passed, because
        # this method is reached from the identifier steps, which are not
        # company-scoped — unchanged since 19.0.3.5.0, deliberately, and
        # changing that is a separate decision with its own blast radius.
        #
        # 🔴 BETA IS STILL REFUSED HERE, BY THE OTHER RULE, AND THAT IS A
        # PRE-EXISTING DECISION I HAVE NOT TOUCHED. The comment above has said
        # since 19.0.3.0.0 that "Beta fails this one as well
        # (a high `customer_rank`), so the intercompany case is excluded twice
        # over" — i.e. this method's customer-only rule excludes the sister
        # company on purpose. The later rule says another
        # company's partner should be ALLOWED as a vendor. Those two statements
        # disagree, in code that predates this branch, on this path only. It is
        # raised rather than resolved here.
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
        # Our OWN companies are never offered either — a bill filed against
        # ourselves is not a thing a person should be one click away from, and
        # both company partners carry a phone and a domain (see
        # `_drop_partners_that_are_never_a_vendor`). A customer IS still
        # offered: an offer a human reads is not a decision, and a customer who
        # starts invoicing us is ordinary.
        return self.env["res.partner"]._ai_drop_impossible_vendors(partners)

    # ------------------------------------------------------------------
    # The seed
    # ------------------------------------------------------------------

    @api.model
    def _seed_from_partners(self, batch_size=5000, source="seeded from res.partner"):
        """Derive identifiers from every existing partner's phone/email/website.

        🔴 WITHOUT THIS THE FEATURE SHIPS MEASURABLY DEAD. The existing vendors with
        `supplier_rank > 0` carry no identifier rows on the day this module is
        upgraded, so every phone and domain step would decline and the import
        would go on matching exactly as badly as it did on the sample set — with new code
        in place and a green test suite, which is the worst possible shape for a
        defect.

        It is called from BOTH `post_init_hook` and
        `migrations/19.0.3.0.0/post-seed-partner-identifiers.py`, deliberately,
        because neither alone covers both cases: a migration script does not run
        on a fresh install, and `post_init_hook` does not run on an upgrade.
        A large database has over a hundred thousand partners either way. Running both is harmless
        — `ON CONFLICT DO NOTHING` against the uniqueness constraint makes the
        seed idempotent, which is also what lets it be re-run by hand.

        Raw SQL rather than `create()`: this writes tens of thousands of rows
        that are a pure derivation of columns already present, with no defaults,
        no onchanges and nothing to compute. The ORM path measured in minutes.
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
        written = 0
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

        # 🔴 COUNT THE ROWS, DO NOT ASK `cr.rowcount`. `execute_values` splits a
        # batch into `page_size` statements and psycopg2 leaves `rowcount` at
        # the LAST one, so `written += cr.rowcount` counted 1,000 per 5,000-row
        # batch. MEASURED on a virgin table: it reported "seeded 30500 … (122000
        # were already present)" while all 152,500 rows had in fact been
        # inserted — 30 × 1000 + 500, exactly. Every row was correct; only the
        # number anybody reads was wrong, and it was wrong in the direction that
        # reads like a harmless partial run. That is this project's signature
        # defect shape, so the count is now taken from the table itself.
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
