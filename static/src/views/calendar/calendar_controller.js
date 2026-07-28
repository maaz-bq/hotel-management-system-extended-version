/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { useBus } from "@web/core/utils/hooks";
import { onMounted, onPatched, onWillUnmount } from "@odoo/owl";
import { CalendarController } from "@web/views/calendar/calendar_controller";
import { CalendarModel } from "@web/views/calendar/calendar_model";
import { CalendarRenderer } from "@web/views/calendar/calendar_renderer";
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

function escapeDashboardHtml(value) {
	return String(value ?? "")
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;")
		.replace(/"/g, "&quot;");
}

const DASHBOARD_AVAILABILITY_MAX_LINES = 3;

function buildDashboardAvailabilityLines(availability) {
	const categories = availability?.categories ?? [];
	const lines = [];
	for (const category of categories) {
		const products = category.products ?? [];
		if (products.length) {
			const categoryName = category.name || "";
			lines.push({
				text: categoryName,
				html: `<div class="o_hotel_day_availability__category">${escapeDashboardHtml(categoryName)}</div>`,
			});
			for (const product of products) {
				const display = product.display || product.name || "";
				const escaped = escapeDashboardHtml(display);
				lines.push({
					text: display,
					html: `<div class="o_hotel_day_availability__line">${escaped}</div>`,
				});
			}
			continue;
		}
		if (category.display) {
			const display = category.display;
			const escaped = escapeDashboardHtml(display);
			lines.push({
				text: display,
				html: `<div class="o_hotel_day_availability__line">${escaped}</div>`,
			});
		}
	}
	return lines;
}

function formatDashboardAvailabilityHtml(availability, options = {}) {
	if (!availability) {
		return "";
	}
	const maxLines = options.maxLines ?? DASHBOARD_AVAILABILITY_MAX_LINES;
	const lines = buildDashboardAvailabilityLines(availability);
	if (!lines.length) {
		return "";
	}
	const fullTitle = escapeDashboardHtml(lines.map((line) => line.text).join("\n"));
	const visibleLines = lines.slice(0, maxLines);
	const hiddenCount = lines.length - visibleLines.length;
	const parts = visibleLines.map((line) => line.html);
	if (hiddenCount > 0) {
		parts.push(
			`<div class="o_hotel_day_availability__more">+${hiddenCount} more</div>`,
		);
	}
	return `<div class="o_hotel_day_availability text-muted" title="${fullTitle}">${parts.join("")}</div>`;
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
		const frame = cellEl.querySelector(".fc-daygrid-day-frame") || cellEl;
		const dayEvents = frame.querySelector(".fc-daygrid-day-events");
		const wrapper = document.createElement("div");
		wrapper.className = "o_hotel_day_availability_mount";
		wrapper.innerHTML = html;
		frame.insertBefore(wrapper, dayEvents || null);
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
		return Boolean(
			this.props.context?.hotel_extended_calendar ||
				isHotelExtendedCalendar(this.model)
		);
	},

	get showSideBar() {
		if (this.isExtendedDashboard()) {
			return false;
		}
		return super.showSideBar;
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

	async update_bookingCount(calendar_data, scale) {
		if (isHotelExtendedCalendar(this.model)) {
			return;
		}
		return super.update_bookingCount(...arguments);
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
		if (isHotelExtendedCalendar(this.model)) {
			return;
		}
		return super.fetchRoomTypeData(...arguments);
	},
});

function isAvailabilityCalendarLayout(model, actionContext) {
	if (model.resModel !== "hotel.booking") {
		return false;
	}
	if (actionContext?.hotel_extended_calendar) {
		return true;
	}
	if (model.meta?.context?.hotel_extended_calendar) {
		return true;
	}
	return isHotelExtendedCalendar(model);
}

function applyAvailabilityCalendarLayoutClass(model, actionContext) {
	if (model.resModel !== "hotel.booking") {
		return;
	}
	const calendarContainer = document.querySelector(".o_calendar_container");
	if (!calendarContainer) {
		return;
	}
	const isFullLayout = isAvailabilityCalendarLayout(model, actionContext);
	calendarContainer.classList.add("wk_hotel_container");
	calendarContainer.classList.toggle(
		"o_hotel_availability_calendar_full",
		isFullLayout,
	);
	const summaryGrid = calendarContainer.previousElementSibling;
	if (summaryGrid?.classList.contains("custom-grid")) {
		summaryGrid.classList.toggle(
			"o_hotel_availability_calendar_summary_hidden",
			isFullLayout,
		);
	}
}

patch(CalendarRenderer.prototype, {
	setup() {
		onMounted(() => {
			const model = this.props.model;
			applyAvailabilityCalendarLayoutClass(model, model.meta?.context);
			this._availabilityLayoutHandler = () => {
				applyAvailabilityCalendarLayoutClass(model, model.meta?.context);
			};
			model.bus.addEventListener("update", this._availabilityLayoutHandler);
		});
		onWillUnmount(() => {
			const model = this.props.model;
			if (this._availabilityLayoutHandler) {
				model.bus.removeEventListener("update", this._availabilityLayoutHandler);
				this._availabilityLayoutHandler = null;
			}
		});
		onPatched(() => {
			applyAvailabilityCalendarLayoutClass(
				this.props.model,
				this.props.model.meta?.context,
			);
		});
		super.setup(...arguments);
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
