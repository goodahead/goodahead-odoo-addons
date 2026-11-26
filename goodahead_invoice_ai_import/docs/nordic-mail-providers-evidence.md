# The MX evidence behind the Nordic half of `_PUBLIC_EMAIL_DOMAINS`

**Why this file exists.** 19.0.3.15.0 added 47 Nordic entries to the shipped
free-provider list. 25 of them cite `willwhite/freemail`'s `data/free.txt` *plus*
a live MX record; **22 cite the MX alone** — and that MX lookup lived in a
terminal. It was not in the repository, not dated in any machine-readable way,
and not re-runnable from anything committed: *a measurement nobody can repeat
is a sentence, not a measurement.* (found in review)

**Re-run it.** The dev scripts that once lived in `tools/` are no longer shipped
with the module. Each row below is one DNS query and can be repeated by hand
against the hosts in the shipped constant:

```bash
dig +short MX kolumbus.fi               # one host at a time, from the shipped list
```

---

## 🔴 What the MX test CAN and CANNOT see

Every one of the four exclusions the 19.0.3.15.0 commit is proud of was caught
by one signal: **the MX is Microsoft 365, or there is no MX at all.** That signal
says *"a corporate tenant answers here"*. It does **not** say *"this is a
company"*, and it is silent on an ISP or a portal whose mail is on its own
gateway.

Five shipped entries are exactly that shape — live companies whose consumer mail
is on their own infrastructure. Two were disclosed when they were added; three
were not, and are disclosed here and in the source:

| shipped host | MX | shape | disclosed in 19.0.3.15.0? |
|---|---|---|---|
| `telia.com` | `mail.telia.com` | its own gateway | ✅ yes |
| `comcast.com` | `mxa-*.gslb.pphosted.com` | Proofpoint, corporate | ✅ yes |
| `broadpark.no` | `mx1.broadpark.no`, `mx2.broadpark.no` | its own gateway | ❌ no |
| `internet.is` | `spamvorn.internet.is` | its own gateway | ❌ no |
| `visir.is` | `spamvorn.internet.is` | the **same** gateway as `internet.is`; `visir.is` is also a live commercial news site (Sýn hf.) | ❌ no |

**All five are kept, and the trade is stated rather than discovered later.**
Consumer mailboxes on each are common, so the domain does not identify a
company; the cost is that a bill from Telia, Comcast, Get/Broadpark, Vodafone
Iceland or Sýn itself will not match on its domain and must match on phone,
business id or name. Declining is the safe direction. The opposite error —
leaving a consumer provider OFF the list — is the one that files a bill against
a stranger, and it is the error `emaildomain.com` was.

🔴 **The next person extending this list must not reuse the MX test believing it
answers "is this a company".** It answers "is the mail on a corporate Microsoft
365 tenant", which is a different question with the same-looking answer.

---

## The rejected five, and why

*(measured 2026-10-10)*

| host | MX | why it is NOT on the list |
|---|---|---|
| `outlook.fi` | *(none)* | publishes no MX at all — nobody receives mail there |
| `outlook.no` | *(none)* | same |
| `outlook.se` | `mailin1.outlook.se`, `mailin2.outlook.se` | not Microsoft, so it is somebody's own domain |
| `comhem.se` | `comhem-se.mail.protection.outlook.com` | a corporate Microsoft 365 tenant — it became a company domain when the consumer service closed |
| `tele2.se` | `tele2-se.mail.protection.outlook.com` | same |

Excluding any of these would make a real company's domain permanently
unmatchable, which is the error this list cannot detect in itself.
Pinned by `test_the_domains_measured_as_not_live_were_NOT_added`.

---

## The 47 shipped entries, measured 2026-10-10

`dig +short MX <host>`, answers sorted, from this machine on 2026-10-10.

### Named by `free.txt` under a Nordic ccTLD, and still carrying a live MX (25)

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

### Confirmed by MX alone — the 22 that had no committed evidence

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
| `telia.com` | `1 mail.telia.com.` | ⚠️ **also a live company** — see above |
| `broadpark.no` | `10 mx1/mx2.broadpark.no.` | ⚠️ **also a live company** — see above |
| `frisurf.no` | `10 mx1c60.megamailservers.eu.` + three | Norwegian ISP, hosted |
| `online.no` | `10 mx1c60.megamailservers.eu.` + three | Telenor consumer, same host |
| `start.no` | `10 mx.dka.mailcore.net.` | Norwegian portal, hosted |
| `mail.dk` | `10 fpo9.mail.dk.` | TDC consumer mail |
| `post.tele.dk` | `10 fpo9.mail.dk.` | TDC consumer mail, same gateway |
| `stofanet.dk` | `10 mx-lb-1.stofanet.dk.` | Stofa, Danish ISP |
| `youmail.dk` | `10 fpo9.mail.dk.` | TDC consumer mail, same gateway |
| `internet.is` | `10 spamvorn.internet.is.` | ⚠️ **also a live company** — see above |
| `simnet.is` | `10 mx3–mx6.vist.is.` | Síminn consumer mail, hosted |
| `visir.is` | `10 spamvorn.internet.is.` | ⚠️ **also a live company** — see above |

---

## What this file does **not** evidence

**Membership can never be evidenced by a bench.** The dual-core design says the Nordic
half is "unevidenced by execution" and that is accurate but under-states which
half: `test_the_nordic_providers_ship_in_the_constant_not_in_a_setting` exercises
the *mechanism* on both benches, and no bench can ever show that
`kolumbus.fi` is a free provider — a reference ledger carries no consumer addresses at
all. Only the sources can, and until this file they were half-recorded.
