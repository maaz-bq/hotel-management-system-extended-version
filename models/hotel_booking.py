# -*- coding: utf-8 -*-

from datetime import datetime, timedelta
import datetime as dt

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

from .checkin_utils import truncate_minutes_seconds
from .day_tour_utils import (
    CONFIRMED_BOOKING_STATUSES,
    day_tour_line_calendar_date,
    is_day_long_tour_product,
    stay_spans_multiple_days,
    stay_is_strict_subset,
)
from .room_inventory_utils import (
    find_conflicting_room_assignments,
    line_room_type,
)

class HotelBooking(models.Model):
    _inherit = "hotel.booking"

    other_item_product_ids = fields.Many2many(
        comodel_name="product.product",
        compute="_compute_other_item_product_ids",
        string="Other Item Product Selection",
    )
    folio_product_ids = fields.Many2many(
        comodel_name="product.product",
        compute="_compute_folio_product_ids",
        string="Folio Product Selection",
    )
    folio_room_type_ids = fields.Many2many(
        comodel_name="product.template",
        compute="_compute_folio_room_type_ids",
        string="Folio Room Type Selection",
    )
    folio_line_ids = fields.One2many(
        comodel_name="hotel.booking.line",
        inverse_name="booking_id",
        string="Folio Lines",
        domain=[("is_other_item_line", "=", False)],
    )
    other_item_line_ids = fields.One2many(
        comodel_name="hotel.booking.line",
        inverse_name="booking_id",
        string="Other Items",
        domain=[("is_other_item_line", "=", True)],
    )

    @api.model
    def _is_hotel_extended_calendar(self):
        return bool(self.env.context.get("hotel_extended_calendar"))

    def _prepare_hotel_quotation_vals(self):
        self.ensure_one()
        has_room_lines = any(
            line.product_id.is_room_type
            for line in self.booking_line_ids
            if line.product_id
        )
        return {
            "partner_id": self.partner_id.id,
            "booking_id": self.id,
            "hotel_check_in": self.check_in,
            "hotel_check_out": self.check_out,
            "pricelist_id": self.pricelist_id.id if self.pricelist_id else False,
            "hotel_id": self.hotel_id.id if self.hotel_id else False,
            "booking_count": 1,
            "is_room_type": has_room_lines,
            "company_id": self.company_id.id,
        }

    def _sync_sale_order_flags(self):
        """Keep sale order header flags aligned with folio content."""
        for booking in self:
            order = booking.order_id
            if not order:
                continue
            has_room_lines = any(
                line.product_id.is_room_type
                for line in booking.booking_line_ids
                if line.product_id
            )
            if order.is_room_type != has_room_lines:
                order.with_context(bypass_checkin_checkout=True).write(
                    {"is_room_type": has_room_lines}
                )

    def _ensure_hotel_quotation(self):
        SaleOrder = self.env["sale.order"]
        for booking in self:
            if booking.order_id or not booking.partner_id:
                continue
            quotation = SaleOrder.with_context(bypass_checkin_checkout=True).create(
                booking._prepare_hotel_quotation_vals()
            )
            booking.order_id = quotation.id

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
            ("categ_id.is_bookable", "=", True),
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
            ("categ_id.is_bookable", "=", False),
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

    def _get_folio_room_type_picker_products(self):
        """One picker variant per available room type for the unified folio column."""
        self.ensure_one()
        Template = self.env["product.template"]
        products = self.env["product.product"]
        for template in Template.browse(self._get_folio_room_type_ids()):
            picker = template._get_picker_placeholder_variant()
            if not picker:
                picker = template.get_billing_variant()
            if picker:
                products |= picker
        current_room_products = self.folio_line_ids.filtered(
            lambda line: line_room_type(line).is_room_type and line.product_id
        ).mapped("product_id")
        return products | current_room_products

    def _get_folio_available_product_ids(self):
        """Products selectable inline on the Folio tab (rooms + tours/services)."""
        self.ensure_one()
        Product = self.env["product.product"]
        common = self._folio_common_product_domain()

        services = Product.search(
            common
            + [
                ("categ_id.is_bookable", "=", True),
                ("product_tmpl_id.is_room_type", "=", False),
                ("product_tmpl_id.is_day_long_tour", "=", False),
            ]
        )
        day_tours = Product.search(
            common
            + [
                ("categ_id.is_bookable", "=", True),
                ("product_tmpl_id.is_day_long_tour", "=", True),
            ]
        )
        available_day_tours = self._filter_day_tours_with_capacity(day_tours)
        current_day_tours = self.booking_line_ids.filtered(
            lambda line: is_day_long_tour_product(line.product_id)
        ).mapped("product_id")
        room_products = self._get_folio_room_type_picker_products()

        return (services | available_day_tours | current_day_tours | room_products).ids

    def _get_folio_room_type_ids(self):
        """Room types available on the folio for the booking dates."""
        self.ensure_one()
        Template = self.env["product.template"]
        if not self.hotel_id:
            return []
        domain = [
            ("is_room_type", "=", True),
            ("hotel_id", "=", self.hotel_id.id),
        ]
        templates = Template.search(domain)
        available = Template
        exclude_line_ids = self.booking_line_ids.ids
        for template in templates:
            if self.check_in and self.check_out and self.check_out > self.check_in:
                if not template.has_room_type_capacity(
                    self.check_in,
                    self.check_out,
                    hotel_id=self.hotel_id.id,
                    exclude_line_ids=exclude_line_ids,
                ):
                    continue
            available |= template
        current_types = self.folio_line_ids.mapped("room_type_id")
        return (available | current_types).ids

    def _get_other_item_available_product_ids(self):
        """Non-bookable products selectable on the Other Items tab."""
        self.ensure_one()
        Product = self.env["product.product"]
        others = Product.search(
            self._folio_common_product_domain()
            + [
                ("categ_id.is_bookable", "=", False),
                ("product_tmpl_id.is_room_type", "=", False),
            ]
        )
        current_other_products = self.other_item_line_ids.mapped("product_id")
        return (others | current_other_products).ids

    @api.depends("company_id", "booking_line_ids.product_id")
    def _compute_other_item_product_ids(self):
        for booking in self:
            booking.other_item_product_ids = [
                (6, 0, booking._get_other_item_available_product_ids())
            ]

    def _filter_day_tours_with_capacity(self, products):
        """Day-long tours stay selectable while daily capacity remains."""
        self.ensure_one()
        if not products:
            return products
        if not self.hotel_id:
            return products

        available = self.env["product.product"]
        current_tour_lines = self.booking_line_ids.filtered(
            lambda line: is_day_long_tour_product(line.product_id)
        )
        for product in products:
            template = product.product_tmpl_id
            tour_dates = set()
            product_lines = current_tour_lines.filtered(
                lambda line: line.product_id == product
            )
            if product_lines:
                for line in product_lines:
                    tour_date = day_tour_line_calendar_date(line)
                    if tour_date:
                        tour_dates.add(tour_date)
            elif self.check_in:
                tour_dates.add(
                    fields.Datetime.context_timestamp(self, self.check_in).date()
                )
            else:
                available |= product
                continue

            for tour_date in tour_dates:
                remaining = template.get_day_tour_remaining_occupancy(
                    tour_date,
                    self.hotel_id.id,
                    exclude_booking_id=self.id,
                )
                if remaining > 0:
                    available |= product
                    break
        return available

    def _booking_line_overlaps_stay(self, line, check_in, check_out):
        line_check_in = line.check_in or line.booking_id.check_in
        line_check_out = line.check_out or line.booking_id.check_out
        if not line_check_in or not line_check_out:
            return False
        return line_check_out > check_in and line_check_in <= check_out

    def check_selected_rooms_availability(self, check_in, check_out):
        """Type-level capacity, variant exclusivity, and tour guest pool."""
        self.ensure_one()

        if check_in and check_out and check_out <= check_in:
            return {
                "available": False,
                "message": _("Checkout date must be after check-in date."),
            }

        room_lines = self.booking_line_ids.filtered(
            lambda line: line_room_type(line).is_room_type
        )
        tour_lines = self.booking_line_ids.filtered(
            lambda line: is_day_long_tour_product(line.product_id)
        )

        if not room_lines and not tour_lines:
            return {
                "available": False,
                "message": _("No rooms or tours selected to check availability."),
            }

        exclude_line_ids = self.booking_line_ids.ids
        BookingLine = self.env["hotel.booking.line"]

        for line in room_lines:
            line_check_in = line.check_in or check_in
            line_check_out = line.check_out or check_out
            if not line_check_in or not line_check_out:
                continue
            template = line_room_type(line)
            try:
                template.assert_room_type_capacity(
                    line_check_in,
                    line_check_out,
                    hotel_id=self.hotel_id.id if self.hotel_id else None,
                    exclude_line_ids=exclude_line_ids,
                )
            except ValidationError as error:
                return {"available": False, "message": error.args[0]}

            if (
                line.assigned_room_id
                and line.booking_id.status_bar in ("confirm", "allot")
            ):
                conflicts = BookingLine._find_conflicting_room_assignments(
                    line.assigned_room_id,
                    line_check_in,
                    line_check_out,
                    exclude_line_ids=exclude_line_ids,
                )
                if conflicts:
                    return {
                        "available": False,
                        "message": _(
                            "Room '%(room)s' is not available for the selected dates."
                        )
                        % {"room": line.assigned_room_id.display_name},
                    }

        if tour_lines:
            if not self.hotel_id:
                return {
                    "available": False,
                    "message": _(
                        "Please set a hotel on the booking before confirming "
                        "day-long tours."
                    ),
                }
            try:
                tour_lines._validate_day_tour_occupancy()
            except ValidationError as error:
                return {"available": False, "message": error.args[0]}

        return {"available": True, "message": ""}

    @api.model
    def get_available_room_products(
        self,
        check_in,
        check_out,
        hotel_id,
        room_exchange=False,
        room_template_id=None,
    ):
        """Return billing variants or room types with capacity for legacy callers."""
        if hotel_id != 0 and not hotel_id:
            raise ValidationError(_("Please add a Hotel before adding Rooms."))
        if not check_in or not check_out:
            raise ValidationError(
                _("Please select Check-In and Check-Out dates before adding Rooms.")
            )
        if not room_exchange and check_in.date() < fields.Date.today():
            raise ValidationError(_("Check-In date cannot be in the past."))

        Template = self.env["product.template"]
        domain = [("is_room_type", "=", True)]
        if hotel_id:
            domain.append(("hotel_id", "=", hotel_id))
        templates = Template.search(domain)

        booking = self if len(self) == 1 and self._name == "hotel.booking" else self.browse()
        exclude_line_ids = booking.booking_line_ids.ids if booking else []

        Product = self.env["product.product"]
        available_products = Product
        for template in templates:
            if room_template_id and template.id != room_template_id:
                continue
            if template.has_room_type_capacity(
                check_in,
                check_out,
                hotel_id=hotel_id,
                exclude_line_ids=exclude_line_ids,
            ):
                billing = template.get_billing_variant()
                if billing:
                    available_products |= billing
        return available_products

    def _get_room_lines_for_allot_assignment(self):
        self.ensure_one()
        return self.booking_line_ids.filtered(
            lambda line: line_room_type(line).is_room_type
        )

    def _get_room_lines_needing_assignment(self):
        self.ensure_one()
        return self._get_room_lines_for_allot_assignment().filtered(
            lambda line: not line.assigned_room_id
        )

    def _action_open_allot_room_wizard(self):
        self.ensure_one()
        return {
            "name": _("Allot Room"),
            "type": "ir.actions.act_window",
            "res_model": "hotel.booking.allot.room.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_booking_id": self.id,
            },
        }

    def _complete_allot(self):
        self.ensure_one()
        self.expected_check_out = self.check_out
        template_id = self.env.ref(
            "hotel_management_system.hotel_booking_allot_id"
        )
        self.write({"status_bar": "allot"})
        if self.env["ir.config_parameter"].sudo().get_param(
            "hotel_management_system.send_on_allot"
        ):
            template_id.send_mail(self.id, force_send=True)

    def allot_action(self):
        for booking in self:
            conflict = booking.check_selected_rooms_availability(
                booking.check_in, booking.check_out
            )
            if not conflict.get("available", True):
                return self.env["wk.wizard.message"].genrated_message(
                    "<span class='text-danger' style='font-weight:bold;'>%s</span>"
                    % conflict["message"],
                    name=_("Warning"),
                )
        self.ensure_one()
        if not self.env.context.get("skip_allot_room_wizard"):
            if self._get_room_lines_for_allot_assignment():
                if not self.hotel_id.required_document_ids:
                    return self._action_open_allot_room_wizard()
        return super().allot_action()

    @api.depends(
        "check_in",
        "check_out",
        "hotel_id",
        "booking_line_ids.room_type_id",
    )
    def _compute_folio_room_type_ids(self):
        for booking in self:
            booking.folio_room_type_ids = [
                (6, 0, booking._get_folio_room_type_ids())
            ]

    @api.depends(
        "check_in",
        "check_out",
        "hotel_id",
        "company_id",
        "booking_line_ids.product_id",
        "booking_line_ids.room_type_id",
    )
    def _compute_folio_product_ids(self):
        for booking in self:
            booking.folio_product_ids = [
                (6, 0, booking._get_folio_available_product_ids())
            ]

    def action_add_other_products(self):
        """Deprecated: other items are added inline on the booking form."""
        self.ensure_one()
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
                "default_product_ids": self._get_other_item_available_product_ids(),
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
        domain = [("categ_id.is_bookable", "=", True), ("active", "=", True)]
        if allowed_company_ids is not None:
            domain.append(("company_id", "in", allowed_company_ids + [False]))
        return domain

    @api.model
    def _bookable_template_domain(self, allowed_company_ids=None):
        domain = [("categ_id.is_bookable", "=", True)]
        if allowed_company_ids is not None:
            domain.append(("company_id", "in", allowed_company_ids + [False]))
        return domain

    @api.model
    def _night_stay_template_domain(self, allowed_company_ids=None):
        domain = [
            ("is_room_type", "=", True),
            ("categ_id.is_bookable", "=", True),
            ("active", "=", True),
        ]
        if allowed_company_ids is not None:
            domain.append(("company_id", "in", allowed_company_ids + [False]))
        return domain

    @api.model
    def _day_long_template_domain(self, allowed_company_ids=None):
        domain = [
            ("is_day_long_tour", "=", True),
            ("categ_id.is_bookable", "=", True),
            ("active", "=", True),
        ]
        if allowed_company_ids is not None:
            domain.append(("company_id", "in", allowed_company_ids + [False]))
        return domain

    @api.model
    def _dashboard_selected_day(self, selected_date):
        if isinstance(selected_date, dt.date) and not isinstance(
            selected_date, datetime
        ):
            return selected_date
        if isinstance(selected_date, str):
            return fields.Date.to_date(selected_date)
        if hasattr(selected_date, "date"):
            return selected_date.date()
        return fields.Date.to_date(selected_date)

    @api.model
    def _folio_line_occupies_date(self, line, selected_start, selected_end):
        check_in = line.check_in or line.booking_id.check_in
        check_out = line.check_out or line.booking_id.check_out
        if not check_in or not check_out:
            return False
        return check_out > selected_start and check_in <= selected_end

    @api.model
    def _category_template_domain(self, category, allowed_company_ids=None):
        domain = [("categ_id", "=", category.id), ("active", "=", True)]
        if allowed_company_ids is not None:
            domain.append(("company_id", "in", allowed_company_ids + [False]))
        return domain

    @api.model
    def _get_bookable_categories(self, allowed_company_ids=None):
        """Bookable categories that contain at least one active product."""
        Category = self.env["product.category"]
        categories = Category.search([("is_bookable", "=", True)], order="complete_name")
        if not categories:
            return Category
        template_domain = self._bookable_template_domain(allowed_company_ids)
        template_domain.append(("active", "=", True))
        used_categ_ids = set(
            self.env["product.template"].search(template_domain).mapped("categ_id").ids
        )
        return categories.filtered(lambda category: category.id in used_categ_ids)

    @api.model
    def _get_category_metric_type(self, category, allowed_company_ids=None):
        domain = self._category_template_domain(category, allowed_company_ids)
        templates = self.env["product.template"].search(domain)
        if templates.filtered("is_room_type"):
            return "room"
        if templates.filtered("is_day_long_tour"):
            return "tour"
        return "service"

    @api.model
    def _serialize_category_availability(
        self, category, metric_type, available, total, products=None
    ):
        if metric_type == "tour":
            display = f"{available}/{total}"
        else:
            display = str(available)
        payload = {
            "id": category.id,
            "name": category.name,
            "metric_type": metric_type,
            "available": available,
            "total": total,
            "display": display,
        }
        if products is not None:
            payload["products"] = products
        return payload

    @api.model
    def _serialize_product_availability(
        self, template, metric_type, available, booked, total, is_past=False
    ):
        name = template.name
        if metric_type == "room":
            if is_past:
                available = 0
                booked = total
            display = f"{name} {available} avail, {booked} booked"
        elif metric_type == "tour":
            status = "Full" if is_past or available <= 0 else "Available"
            display = f"{name}: {status}"
        else:
            display = f"{name}: Available"
        return {
            "id": template.id,
            "name": name,
            "metric_type": metric_type,
            "available": available,
            "booked": booked,
            "total": total,
            "display": display,
        }

    @api.model
    def _get_room_template_variant_domain(self, template, allowed_company_ids=None):
        if template.is_room_type:
            variant_domain = [
                ("is_room_type", "=", True),
                ("active", "=", True),
                ("product_tmpl_id", "=", template.id),
            ]
        else:
            variant_domain = [
                ("categ_id.is_bookable", "=", True),
                ("active", "=", True),
                ("product_tmpl_id", "=", template.id),
            ]
        if allowed_company_ids:
            variant_domain.append(
                ("company_id", "in", allowed_company_ids + [False])
            )
        return variant_domain

    @api.model
    def _get_products_availability_for_day(
        self,
        category,
        selected_date,
        allowed_company_ids=None,
        confirmed_lines=None,
    ):
        metric_type = self._get_category_metric_type(category, allowed_company_ids)
        selected_day = self._dashboard_selected_day(selected_date)
        is_past = selected_day < fields.Date.today()
        templates = self.env["product.template"].search(
            self._category_template_domain(category, allowed_company_ids),
            order="name",
        )
        products = []

        if metric_type == "room":
            selected_datetime = datetime.combine(selected_day, dt.time.min)
            for template in templates.filtered("is_room_type"):
                variant_domain = self._get_room_template_variant_domain(
                    template, allowed_company_ids
                )
                total = len(self.env["product.product"].search(variant_domain))
                if is_past:
                    products.append(
                        self._serialize_product_availability(
                            template, "room", 0, total, total, is_past=True
                        )
                    )
                    continue
                booked_rooms, available_rooms = self.get_booked_and_available_rooms(
                    selected_datetime, template.id
                )
                available = len(available_rooms)
                booked = len(booked_rooms)
                products.append(
                    self._serialize_product_availability(
                        template,
                        "room",
                        available,
                        booked,
                        available + booked,
                    )
                )
            return products

        if metric_type == "tour":
            for template in templates.filtered("is_day_long_tour"):
                if is_past:
                    products.append(
                        self._serialize_product_availability(
                            template, "tour", 0, 1, 1, is_past=True
                        )
                    )
                    continue
                occupancy = template.get_day_tour_dashboard_occupancy(selected_day)
                remaining = occupancy.get("day_tour_remaining_occupancy", 0)
                slot_available = 1 if remaining > 0 else 0
                products.append(
                    self._serialize_product_availability(
                        template,
                        "tour",
                        slot_available,
                        1 - slot_available,
                        1,
                    )
                )
            return products

        for template in templates.filtered(
            lambda tmpl: not tmpl.is_room_type and not tmpl.is_day_long_tour
        ):
            products.append(
                self._serialize_product_availability(template, "service", 1, 0, 1)
            )
        return products

    @api.model
    def _attach_products_to_category_availability(
        self,
        payload,
        category,
        selected_date,
        allowed_company_ids=None,
        confirmed_lines=None,
    ):
        payload["products"] = self._get_products_availability_for_day(
            category,
            selected_date,
            allowed_company_ids=allowed_company_ids,
            confirmed_lines=confirmed_lines,
        )
        return payload

    @api.model
    def _get_dashboard_confirmed_lines(self, allowed_company_ids=None):
        line_domain = [
            ("product_id.categ_id.is_bookable", "=", True),
            ("booking_id.status_bar", "in", list(CONFIRMED_BOOKING_STATUSES)),
        ]
        if allowed_company_ids:
            line_domain.append(
                ("booking_id.company_id", "in", allowed_company_ids + [False])
            )
        return self.env["hotel.booking.line"].search(line_domain)

    @api.model
    def _get_category_availability_for_day(
        self,
        category,
        selected_date,
        allowed_company_ids=None,
        confirmed_lines=None,
    ):
        metric_type = self._get_category_metric_type(category, allowed_company_ids)
        selected_day = self._dashboard_selected_day(selected_date)
        is_past = selected_day < fields.Date.today()

        if metric_type == "room":
            templates = self.env["product.template"].search(
                self._category_template_domain(category, allowed_company_ids)
                + [("is_room_type", "=", True)]
            )
            total = sum(template.room_count or 0 for template in templates)
            if is_past:
                available = 0
            else:
                if confirmed_lines is None:
                    confirmed_lines = self._get_dashboard_confirmed_lines(
                        allowed_company_ids
                    )
                selected_start = datetime.combine(selected_day, dt.time.min)
                selected_end = datetime.combine(selected_day, dt.time.max)
                booked_slots = 0
                for template in templates:
                    booked_slots += template.get_booked_slot_count_for_day(
                        selected_day,
                        hotel_id=template.hotel_id.id if template.hotel_id else None,
                    )
                available = max(total - booked_slots, 0)
            payload = self._serialize_category_availability(
                category, metric_type, available, total
            )
            return self._attach_products_to_category_availability(
                payload,
                category,
                selected_date,
                allowed_company_ids=allowed_company_ids,
                confirmed_lines=confirmed_lines,
            )

        if metric_type == "tour":
            templates = self.env["product.template"].search(
                self._category_template_domain(category, allowed_company_ids)
                + [("is_day_long_tour", "=", True)]
            )
            total = len(templates)
            if is_past:
                available = 0
            else:
                available = sum(
                    1
                    for template in templates
                    if template.get_day_tour_dashboard_occupancy(selected_day).get(
                        "day_tour_remaining_occupancy", 0
                    )
                    > 0
                )
            payload = self._serialize_category_availability(
                category, metric_type, available, total
            )
            return self._attach_products_to_category_availability(
                payload,
                category,
                selected_date,
                allowed_company_ids=allowed_company_ids,
                confirmed_lines=confirmed_lines,
            )

        templates = self.env["product.template"].search(
            self._category_template_domain(category, allowed_company_ids)
            + [("is_room_type", "=", False), ("is_day_long_tour", "=", False)]
        )
        total = len(templates)
        payload = self._serialize_category_availability(
            category, "service", total, total
        )
        return self._attach_products_to_category_availability(
            payload,
            category,
            selected_date,
            allowed_company_ids=allowed_company_ids,
            confirmed_lines=confirmed_lines,
        )

    @api.model
    def _get_categories_availability_for_day(
        self, selected_date, allowed_company_ids=None, confirmed_lines=None
    ):
        categories = self._get_bookable_categories(allowed_company_ids)
        return [
            self._get_category_availability_for_day(
                category,
                selected_date,
                allowed_company_ids=allowed_company_ids,
                confirmed_lines=confirmed_lines,
            )
            for category in categories
        ]

    @api.model
    def _build_bookable_categories_dashboard_data(self, allowed_company_ids=None):
        ProductTemplate = self.env["product.template"].sudo()
        bookable_categories = []
        night_stay_data = []
        day_long_data = []
        room_data = []
        read_fields = [
            "name",
            "product_variant_count",
            "is_room_type",
            "is_day_long_tour",
            "day_tour_max_occupancy",
        ]

        for category in self._get_bookable_categories(allowed_company_ids):
            metric_type = self._get_category_metric_type(category, allowed_company_ids)
            templates = ProductTemplate.search(
                self._category_template_domain(category, allowed_company_ids)
            )
            products = templates.read(read_fields)
            for item in products:
                item["is_day_long_tour"] = bool(item.get("is_day_long_tour"))
            bookable_categories.append(
                {
                    "id": category.id,
                    "name": category.name,
                    "complete_name": category.complete_name,
                    "metric_type": metric_type,
                    "products": products,
                }
            )
            room_data.extend(products)
            if metric_type == "room":
                night_stay_data.extend(products)
            elif metric_type == "tour":
                day_long_data.extend(products)

        return bookable_categories, room_data, night_stay_data, day_long_data

    @api.model
    def get_night_stay_category_availability(self, selected_date):
        """Legacy aggregate for room-type bookable categories."""
        allowed_company_ids = self.env.context.get("allowed_company_ids", [])
        totals = {"total": 0, "booked": 0, "available": 0}
        confirmed_lines = self._get_dashboard_confirmed_lines(allowed_company_ids)
        selected_day = self._dashboard_selected_day(selected_date)
        selected_start = datetime.combine(selected_day, dt.time.min)
        selected_end = datetime.combine(selected_day, dt.time.max)
        for category in self._get_bookable_categories(allowed_company_ids):
            if self._get_category_metric_type(category, allowed_company_ids) != "room":
                continue
            payload = self._get_category_availability_for_day(
                category,
                selected_date,
                allowed_company_ids=allowed_company_ids,
                confirmed_lines=confirmed_lines,
            )
            totals["total"] += payload["total"]
            totals["available"] += payload["available"]
        totals["booked"] = max(totals["total"] - totals["available"], 0)
        return totals

    @api.model
    def get_day_long_category_availability(self, selected_date):
        """Legacy aggregate for day-long bookable categories."""
        allowed_company_ids = self.env.context.get("allowed_company_ids", [])
        total_tours = 0
        available_tours = 0
        for category in self._get_bookable_categories(allowed_company_ids):
            if self._get_category_metric_type(category, allowed_company_ids) != "tour":
                continue
            payload = self._get_category_availability_for_day(
                category, selected_date, allowed_company_ids=allowed_company_ids
            )
            total_tours += payload["total"]
            available_tours += payload["available"]
        return {
            "total_tours": total_tours,
            "available_tours": available_tours,
            "fully_booked_tours": total_tours - available_tours,
        }

    @api.model
    def fetch_category_availability_range(self, start_date, end_date):
        """Per-day availability for each bookable product category."""
        if not self._is_hotel_extended_calendar():
            return {}
        start = self._dashboard_selected_day(start_date)
        end = self._dashboard_selected_day(end_date)
        if end > start:
            end = end - timedelta(days=1)
        if (end - start).days > 62:
            end = start + timedelta(days=62)

        allowed_company_ids = self.env.context.get("allowed_company_ids", [])
        categories = self._get_bookable_categories(allowed_company_ids)
        confirmed_lines = self._get_dashboard_confirmed_lines(allowed_company_ids)

        result = {}
        current = start
        while current <= end:
            day_key = fields.Date.to_string(current)
            result[day_key] = {
                "categories": [
                    self._get_category_availability_for_day(
                        category,
                        current,
                        allowed_company_ids=allowed_company_ids,
                        confirmed_lines=confirmed_lines,
                    )
                    for category in categories
                ]
            }
            current += timedelta(days=1)
        return result

    @api.model
    def fetch_data_for_dashboard(self, **kwargs):
        """Availability Calendar: Night Stay / Day-Long product panel data."""
        if not self._is_hotel_extended_calendar():
            return super().fetch_data_for_dashboard(**kwargs)
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

        (
            bookable_categories,
            room_data,
            night_stay_data,
            day_long_data,
        ) = self._build_bookable_categories_dashboard_data(allowed_company_ids)

        today = fields.Date.today()
        category_availability = self._get_categories_availability_for_day(
            today, allowed_company_ids
        )
        night_stay_availability = self.get_night_stay_category_availability(today)
        day_long_availability = self.get_day_long_category_availability(today)

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
                "extended_calendar": True,
                "room_data": room_data,
                "night_stay_data": night_stay_data,
                "day_long_data": day_long_data,
                "bookable_categories": bookable_categories,
                "category_availability": category_availability,
                "night_stay_availability": night_stay_availability,
                "day_long_availability": day_long_availability,
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

    def _day_tour_stay_dates_from_lines(self, tour_lines):
        check_ins = [dt for dt in tour_lines.mapped("check_in") if dt]
        check_outs = [dt for dt in tour_lines.mapped("check_out") if dt]
        if not check_ins or not check_outs:
            return False, False
        return min(check_ins), max(check_outs)

    def _get_day_tour_stay_dates(self):
        """Return the combined stay window from day-long tour folio lines."""
        self.ensure_one()
        tour_lines = self.booking_line_ids.filtered(
            lambda line: is_day_long_tour_product(line.product_id)
        )
        if not tour_lines:
            return False, False
        tour_lines._ensure_day_tour_line_dates()
        return self._day_tour_stay_dates_from_lines(tour_lines)

    def _has_room_folio_lines(self):
        self.ensure_one()
        return bool(
            self.booking_line_ids.filtered(
                lambda line: line.product_id and line.product_id.is_room_type
            )
        )

    def _get_room_folio_stay_dates(self):
        """Min/max stay window from room folio lines."""
        self.ensure_one()
        room_lines = self.booking_line_ids.filtered(
            lambda line: line.product_id and line.product_id.is_room_type
        )
        check_ins = [dt for dt in room_lines.mapped("check_in") if dt]
        check_outs = [dt for dt in room_lines.mapped("check_out") if dt]
        if not check_ins or not check_outs:
            return False, False
        return min(check_ins), max(check_outs)

    def _stay_calendar_bounds(self, check_in, check_out):
        if not check_in or not check_out:
            return False, False
        start = fields.Datetime.context_timestamp(self, check_in).date()
        end = fields.Datetime.context_timestamp(self, check_out).date()
        return start, end

    def _format_stay_bounds_label(self, check_in, check_out):
        start, end = self._stay_calendar_bounds(check_in, check_out)
        if not start or not end:
            return _("-")
        if start == end:
            return start.strftime("%Y-%m-%d")
        return "%s – %s" % (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))

    def _get_date_check_folio_lines(self):
        """Folio lines whose stay dates must agree with the booking header."""
        self.ensure_one()
        return self.booking_line_ids.filtered(
            lambda line: (
                line.product_id
                and not line.is_other_item_line
                and (
                    line.product_id.is_room_type
                    or is_day_long_tour_product(line.product_id)
                )
            )
        )

    def _get_stored_line_stay_datetimes(self, lines):
        """Read persisted line dates directly (avoid related-field shadowing)."""
        dates = {}
        stored_lines = lines.filtered("id")
        if stored_lines:
            self.env.cr.execute(
                """
                SELECT id, check_in, check_out
                FROM hotel_booking_line
                WHERE id = ANY(%s)
                """,
                (stored_lines.ids,),
            )
            for line_id, check_in, check_out in self.env.cr.fetchall():
                if not check_in or not check_out:
                    line = stored_lines.browse(line_id)
                    check_in = check_in or line.check_in
                    check_out = check_out or line.check_out
                dates[line_id] = (check_in, check_out)
        for line in lines - stored_lines:
            dates[line.id] = (line.check_in, line.check_out)
        return dates

    def _folio_line_header_date_conflict(self, line, line_in, line_out):
        """Return a warning when one folio line disagrees with the booking header."""
        self.ensure_one()
        if (
            not line.product_id
            or line.is_other_item_line
            or not (line.product_id.is_room_type or is_day_long_tour_product(line.product_id))
        ):
            return False

        header_in, header_out = self.check_in, self.check_out
        if not header_in or not header_out or not line_in or not line_out:
            return False

        header_start, header_end = self._stay_calendar_bounds(header_in, header_out)
        line_start, line_end = self._stay_calendar_bounds(line_in, line_out)
        if not header_start or not header_end or not line_start or not line_end:
            return False

        header_label = self._format_stay_bounds_label(header_in, header_out)
        folio_label = self._format_stay_bounds_label(line_in, line_out)
        product_name = line.product_id.display_name
        room_lines = self.booking_line_ids.filtered(
            lambda booking_line: (
                booking_line.product_id and booking_line.product_id.is_room_type
            )
        )

        if line.product_id.is_room_type:
            if line_start != header_start or line_end != header_end:
                return _(
                    "The booking header check-in/check-out (%(header)s) does not "
                    "match the folio line check-in/check-out (%(folio)s) for "
                    "'%(product)s'."
                ) % {
                    "header": header_label,
                    "folio": folio_label,
                    "product": product_name,
                }
            return False

        if not room_lines:
            if line_start != header_start or line_end != header_end:
                return _(
                    "The booking header check-in/check-out (%(header)s) does not "
                    "match the folio line check-in/check-out (%(folio)s) for "
                    "'%(product)s'."
                ) % {
                    "header": header_label,
                    "folio": folio_label,
                    "product": product_name,
                }
            return False

        if line_start < header_start or line_end > header_end:
            return _(
                "The folio line check-in/check-out (%(folio)s) for '%(product)s' "
                "exceeds the booking header check-in/check-out (%(header)s)."
            ) % {
                "folio": folio_label,
                "product": product_name,
                "header": header_label,
            }
        return False

    def _get_header_folio_date_mismatch_message(self):
        """Return a warning message when header and folio stay dates disagree."""
        self.ensure_one()
        header_in, header_out = self.check_in, self.check_out
        if not header_in or not header_out:
            return _("Please set check-in and check-out on the booking header.")

        stay_lines = self._get_date_check_folio_lines()
        if not stay_lines:
            return False

        line_dates = self._get_stored_line_stay_datetimes(stay_lines)
        mismatches = []
        for line in stay_lines:
            line_in, line_out = line_dates.get(line.id, (False, False))
            if not line_in or not line_out:
                return _(
                    "Please set check-in and check-out on folio line '%(product)s'."
                ) % {"product": line.product_id.display_name}
            conflict = self._folio_line_header_date_conflict(line, line_in, line_out)
            if conflict:
                mismatches.append(conflict)

        if mismatches:
            return "%s\n\n%s" % (
                _("Please align the dates before confirming:"),
                "\n".join(mismatches),
            )
        return False

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
            selected_day = (
                selected_date.date()
                if hasattr(selected_date, "date")
                and not isinstance(selected_date, dt.date)
                else selected_date
            )
            total_count = template.room_count or 0
            booked_count = template.get_booked_slot_count_for_day(
                selected_day,
                hotel_id=template.hotel_id.id if template.hotel_id else None,
            )
            available_count = max(total_count - booked_count, 0)
            selected_start = datetime.combine(selected_day, dt.time.min)
            selected_end = datetime.combine(selected_day, dt.time.max)
            physical_rooms = template.physical_room_ids.filtered("active")
            booked_rooms = physical_rooms.filtered(
                lambda room: find_conflicting_room_assignments(
                    self.env,
                    room,
                    selected_start,
                    selected_end,
                )
            )
            available_rooms = physical_rooms - booked_rooms
            if not physical_rooms and available_count > 0:
                placeholder = template.get_placeholder_variant()
                if placeholder:
                    available_rooms = placeholder
            return booked_rooms, available_rooms

        variant_domain = [
            ("categ_id.is_bookable", "=", True),
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
        """Add room counts or day-tour occupancy for the Availability Calendar."""
        if not self._is_hotel_extended_calendar():
            return super().get_count_of_booking(
                selected_date_data, today_date, room
            )
        result = super().get_count_of_booking(
            selected_date_data, today_date, room
        )
        allowed_company_ids = self.env.context.get("allowed_company_ids", [])
        result["category_availability"] = self._get_categories_availability_for_day(
            selected_date_data, allowed_company_ids
        )
        result["night_stay_availability"] = self.get_night_stay_category_availability(
            selected_date_data
        )
        result["day_long_availability"] = self.get_day_long_category_availability(
            selected_date_data
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
            total_count = template.room_count or 0
            if selected_date_data < today_date:
                available_count = 0
            else:
                booked = template.get_booked_slot_count_for_day(
                    selected_date_data,
                    hotel_id=template.hotel_id.id if template.hotel_id else None,
                )
                available_count = max(total_count - booked, 0)
            result.update(
                {
                    "total_room_count": total_count,
                    "booked_room_count": max(total_count - available_count, 0),
                    "available_rooms": available_count,
                }
            )
            return result
        else:
            domain.insert(0, ("categ_id.is_bookable", "=", True))
        total_count = self.env["product.product"].search_count(domain)
        available_count = result.get("available_rooms", 0)
        result.update(
            {
                "total_room_count": total_count,
                "booked_room_count": max(total_count - available_count, 0),
            }
        )
        return result

    def fetch_booking_count_for_dashboard(self, **kwarg):
        """Dashboard RPC entry point."""
        return super().fetch_booking_count_for_dashboard(**kwarg)

    @api.depends("check_out", "check_in", "expected_check_out")
    def _compute_booking_days(self):
        super()._compute_booking_days()
        for booking in self:
            if booking.check_in and booking.check_out and not booking.booking_days:
                booking.booking_days = 1

    @api.constrains("check_in", "check_out", "hotel_id")
    def _check_day_tour_occupancy_on_booking(self):
        # Save-time validation disabled; day-tour rules run on confirm instead.
        return

    def _sync_folio_line_dates(self):
        """Sync stay dates from the booking header without touching day-tour lines."""
        for booking in self:
            stay_lines = booking.booking_line_ids.filtered(
                lambda line: line.product_id
                and (
                    line.product_id.is_room_type
                    or (
                        line.product_id.categ_id.is_bookable
                        and not is_day_long_tour_product(line.product_id)
                    )
                )
            )
            stay_lines._sync_line_dates_from_booking()

    def write(self, vals):
        vals = dict(vals)
        # Readonly header dates on confirmed bookings are often posted as False on save.
        for field in ("check_in", "check_out"):
            if field in vals and not vals[field]:
                vals.pop(field)
        header_snapshots = {}
        header_fields_in_vals = any(
            field in vals for field in ("check_in", "check_out")
        )
        if header_fields_in_vals:
            for booking in self:
                header_snapshots[booking.id] = (
                    booking.check_in,
                    booking.check_out,
                )
        if not self.env.context.get("skip_protect_booking_stay") and header_fields_in_vals:
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
        if header_fields_in_vals:
            bookings_to_sync = self.filtered(
                lambda booking: header_snapshots.get(booking.id)
                != (booking.check_in, booking.check_out)
            )
            if bookings_to_sync:
                bookings_to_sync.with_context(
                    skip_sync_folio_line_dates=True
                )._sync_folio_line_dates()
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
        """Return one calendar event per folio line for the Availability Calendar."""
        if not self._is_hotel_extended_calendar():
            return []
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
                    and (
                        booking_line.product_id.is_room_type
                        or is_day_long_tour_product(booking_line.product_id)
                    )
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
        initial_bookings = self.filtered(lambda booking: booking.status_bar == "initial")
        for booking in initial_bookings:
            mismatch = booking._get_header_folio_date_mismatch_message()
            if mismatch:
                return self.env["wk.wizard.message"].genrated_message(
                    "<span class='text-danger' style='font-weight:bold;'>%s</span>"
                    % mismatch.replace("\n", "<br/>"),
                    name=_("Warning"),
                )

        self._ensure_booking_line_guests()
        self.validate_guest()
        if not self.env.context.get("bypass_checkin_checkout", False):
            self._check_validity_check_in_check_out_booking()

        if self.status_bar == "initial":
            stay_snapshots = self._snapshot_booking_stay_dates()
            if not self.booking_line_ids:
                raise ValidationError(
                    _("Please add rooms or day-long tours for booking confirmation!")
                )
            room_lines = self.booking_line_ids.filtered(
                lambda line: line.product_id and line.product_id.is_room_type
            )
            tour_lines = self.booking_line_ids.filtered(
                lambda line: is_day_long_tour_product(line.product_id)
            )
            if not room_lines and not tour_lines:
                raise ValidationError(
                    _("Please add rooms or day-long tours for booking confirmation!")
                )
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
            confirm_lines = room_lines or tour_lines
            if not all(
                [
                    line.guest_info_ids.ids
                    or line.adult_count
                    or line.child_count
                    or line.infant_count
                    for line in confirm_lines
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

                        sync_vals = {
                            "tax_id": line.tax_ids,
                            "product_uom_qty": line.booking_days or self.booking_days or 1,
                            "price_unit": line.price,
                            "discount": line.discount,
                        }
                        if line._can_sync_product_id_to_sale_line(sale_order_line):
                            sync_vals["product_id"] = line.product_id.id
                        sale_order_line.with_context(
                            bypass_for_exchange_room=True
                        ).write(sync_vals)

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
