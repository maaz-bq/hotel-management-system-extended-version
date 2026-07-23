# -*- coding: utf-8 -*-

from odoo import api, fields, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    tour_lines = env["hotel.booking.line"].search(
        [
            ("product_id.product_tmpl_id.is_day_long_tour", "=", True),
            ("tour_date", "=", False),
        ]
    )
    for line in tour_lines:
        if line.booking_id.check_in:
            line.tour_date = fields.Date.to_date(line.booking_id.check_in)
