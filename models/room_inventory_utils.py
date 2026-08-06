# -*- coding: utf-8 -*-

from datetime import datetime, time, timedelta

from odoo import fields
from odoo.exceptions import ValidationError

OVERNIGHT_CAPACITY_STATUSES = frozenset({"confirm", "allot"})
ROOM_ASSIGNMENT_STATUSES = frozenset({"confirm", "allot"})


def line_check_in_out(line):
    """Effective check-in/out for a folio line."""
    check_in = line.check_in or line.booking_id.check_in
    check_out = line.check_out or line.booking_id.check_out
    return check_in, check_out


def line_overlaps_range(line, range_start, range_end):
    check_in, check_out = line_check_in_out(line)
    if not check_in or not check_out:
        return False
    return check_out > range_start and check_in <= range_end


def iter_occupying_dates(check_in, check_out):
    """Yield each calendar date a stay occupies (per-night capacity)."""
    if not check_in or not check_out:
        return
    start = fields.Datetime.to_datetime(check_in)
    end = fields.Datetime.to_datetime(check_out)
    current = start.date()
    last = end.date()
    while current <= last:
        day_start = datetime.combine(current, time.min)
        day_end = datetime.combine(current, time.max)
        if end > day_start and start <= day_end:
            yield current
        current += timedelta(days=1)


def line_room_type(line):
    """Room type template for a folio line."""
    if line.room_type_id:
        return line.room_type_id
    product = line.product_id
    if product and product.is_room_type:
        return product.product_tmpl_id
    return line.env["product.template"]


def line_counts_for_room_capacity(line):
    """Whether a folio line consumes a room-type capacity slot."""
    template = line_room_type(line)
    if not template or not template.is_room_type:
        return False
    return line.booking_id.status_bar in OVERNIGHT_CAPACITY_STATUSES


def capacity_statuses_for_line(line):
    """Booking statuses that count this line against room_count."""
    return OVERNIGHT_CAPACITY_STATUSES


def search_capacity_lines(
    env,
    template,
    date_from,
    date_to,
    hotel_id=None,
    exclude_line_ids=None,
):
    """Folio lines that consume room_count for a template in a date range."""
    BookingLine = env["hotel.booking.line"]
    domain = [
        "|",
        ("room_type_id", "=", template.id),
        ("product_id.product_tmpl_id", "=", template.id),
    ]
    if hotel_id:
        domain.append(("booking_id.hotel_id", "=", hotel_id))
    if exclude_line_ids:
        domain.append(("id", "not in", exclude_line_ids))

    lines = BookingLine.search(domain)
    return lines.filtered(
        lambda line: (
            line_counts_for_room_capacity(line)
            and line_overlaps_range(line, date_from, date_to)
        )
    )


def get_booked_slot_count(
    template,
    date_from,
    date_to,
    hotel_id=None,
    exclude_line_ids=None,
):
    """Peak booked slots across each night in the range."""
    peak = 0
    for occupy_date in iter_occupying_dates(date_from, date_to):
        day_start = datetime.combine(occupy_date, time.min)
        day_end = datetime.combine(occupy_date, time.max)
        booked = len(
            search_capacity_lines(
                template.env,
                template,
                day_start,
                day_end,
                hotel_id=hotel_id,
                exclude_line_ids=exclude_line_ids,
            )
        )
        peak = max(peak, booked)
    return peak


def get_available_slot_count(
    template,
    date_from,
    date_to,
    hotel_id=None,
    exclude_line_ids=None,
):
    """Minimum free slots across each night in the range."""
    room_count = template.room_count or 0
    if not room_count:
        return 0
    min_available = room_count
    for occupy_date in iter_occupying_dates(date_from, date_to):
        day_start = datetime.combine(occupy_date, time.min)
        day_end = datetime.combine(occupy_date, time.max)
        booked = len(
            search_capacity_lines(
                template.env,
                template,
                day_start,
                day_end,
                hotel_id=hotel_id,
                exclude_line_ids=exclude_line_ids,
            )
        )
        min_available = min(min_available, max(room_count - booked, 0))
    return min_available


def assert_room_type_capacity(
    template,
    date_from,
    date_to,
    qty=1,
    hotel_id=None,
    exclude_line_ids=None,
):
    """Raise ValidationError when room_count would be exceeded."""
    available = get_available_slot_count(
        template,
        date_from,
        date_to,
        hotel_id=hotel_id,
        exclude_line_ids=exclude_line_ids,
    )
    if available < qty:
        raise ValidationError(
            template.env._(
                "No availability for '%(room_type)s' on the selected dates "
                "(%(available)s of %(total)s slots free)."
            )
            % {
                "room_type": template.display_name,
                "available": available,
                "total": template.room_count or 0,
            }
        )


def find_conflicting_room_assignments(
    env,
    room,
    date_from,
    date_to,
    exclude_line_ids=None,
):
    """Physical-room exclusivity: overlapping active folio lines."""
    domain = [
        ("assigned_room_id", "=", room.id),
        ("booking_id.status_bar", "in", list(ROOM_ASSIGNMENT_STATUSES)),
    ]
    if exclude_line_ids:
        domain.append(("id", "not in", exclude_line_ids))
    lines = env["hotel.booking.line"].search(domain)
    return lines.filtered(
        lambda line: line_overlaps_range(line, date_from, date_to)
    )


def assert_physical_room_available(
    env,
    room,
    date_from,
    date_to,
    exclude_line_ids=None,
):
    """Raise ValidationError when a physical room is already assigned."""
    conflicts = find_conflicting_room_assignments(
        env, room, date_from, date_to, exclude_line_ids=exclude_line_ids
    )
    if conflicts:
        raise ValidationError(
            env._(
                "Room '%(room)s' is not available for the selected dates."
            )
            % {"room": room.display_name}
        )
