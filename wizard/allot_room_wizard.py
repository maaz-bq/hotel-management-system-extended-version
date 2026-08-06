# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class HotelBookingAllotRoomWizard(models.TransientModel):
    _name = "hotel.booking.allot.room.wizard"
    _description = "Assign physical rooms before allotment"

    booking_id = fields.Many2one(
        comodel_name="hotel.booking",
        required=True,
        readonly=True,
    )
    line_ids = fields.One2many(
        comodel_name="hotel.booking.allot.room.wizard.line",
        inverse_name="wizard_id",
        string="Room Lines",
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        booking = self.env["hotel.booking"].browse(
            self.env.context.get("default_booking_id")
            or self.env.context.get("active_id")
        )
        if not booking:
            return res
        res["booking_id"] = booking.id
        line_vals = []
        for booking_line in booking._get_room_lines_for_allot_assignment():
            line_vals.append(
                (
                    0,
                    0,
                    {
                        "booking_line_id": booking_line.id,
                        "assigned_room_id": booking_line.assigned_room_id.id,
                    },
                )
            )
        res["line_ids"] = line_vals
        return res

    def action_confirm(self):
        self.ensure_one()
        wizard_lines = self.line_ids.filtered("booking_line_id")
        if not wizard_lines:
            raise ValidationError(
                _("No room lines were found to allot. Please close and try again.")
            )
        for wizard_line in wizard_lines:
            booking_line = wizard_line.booking_line_id
            if not wizard_line.assigned_room_id:
                room_type = booking_line.room_type_id or (
                    booking_line.product_id.product_tmpl_id
                    if booking_line.product_id
                    else False
                )
                raise ValidationError(
                    _("Please assign a room number for '%(room_type)s'.")
                    % {"room_type": room_type.display_name if room_type else _("Room")}
                )
            booking_line.assign_physical_room(wizard_line.assigned_room_id)
        self.booking_id._complete_allot()
        return {"type": "ir.actions.act_window_close"}


class HotelBookingAllotRoomWizardLine(models.TransientModel):
    _name = "hotel.booking.allot.room.wizard.line"
    _description = "Room line to assign during allotment"

    wizard_id = fields.Many2one(
        comodel_name="hotel.booking.allot.room.wizard",
        required=True,
        ondelete="cascade",
    )
    booking_line_id = fields.Many2one(
        comodel_name="hotel.booking.line",
        required=True,
        ondelete="cascade",
    )
    room_type_id = fields.Many2one(
        related="booking_line_id.room_type_id",
        string="Room Type",
    )
    current_room_id = fields.Many2one(
        related="booking_line_id.assigned_room_id",
        string="Current Room",
    )
    check_in = fields.Datetime(related="booking_line_id.check_in")
    check_out = fields.Datetime(related="booking_line_id.check_out")
    available_room_ids = fields.Many2many(
        comodel_name="hotel.room",
        compute="_compute_available_room_ids",
    )
    assigned_room_id = fields.Many2one(
        comodel_name="hotel.room",
        string="Assign Room",
        domain="[('id', 'in', available_room_ids)]",
    )

    @api.depends("booking_line_id", "booking_line_id.check_in", "booking_line_id.check_out")
    def _compute_available_room_ids(self):
        for line in self:
            if line.booking_line_id:
                line.available_room_ids = line.booking_line_id._get_assignable_rooms()
            else:
                line.available_room_ids = [(5, 0, 0)]
