# -*- coding: utf-8 -*-

from odoo import fields, models


class ProductCategory(models.Model):
    _inherit = "product.category"

    is_bookable = fields.Boolean(
        string="Is Bookable",
        help="Products in this category can be added to hotel folio booking flows.",
    )
