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


class ProductTemplate(models.Model):
    _inherit = "product.template"

    is_bookable = fields.Boolean(
        string="Is Bookable",
        help="Products that can be reserved through hotel booking flows "
        "(rooms, day tours, and other bookable services).",
    )
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

    def _assign_day_long_tour_category_vals(self, vals):
        if not vals.get("is_day_long_tour"):
            return vals
        category = self._get_day_long_tour_category()
        if category:
            vals = dict(vals)
            vals["categ_id"] = category.id
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        prepared_vals_list = [
            self._assign_day_long_tour_category_vals(vals) for vals in vals_list
        ]
        return super().create(prepared_vals_list)

    def write(self, vals):
        vals = self._assign_day_long_tour_category_vals(vals)
        return super().write(vals)

    @api.onchange("is_room_type")
    def _onchange_is_room_type(self):
        if self.is_room_type:
            self.is_bookable = True
            self.is_day_long_tour = False

    @api.onchange("is_day_long_tour")
    def _onchange_is_day_long_tour(self):
        if self.is_day_long_tour:
            self.is_bookable = True
            self.is_room_type = False
            category = self._get_day_long_tour_category()
            if category:
                self.categ_id = category

    @api.constrains(
        "is_day_long_tour",
        "is_bookable",
        "is_room_type",
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
            if not template.is_bookable:
                raise ValidationError(
                    _("Day-long tours must be bookable products.")
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
            variant_domain = [
                ("is_room_type", "=", True),
                ("active", "=", True),
                ("product_tmpl_id", "=", self.id),
            ]
        else:
            variant_domain = [
                ("is_bookable", "=", True),
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

    is_bookable = fields.Boolean(
        related="product_tmpl_id.is_bookable",
        store=True,
    )
    is_day_long_tour = fields.Boolean(
        related="product_tmpl_id.is_day_long_tour",
    )
