# -*- coding: utf-8 -*-

from datetime import datetime, time

from odoo import fields, _

from .category_utils import is_bookable_product

# Bookings in these states consume day-tour capacity when validating saves.
DAY_TOUR_ACTIVE_BOOKING_STATUSES = ("initial", "confirm", "allot")

# Only confirmed bookings appear in dashboard availability counts.
CONFIRMED_BOOKING_STATUSES = ("confirm", "allot")


def is_day_long_tour_product(product):
    return bool(
        product
        and product.product_tmpl_id.is_day_long_tour
        and is_bookable_product(product)
        and not product.is_room_type
    )


def day_tour_line_guest_count(line):
    """Total guests on a folio line (for dashboard booked counts)."""
    return (
        (line.adult_count or 0)
        + (line.child_count or 0)
        + (line.infant_count or 0)
        + (line.driver_count or 0)
    )


def day_tour_day_bounds(tour_date):
    """Start/end datetimes covering a calendar day (for search domains)."""
    return (
        datetime.combine(tour_date, time.min),
        datetime.combine(tour_date, time.max),
    )


def day_tour_line_calendar_date(line):
    """Calendar day for a day-long tour line (user/timezone aware)."""
    if line.check_in:
        return fields.Datetime.context_timestamp(line, line.check_in).date()
    if line.booking_id and line.booking_id.check_in:
        return fields.Datetime.context_timestamp(
            line.booking_id, line.booking_id.check_in
        ).date()
    return False


def day_tour_default_check_in_out(booking):
    """Default same-day window from booking check-in in user timezone."""
    if not booking or not booking.check_in:
        return False, False
    tour_date = fields.Datetime.context_timestamp(
        booking, booking.check_in
    ).date()
    return day_tour_day_bounds(tour_date)


def day_tour_same_day_window(line):
    """Same-calendar-day check-in/out for a day-long tour line."""
    tour_date = day_tour_line_calendar_date(line)
    if not tour_date and line.booking_id:
        return day_tour_default_check_in_out(line.booking_id)
    if not tour_date:
        return False, False
    return day_tour_day_bounds(tour_date)


def stay_spans_multiple_days(check_in, check_out, record=None):
    """True when check-out is on a later calendar day than check-in."""
    if not check_in or not check_out:
        return False
    if record is not None:
        check_in_day = fields.Datetime.context_timestamp(record, check_in).date()
        check_out_day = fields.Datetime.context_timestamp(record, check_out).date()
        return check_out_day > check_in_day
    return fields.Date.to_date(check_out) > fields.Date.to_date(check_in)


def stay_is_strict_subset(check_in, check_out, parent_check_in, parent_check_out, record=None):
    """True when the child window is narrower than the parent stay."""
    if not all([check_in, check_out, parent_check_in, parent_check_out]):
        return False
    if record is not None:
        start = fields.Datetime.context_timestamp(record, check_in).date()
        end = fields.Datetime.context_timestamp(record, check_out).date()
        parent_start = fields.Datetime.context_timestamp(
            record, parent_check_in
        ).date()
        parent_end = fields.Datetime.context_timestamp(
            record, parent_check_out
        ).date()
    else:
        start = fields.Date.to_date(check_in)
        end = fields.Date.to_date(check_out)
        parent_start = fields.Date.to_date(parent_check_in)
        parent_end = fields.Date.to_date(parent_check_out)
    if start < parent_start or end > parent_end:
        return False
    return start > parent_start or end < parent_end


def day_tour_same_calendar_day(check_in, check_out, record=None):
    """True when check-out is on the same calendar day as check-in."""
    if not check_in or not check_out:
        return True
    if record is not None:
        check_in_day = fields.Datetime.context_timestamp(record, check_in).date()
        check_out_day = fields.Datetime.context_timestamp(record, check_out).date()
        return check_in_day == check_out_day
    return fields.Date.to_date(check_in) == fields.Date.to_date(check_out)


def day_tour_end_of_calendar_day(record, dt_value):
    """Last moment of the check-in/out calendar day in local time."""
    tour_day = fields.Datetime.context_timestamp(record, dt_value).date()
    return datetime.combine(tour_day, time.max)


def day_tour_line_window_error(line):
    """Return a warning dict when day-tour check-in/out is invalid."""
    if not is_day_long_tour_product(line.product_id):
        return False
    if not line.check_in or not line.check_out:
        return {
            "title": _("Day-long tour"),
            "message": _("Please set check-in and check-out for this day-long tour."),
        }
    if not day_tour_same_calendar_day(line.check_in, line.check_out, line):
        return {
            "title": _("Invalid tour dates"),
            "message": _(
                "Check-in and check-out must be on the same calendar date "
                "for day-long tours."
            ),
        }
    return False
