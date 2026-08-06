# -*- coding: utf-8 -*-

from datetime import datetime

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

from .day_tour_utils import (
    CONFIRMED_BOOKING_STATUSES,
    DAY_TOUR_ACTIVE_BOOKING_STATUSES,
    day_tour_line_calendar_date,
    day_tour_line_guest_count,
)
from .room_inventory_utils import (
    get_available_slot_count,
    search_capacity_lines,
)


class ProductTemplate(models.Model):
    _inherit = "product.template"

    is_day_long_tour = fields.Boolean(
        string="Is Day-Long Tour",
        help="When enabled, each guest on a folio line reduces this tour's "
        "daily occupancy pool for the line's check-in date.",
    )
    day_tour_max_occupancy = fields.Integer(
        string="Tour Max Occupancy",
        help="Maximum total guests allowed for this day-long tour on a single "
        "calendar day at one hotel.",
    )
    room_count = fields.Integer(
        string="Number of Rooms",
        default=1,
        help="How many room slots can be sold for this room type per night, "
        "independent of the number of numbered variants.",
    )
    physical_room_count = fields.Integer(
        string="Physical Rooms",
        compute="_compute_physical_room_count",
        store=True,
        help="Count of active physical room records linked to this room type.",
    )
    physical_room_ids = fields.One2many(
        comodel_name="hotel.room",
        inverse_name="room_type_id",
        string="Physical Rooms",
    )
    billing_variant_id = fields.Many2one(
        comodel_name="product.product",
        string="Billing Variant",
        readonly=True,
        copy=False,
        help="Product variant used on sale orders for this room type.",
    )
    placeholder_variant_id = fields.Many2one(
        comodel_name="product.product",
        string="Placeholder Variant",
        readonly=True,
        copy=False,
        help="Variant used at booking time before a numbered room is assigned.",
    )
    room_count_mismatch_warning = fields.Char(
        compute="_compute_room_count_mismatch_warning",
    )

    @api.model
    def _get_day_long_tour_category(self):
        """Return the existing All / Day-Long product category, if configured."""
        return self.env["product.category"].search(
            [
                ("name", "in", ["Day-Long", "Day Long"]),
                ("parent_id.name", "=", "All"),
            ],
            limit=1,
        )

    @api.model
    def _get_night_stay_category(self):
        """Return the existing All / Night Stay product category, if configured."""
        return self.env["product.category"].search(
            [
                ("name", "=", "Night Stay"),
                ("parent_id.name", "=", "All"),
            ],
            limit=1,
        )

    def _assign_hotel_product_category_vals(self, vals):
        if vals.get("is_day_long_tour"):
            category = self._get_day_long_tour_category()
        elif vals.get("is_room_type"):
            category = self._get_night_stay_category()
        else:
            return vals
        if category:
            vals = dict(vals)
            vals["categ_id"] = category.id
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        prepared_vals_list = [
            self._assign_hotel_product_category_vals(vals) for vals in vals_list
        ]
        return super().create(prepared_vals_list)

    def write(self, vals):
        vals = self._assign_hotel_product_category_vals(vals)
        return super().write(vals)

    @api.onchange("is_room_type")
    def _onchange_is_room_type(self):
        if self.is_room_type:
            self.is_day_long_tour = False
            category = self._get_night_stay_category()
            if category:
                self.categ_id = category

    @api.onchange("is_day_long_tour")
    def _onchange_is_day_long_tour(self):
        if self.is_day_long_tour:
            self.is_room_type = False
            category = self._get_day_long_tour_category()
            if category:
                self.categ_id = category

    @api.depends("physical_room_ids", "physical_room_ids.active", "is_room_type")
    def _compute_physical_room_count(self):
        for template in self:
            if not template.is_room_type:
                template.physical_room_count = 0
                continue
            template.physical_room_count = len(
                template.physical_room_ids.filtered("active")
            )

    def action_view_physical_rooms(self):
        self.ensure_one()
        return {
            "name": _("Physical Rooms"),
            "type": "ir.actions.act_window",
            "res_model": "hotel.room",
            "view_mode": "list,form",
            "domain": [("room_type_id", "=", self.id)],
            "context": {"default_room_type_id": self.id},
        }

    @api.depends("room_count", "physical_room_count", "is_room_type")
    def _compute_room_count_mismatch_warning(self):
        for template in self:
            if not template.is_room_type:
                template.room_count_mismatch_warning = False
                continue
            physical = template.physical_room_count or 0
            configured = template.room_count or 0
            if physical and physical != configured:
                template.room_count_mismatch_warning = _(
                    "Physical room count (%(physical)s) does not match "
                    "configured room count (%(configured)s)."
                ) % {"physical": physical, "configured": configured}
            else:
                template.room_count_mismatch_warning = False

    def _get_numbered_variants(self):
        self.ensure_one()
        return self.product_variant_ids.filtered(
            lambda variant: variant.active and not variant.is_room_placeholder
        )

    def get_billing_variant(self):
        """Return the product variant used for sale orders and folio billing."""
        self.ensure_one()
        if self.billing_variant_id:
            return self.billing_variant_id
        variant = self._find_default_variant_slot()
        if not variant:
            variant = self.product_variant_ids.filtered("active")[:1]
        if variant:
            self.billing_variant_id = variant.id
        return variant

    def _find_default_variant_slot(self):
        """Return the single no-attribute variant slot for this template, if any."""
        self.ensure_one()
        Product = self.env["product.product"].with_context(active_test=False)
        return Product.search(
            [
                ("product_tmpl_id", "=", self.id),
                ("combination_indices", "in", ["", False]),
            ],
            limit=1,
        )

    def _placeholder_label(self):
        return _("Assign at check-in")

    def _variant_is_assign_at_checkin(self, variant):
        label = self._placeholder_label()
        for ptav in variant.product_template_attribute_value_ids:
            if (
                ptav.name == label
                or ptav.product_attribute_value_id.name == label
            ):
                return True
        return False

    def _clear_invalid_placeholder_flags(self):
        """Unmark numbered variants that were incorrectly flagged as placeholders."""
        self.ensure_one()
        attribute_lines = (
            self.valid_product_template_attribute_line_ids._without_no_variant_attributes()
        )
        numbered = self._get_numbered_variants()
        invalid = self.env["product.product"]
        for variant in self.product_variant_ids.filtered("is_room_placeholder"):
            if attribute_lines and not self._variant_is_assign_at_checkin(variant):
                invalid |= variant
            elif numbered and not attribute_lines:
                invalid |= variant
        if invalid:
            invalid.write({"is_room_placeholder": False})
            if self.placeholder_variant_id in invalid:
                self.placeholder_variant_id = False

    def _bootstrap_placeholder_attribute_line(self):
        """Add a room-number attribute when numbered variants exist without one."""
        self.ensure_one()
        lines = (
            self.valid_product_template_attribute_line_ids._without_no_variant_attributes()
        )
        if lines:
            return lines[0]

        variants = self.product_variant_ids.filtered("active")
        if len(variants) <= 1:
            return self.env["product.template.attribute.line"]

        label = self._placeholder_label()
        attr = self.env["product.attribute"].search(
            [("name", "=", _("Room Number"))],
            limit=1,
        )
        if not attr:
            attr = self.env["product.attribute"].create(
                {
                    "name": _("Room Number"),
                    "create_variant": "always",
                }
            )

        ProductAttributeValue = self.env["product.attribute.value"]
        value_names = []
        for variant in variants:
            name = (variant.name or "").strip()
            if name and name not in value_names and name != label:
                value_names.append(name)
        if label not in value_names:
            value_names.append(label)

        value_ids = []
        for name in value_names:
            attr_value = ProductAttributeValue.search(
                [
                    ("attribute_id", "=", attr.id),
                    ("name", "=", name),
                ],
                limit=1,
            )
            if not attr_value:
                attr_value = ProductAttributeValue.create(
                    {
                        "attribute_id": attr.id,
                        "name": name,
                    }
                )
            value_ids.append(attr_value.id)

        return self.env["product.template.attribute.line"].create(
            {
                "product_tmpl_id": self.id,
                "attribute_id": attr.id,
                "value_ids": [(6, 0, value_ids)],
            }
        )

    def _get_picker_placeholder_variant(self):
        """Placeholder variant for folio pickers; never raises."""
        self.ensure_one()
        if not self.is_room_type:
            return self.env["product.product"]
        try:
            return self._find_placeholder_variant(create_if_missing=True)
        except ValidationError:
            return self._find_placeholder_variant(create_if_missing=False)

    def _find_placeholder_variant(self, create_if_missing=False):
        """Find or optionally create the placeholder variant for a room type."""
        self.ensure_one()
        if not self.is_room_type:
            return self.env["product.product"]

        self._clear_invalid_placeholder_flags()

        Product = self.env["product.product"]
        if (
            self.placeholder_variant_id
            and self.placeholder_variant_id.is_room_placeholder
        ):
            return self.placeholder_variant_id

        attribute_lines = (
            self.valid_product_template_attribute_line_ids._without_no_variant_attributes()
        )
        existing = Product.search(
            [
                ("product_tmpl_id", "=", self.id),
                ("is_room_placeholder", "=", True),
            ],
            limit=1,
        )
        if existing:
            if attribute_lines and not self._variant_is_assign_at_checkin(existing):
                existing.is_room_placeholder = False
                existing = Product
            else:
                self.placeholder_variant_id = existing.id
                return existing

        numbered = self._get_numbered_variants()
        variant = Product

        if attribute_lines:
            if create_if_missing:
                variant = self._get_or_create_placeholder_attribute_variant()
            else:
                variant = Product.search(
                    [
                        ("product_tmpl_id", "=", self.id),
                        ("is_room_placeholder", "=", True),
                    ],
                    limit=1,
                )
        elif numbered and create_if_missing:
            if not attribute_lines:
                self._bootstrap_placeholder_attribute_line()
                attribute_lines = (
                    self.valid_product_template_attribute_line_ids._without_no_variant_attributes()
                )
            if attribute_lines:
                variant = self._get_or_create_placeholder_attribute_variant()
            if not variant and len(numbered) == 1:
                variant = numbered[:1]
                variant.is_room_placeholder = True
            elif not variant:
                variant = self._get_or_create_placeholder_attribute_variant()
        elif numbered and not create_if_missing:
            variant = Product.search(
                [
                    ("product_tmpl_id", "=", self.id),
                    ("is_room_placeholder", "=", True),
                ],
                limit=1,
            )
        elif not numbered:
            variant = self._find_default_variant_slot()
            if not variant:
                variant = self.product_variant_ids.filtered("active")[:1]
            if not variant:
                if create_if_missing:
                    variant = Product.create(
                        {
                            "product_tmpl_id": self.id,
                            "is_room_placeholder": True,
                        }
                    )
            elif create_if_missing:
                variant.is_room_placeholder = True

        if not variant and create_if_missing:
            fallback = self.get_billing_variant() or numbered[:1]
            if fallback:
                variant = fallback
                if not variant.is_room_placeholder:
                    variant.is_room_placeholder = True

        if not variant:
            return Product

        if create_if_missing:
            self.placeholder_variant_id = variant.id
        return variant

    def get_placeholder_variant(self):
        """Return or create the placeholder variant for this room type."""
        self.ensure_one()
        variant = self._find_placeholder_variant(create_if_missing=True)
        if not variant:
            variant = self.get_billing_variant()
        if not variant:
            variant = self.product_variant_ids.filtered("active")[:1]
        if not variant:
            raise ValidationError(
                _("Could not create a placeholder variant for '%(room_type)s'.")
                % {"room_type": self.display_name}
            )
        if not self.placeholder_variant_id:
            self.placeholder_variant_id = variant.id
        return variant

    def _get_placeholder_attribute_line(self, lines=None):
        """Attribute line that carries numbered room values (or the main one)."""
        self.ensure_one()
        if lines is None:
            lines = (
                self.valid_product_template_attribute_line_ids._without_no_variant_attributes()
            )
        if not lines:
            return self.env["product.template.attribute.line"]
        preferred = lines.filtered(
            lambda ptal: ptal.attribute_id.name
            in (_("Room Number"), "Room Number", "Room")
        )
        if preferred:
            return preferred.sorted(
                key=lambda ptal: len(ptal.product_template_value_ids),
                reverse=True,
            )[:1]
        return lines.sorted(
            key=lambda ptal: len(ptal.product_template_value_ids),
            reverse=True,
        )[:1]

    def _placeholder_variant_combination(self, placeholder_ptav):
        """Full variant combination including the placeholder value."""
        self.ensure_one()
        lines = (
            self.valid_product_template_attribute_line_ids._without_no_variant_attributes()
        )
        combination = placeholder_ptav
        placeholder_line = placeholder_ptav.attribute_line_id
        for line in lines:
            if line.id == placeholder_line.id:
                continue
            active_ptavs = line.product_template_value_ids._only_active()
            if active_ptavs:
                combination |= active_ptavs[0]
        return combination

    def _get_or_create_placeholder_attribute_variant(self):
        """Add an 'Assign at check-in' attribute value and return its variant."""
        self.ensure_one()
        placeholder_label = self._placeholder_label()
        lines = (
            self.valid_product_template_attribute_line_ids._without_no_variant_attributes()
        )
        if not lines:
            return self.env["product.product"]

        existing = self.env["product.product"].search(
            [
                ("product_tmpl_id", "=", self.id),
                ("is_room_placeholder", "=", True),
            ],
            limit=1,
        )
        if existing:
            return existing

        line = self._get_placeholder_attribute_line(lines)
        ProductAttributeValue = self.env["product.attribute.value"]
        attr_value = ProductAttributeValue.search(
            [
                ("attribute_id", "=", line.attribute_id.id),
                ("name", "=", placeholder_label),
            ],
            limit=1,
        )
        if not attr_value:
            attr_value = ProductAttributeValue.create(
                {
                    "attribute_id": line.attribute_id.id,
                    "name": placeholder_label,
                }
            )

        if attr_value not in line.value_ids:
            line.write({"value_ids": [(4, attr_value.id)]})
            self._create_variant_ids()

        PTAV = self.env["product.template.attribute.value"]
        ptav = PTAV.search(
            [
                ("attribute_line_id", "=", line.id),
                ("product_attribute_value_id", "=", attr_value.id),
            ],
            limit=1,
        )
        if not ptav:
            self._create_variant_ids()
            ptav = PTAV.search(
                [
                    ("attribute_line_id", "=", line.id),
                    ("product_attribute_value_id", "=", attr_value.id),
                ],
                limit=1,
            )
        if ptav and not ptav.ptav_active:
            ptav.write({"ptav_active": True})

        if not ptav:
            return self.env["product.product"]

        combination = self._placeholder_variant_combination(ptav)
        variant = self._get_variant_for_combination(combination)
        if not variant:
            if self.has_dynamic_attributes():
                variant = self._create_product_variant(
                    combination, log_warning=True
                )
            else:
                self._create_variant_ids()
                variant = self._get_variant_for_combination(combination)

        if variant:
            variant.is_room_placeholder = True
        return variant

    @api.constrains("is_room_type", "room_count")
    def _check_room_count(self):
        for template in self.filtered("is_room_type"):
            if not template.room_count or template.room_count < 1:
                raise ValidationError(
                    _("Please set Number of Rooms to at least 1 for room types.")
                )

    @api.onchange("is_room_type", "room_count", "product_variant_ids")
    def _onchange_room_count_warning(self):
        if self.is_room_type and self.room_count_mismatch_warning:
            return {
                "warning": {
                    "title": _("Room count mismatch"),
                    "message": self.room_count_mismatch_warning,
                }
            }

    def get_booked_slot_count_for_day(
        self, selected_date, hotel_id=None, exclude_line_ids=None
    ):
        self.ensure_one()
        day_start = datetime.combine(selected_date, datetime.min.time())
        day_end = datetime.combine(selected_date, datetime.max.time())
        return len(
            search_capacity_lines(
                self.env,
                self,
                day_start,
                day_end,
                hotel_id=hotel_id,
                exclude_line_ids=exclude_line_ids,
            )
        )

    def has_room_type_capacity(
        self,
        date_from,
        date_to,
        hotel_id=None,
        exclude_line_ids=None,
        qty=1,
    ):
        self.ensure_one()
        available = get_available_slot_count(
            self,
            date_from,
            date_to,
            hotel_id=hotel_id,
            exclude_line_ids=exclude_line_ids,
        )
        return available >= qty

    def assert_room_type_capacity(
        self,
        date_from,
        date_to,
        qty=1,
        hotel_id=None,
        exclude_line_ids=None,
    ):
        from .room_inventory_utils import assert_room_type_capacity as _assert

        self.ensure_one()
        _assert(
            self,
            date_from,
            date_to,
            qty=qty,
            hotel_id=hotel_id,
            exclude_line_ids=exclude_line_ids,
        )

    @api.constrains(
        "is_day_long_tour",
        "is_room_type",
        "categ_id",
        "day_tour_max_occupancy",
    )
    def _check_day_long_tour_config(self):
        for template in self:
            if not template.is_day_long_tour:
                continue
            if template.is_room_type:
                raise ValidationError(
                    _("Day-long tours cannot be marked as room types.")
                )
            if not template.categ_id.is_bookable:
                raise ValidationError(
                    _("Day-long tours must belong to a bookable product category.")
                )
            if not template.day_tour_max_occupancy or template.day_tour_max_occupancy < 1:
                raise ValidationError(
                    _("Please set Max Occupancy to at least 1 for day-long tours.")
                )

    def _get_day_tour_booking_lines(self, tour_date, hotel_id, active_only=True):
        self.ensure_one()
        statuses = (
            DAY_TOUR_ACTIVE_BOOKING_STATUSES
            if active_only
            else CONFIRMED_BOOKING_STATUSES
        )
        domain = [
            ("product_id.product_tmpl_id", "=", self.id),
            ("check_in", "!=", False),
            ("booking_id.status_bar", "in", list(statuses)),
        ]
        if hotel_id:
            domain.append(("booking_id.hotel_id", "=", hotel_id))
        lines = self.env["hotel.booking.line"].search(domain)
        tour_day = fields.Date.to_date(tour_date)
        return lines.filtered(
            lambda line: day_tour_line_calendar_date(line) == tour_day
        )

    def get_day_tour_booked_guests(
        self,
        tour_date,
        hotel_id,
        exclude_booking_id=None,
        exclude_line_ids=None,
    ):
        """Guests already booked for this tour product on a date at a hotel."""
        self.ensure_one()
        if not self.is_day_long_tour or not tour_date or not hotel_id:
            return 0
        lines = self._get_day_tour_booking_lines(tour_date, hotel_id)
        if exclude_booking_id:
            lines = lines.filtered(
                lambda line: line.booking_id.id != exclude_booking_id
            )
        if exclude_line_ids:
            lines = lines.filtered(lambda line: line.id not in exclude_line_ids)
        return sum(day_tour_line_guest_count(line) for line in lines)

    def get_day_tour_remaining_occupancy(
        self,
        tour_date,
        hotel_id,
        exclude_booking_id=None,
        exclude_line_ids=None,
    ):
        self.ensure_one()
        max_occupancy = self.day_tour_max_occupancy or 0
        booked = self.get_day_tour_booked_guests(
            tour_date,
            hotel_id,
            exclude_booking_id=exclude_booking_id,
            exclude_line_ids=exclude_line_ids,
        )
        return max(max_occupancy - booked, 0)

    def _get_day_tour_booked_guests_for_dashboard(self, tour_date, hotel_id=None):
        """Confirmed guest count for this tour on a calendar day."""
        self.ensure_one()
        if not self.is_day_long_tour or not tour_date:
            return 0
        lines = self._get_day_tour_booking_lines(
            tour_date, hotel_id=False, active_only=False
        )
        if self.hotel_id:
            lines = lines.filtered(
                lambda line: line.booking_id.hotel_id == self.hotel_id
            )
        return sum(day_tour_line_guest_count(line) for line in lines)

    def get_day_tour_dashboard_occupancy(self, tour_date, hotel_id=None):
        """Occupancy summary for the Front Desk Dashboard (confirmed only)."""
        self.ensure_one()
        max_occupancy = self.day_tour_max_occupancy or 0
        booked_guests = self._get_day_tour_booked_guests_for_dashboard(
            tour_date, hotel_id=hotel_id
        )
        remaining = max(max_occupancy - booked_guests, 0)
        return {
            "is_day_long_tour": True,
            "day_tour_max_occupancy": max_occupancy,
            "day_tour_booked_guests": booked_guests,
            "day_tour_remaining_occupancy": remaining,
            "total_room_count": max_occupancy,
            "available_room_count": remaining,
            "booked_room_count": booked_guests,
        }

    def _get_day_tour_dashboard_booking_lines(self):
        """Confirmed folio lines for this tour shown on the dashboard calendar."""
        self.ensure_one()
        return self.env["hotel.booking.line"].search(
            [
                ("product_id.product_tmpl_id", "=", self.id),
                ("check_in", "!=", False),
                ("booking_id.status_bar", "in", list(CONFIRMED_BOOKING_STATUSES)),
            ]
        )

    def _get_day_tour_dashboard_booking_ids(self):
        self.ensure_one()
        return self._get_day_tour_dashboard_booking_lines().mapped("booking_id").ids

    def _fetch_day_tour_data_for_dashboard(self, **kwargs):
        selected_date = (
            str(kwargs["selected_date"].get("day"))
            + "/"
            + str(kwargs["selected_date"].get("month"))
            + "/"
            + str(kwargs["selected_date"].get("year"))
        )
        tour_date = datetime.strptime(selected_date, "%d/%m/%Y").date()
        tour_record = self.read(
            ["name", "is_day_long_tour", "day_tour_max_occupancy", "hotel_id"]
        )[0]
        if tour_date < fields.Date.today():
            max_occupancy = tour_record["day_tour_max_occupancy"]
            occupancy = {
                "day_tour_max_occupancy": max_occupancy,
                "day_tour_booked_guests": 0,
                "day_tour_remaining_occupancy": 0,
                "total_room_count": max_occupancy,
                "available_room_count": 0,
                "booked_room_count": 0,
            }
        else:
            occupancy = self.get_day_tour_dashboard_occupancy(tour_date)
        tour_record.update(occupancy)
        tour_record["room_variant_data"] = []
        return {
            "room_record": [tour_record],
            "b_ids": self._get_day_tour_dashboard_booking_ids(),
            "is_day_long_tour": True,
        }

    def fetch_data_for_room(self, **kwargs):
        """Include room counts or day-tour occupancy for the dashboard panel."""
        if self.is_day_long_tour:
            return self._fetch_day_tour_data_for_dashboard(**kwargs)
        result = super().fetch_data_for_room(**kwargs)
        if not result.get("room_record"):
            return result

        room_record = result["room_record"][0]
        if self.is_room_type:
            selected_date = (
                str(kwargs["selected_date"].get("day"))
                + "/"
                + str(kwargs["selected_date"].get("month"))
                + "/"
                + str(kwargs["selected_date"].get("year"))
            )
            tour_date = datetime.strptime(selected_date, "%d/%m/%Y").date()
            total_count = self.room_count or 0
            if tour_date < fields.Date.today():
                available_count = 0
            else:
                booked = self.get_booked_slot_count_for_day(
                    tour_date, hotel_id=self.hotel_id.id if self.hotel_id else None
                )
                available_count = max(total_count - booked, 0)
            placeholder = self.get_placeholder_variant()
            variant_data = []
            if available_count > 0 and placeholder:
                variant_data = placeholder.read(["display_name"])
            room_record.update(
                {
                    "total_room_count": total_count,
                    "available_room_count": available_count,
                    "booked_room_count": max(total_count - available_count, 0),
                    "room_variant_data": variant_data,
                }
            )
            return result

        variant_domain = [
            ("categ_id.is_bookable", "=", True),
            ("active", "=", True),
            ("product_tmpl_id", "=", self.id),
        ]
        variants = self.env["product.product"].search(variant_domain)
        total_count = len(variants)
        available_count = len(room_record.get("room_variant_data") or [])
        room_record.update(
            {
                "total_room_count": total_count,
                "available_room_count": available_count,
                "booked_room_count": max(total_count - available_count, 0),
            }
        )
        return result


class ProductProduct(models.Model):
    _inherit = "product.product"

    is_day_long_tour = fields.Boolean(
        related="product_tmpl_id.is_day_long_tour",
    )
    is_room_type = fields.Boolean(
        related="product_tmpl_id.is_room_type",
        store=True,
    )
    is_room_placeholder = fields.Boolean(
        string="Room Placeholder",
        help="Marks a variant used at booking time before a numbered room "
        "is assigned at check-in.",
        index=True,
    )
    physical_room_count = fields.Integer(
        related="product_tmpl_id.physical_room_count",
    )

    def action_view_physical_rooms(self):
        self.ensure_one()
        return self.product_tmpl_id.action_view_physical_rooms()
