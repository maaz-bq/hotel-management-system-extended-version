# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

from .day_tour_utils import (
    day_tour_date_from_booking,
    day_tour_line_guest_count,
    is_day_long_tour_product,
)
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
    allowed_product_ids = fields.Many2many(
        related="booking_id.folio_product_ids",
        string="Allowed Folio Products",
    )
    product_is_room_type = fields.Boolean(
        related="product_id.is_room_type",
    )
    product_is_bookable = fields.Boolean(
        related="product_id.is_bookable",
    )
    is_other_product_line = fields.Boolean(
        string="Is Other Product Line",
        compute="_compute_line_product_flags",
    )
    product_is_day_long_tour = fields.Boolean(
        related="product_id.is_day_long_tour",
    )
    day_tour_remaining_occupancy = fields.Integer(
        string="Remaining Tour Capacity",
        compute="_compute_day_tour_remaining_occupancy",
    )

    @api.depends(
        "product_id",
        "product_id.is_day_long_tour",
        "booking_id.check_in",
        "booking_id.hotel_id",
        "adult_count",
        "child_count",
        "infant_count",
        "driver_count",
    )
    def _compute_day_tour_remaining_occupancy(self):
        for line in self:
            line.day_tour_remaining_occupancy = 0
            if not is_day_long_tour_product(line.product_id):
                continue
            booking = line.booking_id
            tour_date = day_tour_date_from_booking(booking)
            if not tour_date or not booking.hotel_id:
                continue
            template = line.product_id.product_tmpl_id
            line.day_tour_remaining_occupancy = template.get_day_tour_remaining_occupancy(
                tour_date,
                booking.hotel_id.id,
                exclude_booking_id=booking.id,
            )

    def _validate_day_tour_occupancy(self):
        bookings = self.mapped("booking_id")
        for booking in bookings:
            tour_lines = booking.booking_line_ids.filtered(
                lambda booking_line: is_day_long_tour_product(booking_line.product_id)
            )
            if not tour_lines:
                continue

            if not booking.hotel_id:
                raise ValidationError(
                    _("Please set a hotel on the booking before adding day-long tours.")
                )
            tour_date = day_tour_date_from_booking(booking)
            if not tour_date:
                raise ValidationError(
                    _("Please set a check-in date before adding day-long tours.")
                )

            totals_by_template = {}
            for line in tour_lines:
                template = line.product_id.product_tmpl_id
                totals_by_template[template] = (
                    totals_by_template.get(template, 0)
                    + day_tour_line_guest_count(line)
                )

            for template, total_guests in totals_by_template.items():
                if total_guests <= 0:
                    raise ValidationError(
                        _("Day-long tour '%(tour)s' requires at least one guest.")
                        % {"tour": template.display_name}
                    )
                if total_guests > template.day_tour_max_occupancy:
                    raise ValidationError(
                        _(
                            "Day-long tour '%(tour)s' allows at most %(max)s guests per booking."
                        )
                        % {
                            "tour": template.display_name,
                            "max": template.day_tour_max_occupancy,
                        }
                    )
                remaining = template.get_day_tour_remaining_occupancy(
                    tour_date,
                    booking.hotel_id.id,
                    exclude_booking_id=booking.id,
                )
                if total_guests > remaining:
                    raise ValidationError(
                        _(
                            "Not enough day-long tour capacity for '%(tour)s' on %(date)s. "
                            "Requested: %(requested)s, remaining: %(remaining)s."
                        )
                        % {
                            "tour": template.display_name,
                            "date": tour_date,
                            "requested": total_guests,
                            "remaining": remaining,
                        }
                    )

    def _day_tour_occupancy_warning(self):
        self.ensure_one()
        if not is_day_long_tour_product(self.product_id):
            return False
        booking = self.booking_id
        template = self.product_id.product_tmpl_id
        tour_date = day_tour_date_from_booking(booking)
        if not booking.hotel_id or not tour_date:
            return {
                "title": _("Day-long tour"),
                "message": _(
                    "Set the booking hotel and check-in date to validate tour capacity."
                ),
            }
        guest_count = day_tour_line_guest_count(self)
        if guest_count <= 0:
            return {
                "title": _("Day-long tour"),
                "message": _("Please enter at least one guest for this day-long tour."),
            }
        total_on_booking = sum(
            day_tour_line_guest_count(line)
            for line in booking.booking_line_ids.filtered(
                lambda booking_line: (
                    booking_line.product_id.product_tmpl_id == template
                )
            )
        )
        if total_on_booking > template.day_tour_max_occupancy:
            return {
                "title": _("Day-long tour capacity exceeded"),
                "message": _(
                    "This tour allows at most %(max)s guests per booking.",
                    max=template.day_tour_max_occupancy,
                ),
            }
        remaining = template.get_day_tour_remaining_occupancy(
            tour_date,
            booking.hotel_id.id,
            exclude_booking_id=booking.id,
        )
        if total_on_booking > remaining:
            return {
                "title": _("Day-long tour capacity exceeded"),
                "message": _(
                    "Only %(remaining)s guest places remain for '%(tour)s' on %(date)s.",
                    remaining=remaining,
                    tour=template.display_name,
                    date=tour_date,
                ),
            }
        return False

    @api.depends("product_id", "product_id.is_room_type", "product_id.is_bookable")
    def _compute_line_product_flags(self):
        for line in self:
            line.is_other_product_line = bool(_is_other_product(line.product_id))

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
            line.with_context(
                bypass_for_exchange_room=True,
                skip_ensure_sale_order_lines=True,
            ).write({"sale_order_line_id": sale_line.id})

    def _break_sale_order_line_link(self):
        """Clear sale_order_line_id without recreating quotation lines."""
        line_ids = self.ids
        if not line_ids:
            return
        self.env.cr.execute(
            "UPDATE hotel_booking_line SET sale_order_line_id = NULL WHERE id = ANY(%s)",
            (line_ids,),
        )
        self.invalidate_recordset(["sale_order_line_id"])

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
                elif product.is_room_type and not vals.get("adult_count"):
                    vals["adult_count"] = 1
                elif _is_other_product(product):
                    vals["booking_days"] = vals.get("booking_days") or 1
                    vals["adult_count"] = 0
                    vals["child_count"] = 0
                    vals["infant_count"] = 0
                    vals["driver_count"] = 0

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
            qty = max(line.booking_days or 1, 1)
            line.booking_days = qty
            if line.sale_order_line_id:
                line.sale_order_line_id.with_context(
                    bypass_for_exchange_room=True
                ).write({"product_uom_qty": qty})

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

    @api.onchange("product_id")
    def _onchange_folio_product_id(self):
        for line in self:
            product = line.product_id
            booking = line.booking_id
            if not product or not booking:
                continue

            line.tax_ids = product.taxes_id

            if product.is_room_type:
                if not line.adult_count:
                    line.adult_count = 1
                if (
                    booking.check_in
                    and booking.check_out
                    and booking.hotel_id
                    and product.id not in booking._get_folio_available_product_ids()
                ):
                    return {
                        "warning": {
                            "title": _("Room unavailable"),
                            "message": _(
                                "This room is not available for the selected dates."
                            ),
                        }
                    }
            elif _is_bookable_service_product(product):
                if not (
                    line.adult_count
                    or line.child_count
                    or line.driver_count
                    or line.infant_count
                ):
                    line.adult_count = 1
                guest_total = (
                    (line.adult_count or 0)
                    + (line.child_count or 0)
                    + (line.driver_count or 0)
                    + (line.infant_count or 0)
                )
                line.booking_days = max(guest_total, 1)
                warning = line._day_tour_occupancy_warning()
                if warning:
                    return {"warning": warning}
            elif _is_other_product(product):
                line.adult_count = 0
                line.child_count = 0
                line.infant_count = 0
                line.driver_count = 0
                line.guest_info_ids = [(5, 0, 0)]
                line.booking_days = line.booking_days or 1
                if booking.pricelist_id:
                    line.price = booking.pricelist_id._get_product_price(
                        product, line.booking_days
                    )

    @api.constrains(
        "product_id",
        "booking_id",
        "adult_count",
        "child_count",
        "infant_count",
        "driver_count",
    )
    def _check_day_tour_occupancy(self):
        self._validate_day_tour_occupancy()

    @api.constrains("product_id", "booking_id")
    def _check_folio_product_selection(self):
        for line in self:
            product = line.product_id
            booking = line.booking_id
            if not product or not booking:
                continue

            if product.id not in booking._get_folio_available_product_ids():
                raise ValidationError(
                    _("%s is not available for this booking.")
                    % product.display_name
                )

            if product.is_room_type:
                duplicate_room = booking.booking_line_ids.filtered(
                    lambda booking_line: (
                        booking_line.product_id == product
                        and booking_line.id != line.id
                    )
                )
                if duplicate_room:
                    raise ValidationError(
                        _("Room %s is already on this folio.")
                        % product.display_name
                    )

    @api.onchange("adult_count", "child_count", "driver_count", "infant_count", "product_id")
    def _onchange_service_guest_qty(self):
        warning_payload = False
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
            if line.booking_id.pricelist_id and line.product_id:
                line.price = line.booking_id.pricelist_id._get_product_price(
                    line.product_id, line.booking_days
                )
            warning = line._day_tour_occupancy_warning()
            if warning:
                warning_payload = warning
        if warning_payload:
            return {"warning": warning_payload}

    @api.onchange("booking_days", "product_id")
    def _onchange_other_product_qty(self):
        for line in self:
            if not _is_other_product(line.product_id):
                continue
            qty = max(line.booking_days or 1, 1)
            line.booking_days = qty
            if line.booking_id.pricelist_id and line.product_id:
                line.price = line.booking_id.pricelist_id._get_product_price(
                    line.product_id, qty
                )

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
            if not line.product_id or not line.product_id.is_room_type:
                continue
            adult, child, infant = guest_counts_from_line(line)
            guests = total_guests(adult, child, infant) or len(line.guest_info_ids)
            if not guests or not line.booking_id.pricelist_id:
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
        clearing_so_link = (
            "sale_order_line_id" in vals and not vals.get("sale_order_line_id")
        )
        rec = super().write(vals)

        if any(field in vals for field in _GUEST_COUNT_FIELDS):
            self.filtered(line_uses_guest_count_validation)._sync_guest_info_from_counts()

        if any(
            field in vals
            for field in ("adult_count", "child_count", "driver_count", "infant_count", "product_id")
        ):
            self._sync_service_product_taxes()
            self._sync_service_booking_days_from_guest_counts()

        if "product_id" in vals:
            other_lines = self.filtered(lambda line: _is_other_product(line.product_id))
            if other_lines:
                super(HotelBookingLine, other_lines).write(
                    {
                        "adult_count": 0,
                        "child_count": 0,
                        "infant_count": 0,
                        "driver_count": 0,
                    }
                )
                other_lines.guest_info_ids.unlink()

        if "booking_days" in vals:
            for line in self.filtered(
                lambda booking_line: _is_other_product(booking_line.product_id)
                and booking_line.sale_order_line_id
            ):
                line.sale_order_line_id.with_context(
                    bypass_for_exchange_room=True
                ).write({"product_uom_qty": line.booking_days or 1})

        if (
            not self.env.context.get("skip_ensure_sale_order_lines")
            and not clearing_so_link
        ):
            self._ensure_sale_order_lines()
        return rec

    def _unlink_linked_sale_order_lines(self):
        """Delete sale order lines linked to these booking lines."""
        lines_with_so = self.filtered("sale_order_line_id")
        if not lines_with_so:
            return

        lines_with_so.mapped("guest_info_ids").unlink()

        so_lines = lines_with_so.mapped("sale_order_line_id")
        so_lines.mapped("guest_info_ids").unlink()

        orders_to_restore = {}
        for order in so_lines.mapped("order_id"):
            if order.state not in ("draft", "sent", "cancel"):
                orders_to_restore[order.id] = order.state
                order.with_context(bypass_checkin_checkout=True).write(
                    {"state": "draft"}
                )

        lines_with_so._break_sale_order_line_link()

        so_lines.with_context(bypass_for_exchange_room=True).unlink()

        for order_id, state in orders_to_restore.items():
            order = self.env["sale.order"].browse(order_id)
            if order.exists():
                order.with_context(bypass_checkin_checkout=True).write(
                    {"state": state}
                )

    def unlink(self):
        bookings = self.mapped("booking_id")
        self_ctx = self.with_context(
            skip_ensure_sale_order_lines=True,
            bypass_for_exchange_room=True,
        )
        self_ctx._unlink_linked_sale_order_lines()
        res = super(HotelBookingLine, self_ctx).unlink()
        bookings._cleanup_orphan_sale_order_lines()
        return res
