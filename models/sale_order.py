# -*- coding: utf-8 -*-

from odoo import api, models
from odoo.addons.sale.wizard.sale_make_invoice_advance import (
    SaleAdvancePaymentInv as SaleAdvancePaymentInvSale,
)

from .checkin_utils import truncate_minutes_seconds


class SaleOrder(models.Model):
    _inherit = "sale.order"

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        for field_name in ("hotel_check_in", "hotel_check_out"):
            if field_name in res and res.get(field_name):
                res[field_name] = truncate_minutes_seconds(res[field_name])
        return res

    def _prepare_invoice(self):
        res = super()._prepare_invoice()
        if self.booking_id:
            res["hotel_booking_id"] = self.booking_id.id
        return res

    @api.onchange("hotel_id", "order_line")
    def _onchange_hotel_id_is_room_type(self):
        has_room_lines = any(
            line.product_id.is_room_type
            for line in self.order_line
            if line.product_id
        )
        self.is_room_type = bool(self.hotel_id or has_room_lines)

    @api.depends_context("lang")
    @api.depends(
        "order_line.price_subtotal",
        "currency_id",
        "company_id",
        "payment_term_id",
        "booking_id.hotel_service_lines.amount",
        "booking_id.hotel_service_lines.service_type",
        "paid_amount",
        "balance_amount",
        "payment_ids.amount",
        "amount_total",
    )
    def _compute_tax_totals(self):
        super()._compute_tax_totals()
        for order in self:
            tax_totals = order.tax_totals
            if not tax_totals:
                continue

            subtotals = [
                subtotal
                for subtotal in tax_totals.get("subtotals", [])
                if subtotal.get("name") not in ("Paid Amount", "Balance Amount")
            ]
            order.tax_totals = {
                **tax_totals,
                "subtotals": subtotals,
                "paid_amount_currency": order.paid_amount,
                "balance_amount_currency": order.balance_amount,
            }


class SaleAdvancePaymentInv(models.TransientModel):
    _inherit = "sale.advance.payment.inv"

    def create_invoices(self):
        # Bypass hotel_management_system block; call sale's implementation directly.
        return SaleAdvancePaymentInvSale.create_invoices(self)
