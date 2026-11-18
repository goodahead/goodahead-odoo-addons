# Sources for the Nordic entries in `_PUBLIC_EMAIL_DOMAINS`

The shipped list of public mail-provider domains (`_PUBLIC_EMAIL_DOMAINS`) includes
47 Nordic entries. A domain on this list never identifies a vendor, because many
unrelated people and businesses use it. This page records how the Nordic entries
were chosen, so the list can be reviewed and extended on the same basis.

## Method

Two sources were combined:

1. **The `willwhite/freemail` list** (`data/free.txt`), a public list of free email
   providers. Entries under a Nordic ccTLD (`.fi`, `.se`, `.no`, `.dk`, `.is`) were
   taken as candidates.
2. **A live MX lookup** (`dig +short MX <host>`) for every candidate and for
   well-known Nordic ISP and portal mail domains not on that list. A domain was
   kept only if it still receives mail and its MX points at a consumer mail service
   (Microsoft consumer Outlook, Yahoo, an ISP's or portal's own mail gateway, or a
   hosted consumer mail platform).

25 entries are backed by both sources; 22 by the MX lookup alone. The MX snapshot
below was taken in October 2026; re-run the lookups before relying on any single
entry.

---

## What the MX test can and cannot show

The decisive signal for rejecting a candidate was usually "the MX is a corporate
Microsoft 365 tenant, or there is no MX at all". That shows a corporate tenant
answers for the domain. It does not show that a domain is *not* a company, and it
says nothing about an ISP or portal that runs its own mail gateway.

Five shipped entries (four Nordic, plus the US `comcast.com`) are domains of live
companies whose consumer mail runs on their own infrastructure:

| shipped host | MX | shape |
|---|---|---|
| `telia.com` | `mail.telia.com` | own gateway |
| `comcast.com` | `mxa-*.gslb.pphosted.com` | Proofpoint, corporate |
| `broadpark.no` | `mx1.broadpark.no`, `mx2.broadpark.no` | own gateway |
| `internet.is` | `spamvorn.internet.is` | own gateway |
| `visir.is` | `spamvorn.internet.is` | same gateway as `internet.is`; also a commercial news site |

All five are kept. Consumer mailboxes on them are common, so the domain does not
identify a single company. The trade-off is that a bill from one of these companies
will not match on its email domain and must match on phone, business id or name.
That is the safe direction: leaving a consumer provider off the list is the error
that files a bill against an unrelated contact.

When extending the list, do not treat the MX test as answering "is this a company".
It answers "is the mail on a corporate Microsoft 365 tenant", which is a different
question.

---

## Rejected candidates

| host | MX | why it is not on the list |
|---|---|---|
| `outlook.fi` | *(none)* | publishes no MX; nobody receives mail there |
| `outlook.no` | *(none)* | same |
| `outlook.se` | `mailin1.outlook.se`, `mailin2.outlook.se` | not Microsoft, so it is someone's own domain |
| `comhem.se` | `comhem-se.mail.protection.outlook.com` | corporate Microsoft 365 tenant; the consumer service has closed |
| `tele2.se` | `tele2-se.mail.protection.outlook.com` | same |

Adding any of these would make a real company's domain permanently unmatchable.
Pinned by `test_domains_without_live_mail_were_not_added`.

---

## The 47 shipped entries (MX snapshot, October 2026)

### In `free.txt` under a Nordic ccTLD, with a live MX (25)

