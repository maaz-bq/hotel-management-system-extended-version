# -*- coding: utf-8 -*-

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    templates = env["product.template"].search([("is_room_type", "=", True)])
    for template in templates:
        numbered = template.product_variant_ids.filtered(
            lambda variant: variant.active and not variant.is_room_placeholder
        )
        variant_count = len(numbered) or template.product_variant_count or 1
        if not template.room_count or template.room_count < 1:
            template.room_count = variant_count
        template.get_placeholder_variant()
