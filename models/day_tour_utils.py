# -*- coding: utf-8 -*-

from datetime import datetime, time, timedelta

from odoo import fields

# Bookings in these states consume day-tour capacity; cancelled/checkout do not.
DAY_TOUR_ACTIVE_BOOKING_STATUSES = ("initial", "confirm", "allot")


def is_day_long_tour_product(product):
    return bool(
        product
        and product.product_tmpl_id.is_day_long_tour
        and product.is_bookable
        and not product.is_room_type
    )


def day_tour_line_guest_count(line):
    """Total people on a folio line that consume day-tour occupancy."""
    return (
        (line.adult_count or 0)
        + (line.child_count or 0)
        + (line.infant_count or 0)
        + (line.driver_count or 0)
    )


def day_tour_date_from_booking(booking):
    if not booking or not booking.check_in:
        return False
    return fields.Date.to_date(booking.check_in)


def day_tour_stay_date_range(booking):
    """Return inclusive first/last calendar days valid for tour_date on a booking."""
    if not booking or not booking.check_in:
        return False, False
    start = fields.Date.to_date(booking.check_in)
    if not booking.check_out:
        return start, start
    checkout_date = fields.Date.to_date(booking.check_out)
    if checkout_date <= start:
        return start, start
    return start, checkout_date - timedelta(days=1)


def day_tour_date_in_stay(booking, tour_date):
    start, last = day_tour_stay_date_range(booking)
    if not start or not tour_date:
        return False
    return start <= tour_date <= last


def day_tour_date_from_line(line):
    if line.tour_date:
        return line.tour_date
    return day_tour_date_from_booking(line.booking_id)


def day_tour_check_in_bounds(tour_date):
    start_dt = datetime.combine(tour_date, time.min)
    end_dt = datetime.combine(tour_date, time.max)
    return start_dt, end_dt
