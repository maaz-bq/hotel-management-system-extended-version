# -*- coding: utf-8 -*-

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Category = env["product.category"]

    cr.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'product_template'
          AND column_name = 'is_bookable'
        """
    )
    has_product_is_bookable = bool(cr.fetchone())

    if has_product_is_bookable:
        cr.execute(
            """
            SELECT DISTINCT categ_id
            FROM product_template
            WHERE COALESCE(is_bookable, FALSE) = TRUE
              AND categ_id IS NOT NULL
            """
        )
        for (categ_id,) in cr.fetchall():
            Category.browse(categ_id).write({"is_bookable": True})

    night_stay = Category.search(
        [("name", "=", "Night Stay"), ("parent_id.name", "=", "All")],
        limit=1,
    )
    day_long = Category.search(
        [
            ("name", "in", ["Day-Long", "Day Long"]),
            ("parent_id.name", "=", "All"),
        ],
        limit=1,
    )
    for category in night_stay | day_long:
        category.write({"is_bookable": True})

    if has_product_is_bookable:
        cr.execute("ALTER TABLE product_template DROP COLUMN IF EXISTS is_bookable")
        cr.execute("ALTER TABLE product_product DROP COLUMN IF EXISTS is_bookable")

    cr.execute(
        """
        DELETE FROM ir_model_fields
        WHERE model IN ('product.template', 'product.product')
          AND name = 'is_bookable'
        """
    )