| host | MX |
|---|---|
| `alskens.dk` | `10 dothilde.alskens.dk.` |
| `bluemail.dk` | `10 banana.beta-inter.net.` |
| `e-mail.dk` | `10 mx.dka.mailcore.net.` |
| `fastmail.se` | `10 eu1-smtp.messagingengine.com.` · `20 eu2-smtp.messagingengine.com.` |
| `forum.dk` | `10 forummail.forum.dk.` |
| `home.se` | `10 mx1/mx2/mx3.spray.mail2world.com.` |
| `hotmail.no` | `10 eur.olc.protection.outlook.com.` |
| `iobox.fi` | `10 mx01.iobox.com.` |
| `jippii.fi` | `1 mx-stp.mail.saunalahti.fi.` · `1 mx-tnl.mail.saunalahti.fi.` |
| `jubiimail.dk` | `10 mx.dka.mailcore.net.` |
| `live.dk` | `10 eur.olc.protection.outlook.com.` |
| `live.no` | `10 eur.olc.protection.outlook.com.` |
| `live.se` | `10 eur.olc.protection.outlook.com.` |
| `luukku.com` | `10 mx.dka.mailcore.net.` |
| `mailme.dk` | `10 mx1/mx2/mx3.mailme.dk.` |
| `netby.dk` | `10 webhotel20.webhosting.dk.` |
| `outlook.dk` | `10 eur.olc.protection.outlook.com.` |
| `sci.fi` | `1 mx-stp.mail.saunalahti.fi.` · `1 mx-tnl.mail.saunalahti.fi.` |
| `sol.dk` | `10 mx.dka.mailcore.net.` |
| `spray.no` | `1 ASPMX.L.GOOGLE.COM.` + four ALT records |
| `spray.se` | `10 mx1/mx2/mx3.spray.mail2world.com.` |
| `yahoo.dk` | `10 mx-eu.mail.am0.yahoodns.net.` |
| `yahoo.fi` | `10 mx-eu.mail.am0.yahoodns.net.` |
| `yahoo.no` | `10 mx-eu.mail.am0.yahoodns.net.` |
| `yahoo.se` | `10 mx-eu.mail.am0.yahoodns.net.` |

### Confirmed by MX lookup alone (22)

| host | MX | reading |
|---|---|---|
| `hotmail.dk` | `10 eur.olc.protection.outlook.com.` | Microsoft consumer mail |
| `hotmail.fi` | `10 eur.olc.protection.outlook.com.` | Microsoft consumer mail |
| `hotmail.se` | `10 eur.olc.protection.outlook.com.` | Microsoft consumer mail |
| `live.fi` | `10 eur.olc.protection.outlook.com.` | Microsoft consumer mail |
| `dnainternet.net` | `10 mx.dnamail.fi.` | DNA, Finnish operator |
| `elisanet.fi` | `1 mx-stp/mx-tnl.mail.saunalahti.fi.` | Elisa, on the Saunalahti gateway |
| `kolumbus.fi` | `1 mx-stp/mx-tnl.mail.saunalahti.fi.` | same gateway |
| `saunalahti.fi` | `1 mx-stp/mx-tnl.mail.saunalahti.fi.` | the gateway itself |
| `suomi24.fi` | `10..50 mx1–mx5.suomi24.heinlein-hosting.de.` | portal webmail, hosted |
| `passagen.se` | `10 mx1/mx2/mx3.spray.mail2world.com.` | Swedish portal, Spray's gateway |
| `telia.com` | `1 mail.telia.com.` | also a live company (see above) |
| `broadpark.no` | `10 mx1/mx2.broadpark.no.` | also a live company (see above) |
| `frisurf.no` | `10 mx1c60.megamailservers.eu.` + three | Norwegian ISP, hosted |
| `online.no` | `10 mx1c60.megamailservers.eu.` + three | Telenor consumer, same host |
| `start.no` | `10 mx.dka.mailcore.net.` | Norwegian portal, hosted |
| `mail.dk` | `10 fpo9.mail.dk.` | TDC consumer mail |
| `post.tele.dk` | `10 fpo9.mail.dk.` | TDC consumer mail, same gateway |
| `stofanet.dk` | `10 mx-lb-1.stofanet.dk.` | Stofa, Danish ISP |
| `youmail.dk` | `10 fpo9.mail.dk.` | TDC consumer mail, same gateway |
| `internet.is` | `10 spamvorn.internet.is.` | also a live company (see above) |
| `simnet.is` | `10 mx3–mx6.vist.is.` | Síminn consumer mail, hosted |
| `visir.is` | `10 spamvorn.internet.is.` | also a live company (see above) |

---

## Limits

Tests verify the mechanism (the Nordic entries ship in the constant and are
honoured by the matcher). They cannot verify membership: whether a domain is a
consumer provider can only be established from sources like the ones above.
