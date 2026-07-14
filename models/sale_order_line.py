# -*- coding: utf-8 -*-

from odoo import api, fields, models

from .guest_member_utils import (
    extra_guest_charge,
    guest_counts_from_line,
    line_uses_guest_count_validation,
    sync_guest_info_records,
    total_guests,
    validate_guest_count,
)

_GUEST_COUNT_FIELDS = ("adult_guest", "children_guest", "infant_guest")


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    infant_guest = fields.Integer(string="Infant Guest", default=0)
    max_infants = fields.Integer(related="product_template_id.max_infants", string="Max Infants")

    def _get_member_partner(self):
        self.ensure_one()
        return self.order_id.partner_id

    def _sync_guest_info_from_counts(self):
        for line in self:
            adult, child, infant = guest_counts_from_line(line)
            partner = line._get_member_partner()
            partner_name = partner.name if partner else "Guest"
            sync_guest_info_records(
                self.env,
                adult=adult,
                child=child,
                infant=infant,
                partner_name=partner_name,
                sale_order_line_id=line.id,
                existing_guests=line.guest_info_ids,
            )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records.filtered(line_uses_guest_count_validation)._sync_guest_info_from_counts()
        return records

    def write(self, vals):
        res = super().write(vals)
        if any(field in vals for field in _GUEST_COUNT_FIELDS):
            self.filtered(line_uses_guest_count_validation)._sync_guest_info_from_counts()
        return res

    @api.depends(
        *_GUEST_COUNT_FIELDS,
        "guest_info_ids",
        "guest_info_ids.is_adult",
        "max_adult",
        "max_child",
        "max_occupancy",
        "product_id",
        "product_id.max_infants",
    )
    def _compute_warning(self):
        lines_with_counts = self.filtered(line_uses_guest_count_validation)
        super(SaleOrderLine, self - lines_with_counts)._compute_warning()
        for line in lines_with_counts:
            adult, child, infant = guest_counts_from_line(line)
            line.warning = validate_guest_count(line, adult, child, infant)

    @api.onchange("product_id")
    def _onchange_product_guest_defaults(self):
        for line in self:
            if (
                line.product_id
                and line.product_id.is_room_type
                and not line.adult_guest
                and not line.children_guest
                and not line.infant_guest
            ):
                line.adult_guest = 1

    @api.onchange(*_GUEST_COUNT_FIELDS, "guest_info_ids", "product_id")
    def update_extra_price(self):
        for line in self:
            adult, child, infant = guest_counts_from_line(line)
            guests = total_guests(adult, child, infant) or len(line.guest_info_ids)
            if (
                not guests
                or not line.product_id
                or not line.order_id.pricelist_id
            ):
                continue
            extra_cost = extra_guest_charge(line, guests)
            base_price = line.order_id.pricelist_id._get_product_price(
                line.product_id, line.product_uom_qty
            )
            line.price_unit = base_price + extra_cost
