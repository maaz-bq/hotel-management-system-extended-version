/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { useBus } from "@web/core/utils/hooks";
import { CalendarController } from "@web/views/calendar/calendar_controller";
import { CalendarModel } from "@web/views/calendar/calendar_model";
import { CalendarCommonRenderer } from "@web/views/calendar/calendar_common/calendar_common_renderer";
import { CalendarCommonPopover } from "@web/views/calendar/calendar_common/calendar_common_popover";

const { DateTime } = luxon;

function getHotelDashboardRpcContext(model) {
	return { ...(model.meta?.context || {}) };
}

function isHotelExtendedCalendar(model) {
	if (model.resModel !== "hotel.booking") {
		return false;
	}
	if (model.meta?.productData?.extended_calendar) {
		return true;
	}
	return Boolean(getHotelDashboardRpcContext(model).hotel_extended_calendar);
}

function getDashboardDateKey(date, scale) {
	const options = scale === "month" ? { zone: "UTC" } : {};
	return DateTime.fromJSDate(date, options).toISODate();
}

function formatDashboardAvailabilityHtml(availability) {
	if (!availability) {
		return "";
	}
	const categories = availability.categories ?? [];
	if (!categories.length) {
		return "";
	}
	const iconForType = (metricType) => {
		if (metricType === "room") {
			return "fa-bed";
		}
		if (metricType === "tour") {
			return "fa-sun-o";
		}
		return "fa-tag";
	};
	const lines = categories
		.map((category) => {
			const icon = iconForType(category.metric_type);
			const title = category.name || "";
			return `<div title="${title}"><i class="fa ${icon}"></i> ${category.display}</div>`;
		})
		.join("");
	return `<div class="o_hotel_day_availability text-muted">${lines}</div>`;
}

