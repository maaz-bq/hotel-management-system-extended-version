# -*- coding: utf-8 -*-

from datetime import datetime, timedelta
import datetime as dt

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

from .checkin_utils import truncate_minutes_seconds
from .day_tour_utils import (
    CONFIRMED_BOOKING_STATUSES,
    is_day_long_tour_product,
    stay_spans_multiple_days,
    stay_is_strict_subset,
)


class HotelBooking(models.Model):
    _inherit = "hotel.booking"

    def _prepare_hotel_quotation_vals(self):
        self.ensure_one()
        return {
            "partner_id": self.partner_id.id,
            "booking_id": self.id,
            "hotel_check_in": self.check_in,
            "hotel_check_out": self.check_out,
            "pricelist_id": self.pricelist_id.id if self.pricelist_id else False,
            "hotel_id": self.hotel_id.id if self.hotel_id else False,
            "booking_count": 1,
            "is_room_type": True,
            "company_id": self.company_id.id,
        }

    def _ensure_hotel_quotation(self):
        SaleOrder = self.env["sale.order"]
        for booking in self:
            if booking.order_id or not booking.partner_id:
                continue
            quotation = SaleOrder.with_context(bypass_checkin_checkout=True).create(
                booking._prepare_hotel_quotation_vals()
            )
            booking.order_id = quotation.id
            booking.booking_line_ids._ensure_sale_order_lines()

    def _cleanup_orphan_sale_order_lines(self):
        """Drop quotation lines that no longer have a matching folio line."""
        for booking in self.filtered("order_id"):
            linked_so_line_ids = set(
                booking.booking_line_ids.mapped("sale_order_line_id").ids
            )
            orphan_lines = booking.order_id.order_line.filtered(
                lambda sol: sol.id not in linked_so_line_ids and not sol.display_type
            )
            if not orphan_lines:
                continue

            orders_to_restore = {}
            for order in orphan_lines.mapped("order_id"):
                if order.state not in ("draft", "sent", "cancel"):
                    orders_to_restore[order.id] = order.state
                    order.with_context(bypass_checkin_checkout=True).write(
                        {"state": "draft"}
                    )

            orphan_lines.with_context(bypass_for_exchange_room=True).unlink()

            for order_id, state in orders_to_restore.items():
                order = self.env["sale.order"].browse(order_id)
                if order.exists():
                    order.with_context(bypass_checkin_checkout=True).write(
                        {"state": state}
                    )

    def action_add_services(self):
        """Deprecated: folio lines are added inline on the booking form."""
        self.ensure_one()
        products = self.env["product.product"].search(
            self._bookable_service_product_domain()
        )
        return {
            "name": "Add Services",
            "type": "ir.actions.act_window",
            "res_model": "hotel.booking.line",
            "view_mode": "form",
            "view_id": self.env.ref(
                "hotel_management_system_extend.service_booking_line_form_view"
            ).id,
            "target": "new",
            "context": {
                "default_booking_id": self.id,
                "default_booking_days": 1,
                "default_product_ids": products.ids,
                "default_product_uom_qty": 1,
            },
        }

    def _bookable_service_product_domain(self):
        self.ensure_one()
        return [
            ("is_bookable", "=", True),
            ("product_tmpl_id.is_room_type", "=", False),
            ("sale_ok", "=", True),
            ("active", "=", True),
            "|",
            ("company_id", "=", False),
            ("company_id", "=", self.company_id.id),
        ]

    def _other_product_domain(self):
        self.ensure_one()
        return [
            ("is_bookable", "=", False),
            ("product_tmpl_id.is_room_type", "=", False),
            ("sale_ok", "=", True),
            ("active", "=", True),
            "|",
            ("company_id", "=", False),
            ("company_id", "=", self.company_id.id),
        ]

    def _folio_common_product_domain(self):
        self.ensure_one()
        return [
            ("sale_ok", "=", True),
            ("active", "=", True),
            "|",
            ("company_id", "=", False),
            ("company_id", "=", self.company_id.id),
        ]

    def _get_folio_available_product_ids(self):
        """Products selectable inline on the Folio tab."""
        self.ensure_one()
        Product = self.env["product.product"]
        common = self._folio_common_product_domain()

        services = Product.search(
            common
            + [
                ("is_bookable", "=", True),
                ("product_tmpl_id.is_room_type", "=", False),
            ]
        )
        others = Product.search(
            common
            + [
                ("is_bookable", "=", False),
                ("product_tmpl_id.is_room_type", "=", False),
            ]
        )

        rooms = Product
        current_rooms = self.booking_line_ids.filtered(
            lambda line: line.product_id.is_room_type
        ).mapped("product_id")
        if self.check_in and self.check_out and self.hotel_id:
            try:
                rooms = self.get_available_room_products(
                    self.check_in, self.check_out, self.hotel_id.id
                )
            except ValidationError:
                rooms = Product
        rooms = rooms | current_rooms

        return (rooms | services | others).ids

    folio_product_ids = fields.Many2many(
        comodel_name="product.product",
        compute="_compute_folio_product_ids",
        string="Folio Product Selection",
    )

    @api.depends(
        "check_in",
        "check_out",
        "hotel_id",
        "company_id",
        "booking_line_ids.product_id",
    )
    def _compute_folio_product_ids(self):
        for booking in self:
            booking.folio_product_ids = [
                (6, 0, booking._get_folio_available_product_ids())
            ]

    def action_add_other_products(self):
        """Deprecated: folio lines are added inline on the booking form."""
        self.ensure_one()
        products = self.env["product.product"].search(self._other_product_domain())
        return {
            "name": "Add Other Products",
            "type": "ir.actions.act_window",
            "res_model": "hotel.booking.line",
            "view_mode": "form",
            "view_id": self.env.ref(
                "hotel_management_system_extend.other_product_booking_line_form_view"
            ).id,
            "target": "new",
            "context": {
                "default_booking_id": self.id,
                "default_booking_days": 1,
                "default_product_ids": products.ids,
            },
        }

    @api.model_create_multi
    def create(self, vals_list):
        bookings = super().create(vals_list)
        manual_bookings = bookings.filtered(
            lambda booking: (
                not booking.order_id
                and booking.booking_reference != "sale_order"
            )
        )
        manual_bookings._ensure_hotel_quotation()
        return bookings

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        for field_name in ("check_in", "check_out"):
            if field_name in res and res.get(field_name):
                res[field_name] = truncate_minutes_seconds(res[field_name])
        return res

    def _should_normalize_check_times_to_restime(self):
        """Only snap to hotel checkout hour when dates were picked without a time."""
        self.ensure_one()
        if not self.check_in or not self.check_out:
            return False
        midnight = datetime.min.time()
        return (
            self.check_in.time() == midnight
            and self.check_out.time() == midnight
        )

    @api.model
    def _bookable_product_domain(self, allowed_company_ids=None):
        domain = [("is_bookable", "=", True), ("active", "=", True)]
        if allowed_company_ids is not None:
            domain.append(("company_id", "in", allowed_company_ids + [False]))
        return domain

    @api.model
    def _bookable_template_domain(self, allowed_company_ids=None):
        domain = [("is_bookable", "=", True)]
        if allowed_company_ids is not None:
            domain.append(("company_id", "in", allowed_company_ids + [False]))
        return domain

    @api.model
    def fetch_data_for_dashboard(self, **kwargs):
        """Front Desk Dashboard: list only is_bookable products."""
        fetch_data = {}
        product = self.env["product.product"]
        booking = self.env["hotel.booking"]
        allowed_company_ids = self.env.context.get("allowed_company_ids", [])

        bookable_products = product.search(
            self._bookable_product_domain(allowed_company_ids)
        )

        today_date = datetime.combine(fields.Date.today(), datetime.min.time())

        scale = kwargs.get("scale", "today")
        if scale == "today":
            start_date = today_date
            end_date = today_date + timedelta(days=1)
        elif scale == "week":
            start_date = today_date - timedelta(days=today_date.weekday())
            end_date = start_date + timedelta(weeks=1)
        elif scale == "month":
            start_date = datetime(today_date.year, today_date.month, 1)
            end_date = (start_date + timedelta(days=31)).replace(day=1)
        elif scale == "year":
            start_date = datetime(today_date.year, 1, 1)
            end_date = datetime(today_date.year + 1, 1, 1)
        else:
            start_date = today_date
            end_date = today_date + timedelta(days=1)

        bookings = booking.search(
            [
                ("check_out", ">", start_date.date()),
                ("check_in", "<=", end_date),
                ("status_bar", "in", list(CONFIRMED_BOOKING_STATUSES)),
                ("company_id", "in", allowed_company_ids + [False]),
            ]
        )

        booked_room = bookings.mapped("booking_line_ids.product_id") & bookable_products
        available_rooms = bookable_products - booked_room

        fetch_data.update(
            {
                "booked_room": len(booked_room),
                "available_rooms": len(available_rooms),
                "booked_room_ids": booked_room.ids,
            }
        )

        room_data = (
            self.env["product.template"]
            .sudo()
            .search(self._bookable_template_domain(allowed_company_ids))
            .read(
                [
                    "name",
                    "product_variant_count",
                    "is_day_long_tour",
                    "day_tour_max_occupancy",
                ]
            )
        )

        current_date_check_in = self.search(
            [
                ("check_in", ">=", start_date),
                ("check_in", "<", end_date),
                ("status_bar", "not in", ["checkout", "cancel"]),
                ("company_id", "in", allowed_company_ids + [False]),
            ]
        )

        current_date_check_out = self.search(
            [
                ("check_out", ">=", start_date),
                ("check_out", "<", end_date),
                ("status_bar", "=", "allot"),
                ("company_id", "in", allowed_company_ids + [False]),
            ]
        )

        bookings_to_confirm = booking.search(
            [
                ("status_bar", "=", "initial"),
                ("company_id", "in", allowed_company_ids + [False]),
            ]
        )

        fetch_data.update(
            {
                "room_data": room_data,
                "check_in_booking": current_date_check_in.ids,
                "check_out_booking": current_date_check_out.ids,
                "current_date_check_in": len(current_date_check_in),
                "current_date_check_out": len(current_date_check_out),
                "bookings_to_confirm": bookings_to_confirm.ids,
            }
        )

        return fetch_data

    def _get_booking_stay_dates_from_room_lines(self):
        """Return the booking stay; header wins for multi-night stays."""
        self.ensure_one()
        if (
            self.check_in
            and self.check_out
            and stay_spans_multiple_days(self.check_in, self.check_out, self)
        ):
            return self.check_in, self.check_out

        room_lines = self.booking_line_ids.filtered(
            lambda line: line.product_id and line.product_id.is_room_type
        )
        if not room_lines:
            return self.check_in, self.check_out

        check_ins = [dt for dt in room_lines.mapped("check_in") if dt]
        check_outs = [dt for dt in room_lines.mapped("check_out") if dt]
        if not check_ins or not check_outs:
            return self.check_in, self.check_out
        return min(check_ins), max(check_outs)

    def _apply_booking_header_stay_dates(self):
        """Keep room folio lines aligned with the booking stay header."""
        for booking in self:
            if not booking.check_in or not booking.check_out:
                continue
            booking.booking_line_ids.filtered(
                lambda line: line.product_id and line.product_id.is_room_type
            )._sync_room_line_dates_from_booking()

    def _snapshot_booking_stay_dates(self):
        snapshots = {}
        for booking in self:
            if (
                booking.check_in
                and booking.check_out
                and stay_spans_multiple_days(
                    booking.check_in, booking.check_out, booking
                )
            ):
                snapshots[booking.id] = (booking.check_in, booking.check_out)
            else:
                snapshots[booking.id] = (
                    booking._get_booking_stay_dates_from_room_lines()
                )
        return snapshots

    def _restore_booking_stay_dates(self, snapshots):
        for booking in self:
            snapshot = snapshots.get(booking.id)
            if not snapshot or not snapshot[0] or not snapshot[1]:
                continue
            check_in, check_out = snapshot
            room_lines = booking.booking_line_ids.filtered(
                lambda line: line.product_id and line.product_id.is_room_type
            )
            if (
                booking.check_in == check_in
                and booking.check_out == check_out
            ):
                room_lines._sync_room_line_dates_from_booking()
                continue
            booking.with_context(
                skip_sync_folio_line_dates=True,
                skip_protect_booking_stay=True,
            ).write({"check_in": check_in, "check_out": check_out})
            room_lines._sync_room_line_dates_from_booking()

    def _persist_folio_line_dates_on_confirm(self):
        """Ensure folio line dates are stored before dashboard availability runs."""
        for booking in self:
            booking._apply_booking_header_stay_dates()
            booking._sync_folio_line_dates()
            booking.booking_line_ids._ensure_day_tour_line_dates()
            booking.booking_line_ids.filtered(
                lambda line: is_day_long_tour_product(line.product_id)
            )._inherit_room_guest_counts_for_day_tour()
            booking._apply_booking_header_stay_dates()

    @api.onchange("check_in", "check_out")
    def _onchange_booking_stay_dates(self):
        """Push booking stay dates to room folio lines in the form."""
        for booking in self:
            room_lines = booking.booking_line_ids.filtered(
                lambda line: line.product_id and line.product_id.is_room_type
            )
            for line in room_lines:
                if booking.check_in:
                    line.check_in = booking.check_in
                if booking.check_out:
                    line.check_out = booking.check_out

    def get_booked_and_available_rooms(self, selected_date, room):
        """Availability counts for a selected bookable product template."""
        template = self.env["product.template"].browse(room)
        product = self.env["product.product"]
        if template.is_room_type:
            variant_domain = [
                ("is_room_type", "=", True),
                ("active", "=", True),
                ("product_tmpl_id", "=", room),
            ]
        else:
            variant_domain = [
                ("is_bookable", "=", True),
                ("active", "=", True),
                ("product_tmpl_id", "=", room),
            ]
        bookable_products = product.search(variant_domain)
        selected_day = (
            selected_date.date()
            if hasattr(selected_date, "date")
            and not isinstance(selected_date, dt.date)
            else selected_date
        )
        selected_start = datetime.combine(selected_day, dt.time.min)
        selected_end = datetime.combine(selected_day, dt.time.max)

        confirmed_lines = self.env["hotel.booking.line"].search(
            [
                ("product_id.product_tmpl_id", "=", room),
                ("booking_id.status_bar", "in", list(CONFIRMED_BOOKING_STATUSES)),
            ]
        )

        def _line_occupies_date(line):
            check_in = line.check_in or line.booking_id.check_in
            check_out = line.check_out or line.booking_id.check_out
            if not check_in or not check_out:
                return False
            return check_out > selected_start and check_in <= selected_end

        booked_rooms = confirmed_lines.filtered(_line_occupies_date).mapped(
            "product_id"
        )
        available_rooms = bookable_products - booked_rooms
        return booked_rooms, available_rooms

    def get_count_of_booking(self, selected_date_data, today_date, room):
        """Add room counts or day-tour occupancy for the Front Desk Dashboard."""
        result = super().get_count_of_booking(
            selected_date_data, today_date, room
        )
        if not room:
            return result

        template = self.env["product.template"].browse(room)
        if template.is_day_long_tour:
            if selected_date_data < today_date:
                max_occupancy = template.day_tour_max_occupancy or 0
                result.update(
                    {
                        "is_day_long_tour": True,
                        "day_tour_max_occupancy": max_occupancy,
                        "day_tour_booked_guests": 0,
                        "day_tour_remaining_occupancy": 0,
                        "total_room_count": max_occupancy,
                        "booked_room_count": 0,
                        "available_rooms": 0,
                    }
                )
            else:
                result.update(template.get_day_tour_dashboard_occupancy(selected_date_data))
                result["available_rooms"] = result["day_tour_remaining_occupancy"]
            return result

        domain = [("active", "=", True), ("product_tmpl_id", "=", room)]
        if template.is_room_type:
            domain.insert(0, ("is_room_type", "=", True))
        else:
            domain.insert(0, ("is_bookable", "=", True))
        total_count = self.env["product.product"].search_count(domain)
        available_count = result.get("available_rooms", 0)
        result.update(
            {
                "total_room_count": total_count,
                "booked_room_count": max(total_count - available_count, 0),
            }
        )
        return result

    @api.model
    def fetch_booking_count_for_dashboard(self, **kwarg):
        """Dashboard RPC entry point (model method, no record ids required)."""
        return super().fetch_booking_count_for_dashboard(**kwarg)

    @api.depends("check_out", "check_in", "expected_check_out")
    def _compute_booking_days(self):
        super()._compute_booking_days()
        for booking in self:
            if booking.check_in and booking.check_out and not booking.booking_days:
                booking.booking_days = 1

    @api.constrains("check_in", "check_out", "hotel_id")
    def _check_day_tour_occupancy_on_booking(self):
        day_tour_lines = self.mapped("booking_line_ids").filtered(
            lambda line: line.product_id.product_tmpl_id.is_day_long_tour
        )
        if day_tour_lines:
            day_tour_lines._validate_day_tour_occupancy()

    def _sync_folio_line_dates(self):
        """Sync stay dates from the booking header without touching day-tour lines."""
        for booking in self:
            stay_lines = booking.booking_line_ids.filtered(
                lambda line: line.product_id
                and (
                    line.product_id.is_room_type
                    or (
                        line.product_id.is_bookable
                        and not is_day_long_tour_product(line.product_id)
                    )
                )
            )
            stay_lines._sync_line_dates_from_booking()

    def write(self, vals):
        vals = dict(vals)
        if not self.env.context.get("skip_protect_booking_stay") and any(
            field in vals for field in ("check_in", "check_out")
        ):
            for booking in self:
                room_lines = booking.booking_line_ids.filtered(
                    lambda line: line.product_id and line.product_id.is_room_type
                )
                if not room_lines:
                    continue
                new_check_in = vals.get("check_in", booking.check_in)
                new_check_out = vals.get("check_out", booking.check_out)
                if not new_check_in or not new_check_out:
                    continue
                room_check_in, room_check_out = (
                    booking._get_booking_stay_dates_from_room_lines()
                )
                if (
                    room_check_in
                    and room_check_out
                    and stay_spans_multiple_days(
                        room_check_in, room_check_out, booking
                    )
                    and (
                        not stay_spans_multiple_days(
                            new_check_in, new_check_out, booking
                        )
                        or stay_is_strict_subset(
                            new_check_in,
                            new_check_out,
                            room_check_in,
                            room_check_out,
                            booking,
                        )
                    )
                ):
                    vals["check_in"] = room_check_in
                    vals["check_out"] = room_check_out
        res = super().write(vals)
        if self.env.context.get("skip_sync_folio_line_dates"):
            return res
        if any(field in vals for field in ("check_in", "check_out")):
            self.with_context(skip_sync_folio_line_dates=True)._sync_folio_line_dates()
        return res

    def _prepare_dashboard_calendar_line_event(self, line):
        """Build one dashboard calendar entry for a room or day-long tour folio line."""
        self.ensure_one()
        product = line.product_id
        if not product:
            return None

        partner = self.partner_id
        partner_value = [partner.id, partner.display_name] if partner else [False, ""]
        booking_name = self.display_name or self.name or ""

        if is_day_long_tour_product(product):
            if not line.check_in or not line.check_out:
                return None
            check_in = line.check_in
            check_out = line.check_out
            line_label = product.display_name
            is_day_tour = True
        elif product.is_room_type:
            check_in = line.check_in or self.check_in
            check_out = line.check_out or self.check_out
            if not check_in or not check_out:
                return None
            line_label = product.display_name
            is_day_tour = False
        else:
            return None

        return {
            "id": -line.id,
            "dashboard_booking_id": self.id,
            "dashboard_line_id": line.id,
            "dashboard_is_day_long_tour": is_day_tour,
            "display_name": booking_name,
            "name": booking_name,
            "check_in": fields.Datetime.to_string(check_in),
            "check_out": fields.Datetime.to_string(check_out),
            "partner_id": partner_value,
            "status_bar": self.status_bar,
            "total_amount": self.total_amount,
            "line_product_name": line_label,
        }

    @api.model
    def fetch_dashboard_calendar_line_events(self, booking_ids, product_tmpl_id=None):
        """Return one calendar event per folio line for the Front Desk Dashboard."""
        bookings = self.browse(booking_ids).exists()
        events = []
        for booking in bookings:
            lines = booking.booking_line_ids.filtered(
                lambda booking_line: (
                    booking_line.product_id and not booking_line.display_type
                )
            )
            if product_tmpl_id:
                lines = lines.filtered(
                    lambda booking_line: (
                        booking_line.product_id.product_tmpl_id.id == product_tmpl_id
                    )
                )
            else:
                lines = lines.filtered(
                    lambda booking_line: (
                        booking_line.product_id.is_room_type
                        or is_day_long_tour_product(booking_line.product_id)
                    )
                )
            for line in lines:
                event = booking._prepare_dashboard_calendar_line_event(line)
                if event:
                    events.append(event)
        return events

    def _ensure_booking_line_guests(self):
        GuestInfo = self.env["guest.info"]
        for booking in self:
            partner = booking.partner_id
            for line in booking.booking_line_ids.filtered(
                lambda booking_line: (
                    booking_line.product_id
                    and booking_line.product_id.is_room_type
                    and not booking_line.guest_info_ids
                    and not (
                        booking_line.adult_count
                        or booking_line.child_count
                        or booking_line.infant_count
                    )
                )
            ):
                sale_line = line.sale_order_line_id
                sale_guests = sale_line.guest_info_ids if sale_line else GuestInfo
                if sale_guests:
                    sale_guests.write({"booking_line_id": line.id})
                    continue
                if partner:
                    GuestInfo.create(
                        {
                            "name": partner.name or "Guest",
                            "booking_line_id": line.id,
                            "sale_order_line_id": sale_line.id if sale_line else False,
                            "age": 18,
                        }
                    )

    def action_confirm_booking(self):
        self._ensure_booking_line_guests()
        self.validate_guest()
        if not self.env.context.get("bypass_checkin_checkout", False):
            self._check_validity_check_in_check_out_booking()

        if self.status_bar == "initial":
            stay_snapshots = self._snapshot_booking_stay_dates()
            conflict = self.check_selected_rooms_availability(self.check_in, self.check_out)
            if conflict["message"]:
                return self.env["wk.wizard.message"].genrated_message(
                    "<span class='text-danger' style='font-weight:bold;'>%s</span>" % _(
                        conflict["message"]
                    ),
                    name="Warning",
                )
            if self.booking_reference == "via_agent" and self.commission_type == "fixed" and not self.agent_commission_amount:
                raise ValidationError(_("Please specify the agent commission on agent info tab!"))
            if self.booking_reference == "via_agent" and self.commission_type == "percentage" and not self.agent_commission_percentage:
                raise ValidationError(_("Please specify the agent commission on agent info tab!"))
            if not self.booking_line_ids:
                raise ValidationError(_("Please add rooms for booking confirmation!"))
            room_lines = self.booking_line_ids.filtered(
                lambda line: line.product_id and line.product_id.is_room_type
            )
            if not all(
                [
                    line.guest_info_ids.ids
                    or line.adult_count
                    or line.child_count
                    or line.infant_count
                    for line in room_lines
                ]
            ):
                raise ValidationError(_("Please fill the members details !!"))
            else:
                if self.booking_reference != "sale_order":
                    sale_order = self.order_id or self.env["sale.order"].create(
                        {
                            "state": "draft",
                            "hotel_check_in": self.check_in,
                            "booking_id": self.id,
                            "partner_id": self.partner_id.id,
                            "hotel_check_out": self.check_out,
                            "pricelist_id": self.pricelist_id.id if self.pricelist_id else False,
                            "hotel_id": self.hotel_id.id,
                            "booking_count": 1,
                        }
                    )
                    if not self.order_id:
                        self.order_id = sale_order

                    sale_order.with_context(bypass_checkin_checkout=True).write(
                        {
                            "partner_id": self.partner_id.id,
                            "hotel_check_in": self.check_in,
                            "hotel_check_out": self.check_out,
                            "pricelist_id": self.pricelist_id.id if self.pricelist_id else False,
                            "hotel_id": self.hotel_id.id,
                            "booking_id": self.id,
                        }
                    )

                    if sale_order.state in ("draft", "sent", "quotation"):
                        sale_order.with_context(
                            tracking_disable=True,
                            from_hotel_booking_confirm=True,
                        ).action_confirm()
                    elif sale_order.state != "sale":
                        sale_order.write({"state": "sale"})

                    for line in self.booking_line_ids:
                        sale_order_line = line.sale_order_line_id
                        if not sale_order_line:
                            sale_order_line = self.env["sale.order.line"].create(
                                {
                                    "order_id": sale_order.id,
                                    "product_id": line.product_id.id,
                                    "product_uom_qty": line.booking_days or self.booking_days or 1,
                                    "price_unit": line.price,
                                    "discount": line.discount,
                                }
                            )
                            line.sale_order_line_id = sale_order_line.id

                        sale_order_line.with_context(bypass_for_exchange_room=True).write(
                            {
                                "tax_id": line.tax_ids,
                                "product_id": line.product_id.id,
                                "product_uom_qty": line.booking_days or self.booking_days or 1,
                                "price_unit": line.price,
                                "discount": line.discount,
                            }
                        )

                    self.order_id = sale_order
                self.status_bar = "confirm"
                self._apply_booking_header_stay_dates()
                self.manage_check_in_out_based_on_restime()
                self._persist_folio_line_dates_on_confirm()
                self._restore_booking_stay_dates(stay_snapshots)
                template_id = self.env.ref("hotel_management_system.hotel_booking_confirm_id")
                confirm_config = (
                    self.env["ir.config_parameter"].sudo().get_param(
                        "hotel_management_system.send_on_confirm"
                    )
                )

                if (
                    not self.env.context.get("bypass_checkin_checkout", False)
                    and confirm_config
                ):
                    template_id.send_mail(self.id, force_send=True)

    def manage_check_in_out_based_on_restime(self):
        self._apply_booking_header_stay_dates()
        self._ensure_booking_line_guests()
        to_normalize = self.filtered(
            lambda booking: booking._should_normalize_check_times_to_restime()
        )
        if to_normalize:
            super(HotelBooking, to_normalize).manage_check_in_out_based_on_restime()
        for booking in self:
            if not booking.check_in or not booking.check_out:
                continue
            if booking.check_out <= booking.check_in:
                booking.with_context(skip_sync_folio_line_dates=True).write(
                    {"check_out": booking.check_in + timedelta(days=1)}
                )
            if booking.order_id:
                booking.order_id.with_context(bypass_checkin_checkout=True).write(
                    {
                        "hotel_check_in": booking.check_in,
                        "hotel_check_out": booking.check_out,
                    }
                )
        self._apply_booking_header_stay_dates()
