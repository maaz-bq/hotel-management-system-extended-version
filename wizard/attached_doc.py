# -*- coding: utf-8 -*-

from odoo import models


class CustomerDocument(models.TransientModel):
    _inherit = "customer.document"

    def confirm_doc(self):
        """Save documents, assign rooms, then complete allotment."""
        self.ensure_one()
        booking = self.booking_id or self.env["hotel.booking"].browse(
            self._context.get("active_ids")
        )
        data = [
            (0, 0, {"file": doc.file, "name": doc.req_document_id.name})
            for doc in self.add_docs_ids
        ]
        booking.write(
            {
                "docs_ids": data,
                "expected_check_out": booking.check_out,
            }
        )
        if booking._get_room_lines_needing_assignment():
            return booking._action_open_allot_room_wizard()
        booking._complete_allot()
        return {"type": "ir.actions.act_window_close"}
