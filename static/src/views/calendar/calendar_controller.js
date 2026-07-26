/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { CalendarController } from "@web/views/calendar/calendar_controller";
import { CalendarModel } from "@web/views/calendar/calendar_model";
import { CalendarCommonRenderer } from "@web/views/calendar/calendar_common/calendar_common_renderer";
import { CalendarCommonPopover } from "@web/views/calendar/calendar_common/calendar_common_popover";

function isHotelDashboardCalendar(model) {
	return model.resModel === "hotel.booking" && Boolean(model.meta?.productData);
}

function isDashboardFolioLineEvent(model, recordOrId) {
	if (!isHotelDashboardCalendar(model)) {
		return false;
	}
	if (typeof recordOrId === "number") {
		return recordOrId < 0;
	}
	return Boolean(recordOrId?.rawRecord?.dashboard_booking_id);
}

function getDashboardFolioLineId(recordOrId) {
	if (typeof recordOrId === "number") {
		return recordOrId < 0 ? -recordOrId : null;
	}
	if (recordOrId?.rawRecord?.dashboard_line_id) {
		return recordOrId.rawRecord.dashboard_line_id;
	}
	if (recordOrId?.id < 0) {
		return -recordOrId.id;
	}
	return null;
}

/**
 * Front Desk Dashboard: folio-line calendar events, bookable product panel.
 */
patch(CalendarModel.prototype, {
	get canEdit() {
		if (isHotelDashboardCalendar(this)) {
			return false;
		}
		return (
			this.meta.canEdit &&
			!this.meta.fields[this.meta.fieldMapping.date_start].readonly
		);
	},

	async unlinkRecord(recordId) {
		if (isDashboardFolioLineEvent(this, recordId)) {
			const lineId = getDashboardFolioLineId(recordId);
			if (lineId) {
				await this.orm.unlink("hotel.booking.line", [lineId]);
				await this.load();
			}
			return;
		}
		return super.unlinkRecord(...arguments);
	},

	async updateRecord(record, options = {}) {
		if (isDashboardFolioLineEvent(this, record)) {
			return;
		}
		return super.updateRecord(...arguments);
	},

	async loadRecords(data) {
		if (this.resModel !== "hotel.booking") {
			return super.loadRecords(...arguments);
		}
		const rawBookings = await this.fetchRecords(data);
		const bookingIds = rawBookings.map((booking) => booking.id);
		if (!bookingIds.length) {
			return {};
		}
		const lineEvents = await this.orm.call(
			"hotel.booking",
			"fetch_dashboard_calendar_line_events",
			[bookingIds],
			{ product_tmpl_id: this.room_id || false },
		);
		const records = {};
		for (const rawRecord of lineEvents) {
			records[rawRecord.id] = this.normalizeDashboardLineRecord(rawRecord);
		}
		return records;
	},

	normalizeDashboardLineRecord(rawRecord) {
		const res = super.normalizeRecord(rawRecord);
		const partnerName = rawRecord.partner_id?.[1] || "";
		const productName = rawRecord.line_product_name || "";
		res.title = `${res.title} ${partnerName} (${productName})`.trim();
		if (rawRecord.dashboard_is_day_long_tour) {
			res.isAllDay = true;
		}
		return res;
	},
});

patch(CalendarCommonRenderer.prototype, {
	convertRecordToEvent(record) {
		const event = super.convertRecordToEvent(...arguments);
		if (
			this.props.model.resModel !== "hotel.booking" ||
			!this.props.model.meta?.productData ||
			!record.rawRecord?.dashboard_is_day_long_tour
		) {
			return event;
		}
		return {
			...event,
			allDay: true,
		};
	},
});

