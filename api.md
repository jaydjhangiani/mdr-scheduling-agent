# 🧠 Appointment Scheduling API – System Design & Logic

## 🚀 Overview
This API provides a doctor appointment scheduling system with three core capabilities:

1. Check availability  
2. Suggest next available slots  
3. Book an appointment  

The system dynamically generates time slots based on business rules instead of storing all slots in a database.

---

## ⚙️ Core Configuration

```js
const SLOT_MINUTES = 30;
const WORK_START_HOUR = 9;
const WORK_END_HOUR = 17;
const LUNCH_START_HOUR = 12;
const LUNCH_END_HOUR = 13;
```

### Constraints:
- Only weekdays (Mon–Fri)
- Working hours: 9 AM – 5 PM
- Lunch break: 12 PM – 1 PM
- Slot duration: 30 minutes

---

## 🧱 Data Layer (Simulated DB)

```js
const booked = new Set();
```

- Stores booked slots as: YYYY-MM-DDTHH:mm:ss
- Example: 2026-02-05T10:00:00
- Uses Set for O(1) lookup

---

## 🔧 Helper Functions

### Date Parsing
```js
parseYYYYMMDD(dateStr)
```

### Time Parsing
```js
parseTimeHHMM(timeStr)
```

### Slot Validation
```js
isValidSlot(date)
```

### Booking Check
```js
isBooked(date)
```

### Time Alignment
```js
roundUp(now)
```

---

## 🧠 Slot Generation Engine

### nextSlotsFrom(start, count)

- Iterates forward in 30-min increments  
- Skips weekends, non-working hours, and lunch  
- Returns next available slots  

---

### slotsForDate(date)

- Today → start from current time  
- Future → start from 9 AM  

---

## 🌐 API Endpoints

### Health Check
GET /api/health

Response:
{ "status": "ok" }

---

## 📍 Availability API

GET /api/appointments/availability

### Case A: No Input
Returns next 3 available slots from now

### Case B: Date Only
Returns next 3 slots for that date

### Case C: Date + Time
- If available → { "available": true }
- Else → { "available": false, "next3": [...] }

---

## ⏱ Time Window Filtering

```js
const TIME_WINDOWS = {
  morning: { start: "09:00", end: "12:00" },
  afternoon: { start: "13:00", end: "17:00" },
  evening: { start: "17:00", end: "20:00" }
};
```

---

## 📦 Booking API

POST /api/appointments/book

### Success
{ "status": "booked", "start": "..." }

### Errors
- Invalid slot  
- Already booked  

---

## 🔍 Debug Endpoint

GET /api/appointments/booked

---

## 🧠 System Design Summary

The system dynamically generates appointment slots based on rules like working hours, lunch breaks, and weekdays.  
It filters booked slots using an in-memory set for fast lookup.  
The API supports flexible queries and time window filtering.

---

## 🚀 Strengths

- Stateless slot generation  
- Flexible API design  
- Clean separation of concerns  
- Extensible architecture  

---

## ⚠️ Improvements

- Replace in-memory DB with PostgreSQL/Redis  
- Handle concurrency (avoid double booking)  
- Add timezone support  
- Improve scalability with caching  

---

## 🏁 Conclusion

A slot-based scheduling system optimized for flexibility and extensibility.
