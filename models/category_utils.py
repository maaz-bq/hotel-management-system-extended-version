# -*- coding: utf-8 -*-


def is_bookable_product(product):
    return bool(product and product.categ_id.is_bookable)


def is_bookable_service_product(product):
    return bool(
        is_bookable_product(product)
        and not product.is_room_type
        and not product.product_tmpl_id.is_day_long_tour
    )


def is_other_product(product):
    return bool(
        product and not is_bookable_product(product) and not product.is_room_type
    )
