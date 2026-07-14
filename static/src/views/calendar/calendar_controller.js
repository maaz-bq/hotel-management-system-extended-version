/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { CalendarController } from "@web/views/calendar/calendar_controller";

/**
 * Front Desk Dashboard product lists use is_bookable (not is_room_type).
 */
patch(CalendarController.prototype, {
    openViewonClick(ev) {
        if (
            $(ev.target).hasClass("total_available") ||
            $(ev.target).closest(".total_available").length
        ) {
            let roomTypeDomain = [["is_bookable", "=", true], ["active", "=", true]];
            let group = [];

            if ($(ev.target).hasClass("total_available_room")) {
                roomTypeDomain.push([
                    "id",
                    "not in",
                    this.model.meta.productData.booked_room_ids,
                ]);
                if (this.model.meta.productData.available_rooms) {
                    group.push("product_tmpl_id");
                }
            } else if ($(ev.target).hasClass("total_booked")) {
                roomTypeDomain.push([
                    "id",
                    "in",
                    this.model.meta.productData.booked_room_ids,
                ]);
                if (this.model.meta.productData.booked_room_ids.length) {
                    group.push("product_tmpl_id");
                }
            }
            this.actionService.doAction(
                {
                    type: "ir.actions.act_window",
                    res_model: "product.product",
                    name: "Rooms",
                    views: [[false, "list"], [false, "form"]],
                    domain: roomTypeDomain,
                    res_id: false,
                    context: {
                        group_by: group,
                        create: true,
                    },
                },
                {
                    additionalContext: this.props.context,
                }
            );
            return;
        }
        return super.openViewonClick(...arguments);
    },
});
