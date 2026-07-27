# -*- coding: utf-8 -*-


def migrate(cr, version):
    """Backfill is_other_item_line without ORM write (base write requires singleton)."""
    cr.execute(
        """
        UPDATE hotel_booking_line AS bl
        SET is_other_item_line = TRUE
        FROM product_product AS pp
        JOIN product_template AS pt ON pp.product_tmpl_id = pt.id
        WHERE bl.product_id = pp.id
          AND COALESCE(pt.is_bookable, FALSE) = FALSE
          AND COALESCE(pt.is_room_type, FALSE) = FALSE
        """
    )
    cr.execute(
        """
        UPDATE hotel_booking_line AS bl
        SET is_other_item_line = FALSE
        FROM product_product AS pp
        JOIN product_template AS pt ON pp.product_tmpl_id = pt.id
        WHERE bl.product_id = pp.id
          AND (
              COALESCE(pt.is_bookable, FALSE) = TRUE
              OR COALESCE(pt.is_room_type, FALSE) = TRUE
          )
        """
    )
    cr.execute(
        """
        UPDATE hotel_booking_line
        SET is_other_item_line = FALSE
        WHERE product_id IS NULL
        """
    )
