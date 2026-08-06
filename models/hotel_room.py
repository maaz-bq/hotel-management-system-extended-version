# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class HotelRoom(models.Model):
    _name = "hotel.room"
    _description = "Physical Room"
    _order = "room_type_id, name, id"

    name = fields.Char(string="Room Number", required=True)
    room_type_id = fields.Many2one(
        comodel_name="product.template",
        string="Room Type",
        required=True,
        ondelete="restrict",
        domain=[("is_room_type", "=", True)],
        index=True,
    )
    hotel_id = fields.Many2one(
        comodel_name="hotel.hotels",
        string="Hotel",
        related="room_type_id.hotel_id",
        store=True,
        readonly=True,
        index=True,
    )
    active = fields.Boolean(default=True)
    floor = fields.Char()
    notes = fields.Text()
    display_name = fields.Char(compute="_compute_display_name", store=True)

    _sql_constraints = [
        (
            "room_type_name_unique",
            "unique(room_type_id, name)",
            "Room number must be unique per room type.",
        ),
    ]

    @api.depends("name", "room_type_id", "room_type_id.name")
    def _compute_display_name(self):
        for room in self:
            if room.room_type_id:
                room.display_name = "%s — %s" % (
                    room.room_type_id.name,
                    room.name,
                )
            else:
                room.display_name = room.name or ""

    @api.constrains("room_type_id", "active")
    def _check_room_type_is_room(self):
        for room in self.filtered("room_type_id"):
            if not room.room_type_id.is_room_type:
                raise ValidationError(
                    _("Physical rooms must be linked to a room type product.")
                )
