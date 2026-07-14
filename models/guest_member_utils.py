# -*- coding: utf-8 -*-

from odoo import fields


def guest_counts_from_line(line):
    """Read adult / child / infant counts from a booking or sale order line."""
    if line._name == "hotel.booking.line":
        return line.adult_count, line.child_count, line.infant_count
    return (
        line.adult_guest or 0,
        line.children_guest or 0,
        line.infant_guest or 0,
    )


def total_guests(adult, child, infant):
    return (adult or 0) + (child or 0) + (infant or 0)


def line_has_guest_counts(line):
    adult, child, infant = guest_counts_from_line(line)
    return total_guests(adult, child, infant) > 0


def line_uses_guest_count_validation(line):
    product = line.product_id
    return bool(product and product.is_room_type and line_has_guest_counts(line))


def max_infants_from_line(line):
    product = line.product_id
    if not product:
        return 0
    tmpl = product.product_tmpl_id
    return tmpl.max_infants if tmpl else 0


def validate_guest_count(line, adult, child, infant=0):
    total = total_guests(adult, child, infant)
    if not total:
        return "Please fill the members details !!"
    if line.max_adult < adult and line.max_child < child:
        return (
            "No. of Adult Guests and Child Guests cannot be greater than "
            "Max Adult and Child count"
        )
    if line.max_adult < adult:
        return "No. of Adult Guests cannot be greater than Max Adult count"
    if line.max_child < child:
        return "No. of Child Guests cannot be greater than Max Child count"
    max_infants = max_infants_from_line(line)
    if infant and infant > max_infants:
        return "No. of Infant Guests cannot be greater than Max Infant count"
    if total > line.max_occupancy:
        return "Total number of guests cannot exceed the maximum occupancy limit"
    return ""


def extra_guest_charge(line, total_guest_count):
    if not total_guest_count or total_guest_count <= line.base_occupancy:
        return 0
    extra_guests = total_guest_count - line.base_occupancy
    return extra_guests * line.extra_charge_per_person


def sync_guest_info_records(
    env,
    *,
    adult,
    child,
    infant,
    partner_name,
    booking_line_id=False,
    sale_order_line_id=False,
    existing_guests=None,
):
    """Rebuild guest.info rows from counts (keeps base hotel validation working)."""
    GuestInfo = env["guest.info"]
    guests = existing_guests or GuestInfo
    guests.unlink()

    if total_guests(adult, child, infant) <= 0:
        return GuestInfo

    vals_list = []
    label = partner_name or "Guest"

    for index in range(adult or 0):
        vals_list.append(
            {
                "name": label if index == 0 and not child and not infant else f"{label} (Adult {index + 1})",
                "age": 18,
                "gender": "male",
                "booking_line_id": booking_line_id,
                "sale_order_line_id": sale_order_line_id,
            }
        )
    for index in range(child or 0):
        vals_list.append(
            {
                "name": f"{label} (Child {index + 1})",
                "age": 10,
                "gender": "male",
                "booking_line_id": booking_line_id,
                "sale_order_line_id": sale_order_line_id,
            }
        )
    for index in range(infant or 0):
        vals_list.append(
            {
                "name": f"{label} (Infant {index + 1})",
                "age": 1,
                "gender": "male",
                "booking_line_id": booking_line_id,
                "sale_order_line_id": sale_order_line_id,
            }
        )

    return GuestInfo.create(vals_list)
