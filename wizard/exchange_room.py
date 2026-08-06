# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class ExchangeRoom(models.TransientModel):
    _inherit = "exchange.room"

    available_physical_room_ids = fields.Many2many(
        comodel_name="hotel.room",
        string="Available Physical Rooms",
    )
    exchange_physical_room = fields.Many2one(
        comodel_name="hotel.room",
        string="Exchange Room",
        domain="[('id', 'in', available_physical_room_ids)]",
    )

    @api.onchange("booking_line_id")
    def booking_line_compute(self):
        booking_line = self.env["hotel.booking.line"].browse(
            self._context.get("active_ids")
        )
        if not booking_line:
            return
        self.booking_line_id = booking_line
        self.exchange_physical_room = booking_line.assigned_room_id
        self.available_physical_room_ids = booking_line._get_assignable_rooms()

    def action_exchange_room(self):
        if self.exchange_physical_room:
            booking_line = self.env["hotel.booking.line"].browse(
                self._context.get("active_ids")
            )
            booking_line.assign_physical_room(self.exchange_physical_room)
        return super().action_exchange_room()
