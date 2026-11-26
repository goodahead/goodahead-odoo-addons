# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The APIX listing window, as pure date arithmetic. No Odoo
import.

APIX lists invoices of the current and the previous calendar month. A package
whose document date falls in month M is listed through the end of month M+1
and leaves the listing when month M+2 starts. The Administrator is warned
``LEAD_DAYS`` days before that (spec §6 "Listing window": reported ≥ 7 days
before the package leaves the listing).
"""

from datetime import timedelta

#: Days of warning before a package leaves the listing window.
LEAD_DAYS = 7


def _first_of_month_after(day, months):
    month_index = day.year * 12 + (day.month - 1) + months
    return day.replace(year=month_index // 12, month=month_index % 12 + 1, day=1)


def window_end(document_date):
    """The first day the package is no longer listed: the first day of the
    month two months after the document date's month."""
    return _first_of_month_after(document_date, 2)


def warn_from(document_date, lead_days=LEAD_DAYS):
    """The first day the ageing warning is due."""
    return window_end(document_date) - timedelta(days=lead_days)


def is_ageing(document_date, today, lead_days=LEAD_DAYS):
    """Whether a package dated ``document_date`` is within ``lead_days`` of
    leaving the listing (or has left it already) on ``today``."""
    if not document_date:
        return False
    return today >= warn_from(document_date, lead_days)


def days_left(document_date, today):
    """Days until the package leaves the listing; 0 or less once it has."""
    return (window_end(document_date) - today).days


def age_days(document_date, today):
    return (today - document_date).days
