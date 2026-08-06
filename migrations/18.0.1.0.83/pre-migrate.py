# -*- coding: utf-8 -*-


def migrate(cr, version):
    cr.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'hotel_booking_line'
          AND column_name = 'is_day_room_line'
        """
    )
    if cr.fetchone():
        cr.execute(
            "DELETE FROM hotel_booking_line WHERE is_day_room_line IS TRUE"
        )
