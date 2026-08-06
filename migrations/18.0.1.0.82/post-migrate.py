# -*- coding: utf-8 -*-

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Template = env["product.template"]
    for template in Template.search([("is_room_type", "=", True)]):
        template.get_placeholder_variant()
