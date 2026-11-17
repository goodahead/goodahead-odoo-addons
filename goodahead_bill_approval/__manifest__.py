# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
{
    'name': 'Bills and Credit Notes: Two-Step Approval',
    'summary': 'A vendor bill or customer credit note is reviewed, then approved, before it '
               'can be posted. Two per-user approval levels, granted as checkboxes on the '
               'user form, with an optional chosen approver and substitute.',
    'description': """
Two-step approval for vendor bills
==================================

A vendor bill or vendor credit note needs two approvals before it can be posted:

**Step 1 — Review.** The figures are right: vendor, dates, lines, accounts. Given by a user
holding *Bills: may review (step 1)*.

**Step 2 — Approve.** The company agrees to pay. Given by a user holding
*Bills: may approve (step 2)*.

The check is enforced when the move is posted, so it also covers automatic posting (upload,
e-invoicing, OCR or AI import). Because both approvals happen before posting, an unapproved bill
has no journal entry: it does not affect the vendor balance or aged payables, and refusing it
needs no reversal. Core already refuses payments against unposted moves.

A refusal clears both approvals, requires a written reason, logs it in the chatter and returns
the bill to the review queue.

Three menu items under *Vendors* open the queues: bills to review, bills to approve, and approved
bills waiting to be posted.

**Notifications.** When step 2 is given, the reviewer gets an activity on the bill — *Post this
bill, then pay it* — due today. It is marked done when the bill is posted and removed if the bill
is refused. A systray counter shows each user the documents they can act on.

**Customer credit notes** follow the same two steps with the same levels, in their own queues
under *Customers*. Customer invoices are not affected. Exempt from approval, with a chatter note:
a customer credit note created by *Reverse and create invoice* that fully cancels a posted
invoice, and a credit note recording a completed card refund. Vendor credit notes have no
exemption.

**Chosen approver and substitute.** A document can name the one person who gives step 2. Only
they, or the substitute set in their preferences, may then approve it; the form shows "approved
by ... on behalf of ..." when the substitute did. The chosen approver gets an activity while the
document waits, and the *My Approvals* filter lists it.
""",
    'version': '19.0.1.5.0',
    'category': 'Accounting/Accounting',
    'author': 'Goodahead Oy',
    'website': 'https://www.goodahead.com/',
    'license': 'LGPL-3',
    'depends': ['account'],
    'data': [
        'security/res_groups.xml',
        'security/ir.model.access.csv',
        'data/mail_activity_type.xml',
        'wizard/bill_approval_refusal_views.xml',
        'wizard/bill_approval_review_views.xml',
        'views/account_move_views.xml',
        'views/res_users_views.xml',
    ],
    # A duplicate OWL template name silently breaks the whole web.assets_web bundle, so keep
    # template names in these files unique.
    'assets': {
        'web.assets_backend': [
            'goodahead_bill_approval/static/src/js/bill_approval_menu.js',
            'goodahead_bill_approval/static/src/xml/bill_approval_menu.xml',
        ],
    },
    'installable': True,
    'auto_install': False,
}
