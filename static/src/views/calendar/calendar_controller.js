/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { CalendarController } from "@web/views/calendar/calendar_controller";

/**
 * Front Desk Dashboard: is_bookable product lists, room counts, day-tour occupancy.
 */
patch(CalendarController.prototype, {
	openViewonClick(ev) {
		if (
			$(ev.target).hasClass("total_available") ||
			$(ev.target).closest(".total_available").length
		) {
			let roomTypeDomain = [
				["is_bookable", "=", true],
				["active", "=", true],
			];
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
					views: [
						[false, "list"],
						[false, "form"],
					],
					domain: roomTypeDomain,
					res_id: false,
					context: {
						group_by: group,
						create: true,
					},
				},
				{
					additionalContext: this.props.context,
				},
			);
			return;
		}
		return super.openViewonClick(...arguments);
	},

	_isDayLongTourSelected() {
		return Boolean(this.selected_room_is_day_long_tour);
	},

	_getRoomDataEntry(roomId) {
		const roomData = this.model.meta.productData.room_data || [];
		return roomData.find((room) => room.id === roomId);
	},

	_tourOccupancySummaryHtml(counts) {
		const maxOccupancy = counts.total ?? counts.day_tour_max_occupancy ?? 0;
		const booked = counts.booked ?? counts.day_tour_booked_guests ?? 0;
		const remaining = counts.available ?? counts.day_tour_remaining_occupancy ?? 0;
		return `
            <h3 class="pt-2">Tour occupancy</h3>
            <table class="table table-sm mb-2">
                <tbody>
                    <tr><td>MAX OCCUPANCY</td><td id="totalRoomCount">${maxOccupancy}</td></tr>
                    <tr><td>BOOKED GUESTS</td><td id="bookedRoomCount">${booked}</td></tr>
                    <tr><td>REMAINING</td><td id="availableRoomCount">${remaining}</td></tr>
                </tbody>
            </table>`;
	},

	_roomCountSummaryHtml(counts) {
		if (this._isDayLongTourSelected()) {
			return this._tourOccupancySummaryHtml(counts);
		}
		const total = counts.total ?? 0;
		const available = counts.available ?? 0;
		const booked = counts.booked ?? Math.max(total - available, 0);
		return `
            <h3 class="pt-2">Room availability</h3>
            <table class="table table-sm mb-2">
                <tbody>
                    <tr><td>TOTAL</td><td id="totalRoomCount">${total}</td></tr>
                    <tr><td>AVAILABLE</td><td id="availableRoomCount">${available}</td></tr>
                    <tr><td>BOOKED</td><td id="bookedRoomCount">${booked}</td></tr>
                </tbody>
            </table>`;
	},

	_updateRoomCountSummary(counts) {
		if (!$("#totalRoomCount").length) {
			return;
		}
		if (this._isDayLongTourSelected()) {
			$("#totalRoomCount").text(
				counts.total ?? counts.day_tour_max_occupancy ?? 0
			);
			$("#bookedRoomCount").text(
				counts.booked ?? counts.day_tour_booked_guests ?? 0
			);
			$("#availableRoomCount").text(
				counts.available ?? counts.day_tour_remaining_occupancy ?? 0
			);
			return;
		}
		$("#totalRoomCount").text(counts.total ?? 0);
		$("#availableRoomCount").text(counts.available ?? 0);
		$("#bookedRoomCount").text(counts.booked ?? 0);
	},

	update_available_rooms_data(booking_count) {
		super.update_available_rooms_data(...arguments);
		if (!this.selected_room) {
			return;
		}
		if (booking_count.is_day_long_tour) {
			this._updateRoomCountSummary({
				total: booking_count.day_tour_max_occupancy,
				available: booking_count.day_tour_remaining_occupancy,
				booked: booking_count.day_tour_booked_guests,
				day_tour_max_occupancy: booking_count.day_tour_max_occupancy,
				day_tour_booked_guests: booking_count.day_tour_booked_guests,
				day_tour_remaining_occupancy: booking_count.day_tour_remaining_occupancy,
			});
			return;
		}
		if (booking_count.total_room_count === undefined) {
			return;
		}
		this._updateRoomCountSummary({
			total: booking_count.total_room_count,
			available: booking_count.available_rooms,
			booked: booking_count.booked_room_count,
		});
	},

	_dayTourDetailHtml(roomDetail, counts) {
		const maxOccupancy = roomDetail.day_tour_max_occupancy ?? 0;
		const booked = counts.booked ?? roomDetail.day_tour_booked_guests ?? 0;
		const remaining = counts.available ?? roomDetail.day_tour_remaining_occupancy ?? 0;
		return `
            <tr><td>TOUR MAX OCCUPANCY</td><td>${maxOccupancy}</td></tr>
            <tr><td>BOOKED GUESTS</td><td>${booked}</td></tr>
            <tr><td>REMAINING</td><td>${remaining}</td></tr>`;
	},

	async fetchRoomTypeData(ev = null) {
		var room_id;
		if (ev) {
			room_id = $(ev.target).data("id");
		} else {
			room_id = this.model.meta.productData.room_data[0]?.id;
		}

		if (!room_id) {
			this.selected_room_is_day_long_tour = false;
			return super.fetchRoomTypeData(...arguments);
		}

		const roomEntry = this._getRoomDataEntry(room_id);
		this.selected_room_is_day_long_tour = Boolean(roomEntry?.is_day_long_tour);

		this.model.room_id = room_id;
		this.selected_room = room_id;

		const d = await this.orm.call(
			"product.template",
			"fetch_data_for_room",
			[[room_id]],
			{ selected_date: this.model.date.c },
		);
		this.model.room_book_ids = d.b_ids;
		const room_detail = d.room_record;
		const isDayTour = Boolean(room_detail[0]?.is_day_long_tour);
		this.selected_room_is_day_long_tour = isDayTour;

		const counts = {
			total: room_detail[0].total_room_count ?? room_detail[0].day_tour_max_occupancy ?? 0,
			available:
				room_detail[0].available_room_count ??
				room_detail[0].day_tour_remaining_occupancy ??
				0,
			booked:
				room_detail[0].booked_room_count ??
				room_detail[0].day_tour_booked_guests ??
				0,
			day_tour_max_occupancy: room_detail[0].day_tour_max_occupancy,
			day_tour_booked_guests: room_detail[0].day_tour_booked_guests,
			day_tour_remaining_occupancy: room_detail[0].day_tour_remaining_occupancy,
		};

		const detailRows = isDayTour
			? this._dayTourDetailHtml(room_detail[0], counts)
			: this.record_html(room_detail);
		const summaryHtml = this._roomCountSummaryHtml(counts);
		const variantsSection = isDayTour
			? ""
			: `<h3 class="pt-2">Available room variants</h3>
                <table id="availableRoomsTable">
                ${this.record_html(room_detail, true)}</table>`;

		$("#roomInformation")
			.html(
				`
                <div class="selected_room_container p-2" data-prod-tmplt=${room_id}>
                <h3>${room_detail[0].name}</h3>
                <table>
                ${isDayTour ? `<tbody>${detailRows}</tbody>` : detailRows}
                </table>
                ${summaryHtml}
                ${variantsSection}</div>`,
			)
			.show();

		$(".allBookingRoom")
			.find(".o_calendar_filter_item")
			.each(function () {
				if ($(this).attr("data-value") === room_id.toString()) {
					$(this).find("input").prop("checked", true).prop("disabled", false);
				} else {
					$(this).find("input").prop("checked", false).prop("disabled", true);
				}
			});
		await this.model.load();
	},
});
