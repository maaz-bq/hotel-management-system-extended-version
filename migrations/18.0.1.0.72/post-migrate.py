# -*- coding: utf-8 -*-

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    templates = env["product.template"].search([("is_room_type", "=", True)])
    for template in templates:
        template.get_placeholder_variant()
