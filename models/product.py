# -*- coding: utf-8 -*-

from odoo import api, fields, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    is_bookable = fields.Boolean(
        string="Is Bookable",
        help="Products that can be reserved through hotel booking flows "
        "(rooms, day tours, and other capacity-based services).",
    )

    @api.onchange("is_room_type")
    def _onchange_is_room_type(self):
        if self.is_room_type:
            self.is_bookable = True


class ProductProduct(models.Model):
    _inherit = "product.product"

    is_bookable = fields.Boolean(
        related="product_tmpl_id.is_bookable",
        store=True,
    )
