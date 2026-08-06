# -*- coding: utf-8 -*-

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    BookingLine = env["hotel.booking.line"]
    for line in BookingLine.search(
        [
            "|",
            ("room_type_id", "=", False),
            ("product_id", "!=", False),
        ]
    ):
        template = False
        if line.room_type_id:
            template = line.room_type_id
        elif line.product_id and line.product_id.is_room_type:
            template = line.product_id.product_tmpl_id
        if not template:
            continue
        billing = template.get_billing_variant()
        vals = {"room_type_id": template.id}
        if billing:
            vals["product_id"] = billing.id
        line.write(vals)
