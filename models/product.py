# -*- coding: utf-8 -*-

from datetime import datetime

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

from .day_tour_utils import (
    DAY_TOUR_ACTIVE_BOOKING_STATUSES,
    day_tour_line_guest_count,
)


class ProductTemplate(models.Model):
    _inherit = "product.template"

    is_bookable = fields.Boolean(
        string="Is Bookable",
        help="Products that can be reserved through hotel booking flows "
        "(rooms, day tours, and other capacity-based services).",
    )
    is_day_long_tour = fields.Boolean(
        string="Is Day-Long Tour",
        help="When enabled, each guest on a folio line reduces this tour's "
        "daily occupancy pool for the line's tour date.",
    )
    day_tour_max_occupancy = fields.Integer(
        string="Tour Max Occupancy",
        help="Maximum total guests allowed for this day-long tour on a single "
        "calendar day at one hotel.",
    )

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

    def _get_day_tour_booking_line_domain(self, tour_date, hotel_id):
        self.ensure_one()
        return [
            ("product_id.product_tmpl_id", "=", self.id),
            ("tour_date", "=", tour_date),
            ("booking_id.status_bar", "in", list(DAY_TOUR_ACTIVE_BOOKING_STATUSES)),
            ("booking_id.hotel_id", "=", hotel_id),
        ]

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
        lines = self.env["hotel.booking.line"].search(
            self._get_day_tour_booking_line_domain(tour_date, hotel_id)
        )
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
        """Booked guests for dashboard — scoped to hotel when product has one."""
        self.ensure_one()
        if not self.is_day_long_tour or not tour_date:
            return 0
        if hotel_id:
            return self.get_day_tour_booked_guests(tour_date, hotel_id)
        lines = self.env["hotel.booking.line"].search(
            [
                ("product_id.product_tmpl_id", "=", self.id),
                ("tour_date", "=", tour_date),
                ("booking_id.status_bar", "in", list(DAY_TOUR_ACTIVE_BOOKING_STATUSES)),
            ]
        )
        return sum(day_tour_line_guest_count(line) for line in lines)

    def get_day_tour_dashboard_occupancy(self, tour_date):
        """Occupancy summary for the Front Desk Dashboard."""
        self.ensure_one()
        max_occupancy = self.day_tour_max_occupancy or 0
        hotel_id = self.hotel_id.id if self.hotel_id else None
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
        """Folio lines for this tour that consume dashboard calendar capacity."""
        self.ensure_one()
        return self.env["hotel.booking.line"].search(
            [
                ("product_id.product_tmpl_id", "=", self.id),
                ("tour_date", "!=", False),
                ("booking_id.status_bar", "in", list(DAY_TOUR_ACTIVE_BOOKING_STATUSES)),
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
            occupancy = {
                "day_tour_max_occupancy": tour_record["day_tour_max_occupancy"],
                "day_tour_booked_guests": 0,
                "day_tour_remaining_occupancy": 0,
                "total_room_count": tour_record["day_tour_max_occupancy"],
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
        variants = self.env["product.product"].search(
            [
                ("is_bookable", "=", True),
                ("active", "=", True),
                ("product_tmpl_id", "=", self.id),
            ]
        )
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
