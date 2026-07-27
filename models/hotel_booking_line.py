# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

from .day_tour_utils import (
    day_tour_default_check_in_out,
    day_tour_end_of_calendar_day,
    day_tour_line_calendar_date,
    day_tour_line_guest_count,
    day_tour_line_window_error,
    day_tour_same_calendar_day,
    day_tour_same_day_window,
    is_day_long_tour_product,
    stay_spans_multiple_days,
    stay_is_strict_subset,
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

    check_in = fields.Datetime(
        string="Check In",
        store=True,
        readonly=False,
        copy=False,
        related=False,
    )
    check_out = fields.Datetime(
        string="Check Out",
        store=True,
        readonly=False,
        copy=False,
        related=False,
    )

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
    allowed_other_product_ids = fields.Many2many(
        related="booking_id.other_item_product_ids",
        string="Allowed Other Item Products",
    )
    product_is_room_type = fields.Boolean(
        related="product_id.is_room_type",
    )
    product_is_bookable = fields.Boolean(
        related="product_id.is_bookable",
    )
    is_other_item_line = fields.Boolean(
        string="Other Item Line",
        default=False,
        index=True,
    )
    is_other_product_line = fields.Boolean(
        string="Is Other Product Line",
        compute="_compute_line_product_flags",
    )
    product_is_day_long_tour = fields.Boolean(
        related="product_id.is_day_long_tour",
    )

    def _write_line_check_in_out(self, check_in, check_out):
        """Persist line dates without ORM recursion through booking writes."""
        if not self:
            return
        self.env.cr.execute(
            """
            UPDATE hotel_booking_line
            SET check_in = %s, check_out = %s
            WHERE id = ANY(%s)
            """,
            (check_in, check_out, list(self.ids)),
        )
        self.invalidate_recordset(["check_in", "check_out"])

    def _validate_day_tour_occupancy(self):
        bookings = self.mapped("booking_id")
        for booking in bookings:
            tour_lines = booking.booking_line_ids.filtered(
                lambda booking_line: is_day_long_tour_product(booking_line.product_id)
            )
            if not tour_lines:
                continue

            tour_lines._ensure_day_tour_line_dates()

            if not booking.hotel_id:
                raise ValidationError(
                    _("Please set a hotel on the booking before adding day-long tours.")
                )

            totals_by_key = {}
            for line in tour_lines:
                tour_date = day_tour_line_calendar_date(line)
                if not tour_date:
                    raise ValidationError(
                        _("Please set check-in for day-long tour '%(tour)s'.")
                        % {"tour": line.product_id.display_name}
                    )
                if not day_tour_same_calendar_day(line.check_in, line.check_out, line):
                    raise ValidationError(
                        _(
                            "Check-in and check-out must be on the same calendar date "
                            "for day-long tour '%(tour)s'."
                        )
                        % {"tour": line.product_id.display_name}
                    )
                template = line.product_id.product_tmpl_id
                key = (template, tour_date)
                totals_by_key[key] = (
                    totals_by_key.get(key, 0) + day_tour_line_guest_count(line)
                )

            for (template, tour_date), total_guests in totals_by_key.items():
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
        window_warning = day_tour_line_window_error(self)
        if window_warning:
            return window_warning
        if not is_day_long_tour_product(self.product_id):
            return False
        booking = self.booking_id
        template = self.product_id.product_tmpl_id
        tour_date = day_tour_line_calendar_date(self)
        if not booking.hotel_id or not tour_date:
            return {
                "title": _("Day-long tour"),
                "message": _(
                    "Set the booking hotel and tour check-in to validate tour capacity."
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
                    and day_tour_line_calendar_date(booking_line) == tour_date
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

    @classmethod
    def _default_line_check_in_out(cls, product, booking):
        if booking and booking.check_in and booking.check_out:
            return booking.check_in, booking.check_out
        if is_day_long_tour_product(product):
            return day_tour_default_check_in_out(booking)
        return False, False

    def _ensure_day_tour_line_dates(self):
        """Keep day-long tour lines on a single calendar day."""
        for line in self.filtered(
            lambda booking_line: is_day_long_tour_product(booking_line.product_id)
        ):
            if (
                line.check_in
                and line.check_out
                and day_tour_same_calendar_day(line.check_in, line.check_out, line)
            ):
                continue
            check_in, check_out = day_tour_same_day_window(line)
            if check_in and check_out:
                line._write_line_check_in_out(check_in, check_out)

    def _sync_room_line_dates_from_booking(self):
        """Copy the booking stay window onto room folio lines."""
        for line in self.filtered(
            lambda booking_line: booking_line.product_id
            and booking_line.product_id.is_room_type
        ):
            booking = line.booking_id
            if booking.check_in and booking.check_out:
                line._write_line_check_in_out(booking.check_in, booking.check_out)

    def _protect_room_line_date_vals(self, vals, room_lines):
        """Keep tour or narrowed dates off room folio lines."""
        vals = dict(vals)
        if not any(field in vals for field in ("check_in", "check_out")):
            return vals
        for line in room_lines:
            booking = line.booking_id
            new_check_in = vals.get("check_in", line.check_in)
            new_check_out = vals.get("check_out", line.check_out)
            if not new_check_in or not new_check_out:
                for field in ("check_in", "check_out"):
                    if field in vals and not vals[field]:
                        vals.pop(field)
                continue
            restore_check_in = None
            restore_check_out = None
            if (
                line.check_in
                and line.check_out
                and stay_spans_multiple_days(line.check_in, line.check_out, line)
            ):
                restore_check_in = line.check_in
                restore_check_out = line.check_out
            elif (
                booking.check_in
                and booking.check_out
                and stay_spans_multiple_days(
                    booking.check_in, booking.check_out, booking
                )
            ):
                restore_check_in = booking.check_in
                restore_check_out = booking.check_out
            if not restore_check_in or not restore_check_out:
                continue
            if not stay_spans_multiple_days(new_check_in, new_check_out, line) or (
                stay_is_strict_subset(
                    new_check_in,
                    new_check_out,
                    restore_check_in,
                    restore_check_out,
                    line,
                )
            ):
                vals["check_in"] = restore_check_in
                vals["check_out"] = restore_check_out
                break
        return vals

    def _push_room_line_dates_to_booking(self):
        """When a room line date changes, keep the booking header in sync."""
        for line in self.filtered(
            lambda booking_line: booking_line.product_id
            and booking_line.product_id.is_room_type
        ):
            booking = line.booking_id
            check_in = line.check_in
            check_out = line.check_out
            if not check_in or not check_out:
                continue
            if (
                booking.check_in
                and booking.check_out
                and stay_spans_multiple_days(
                    booking.check_in, booking.check_out, booking
                )
                and (
                    not stay_spans_multiple_days(check_in, check_out, line)
                    or stay_is_strict_subset(
                        check_in,
                        check_out,
                        booking.check_in,
                        booking.check_out,
                        line,
                    )
                )
            ):
                line._write_line_check_in_out(booking.check_in, booking.check_out)
                continue
            if (
                booking.check_in == check_in
                and booking.check_out == check_out
            ):
                continue
            booking.with_context(skip_protect_booking_stay=True).write(
                {
                    "check_in": check_in,
                    "check_out": check_out,
                }
            )

    def _sync_bookable_service_line_dates_from_booking(self):
        """Non-tour bookable services follow the booking stay window."""
        for line in self.filtered(
            lambda booking_line: booking_line.product_id
            and _is_bookable_service_product(booking_line.product_id)
            and not is_day_long_tour_product(booking_line.product_id)
        ):
            booking = line.booking_id
            if booking.check_in and booking.check_out:
                line._write_line_check_in_out(booking.check_in, booking.check_out)

    def _inherit_room_guest_counts_for_day_tour(self, use_write=True):
        """When a tour is added after room lines, copy guest counts for capacity."""
        for line in self.filtered(
            lambda booking_line: is_day_long_tour_product(booking_line.product_id)
        ):
            booking = line.booking_id
            room_lines = booking.booking_line_ids.filtered(
                lambda booking_line: (
                    booking_line.product_id.is_room_type
                    and booking_line.id != line.id
                )
            )
            if not room_lines:
                continue
            room_guests = sum(
                (room_line.adult_count or 0)
                + (room_line.child_count or 0)
                + (room_line.infant_count or 0)
                + (room_line.driver_count or 0)
                for room_line in room_lines
            )
            tour_guests = (
                (line.adult_count or 0)
                + (line.child_count or 0)
                + (line.infant_count or 0)
                + (line.driver_count or 0)
            )
            if room_guests <= tour_guests:
                continue
            vals = {
                "adult_count": sum(room_lines.mapped("adult_count")),
                "child_count": sum(room_lines.mapped("child_count")),
                "infant_count": sum(room_lines.mapped("infant_count")),
                "driver_count": sum(room_lines.mapped("driver_count")),
                "booking_days": max(room_guests, 1),
            }
            if use_write:
                line.write(vals)
            else:
                for field_name, value in vals.items():
                    line[field_name] = value

    def _sync_line_dates_from_booking(self):
        self._sync_room_line_dates_from_booking()
        self._sync_bookable_service_line_dates_from_booking()

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

    @api.model
    def _prepare_line_tab_flag_vals(self, vals):
        vals = dict(vals)
        if vals.get("product_id"):
            product = self.env["product.product"].browse(vals["product_id"])
            vals["is_other_item_line"] = _is_other_product(product)
        elif "is_other_item_line" not in vals:
            if self.env.context.get("booking_line_tab") == "other_items":
                vals["is_other_item_line"] = True
            else:
                vals["is_other_item_line"] = bool(
                    self.env.context.get("default_is_other_item_line")
                )
        return vals

    def _find_unlinked_sale_order_line(self):
        """Reuse an existing SO line for this product when re-syncing."""
        self.ensure_one()
        order = self.booking_id.order_id
        if not order or not self.product_id:
            return self.env["sale.order.line"]
        linked_line_ids = set(
            self.booking_id.booking_line_ids.mapped("sale_order_line_id").ids
        )
        return order.order_line.filtered(
            lambda sol: (
                sol.product_id == self.product_id
                and sol.id not in linked_line_ids
                and not sol.display_type
            )
        )[:1]

    def _sync_to_sale_order_line(self, sale_line):
        self.ensure_one()
        sale_line.with_context(bypass_for_exchange_room=True).write(
            {
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
        )

    def _ensure_sale_order_lines(self):
        SaleOrderLine = self.env["sale.order.line"]
        bookings = self.env["hotel.booking"]
        for line in self:
            if (
                line.sale_order_line_id
                or not line.product_id
                or line.display_type
            ):
                if line.sale_order_line_id:
                    line._sync_to_sale_order_line(line.sale_order_line_id)
                    bookings |= line.booking_id
                continue
            booking = line.booking_id
            if not booking.order_id:
                booking._ensure_hotel_quotation()
            if not booking.order_id:
                continue
            order = booking.order_id
            existing_line = line._find_unlinked_sale_order_line()
            if existing_line:
                line.with_context(
                    bypass_for_exchange_room=True,
                    skip_ensure_sale_order_lines=True,
                ).write({"sale_order_line_id": existing_line.id})
                line._sync_to_sale_order_line(existing_line)
                bookings |= booking
                continue
            restore_state = None
            if order.state not in ("draft", "sent", "cancel"):
                restore_state = order.state
                order.with_context(bypass_checkin_checkout=True).write(
                    {"state": "draft"}
                )
            sale_line = SaleOrderLine.create(line._prepare_sale_order_line_vals())
            line.with_context(
                bypass_for_exchange_room=True,
                skip_ensure_sale_order_lines=True,
            ).write({"sale_order_line_id": sale_line.id})
            if restore_state:
                order.with_context(bypass_checkin_checkout=True).write(
                    {"state": restore_state}
                )
            bookings |= booking
        if bookings:
            bookings._cleanup_orphan_sale_order_lines()
            bookings._sync_sale_order_flags()

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
        prepared_vals_list = []
        for vals in vals_list:
            vals = self._prepare_line_tab_flag_vals(vals)
            if vals.get("product_id"):
                product = self.env["product.product"].browse(vals["product_id"])
                if product and product.taxes_id:
                    vals["tax_ids"] = [(6, 0, product.taxes_id.ids)]
                elif "tax_ids" not in vals:
                    vals["tax_ids"] = [(5, 0, 0)]
                booking = self.env["hotel.booking"].browse(vals.get("booking_id"))
                if _is_other_product(product):
                    vals.pop("check_in", None)
                    vals.pop("check_out", None)
                elif not vals.get("check_in") or not vals.get("check_out"):
                    check_in, check_out = self._default_line_check_in_out(product, booking)
                    vals.setdefault("check_in", check_in)
                    vals.setdefault("check_out", check_out)
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
                    vals.setdefault("booking_days", 1)
                elif _is_other_product(product):
                    vals["is_other_item_line"] = True
                    vals["booking_days"] = vals.get("booking_days") or 1
                    vals["adult_count"] = 0
                    vals["child_count"] = 0
                    vals["infant_count"] = 0
                    vals["driver_count"] = 0
            prepared_vals_list.append(vals)

        records = super().create(prepared_vals_list)
        folio_records = records.filtered(lambda line: not line.is_other_item_line)
        folio_records.with_context(
            skip_sync_folio_line_dates=True
        )._sync_line_dates_from_booking()
        folio_records.filtered(
            lambda line: is_day_long_tour_product(line.product_id)
        )._inherit_room_guest_counts_for_day_tour()
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
            lambda line: (
                not _is_other_product(line.product_id)
                and not (line.product_id and line.product_id.is_room_type)
            )
        )
        super(HotelBookingLine, lines_for_super)._compute_booking_days()
        for line in self.filtered(
            lambda booking_line: (
                booking_line.product_id and booking_line.product_id.is_room_type
            )
        ):
            if not line.booking_days:
                line.booking_days = 1
        for line in self:
            if _is_other_product(line.product_id):
                continue
            if _is_bookable_service_product(line.product_id) and not (
                line.product_id and line.product_id.is_room_type
            ):
                line.booking_days = max(
                    (line.adult_count or 0)
                    + (line.child_count or 0)
                    + (line.driver_count or 0)
                    + (line.infant_count or 0),
                    1,
                )

    def _inverse_booking_days(self):
        for line in self.filtered(
            lambda booking_line: (
                _is_other_product(booking_line.product_id)
                or (
                    booking_line.product_id
                    and booking_line.product_id.is_room_type
                )
            )
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
            line.is_other_item_line = _is_other_product(product)

            if product.is_room_type:
                if not line.adult_count:
                    line.adult_count = 1
                if booking.check_in and booking.check_out:
                    line.check_in = booking.check_in
                    line.check_out = booking.check_out
                line.booking_days = 1
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
                if is_day_long_tour_product(product):
                    if booking.check_in and booking.check_out:
                        line.check_in = booking.check_in
                        line.check_out = booking.check_out
                    line._inherit_room_guest_counts_for_day_tour(use_write=False)
                elif booking.check_in and booking.check_out:
                    line.check_in = booking.check_in
                    line.check_out = booking.check_out
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

    @api.constrains("check_in", "check_out", "product_id")
    def _check_day_tour_same_calendar_day(self):
        # Save-time validation disabled; day-tour rules run on confirm instead.
        return

    @api.constrains(
        "product_id",
        "booking_id",
        "check_in",
        "check_out",
        "adult_count",
        "child_count",
        "infant_count",
        "driver_count",
    )
    def _check_day_tour_occupancy(self):
        # Save-time validation disabled; day-tour rules run on confirm instead.
        return

    @api.constrains("product_id", "is_other_item_line")
    def _check_line_tab_product_consistency(self):
        # Save-time validation disabled.
        return

    @api.constrains("product_id", "booking_id")
    def _check_folio_product_selection(self):
        # Save-time validation disabled.
        return

    @api.onchange("check_in", "check_out")
    def _onchange_folio_line_check_in_out(self):
        warning_payload = False
        for line in self:
            product = line.product_id
            booking = line.booking_id
            if not product or not booking:
                continue

            if product.is_room_type:
                if line.check_in and line.check_out:
                    if (
                        booking.check_in
                        and booking.check_out
                        and stay_spans_multiple_days(
                            booking.check_in, booking.check_out, booking
                        )
                        and (
                            not stay_spans_multiple_days(
                                line.check_in, line.check_out, line
                            )
                            or stay_is_strict_subset(
                                line.check_in,
                                line.check_out,
                                booking.check_in,
                                booking.check_out,
                                line,
                            )
                        )
                    ):
                        line.check_in = booking.check_in
                        line.check_out = booking.check_out
                        continue
                    booking.check_in = line.check_in
                    booking.check_out = line.check_out
                    for sibling in booking.booking_line_ids.filtered(
                        lambda booking_line: (
                            booking_line.product_id.is_room_type
                            and booking_line.id != line.id
                        )
                    ):
                        sibling.check_in = line.check_in
                        sibling.check_out = line.check_out
                continue

            if not is_day_long_tour_product(product):
                continue

            if line.check_in and line.check_out and not day_tour_same_calendar_day(
                line.check_in, line.check_out, line
            ):
                line.check_out = day_tour_end_of_calendar_day(line, line.check_in)

            date_warning = booking._folio_line_header_date_conflict(
                line, line.check_in, line.check_out
            )
            warning = line._day_tour_occupancy_warning()
            if date_warning:
                warning_payload = {
                    "title": _("Date mismatch"),
                    "message": date_warning,
                }
            elif warning:
                warning_payload = warning

        if warning_payload:
            return {"warning": warning_payload}

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
        vals = dict(vals)
        if vals.get("product_id"):
            product = self.env["product.product"].browse(vals["product_id"])
            if product:
                vals["is_other_item_line"] = _is_other_product(product)
        tour_lines = self.browse()
        lines_for_super = self
        date_fields = ("check_in", "check_out")

        if any(field in vals for field in date_fields):
            tour_lines = self.filtered(
                lambda line: is_day_long_tour_product(line.product_id)
            )
            if tour_lines:
                for line in tour_lines:
                    check_in = (
                        vals["check_in"]
                        if "check_in" in vals
                        else line.check_in
                    )
                    check_out = (
                        vals["check_out"]
                        if "check_out" in vals
                        else line.check_out
                    )
                    if check_in and check_out and not day_tour_same_calendar_day(
                        check_in, check_out, line
                    ):
                        check_out = day_tour_end_of_calendar_day(line, check_in)
                    if check_in and check_out:
                        line._write_line_check_in_out(check_in, check_out)
                lines_for_super = self - tour_lines

        if lines_for_super:
            room_lines = lines_for_super.filtered(
                lambda line: line.product_id and line.product_id.is_room_type
            )
            non_room_lines = lines_for_super - room_lines
            if room_lines and any(field in vals for field in date_fields):
                room_vals = self._protect_room_line_date_vals(vals, room_lines)
                super(HotelBookingLine, room_lines).write(room_vals)
                if non_room_lines:
                    non_room_vals = dict(vals)
                    if non_room_lines.filtered(
                        lambda line: _is_other_product(line.product_id)
                    ):
                        for field in date_fields:
                            non_room_vals.pop(field, None)
                    rec = super(HotelBookingLine, non_room_lines).write(non_room_vals)
                else:
                    rec = True
            else:
                rec = super(HotelBookingLine, lines_for_super).write(vals)
        elif tour_lines:
            remaining_vals = {
                key: value for key, value in vals.items() if key not in date_fields
            }
            rec = (
                super(HotelBookingLine, tour_lines).write(remaining_vals)
                if remaining_vals
                else True
            )
        else:
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
            any(field in vals for field in date_fields)
            and not self.env.context.get("skip_push_room_dates")
        ):
            self.filtered(
                lambda booking_line: (
                    booking_line.product_id
                    and booking_line.product_id.is_room_type
                    and not is_day_long_tour_product(booking_line.product_id)
                )
            )._push_room_line_dates_to_booking()

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
