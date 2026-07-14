# -*- coding: utf-8 -*-

from odoo import api, fields, models

from .guest_member_utils import (
    extra_guest_charge,
    guest_counts_from_line,
    line_uses_guest_count_validation,
    sync_guest_info_records,
    total_guests,
    validate_guest_count,
)

_GUEST_COUNT_FIELDS = ("adult_count", "child_count", "infant_count")


def _is_bookable_service_product(product):
    return bool(product and product.is_bookable and not product.is_room_type)


def _is_other_product(product):
    return bool(
        product
        and not product.is_bookable
        and not product.is_room_type
    )


class HotelBookingLine(models.Model):
    _inherit = "hotel.booking.line"

    adult_count = fields.Integer(string="Number of Adults", default=1)
    child_count = fields.Integer(string="Number of Children", default=0)
    infant_count = fields.Integer(string="Number of Infants", default=0)
    driver_count = fields.Integer(string="Number of Drivers", default=0)
    amount_per_guest = fields.Monetary(
        string="Amount per Guest",
        compute="_compute_amount_per_guest",
        readonly=True,
        currency_field="currency_id",
    )
    max_infants = fields.Integer(related="product_tmpl_id.max_infants", string="Max Infants")

    booking_days = fields.Integer(
        string="Days Book For",
        compute="_compute_booking_days",
        inverse="_inverse_booking_days",
        store=True,
        readonly=False,
        copy=False,
    )

    def _prepare_sale_order_line_vals(self):
        self.ensure_one()
        return {
            "order_id": self.booking_id.order_id.id,
            "product_id": self.product_id.id,
            "product_uom_qty": self.booking_days or self.booking_id.booking_days or 1,
            "price_unit": self.price,
            "tax_id": [(6, 0, self.tax_ids.ids)],
            "discount": self.discount,
            "guest_info_ids": [(6, 0, self.guest_info_ids.ids)],
            "adult_guest": self.adult_count,
            "children_guest": self.child_count,
            "infant_guest": self.infant_count,
        }

    def _ensure_sale_order_lines(self):
        SaleOrderLine = self.env["sale.order.line"]
        for line in self:
            if (
                line.sale_order_line_id
                or not line.booking_id.order_id
                or not line.product_id
                or line.display_type
            ):
                continue
            sale_line = SaleOrderLine.create(line._prepare_sale_order_line_vals())
            line.with_context(bypass_for_exchange_room=True).write(
                {"sale_order_line_id": sale_line.id}
            )

    def _get_member_partner(self):
        self.ensure_one()
        return self.booking_id.partner_id

    def _sync_service_product_taxes(self):
        for line in self:
            if not line.product_id:
                continue
            line.tax_ids = line.product_id.taxes_id

    def _sync_service_booking_days_from_guest_counts(self):
        for line in self:
            if not _is_bookable_service_product(line.product_id):
                continue
            guest_total = (
                (line.adult_count or 0)
                + (line.child_count or 0)
                + (line.driver_count or 0)
                + (line.infant_count or 0)
            )
            target_qty = max(guest_total, 1)
            if line.booking_days != target_qty:
                line.write({"booking_days": target_qty})

    def _sync_guest_info_from_counts(self):
        for line in self:
            adult, child, infant = guest_counts_from_line(line)
            partner = line._get_member_partner()
            partner_name = partner.name if partner else "Guest"
            sync_guest_info_records(
                self.env,
                adult=adult,
                child=child,
                infant=infant,
                partner_name=partner_name,
                booking_line_id=line.id,
                sale_order_line_id=line.sale_order_line_id.id if line.sale_order_line_id else False,
                existing_guests=line.guest_info_ids,
            )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("product_id"):
                product = self.env["product.product"].browse(vals["product_id"])
                if product and product.taxes_id:
                    vals["tax_ids"] = [(6, 0, product.taxes_id.ids)]
                elif "tax_ids" not in vals:
                    vals["tax_ids"] = [(5, 0, 0)]
                if _is_bookable_service_product(product):
                    guest_total = (
                        (vals.get("adult_count") or 0)
                        + (vals.get("child_count") or 0)
                        + (vals.get("driver_count") or 0)
                        + (vals.get("infant_count") or 0)
                    )
                    vals["booking_days"] = max(guest_total, 1)
                elif _is_other_product(product):
                    vals["booking_days"] = vals.get("booking_days") or 1

        records = super().create(vals_list)
        records._sync_service_product_taxes()
        records._sync_service_booking_days_from_guest_counts()
        records.filtered(line_uses_guest_count_validation)._sync_guest_info_from_counts()
        records._ensure_sale_order_lines()
        return records

    @api.depends(
        "product_id",
        "booking_id.booking_days",
        "booking_id.check_in",
        "booking_id.check_out",
        "service_line_id",
        "adult_count",
        "child_count",
        "driver_count",
        "infant_count",
    )
    def _compute_booking_days(self):
        lines_for_super = self.filtered(
            lambda line: not _is_other_product(line.product_id)
        )
        super(HotelBookingLine, lines_for_super)._compute_booking_days()
        bookings_to_sync = self.env["hotel.booking"]
        for line in self:
            if _is_other_product(line.product_id):
                if not line.booking_days:
                    line.booking_days = 1
                continue
            if (
                line.product_id
                and line.product_id.is_room_type
                and line.booking_id.check_in
                and line.booking_id.check_out
                and line.booking_days < 1
            ):
                line.booking_days = 1
                if not line.booking_id.booking_days:
                    bookings_to_sync |= line.booking_id
            elif _is_bookable_service_product(line.product_id):
                line.booking_days = max(
                    (line.adult_count or 0)
                    + (line.child_count or 0)
                    + (line.driver_count or 0)
                    + (line.infant_count or 0),
                    1,
                )
        if bookings_to_sync:
            bookings_to_sync._compute_booking_days()

    def _inverse_booking_days(self):
        for line in self.filtered(
            lambda booking_line: _is_other_product(booking_line.product_id)
        ):
            if line.sale_order_line_id:
                line.sale_order_line_id.with_context(
                    bypass_for_exchange_room=True
                ).write({"product_uom_qty": line.booking_days or 1})

    @api.depends(
        "subtotal_price",
        "adult_count",
        "child_count",
        "driver_count",
        "infant_count",
    )
    def _compute_amount_per_guest(self):
        for line in self:
            guest_count = (
                (line.adult_count or 0)
                + (line.child_count or 0)
                + (line.driver_count or 0)
                + (line.infant_count or 0)
            )
            line.amount_per_guest = (
                line.subtotal_price / guest_count if guest_count else 0.0
            )

    @api.depends(
        *_GUEST_COUNT_FIELDS,
        "guest_info_ids",
        "guest_info_ids.is_adult",
        "max_adult",
        "max_child",
        "max_occupancy",
        "product_id",
        "product_id.max_infants",
    )
    def _compute_warning(self):
        lines_with_counts = self.filtered(line_uses_guest_count_validation)
        super(HotelBookingLine, self - lines_with_counts)._compute_warning()
        for line in lines_with_counts:
            adult, child, infant = guest_counts_from_line(line)
            line.warning = validate_guest_count(line, adult, child, infant)

    @api.onchange("adult_count", "child_count", "driver_count", "infant_count", "product_id")
    def _onchange_service_guest_qty(self):
        for line in self:
            if not _is_bookable_service_product(line.product_id):
                continue
            guest_total = (
                (line.adult_count or 0)
                + (line.child_count or 0)
                + (line.driver_count or 0)
                + (line.infant_count or 0)
            )
            line.booking_days = max(guest_total, 1)

    @api.onchange("product_id", "booking_days")
    def _onchange_other_product_price(self):
        for line in self:
            if not _is_other_product(line.product_id) or not line.booking_id.pricelist_id:
                continue
            line.price = line.booking_id.pricelist_id._get_product_price(
                line.product_id, line.booking_days or 1
            )

    @api.onchange(*_GUEST_COUNT_FIELDS, "product_id", "booking_days")
    def _onchange_guest_counts_price(self):
        for line in self:
            adult, child, infant = guest_counts_from_line(line)
            guests = total_guests(adult, child, infant) or len(line.guest_info_ids)
            if not guests or not line.product_id or not line.booking_id.pricelist_id:
                continue
            extra_cost = extra_guest_charge(line, guests)
            line.price = line.booking_id.pricelist_id._get_product_price(
                line.product_id, line.booking_days
            ) + extra_cost

    @api.onchange("subtotal_price")
    @api.depends(
        "product_id",
        "price",
        "tax_ids",
        "discount",
        "subtotal_price",
        "booking_id.check_out",
        "booking_id.check_in",
        "booking_days",
        *_GUEST_COUNT_FIELDS,
    )
    def _compute_amount(self):
        for line in self:
            if _is_other_product(line.product_id) and line.booking_id.pricelist_id:
                line.price = line.booking_id.pricelist_id._get_product_price(
                    line.product_id, line.booking_days or 1
                )
                continue
            adult, child, infant = guest_counts_from_line(line)
            guests = total_guests(adult, child, infant) or (
                len(line.guest_info_ids) if line.guest_info_ids else 0
            )
            if guests and line.booking_id.pricelist_id and line.product_id:
                extra_cost = extra_guest_charge(line, guests)
                line.price = line.booking_id.pricelist_id._get_product_price(
                    line.product_id, line.booking_days
                ) + extra_cost
        return super()._compute_amount()

    def write(self, vals):
        rec = super().write(vals)

        if any(field in vals for field in _GUEST_COUNT_FIELDS):
            self.filtered(line_uses_guest_count_validation)._sync_guest_info_from_counts()

        if any(
            field in vals
            for field in ("adult_count", "child_count", "driver_count", "infant_count", "product_id")
        ):
            self._sync_service_product_taxes()
            self._sync_service_booking_days_from_guest_counts()

        if "booking_days" in vals:
            for line in self.filtered(
                lambda booking_line: _is_other_product(booking_line.product_id)
                and booking_line.sale_order_line_id
            ):
                line.sale_order_line_id.with_context(
                    bypass_for_exchange_room=True
                ).write({"product_uom_qty": line.booking_days or 1})

        self._ensure_sale_order_lines()
        return rec

    def unlink(self):
        for line in self:
            if line.sale_order_line_id:
                order = line.sale_order_line_id.order_id
                so_line = line.sale_order_line_id
                if order:
                    original_state = order.state
                    if original_state != "draft":
                        order.state = "draft"
                    line.with_context(bypass_for_exchange_room=True).write(
                        {"sale_order_line_id": False}
                    )
                    so_line.unlink()
                    if original_state != "draft":
                        order.state = original_state
                else:
                    line.with_context(bypass_for_exchange_room=True).write(
                        {"sale_order_line_id": False}
                    )
                    so_line.unlink()
        return super().unlink()