patch(CalendarController.prototype, {
	async editRecord(record, context = {}, shouldFetchFormViewId = true) {
		if (
			this.model.resModel === "hotel.booking" &&
			record.rawRecord?.dashboard_booking_id
		) {
			record = { ...record, id: record.rawRecord.dashboard_booking_id };
		}
		const action = await super.editRecord(record, context, shouldFetchFormViewId);
		if (this.model.resModel === "hotel.booking" && this.model.meta?.productData) {
			await this.model.load();
			await this._refreshDashboardPanelAfterLoad();
		}
		return action;
	},

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

	_dayTourBookedSummaryHtml(counts) {
		const maxOccupancy = counts.total ?? counts.day_tour_max_occupancy ?? 0;
		const booked = counts.booked ?? counts.day_tour_booked_guests ?? 0;
		const remaining = counts.available ?? counts.day_tour_remaining_occupancy ?? 0;
		return `
            <h3 class="pt-2">Tour occupancy</h3>
            <table class="table table-sm mb-2">
                <tbody>
                    <tr><td>MAX OCCUPANCY</td><td id="totalRoomCount">${maxOccupancy}</td></tr>
                    <tr><td>BOOKED GUESTS</td><td id="bookedRoomCount">${booked}</td></tr>
                    <tr><td>REMAINING</td><td id="remainingRoomCount">${remaining}</td></tr>
                </tbody>
            </table>`;
	},

	_roomCountSummaryHtml(counts) {
		if (this._isDayLongTourSelected()) {
			return this._dayTourBookedSummaryHtml(counts);
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
		if (this._isDayLongTourSelected()) {
			if ($("#totalRoomCount").length) {
				$("#totalRoomCount").text(
					counts.total ?? counts.day_tour_max_occupancy ?? 0
				);
				$("#bookedRoomCount").text(
					counts.booked ?? counts.day_tour_booked_guests ?? 0
				);
				$("#remainingRoomCount").text(
					counts.available ?? counts.day_tour_remaining_occupancy ?? 0
				);
			}
			return;
		}
		if (!$("#totalRoomCount").length) {
			return;
		}
		$("#totalRoomCount").text(counts.total ?? 0);
		$("#availableRoomCount").text(counts.available ?? 0);
		$("#bookedRoomCount").text(counts.booked ?? 0);
	},

	_renderDashboardRoomPanel(roomId, roomDetail, counts, isDayTour) {
		const detailRows = isDayTour
			? this._dayTourDetailHtml(roomDetail[0], counts)
			: this.record_html(roomDetail);
		const summaryHtml = this._roomCountSummaryHtml(counts);
		const variantsSection = isDayTour
			? ""
			: `<h3 class="pt-2">Available room variants</h3>
                <table id="availableRoomsTable">
                ${this.record_html(roomDetail, true)}</table>`;

		$("#roomInformation")
			.html(
				`
                <div class="selected_room_container p-2" data-prod-tmplt=${roomId}>
                <h3>${roomDetail[0].name}</h3>
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
				if ($(this).attr("data-value") === roomId.toString()) {
					$(this).find("input").prop("checked", true).prop("disabled", false);
				} else {
					$(this).find("input").prop("checked", false).prop("disabled", true);
				}
			});
	},

	_countsFromRoomDetail(roomDetail, isDayTour) {
		return {
			total: roomDetail[0].total_room_count ?? roomDetail[0].day_tour_max_occupancy ?? 0,
			available:
				roomDetail[0].available_room_count ??
				roomDetail[0].day_tour_remaining_occupancy ??
				0,
			booked:
				roomDetail[0].booked_room_count ??
				roomDetail[0].day_tour_booked_guests ??
				0,
			day_tour_max_occupancy: roomDetail[0].day_tour_max_occupancy,
			day_tour_booked_guests: roomDetail[0].day_tour_booked_guests,
			day_tour_remaining_occupancy: roomDetail[0].day_tour_remaining_occupancy,
		};
	},

	async _refreshDashboardPanelAfterLoad() {
		if (!this.selected_room) {
			return;
		}
		await this.update_bookingCount(this.model.date.c, this.model.scale);
		const shouldReloadPanel =
			this.selected_room_is_day_long_tour ||
			!$("#roomInformation .selected_room_container").length;
		if (!shouldReloadPanel) {
			return;
		}
		const d = await this.orm.call(
			"product.template",
			"fetch_data_for_room",
			[[this.selected_room]],
			{ selected_date: this.model.date.c },
		);
		this.model.room_book_ids = d.b_ids;
		const roomDetail = d.room_record;
		const isDayTour = Boolean(d.is_day_long_tour ?? roomDetail[0]?.is_day_long_tour);
		this.selected_room_is_day_long_tour = isDayTour;
		this.model.room_is_day_long_tour = isDayTour;
		const counts = this._countsFromRoomDetail(roomDetail, isDayTour);
		this._renderDashboardRoomPanel(this.selected_room, roomDetail, counts, isDayTour);
	},

	async update_bookingCount(calendar_data, scale) {
		const booking_count = await this.orm.call(
			"hotel.booking",
			"fetch_booking_count_for_dashboard",
			[],
			{
				calendar_data: calendar_data,
				scale: scale,
				dayInMonth: this.model.date.daysInMonth,
				weekDay: this.model.date.weekday,
				room: this.model.room_id,
			},
		);

		this.model.meta.productData.check_in_booking = booking_count.check_in_booking;
		this.model.meta.productData.check_out_booking = booking_count.check_out_booking;
		this.model.meta.productData.booked_room_ids = booking_count.booked_room_ids;

		$("#current_date_check_in").text(booking_count.current_month_check_in);
		$("#current_date_check_out").text(booking_count.current_month_check_out);
		$("#total_available_room").text(booking_count.available_rooms);
		$("#total_booked_room").text(booking_count.booked_room_ids.length);

		this.update_available_rooms_data(booking_count);
	},

	get rendererProps() {
		if (this.model.resModel === "hotel.booking" && this.model.meta?.productData) {
			void this._refreshDashboardPanelAfterLoad();
		}
		return super.rendererProps;
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
			this.model.room_is_day_long_tour = false;
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
		const isDayTour = Boolean(d.is_day_long_tour ?? room_detail[0]?.is_day_long_tour);
		this.selected_room_is_day_long_tour = isDayTour;
		this.model.room_is_day_long_tour = isDayTour;

		const counts = this._countsFromRoomDetail(room_detail, isDayTour);
		this._renderDashboardRoomPanel(room_id, room_detail, counts, isDayTour);

		await this.model.load();
		await this._refreshDashboardPanelAfterLoad();
	},
});

patch(CalendarCommonPopover.prototype, {
	isDashboardFolioLineEvent() {
		return isDashboardFolioLineEvent(this.props.model, this.props.record);
	},

	get popoverResId() {
		if (this.isDashboardFolioLineEvent()) {
			return this.props.record.rawRecord.dashboard_booking_id;
		}
		return this.props.record.id;
	},
});
