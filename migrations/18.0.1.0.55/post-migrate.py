# -*- coding: utf-8 -*-

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    category = env["product.template"]._get_night_stay_category()
    if not category:
        return
    templates = env["product.template"].search([("is_room_type", "=", True)])
    if templates:
        templates.write({"categ_id": category.id})
