# -*- coding: utf-8 -*-


def _column_exists(cr, table, column):
    cr.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
        """,
        (table, column),
    )
    return bool(cr.fetchone())


def migrate(cr, version):
    if _column_exists(cr, "hotel_booking_line", "member_count"):
        cr.execute(
            """
            UPDATE hotel_booking_line
            SET adult_count = member_count,
                child_count = 0,
                infant_count = 0
            WHERE COALESCE(member_count, 0) > 0
            """
        )

    if _column_exists(cr, "sale_order_line", "member_count"):
        cr.execute(
            """
            UPDATE sale_order_line
            SET adult_guest = member_count,
                children_guest = 0,
                infant_guest = 0
            WHERE COALESCE(member_count, 0) > 0
            """
        )

    from odoo import api, SUPERUSER_ID

    env = api.Environment(cr, SUPERUSER_ID, {})
    booking_lines = env["hotel.booking.line"].search([
        "|", "|",
        ("adult_count", ">", 0),
        ("child_count", ">", 0),
        ("infant_count", ">", 0),
    ])
    if booking_lines:
        booking_lines._sync_guest_info_from_counts()

    sale_lines = env["sale.order.line"].search([
        "|", "|",
        ("adult_guest", ">", 0),
        ("children_guest", ">", 0),
        ("infant_guest", ">", 0),
    ])
    if sale_lines:
        sale_lines._sync_guest_info_from_counts()
