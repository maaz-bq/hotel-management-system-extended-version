# -*- coding: utf-8 -*-

from datetime import datetime, timedelta

from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestRoomInventory(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].create({"name": "Room Inventory Guest"})
        cls.hotel_partner = cls.env["res.partner"].create({"name": "Test Resort Hotel"})
        cls.hotel = cls.env["hotel.hotels"].create(
            {"name": "Test Resort", "partner_id": cls.hotel_partner.id}
        )
        cls.night_stay_categ = cls.env["product.category"].create(
            {"name": "Night Stay Test", "is_bookable": True}
        )
        cls.day_tour_categ = cls.env["product.category"].create(
            {"name": "Day-Long Test", "is_bookable": True}
        )
        cls.room_template = cls.env["product.template"].create(
            {
                "name": "Deluxe Test",
                "is_room_type": True,
                "categ_id": cls.night_stay_categ.id,
                "hotel_id": cls.hotel.id,
                "room_count": 3,
                "max_adult": 2,
                "list_price": 100.0,
            }
        )
        cls.billing_variant = cls.room_template.get_billing_variant()
        cls.physical_room_101 = cls.env["hotel.room"].create(
            {"name": "101", "room_type_id": cls.room_template.id}
        )
        cls.physical_room_102 = cls.env["hotel.room"].create(
            {"name": "102", "room_type_id": cls.room_template.id}
        )
        cls.physical_room_103 = cls.env["hotel.room"].create(
            {"name": "103", "room_type_id": cls.room_template.id}
        )
        cls.placeholder = cls.room_template.get_placeholder_variant()
        cls.tour_template = cls.env["product.template"].create(
            {
                "name": "Island Tour",
                "is_day_long_tour": True,
                "day_tour_max_occupancy": 10,
                "categ_id": cls.day_tour_categ.id,
                "hotel_id": cls.hotel.id,
                "list_price": 50.0,
            }
        )
        cls.tour_product = cls.tour_template.product_variant_id
        cls.check_in = datetime.now().replace(hour=15, minute=0, second=0, microsecond=0)
        cls.check_out = cls.check_in + timedelta(days=2)

    def _create_booking(self, partner=None):
        return self.env["hotel.booking"].create(
            {
                "partner_id": (partner or self.partner).id,
                "hotel_id": self.hotel.id,
                "check_in": self.check_in,
                "check_out": self.check_out,
            }
        )

    def _create_room_line(self, booking, room_type=None):
        room_type = room_type or self.room_template
        return self.env["hotel.booking.line"].create(
            {
                "booking_id": booking.id,
                "room_type_id": room_type.id,
                "check_in": self.check_in,
                "check_out": self.check_out,
                "adult_count": 1,
            }
        )

    def _confirm_booking(self, booking):
        booking._ensure_booking_line_guests()
        booking.status_bar = "confirm"

    def test_physical_room_count_matches_hotel_rooms(self):
        self.assertEqual(self.room_template.physical_room_count, 3)

    def test_folio_picker_shows_room_types(self):
        booking = self._create_booking()
        type_ids = booking._get_folio_room_type_ids()
        self.assertIn(self.room_template.id, type_ids)

    def test_folio_picker_includes_rooms_and_services(self):
        booking = self._create_booking()
        product_ids = booking._get_folio_available_product_ids()
        self.assertIn(self.placeholder.id, product_ids)
        self.assertIn(self.tour_product.id, product_ids)

    def test_folio_line_syncs_billing_product(self):
        booking = self._create_booking()
        line = self._create_room_line(booking)
        self.assertEqual(line.room_type_id, self.room_template)
        self.assertEqual(line.product_id, self.billing_variant)

    def test_physical_room_exclusivity(self):
        booking_a = self._create_booking()
        line_a = self._create_room_line(booking_a)
        self._confirm_booking(booking_a)
        line_a.assign_physical_room(self.physical_room_101)
        booking_a._complete_allot()

        booking_b = self._create_booking()
        line_b = self._create_room_line(booking_b)
        self._confirm_booking(booking_b)
        with self.assertRaises(ValidationError):
            line_b.assign_physical_room(self.physical_room_101)

    def test_allot_sets_assigned_room_without_changing_product(self):
        booking = self._create_booking()
        line = self._create_room_line(booking)
        self._confirm_booking(booking)
        product_before = line.product_id
        line.assign_physical_room(self.physical_room_102)
        self.assertEqual(line.assigned_room_id, self.physical_room_102)
        self.assertEqual(line.product_id, product_before)

    def test_assignable_rooms_excludes_booked_room(self):
        booking_a = self._create_booking()
        line_a = self._create_room_line(booking_a)
        self._confirm_booking(booking_a)
        line_a.assign_physical_room(self.physical_room_103)
        booking_a._complete_allot()

        booking_b = self._create_booking()
        line_b = self._create_room_line(booking_b)
        assignable = line_b._get_assignable_rooms()
        self.assertIn(self.physical_room_101, assignable)
        self.assertIn(self.physical_room_102, assignable)
        self.assertNotIn(self.physical_room_103, assignable)

    def test_confirm_blocks_when_room_count_exceeded(self):
        for _ in range(3):
            booking = self._create_booking()
            self._create_room_line(booking)
            self._confirm_booking(booking)

        fourth = self._create_booking()
        self._create_room_line(fourth)
        conflict = fourth.check_selected_rooms_availability(
            fourth.check_in, fourth.check_out
        )
        self.assertFalse(conflict["available"])