function isDashboardFolioLineEvent(model, recordOrId) {
	if (!isHotelExtendedCalendar(model)) {
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
 * Availability Calendar: folio-line events, bookable product panel, grid badges.
 */
patch(CalendarModel.prototype, {
	async load(params = {}) {
		if (this.resModel === "hotel.booking") {
			Object.assign(this.meta, params);
		}
		return super.load(...arguments);
	},

	async fetchData() {
		if (this.resModel !== "hotel.booking") {
			return super.fetchData(...arguments);
		}
		return await this.orm.call(
			"hotel.booking",
			"fetch_data_for_dashboard",
			[],
			{
				scale: this.scale,
				context: getHotelDashboardRpcContext(this),
			},
		);
	},

	get canEdit() {
		if (isHotelExtendedCalendar(this)) {
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
		if (!isHotelExtendedCalendar(this)) {
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
			{
				product_tmpl_id: this.room_id || false,
				context: getHotelDashboardRpcContext(this),
			},
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

	async fetchDashboardCategoryAvailability(startDate, endDate) {
		if (!isHotelExtendedCalendar(this)) {
			return;
		}
		const data = await this.orm.call(
			"hotel.booking",
			"fetch_category_availability_range",
			[],
			{
				start_date: startDate,
				end_date: endDate,
				context: getHotelDashboardRpcContext(this),
			},
		);
		if (!this.meta.categoryAvailabilityByDate) {
			this.meta.categoryAvailabilityByDate = {};
		}
		Object.assign(this.meta.categoryAvailabilityByDate, data);
		this.meta.lastAvailabilityRange = { start: startDate, end: endDate };
		this.bus.trigger("DASHBOARD_AVAILABILITY_UPDATED");
	},
});

patch(CalendarCommonRenderer.prototype, {
	setup() {
		super.setup(...arguments);
		useBus(this.props.model.bus, "DASHBOARD_AVAILABILITY_UPDATED", () => {
			this._refreshDashboardDayCellAvailability();
			if (this.fc?.api) {
				this.fc.api.render();
			}
		});
	},

	get options() {
		const opts = super.options;
		if (!isHotelExtendedCalendar(this.props.model)) {
			return opts;
		}
		return {
			...opts,
			dayCellDidMount: (arg) => this.onDashboardDayCellDidMount(arg),
			dayCellWillUnmount: (arg) => this.onDashboardDayCellWillUnmount(arg),
			datesSet: (arg) => this.onDashboardDatesSet(arg),
		};
	},

	onDashboardDayCellDidMount(arg) {
		if (
			!isHotelExtendedCalendar(this.props.model) ||
			this.props.model.scale !== "month"
		) {
			return;
		}
		this._mountDashboardDayCellAvailability(arg.el, arg.date);
	},

	onDashboardDayCellWillUnmount(arg) {
		arg.el.querySelector(".o_hotel_day_availability_mount")?.remove();
	},

	_mountDashboardDayCellAvailability(cellEl, date) {
		cellEl.querySelector(".o_hotel_day_availability_mount")?.remove();
		const dateKey = getDashboardDateKey(date, this.props.model.scale);
		const html = this.getDashboardAvailabilityHtml(dateKey);
		if (!html) {
			return;
		}
		const mountPoint =
			cellEl.querySelector(".fc-daygrid-day-top") ||
			cellEl.querySelector(".fc-daygrid-day-frame") ||
			cellEl;
		const wrapper = document.createElement("div");
		wrapper.className = "o_hotel_day_availability_mount";
		wrapper.innerHTML = html;
		mountPoint.appendChild(wrapper);
	},

	_refreshDashboardDayCellAvailability() {
		if (
			!isHotelExtendedCalendar(this.props.model) ||
			this.props.model.scale !== "month" ||
			!this.fc?.el
		) {
			return;
		}
		for (const cellEl of this.fc.el.querySelectorAll(".fc-daygrid-day")) {
			const dateStr = cellEl.getAttribute("data-date");
			if (!dateStr) {
				continue;
			}
			const date = new Date(`${dateStr}T00:00:00`);
			this._mountDashboardDayCellAvailability(cellEl, date);
		}
	},

	getDashboardAvailabilityHtml(dateKey) {
		const availability =
			this.props.model.meta?.categoryAvailabilityByDate?.[dateKey];
		return formatDashboardAvailabilityHtml(availability);
	},

	getHeaderHtml(arg) {
		const result = super.getHeaderHtml(...arguments);
		if (
			!isHotelExtendedCalendar(this.props.model) ||
			this.props.model.scale !== "week"
		) {
			return result;
		}
		const dateKey = getDashboardDateKey(arg.date, "week");
		const extraHtml = this.getDashboardAvailabilityHtml(dateKey);
		if (!extraHtml) {
			return result;
		}
		return {
			html: `${result.html}${extraHtml}`,
		};
	},

	async onDashboardDatesSet(info) {
		if (!isHotelExtendedCalendar(this.props.model)) {
			return;
		}
		if (!["month", "week"].includes(this.props.model.scale)) {
			return;
		}
		const start = DateTime.fromJSDate(info.start).toISODate();
		const end = DateTime.fromJSDate(info.end).toISODate();
		await this.props.model.fetchDashboardCategoryAvailability(start, end);
	},

	convertRecordToEvent(record) {
		const event = super.convertRecordToEvent(...arguments);
		if (
			!isHotelExtendedCalendar(this.props.model) ||
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
	isExtendedDashboard() {
		return isHotelExtendedCalendar(this.model);
	},

	async editRecord(record, context = {}, shouldFetchFormViewId = true) {
		if (
			isHotelExtendedCalendar(this.model) &&
			record.rawRecord?.dashboard_booking_id
		) {
			record = { ...record, id: record.rawRecord.dashboard_booking_id };
		}
		const action = await super.editRecord(record, context, shouldFetchFormViewId);
		if (isHotelExtendedCalendar(this.model)) {
			await this.model.load();
			await this._refreshDashboardPanelAfterLoad();
		}
		return action;
	},

	openViewonClick(ev) {
		if (!isHotelExtendedCalendar(this.model)) {
			return super.openViewonClick(...arguments);
		}
		if (
			$(ev.target).hasClass("total_available") ||
			$(ev.target).closest(".total_available").length
		) {
			let roomTypeDomain = [
				["categ_id.is_bookable", "=", true],
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

	_getDashboardProductLists() {
		const productData = this.model.meta?.productData || {};
		const bookableCategories = productData.bookable_categories || [];
		const categoryProducts = bookableCategories.flatMap(
			(category) => category.products || []
		);
		return {
			bookableCategories,
			nightStay: productData.night_stay_data || [],
			dayLong: productData.day_long_data || [],
			all: productData.room_data || categoryProducts,
		};
	},

	_getRoomDataEntry(roomId) {
		const { bookableCategories, nightStay, dayLong, all } =
			this._getDashboardProductLists();
		for (const category of bookableCategories) {
			const match = (category.products || []).find(
				(product) => product.id === roomId
			);
			if (match) {
				return match;
			}
		}
		return (
			nightStay.find((room) => room.id === roomId) ||
			dayLong.find((room) => room.id === roomId) ||
			all.find((room) => room.id === roomId)
		);
	},

	_updateCategoryAvailabilitySummary(bookingCount) {
		const categoryAvailability = bookingCount.category_availability || [];
		if (this.model.meta?.productData) {
			this.model.meta.productData.category_availability =
				categoryAvailability;
		}
	},

	_countBookableCategoryProducts() {
		const { bookableCategories } = this._getDashboardProductLists();
		return bookableCategories.reduce(
			(total, category) => total + (category.products || []).length,
			0
		);
	},

	async _refreshCalendarGridAvailability() {
		const range = this.model.meta?.lastAvailabilityRange;
		if (!range || !isHotelExtendedCalendar(this.model)) {
			return;
		}
		await this.model.fetchDashboardCategoryAvailability(range.start, range.end);
	},

	_getDefaultDashboardProductId() {
		const { bookableCategories, all } = this._getDashboardProductLists();
		const firstCategoryProduct = bookableCategories.find(
			(category) => (category.products || []).length
		)?.products?.[0]?.id;
		return firstCategoryProduct || all[0]?.id;
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
		if (!isHotelExtendedCalendar(this.model)) {
			return;
		}
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
		if (!isHotelExtendedCalendar(this.model)) {
			return super.update_bookingCount(...arguments);
		}
		const booking_count = await this.orm.call(
			"hotel.booking",
			"fetch_booking_count_for_dashboard",
			[,],
			{
				calendar_data: calendar_data,
				scale: scale,
				dayInMonth: this.model.date.daysInMonth,
				weekDay: this.model.date.weekday,
				room: this.model.room_id,
				context: getHotelDashboardRpcContext(this.model),
			},
		);

		this.model.meta.productData.check_in_booking = booking_count.check_in_booking;
		this.model.meta.productData.check_out_booking = booking_count.check_out_booking;
		this.model.meta.productData.booked_room_ids = booking_count.booked_room_ids;

		$("#current_date_check_in").text(booking_count.current_month_check_in);
		$("#current_date_check_out").text(booking_count.current_month_check_out);
		this._updateCategoryAvailabilitySummary(booking_count);
		await this._refreshCalendarGridAvailability();

		this.update_available_rooms_data(booking_count);
	},

	get rendererProps() {
		if (isHotelExtendedCalendar(this.model)) {
			void this._refreshDashboardPanelAfterLoad();
		}
		return super.rendererProps;
	},

	update_available_rooms_data(booking_count) {
		super.update_available_rooms_data(...arguments);
		if (!isHotelExtendedCalendar(this.model)) {
			return;
		}
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
		if (!isHotelExtendedCalendar(this.model)) {
			return super.fetchRoomTypeData(...arguments);
		}
		var room_id;
		if (ev) {
			room_id = $(ev.target).data("id");
		} else {
			room_id = this._getDefaultDashboardProductId();
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
