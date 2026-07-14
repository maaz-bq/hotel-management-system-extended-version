# -*- coding: utf-8 -*-


def migrate(cr, version):
    from odoo import api, SUPERUSER_ID

    env = api.Environment(cr, SUPERUSER_ID, {})
    bookings = env["hotel.booking"].search([
        ("check_in", "!=", False),
        ("check_out", "!=", False),
    ])
    if bookings:
        bookings._compute_booking_days()
