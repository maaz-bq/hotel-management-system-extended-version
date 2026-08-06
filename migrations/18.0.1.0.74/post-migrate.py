# -*- coding: utf-8 -*-

from odoo import SUPERUSER_ID, api


def _variant_room_name(variant):
    name = (variant.name or "").strip()
    if name:
        return name
    return variant.display_name


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Template = env["product.template"]
    HotelRoom = env["hotel.room"]
    Product = env["product.product"]

    placeholder_field = Product._fields.get("is_room_placeholder")
    has_placeholder_flag = bool(placeholder_field)

    for template in Template.search([("is_room_type", "=", True)]):
        if HotelRoom.search_count([("room_type_id", "=", template.id)]):
            continue

        numbered_variants = template.product_variant_ids.filtered("active")
        if has_placeholder_flag:
            numbered_variants = numbered_variants.filtered(
                lambda variant: not variant.is_room_placeholder
            )

        for variant in numbered_variants:
            room_name = _variant_room_name(variant)
            if room_name == template.name:
                continue
            HotelRoom.create(
                {
                    "name": room_name,
                    "room_type_id": template.id,
                }
            )
