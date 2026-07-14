# -*- coding: utf-8 -*-


def post_init_hook(env):
    """Recompute stored booking_days for existing bookings after min-1-day fix."""
    bookings = env["hotel.booking"].search([
        ("check_in", "!=", False),
        ("check_out", "!=", False),
    ])
    if bookings:
        bookings._compute_booking_days()
    booking_lines = env["hotel.booking.line"].search([])
    if booking_lines:
        booking_lines._compute_booking_days()
