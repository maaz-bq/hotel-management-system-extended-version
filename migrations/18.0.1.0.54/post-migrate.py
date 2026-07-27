# -*- coding: utf-8 -*-

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    category = env["product.template"]._get_day_long_tour_category()
    if not category:
        return
    templates = env["product.template"].search([("is_day_long_tour", "=", True)])
    if templates:
        templates.write({"categ_id": category.id})
