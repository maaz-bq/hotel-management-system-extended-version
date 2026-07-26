# -*- coding: utf-8 -*-

from datetime import datetime, time

from odoo import api, SUPERUSER_ID


def _default_day_tour_check_in_out(booking):
    if not booking or not booking.check_in:
        return False, False
    tour_date = booking.check_in.date()
    return (
        datetime.combine(tour_date, time.min),
        datetime.combine(tour_date, time.max),
    )


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    lines = env["hotel.booking.line"].search(
        [
            ("product_id.product_tmpl_id.is_day_long_tour", "=", True),
            "|",
            ("check_in", "=", False),
            ("check_out", "=", False),
        ]
    )
    for line in lines:
        check_in, check_out = _default_day_tour_check_in_out(line.booking_id)
        if check_in and check_out:
            line.write({"check_in": check_in, "check_out": check_out})
