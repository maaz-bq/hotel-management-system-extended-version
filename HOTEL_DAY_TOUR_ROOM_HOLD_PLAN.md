# Day-Long Tour → Room Inventory (Folio Lines)

**Status:** Implemented in `hotel_management_system_extend` v18.0.1.0.71+  
**Scope:** Slow Resort — Odoo 18 `hotel_management_system_extend`  
**Authoritative spec:** [11-room-hold-allocation-approach.md](../slow-resorts-docs/11-room-hold-allocation-approach.md)

---

## Summary

This module uses **folio lines** and **`room_count`** — not a `hotel.room.hold` model.

| Flow | How capacity works |
|------|-------------------|
| **Day-long tour + optional day room** | Tour product has `offers_day_room`. Staff add a **$0 day-room folio line** (placeholder variant). Capacity locks on **line save** (incl. Initial). Delete line to release. Day-room **excluded from SO**. |
| **Overnight stay** | New room lines default to **placeholder variant**. Capacity locks on **confirm**. Staff assign numbered variant (101/103) at check-in via folio or Exchange Room. |

Shared rules:

- **`room_count`** on room type = sales cap per night (independent of variant count)
- **Placeholder variant** per room type — "Deluxe — assign at check-in"
- **Variant exclusivity** — no double-booking of the same numbered room

---

## Configuration

### Room type (`is_room_type`)

On **Room Configuration**:

- **Number of Rooms** (`room_count`) — capacity cap
- **Physical Rooms** (`physical_room_count`) — numbered variants only (read-only)
- **Placeholder Variant** — auto-created

### Day-long tour (`is_day_long_tour`)

On **Day-Long Tour** tab:

- **Offers Day Room** (`offers_day_room`) — enables optional day-room section on folio when tour is booked

---

## Folio UI

- **Folio tab** — overnight/service/tour lines; room picker shows placeholder variants
- **Day Rooms** section (when tour has `offers_day_room`) — linked $0 lines, max 1 per tour line

---

## See also

- [10-room-inventory-variants-approach.md](../slow-resorts-docs/10-room-inventory-variants-approach.md)
- [12-room-hold-implementation-questions.md](../slow-resorts-docs/12-room-hold-implementation-questions.md)
