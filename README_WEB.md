# Delivery Zone Web UI (JavaScript)

This project now includes a browser-based UI with a Node.js backend.

## Files

- `server.js`: Backend API + business logic (fetch/create/plan/apply updates)
- `public/index.html`: UI
- `public/styles.css`: Styling
- `public/app.js`: Frontend behavior

## Run

1. Install Node.js 18+.
2. From project folder:

```bash
npm install
npm start
```

3. Open:

```text
http://localhost:3000
```

## Features

- Fetch zones (with optional per-zone detail enrichment for regions/countries)
- Create zones from CSV/XLS/XLSX
- Plan updates from CSV/XLS/XLSX
- Apply updates with browser confirmation
- Structured zone/update tables and operation logs
- Cookie helpers:
  - Normalize pasted cookie header
  - Optional "remember cookie on this device"
  - Clear saved cookie + clear server-side token cache
  - Backend token cache to avoid re-fetching token on every action
- Session cURL helpers:
  - Paste full cURL and auto-extract `Cookie` + `Authorization`
  - Run `/api/service/application/cart/v1.0/basic` session check from UI
  - Optional auto session-check before every fetch/create/update action
