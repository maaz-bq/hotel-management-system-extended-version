# -*- coding: utf-8 -*-
{
    "name": "Hotel Management System Extend",
    "summary": "Extends hotel sale orders with portal/report fields, payment totals, and payment email fixes.",
    "version": "18.0.1.0.40",
    "category": "Generic Modules/Hotel Reservation",
    "depends": [
        "hotel_management_system",
    ],
    "data": [
        "report/sale_order_report_templates.xml",
        "report/tax_totals_templates.xml",
        "views/sale_portal_templates.xml",
        "views/sale_order_views.xml",
        "views/hotel_booking_add_room_views.xml",
        "views/product_views.xml",
    ],
    "installable": True,
    "application": False,
    "license": "LGPL-3",
    "post_init_hook": "post_init_hook",
    "assets": {
        "web.assets_backend": [
            "hotel_management_system_extend/static/src/views/calendar/calendar_popover.xml",
            "hotel_management_system_extend/static/src/views/calendar/calendar_controller.js",
        ],
    },
}
