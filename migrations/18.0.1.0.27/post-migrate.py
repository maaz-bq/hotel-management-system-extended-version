# -*- coding: utf-8 -*-

from datetime import datetime, time

from odoo import api, SUPERUSER_ID


def _is_day_long_tour_product(product):
    return bool(
        product
        and product.product_tmpl_id.is_day_long_tour
        and product.is_bookable
        and not product.is_room_type
    )


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
    lines = env["hotel.booking.line"].search([])
    for line in lines:
        product = line.product_id
        booking = line.booking_id
        if not product or not booking:
            continue
        if _is_day_long_tour_product(product):
            cr.execute(
                """
                SELECT tour_date FROM hotel_booking_line WHERE id = %s
                """,
                (line.id,),
            )
            row = cr.fetchone()
            tour_date = row[0] if row and row[0] else None
            if tour_date:
                line.write(
                    {
                        "check_in": datetime.combine(tour_date, time.min),
                        "check_out": datetime.combine(tour_date, time.max),
                    }
                )
            elif not line.check_in or not line.check_out:
                check_in, check_out = _default_day_tour_check_in_out(booking)
                if check_in and check_out:
                    line.write({"check_in": check_in, "check_out": check_out})
        elif not line.check_in or not line.check_out:
            if booking.check_in and booking.check_out:
                line.write(
                    {
                        "check_in": booking.check_in,
                        "check_out": booking.check_out,
                    }
                )

    cr.execute(
        """
        ALTER TABLE hotel_booking_line
        DROP COLUMN IF EXISTS tour_date
        """
    )
