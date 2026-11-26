# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Pure helpers for one APIX listing (ListInvoiceZIPs / list2). No Odoo import.

Source of truth: `docs/reference/apix/rest-api-for-external-usage.html`,
"ListInvoiceZIPs" and "List2 StorageStatus/Transferstatus explanation".
"""

from datetime import date

#: "Information for up to 500 invoices are returned, those already marked to
#: be received are dropped first if more than 500 are found." A listing of
#: this size may hide packages.
LISTING_CAP = 500

#: StorageStatus values (list2).
STORAGE_NEW = "NEW"  # still being created, do not fetch
STORAGE_UNRECEIVED = "UNRECEIVED"  # ready to be fetched
STORAGE_RECEIVED = "RECEIVED"  # fetched (marked received) at least once
STORAGE_DELETED = "DELETED"  # scheduled to be removed

#: 🔴 Never stored in `apix_listing_data`: the StorageKey is the per-package
#: download secret and lives only in the admin-only `apix_storage_key` field.
SECRET_KEYS = frozenset({"StorageKey"})


def parse_apix_date(value):
    """`CCYYMMDD` (list2 dates) -> `date`, or None when absent or malformed."""
    value = (value or "").strip()
    if len(value) != 8 or not value.isdigit():
        return None
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    except ValueError:
        return None


def public_listing_data(group):
    """The listing group without its secrets, for the inbox columns."""
    return {key: value for key, value in group.items() if key not in SECRET_KEYS}


def packages_by_storage_id(groups):
    """`{storage_id: group}` in listing order. Groups without a StorageID are
    dropped; a storage id listed twice keeps its last group (one record per
    storage id, ADR-0006)."""
    packages = {}
    for group in groups or ():
        storage_id = (group.get("StorageID") or "").strip()
        if not storage_id:
            continue
        packages[storage_id] = group
    return packages


def storage_status(group):
    return (group.get("StorageStatus") or "").strip().upper()


def is_capped(groups):
    return len(groups or ()) >= LISTING_CAP
