# Delivery Zone Manager — Features & Architecture

**Version:** 1.9.5 · **Build:** auto-incremented per release
**Platform:** Electron desktop app (macOS + Windows) + standalone Node.js server
**Team:** Fynd Platform Team

---

## Table of Contents

1. [Overview](#overview)
2. [Features](#features)
3. [Architecture](#architecture)
4. [File Structure](#file-structure)
5. [Environments](#environments)
6. [Authentication Flow](#authentication-flow)
7. [Core Operations](#core-operations)
8. [CSV / XLS File Format](#csv--xls-file-format)
9. [Security](#security)
10. [Auto-Update System](#auto-update-system)
11. [Build & Versioning](#build--versioning)
12. [Running the App](#running-the-app)
13. [Testing](#testing)

---

## Overview

Delivery Zone Manager is an internal operations console for managing platform delivery zones across SIT, UAT, and PROD environments. It provides a file-driven workflow — users upload a CSV/XLS/XLSX file and the app creates, plans, or applies zone updates against the Fynd logistics API.

It ships as a self-contained desktop app (no Node.js required on end-user machines) and also runs as a plain Node.js web server for development.

---

## Features

### Authentication
- **Email OTP login** — users enter their platform email, receive a 6-digit OTP, and log in without ever seeing or copying cookies manually
- **Session cookie capture** — the Node.js server proxies the OTP flow and captures `Set-Cookie` headers from the platform auth API, storing the cookie in the browser's `localStorage`
- **Remember my email** — optional checkbox persists the user's email address so it is pre-filled on the next login
- **Email autocomplete suggestions** — stores the last 5 used emails as a browser datalist for quick selection
- **Auto-redirect** — if a valid session cookie is already stored, the app skips the login page and goes straight to the console; if no cookie is found on the main app, the user is redirected to `/login`
- **Logout** — clears all session data (cookie, email, remember flag) from localStorage and returns to the login page
- **OTP rate limiting** — login endpoints are rate-limited to 20 requests per 15 minutes to prevent brute-force attacks

### Environment Management
- **Three environments supported:** SIT, UAT, PROD
- Environment is selected once at login and persisted in `localStorage` (`dzm.env`)
- The selected environment is displayed in the hero badge and status bar throughout the session
- All API calls automatically route to the correct environment's endpoints

### Zone Operations
| Operation | Description |
|-----------|-------------|
| **Fetch Zones** | Lists all delivery zones from the platform API with pagination (500 per page). Optionally enriches each zone with full detail (stores, regions, countries) using parallel requests. |
| **Create From File** | Reads a CSV/XLS/XLSX file and creates new delivery zones via the platform API. Reports per-zone success/skip/failure. |
| **Plan Updates** | Dry-run mode — reads the file and compares against existing zones to show exactly what region changes would be applied, without touching the API. |
| **Apply Updates** | Applies the planned region updates to the platform. Requires a confirmation dialog before proceeding. |

### UI & UX
- **Live log panel** — timestamped log output for every API call and operation; copyable to clipboard
- **Summary panel** — structured summary (rows, created, skipped, failed, etc.) after each operation
- **Zones table** — searchable and sortable (name A-Z/Z-A, stores count, regions count) table of all fetched zones
- **Planned updates table** — searchable table showing old vs new region counts per zone
- **Status bar** — shows current environment, last action, session state, zones loaded count, and planned updates count
- **Toast notifications** — non-blocking success/error/info messages
- **Confirm modal** — blocks destructive "Apply Updates" behind an explicit confirmation step
- **Busy state** — all action buttons are disabled while an operation is in progress

### Desktop App (Electron)
- Packaged as a native desktop app — no Node.js or browser required on end-user machines
- Ships as `.dmg` (macOS Intel x64 + Apple Silicon arm64) and `.exe` NSIS installer (Windows x64)
- Embeds an Express.js server that runs locally on port 3000 (falls back to an OS-assigned port if 3000 is busy)
- macOS menu bar with **Check for Updates**, Edit, View, and Window menus
- Light theme forced (`nativeTheme.themeSource = "light"`)
- Minimum window size: 920 × 620; default: 1300 × 840
- **Sandboxed renderer process** — `sandbox: true` in BrowserWindow prevents renderer from accessing Node.js APIs directly
- **Custom Fynd app icon** — uses the Fynd geometric F-mark on a dark background, rendered at 1024×1024

### Auto-Update
- **Silent background download** — checks for updates 5 seconds after launch; downloads automatically if available
- **Update banner** — non-intrusive banner appears in the app showing download progress and a "Restart & Install" button when ready
- **Install on quit** — if the user doesn't restart manually, the update installs on the next quit
- **Mac menu trigger** — "Check for Updates…" in the app menu triggers a manual check
- Updates are distributed via GitHub Releases using `electron-updater`

### Token Management
- Bearer tokens are obtained from the platform OAuth endpoint using the stored session cookie
- Tokens are **cached in memory** keyed by a SHA-256 hash of `env + cookie`, avoiding redundant token requests
- Token expiry is decoded from the JWT `exp` claim; a 60-second skew buffer ensures tokens are refreshed before they actually expire
- Expired cache entries are pruned before each token fetch
- Fallback TTL of 10 minutes if JWT expiry cannot be decoded

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   Electron Shell                        │
│  electron/main.js — app lifecycle, IPC, auto-updater   │
│  electron/preload.js — contextBridge (secure IPC API)  │
└────────────────────┬────────────────────────────────────┘
                     │ spawns
┌────────────────────▼────────────────────────────────────┐
│              Express.js Server (server.js)              │
│  Serves static files from /public                      │
│  Proxies all platform API calls                        │
│  Handles OTP auth, zone CRUD, file parsing             │
│  helmet headers · rate limiting · multer file guard    │
└────────────────────┬────────────────────────────────────┘
                     │ HTTP (localhost only)
┌────────────────────▼────────────────────────────────────┐
│              Browser / Web UI (/public)                 │
│  login.html + login.js  — OTP login page               │
│  index.html + app.js    — main operations console      │
│  updater.js             — update banner (Electron only) │
│  styles.css             — all styling                  │
└─────────────────────────────────────────────────────────┘
                     │ proxied by server.js
┌────────────────────▼────────────────────────────────────┐
│           Fynd Platform Logistics API                   │
│  SIT  — api.jiox0.de                                   │
│  UAT  — api.jiox5.de                                   │
│  PROD — api.jioretailer.com                            │
└─────────────────────────────────────────────────────────┘
```

### Key design decisions

| Decision | Reason |
|----------|--------|
| Express server embedded inside Electron | Users don't need a browser or separate server; the app is fully self-contained |
| `require.main === module` pattern | Allows `server.js` to run standalone (`node server.js`) or be required by Electron's main process |
| Cookies stored in `localStorage`, never in the UI | Ground-level users don't need to understand or paste cookies; login handles it transparently |
| Bearer token cache keyed by `sha256(env + cookie)` | Avoids repeated token fetches for the same session across multiple operations |
| Parallel zone enrichment (concurrency 6) | Matches the Python tool's `ThreadPoolExecutor(max_workers=6)` behavior for consistent performance |
| Env selected at login, not on the main page | Simplifies the main UI; env cannot be accidentally changed mid-session |
| `contextIsolation: true` + `sandbox: true` + preload bridge | Standard Electron security practice; renderer never gets direct Node.js access |
| `exceljs` for file parsing (replaced `xlsx`) | Eliminates two HIGH CVEs (Prototype Pollution + ReDoS) present in the SheetJS/xlsx library |

---

## File Structure

```
DeliveryZoneCreation-v1.9.5-main/
│
├── electron/
│   ├── main.js          # Electron main process — window, IPC, auto-updater
│   └── preload.js       # contextBridge — exposes electronAPI to renderer
│
├── public/
│   ├── index.html       # Main app page
│   ├── app.js           # Main app logic (fetch, create, plan, apply)
│   ├── login.html       # OTP login page
│   ├── login.js         # Login flow logic (send OTP, verify, remember email)
│   ├── logo_black.svg   # Fynd brand logo (used in app hero section)
│   ├── updater.js       # Auto-update banner UI (Electron only)
│   └── styles.css       # All CSS styles
│
├── scripts/
│   └── increment-build.js  # Auto-increments buildNumber before each DMG build
│
├── tests/
│   └── create-zones.test.js  # Integration tests (16 test cases)
│
├── server.js            # Express server — API proxy, file parsing, auth
├── package.json         # Dependencies, Electron-builder config, build scripts
├── build-number.json    # Current build number (auto-incremented, committed)
├── .gitignore           # Ignores dist/, node_modules/, *.dmg, .env, etc.
├── build/
│   ├── icon.png         # App icon — Fynd F-mark, 1024×1024 (DMG + NSIS)
│   └── icon.svg         # Source SVG for icon generation
│
├── Start App.command    # macOS double-click launcher (for non-Electron usage)
├── start.bat            # Windows double-click launcher (for non-Electron usage)
│
└── dist/                # Output directory (generated, not committed)
    ├── Delivery Zone Manager-1.9.5.dmg          # Intel Mac
    ├── Delivery Zone Manager-1.9.5-arm64.dmg    # Apple Silicon
    └── Delivery Zone Manager Setup 1.9.5.exe    # Windows
```

---

## Environments

| Env | API Base | Platform | Auth Base |
|-----|----------|----------|-----------|
| **SIT** | `api.jiox0.de` | `platform.jiox0.de` | `api.jiox0.de/.../authentication` |
| **UAT** | `api.jiox5.de` | `platform.jiox5.de` | `api.jiox5.de/.../authentication` |
| **PROD** | `api.jioretailer.com` | `platform.jioretailer.com` | `api.jioretailer.com/.../authentication` |

All environments use the same API path structure:
`/service/platform/logistics/v2.0/company/1/zones`

---

## Authentication Flow

```
User enters email + selects env
         │
         ▼
POST /api/login/send-otp  (rate-limited: 20 req / 15 min)
  → server proxies to platform auth API
  → captures session cookies from Set-Cookie headers
  → returns { sessionCookies, requestId, resendTimer }
         │
         ▼
User enters 6-digit OTP
         │
         ▼
POST /api/login/verify-otp  (rate-limited: 20 req / 15 min)
  → server posts OTP + sessionCookies to platform verify endpoint
  → captures final auth cookie from Set-Cookie headers
  → returns { cookieString }
         │
         ▼
Browser stores in localStorage:
  dzm.cookie         — the auth cookie (used for all API calls)
  dzm.email          — user's email (shown in account pill)
  dzm.env            — selected environment
  dzm.remember.email — pre-fill email next login (if "Remember" checked)
  dzm.email.history  — last 5 emails used (for autocomplete suggestions)
         │
         ▼
Redirected to main app (/)
```

---

## Core Operations

### Fetch Zones
1. Reads `dzm.cookie` and `dzm.env` from localStorage
2. `POST /api/fetch-zones` → server fetches bearer token → paginates zone list (500/page)
3. If "Include full zone details" is checked: enriches each zone with a detail call in batches of 6
4. Returns zone list with name, slug, zone ID, type, stores count, regions count, countries

### Create From File
1. User uploads CSV/XLS/XLSX (max 10 MB; only `.csv`, `.xls`, `.xlsx` accepted)
2. `POST /api/create-zones` (multipart form)
3. Server parses file with `exceljs`, normalizes headers (case-insensitive, BOM-stripped)
4. For each row: validates required fields → builds API payload → calls platform create API
5. Reports: total rows, created, skipped (already exists or invalid), failed

### Plan Updates
1. Same file upload, calls `POST /api/plan-updates`
2. Server fetches current zone state for each slug in the file
3. Compares existing regions vs file-specified regions
4. Returns a diff table (old region count vs new) — **no writes to the API**

### Apply Updates
1. User must confirm via modal dialog
2. `POST /api/apply-updates` — same as plan but actually sends PUT to the platform API
3. Reports: total rows, planned updates, updated, failed

---

## CSV / XLS File Format

| Column | Required | Description |
|--------|----------|-------------|
| `slug` | Yes | Zone slug identifier |
| `name` | No | Display name (defaults to slug) |
| `company_id` | No | Company ID (defaults to 1) |
| `store_ids` | Yes | Comma-separated or JSON array of store IDs |
| `region_type` | Yes | `pincode` or `non-pincode` |
| `mapping_country` | Yes | Country code(s) |
| `mapping_regions` | Yes* | Region codes — comma-separated or JSON array |
| `pincode` | Yes* | Alias for `mapping_regions` (either one is accepted) |
| `channels` | Yes | Channel identifiers |
| `is_active` | No | `true`/`false`/`1`/`0` (defaults to `true`) |
| `product_type` | No | `all` or `explicit` (defaults to `all`) |
| `product_tags` | No | Comma-separated product tag strings |

\* Either `mapping_regions` or `pincode` must be present (both column names are accepted).

- Supports `.csv`, `.xls`, `.xlsx` formats; max **10 MB**
- Headers are case-insensitive and BOM-stripped
- Blank rows are automatically ignored
- List fields accept both JSON arrays (`["a","b"]`) and plain comma-separated values (`a,b`)

---

## Security

The following security controls are in place:

| Control | Implementation |
|---------|----------------|
| **Security headers** | `helmet` middleware on all Express routes (XSS, clickjacking, MIME sniffing protection) |
| **OTP rate limiting** | `express-rate-limit` — 20 requests per 15 minutes on `/api/login/*` |
| **File upload guard** | `multer` limits: 10 MB max, `.csv`/`.xls`/`.xlsx` only |
| **File parsing (no CVEs)** | `exceljs` replaces `xlsx` (SheetJS) — eliminates Prototype Pollution + ReDoS HIGH CVEs |
| **Sandboxed renderer** | `sandbox: true` in BrowserWindow — renderer process has no Node.js access |
| **Context isolation** | `contextIsolation: true` + preload bridge — only explicitly exposed APIs reach the renderer |
| **No XSS in autocomplete** | Email suggestions built with `document.createElement`, not `innerHTML` |
| **Localhost-only server** | Express binds to `127.0.0.1` inside Electron — not accessible on the network |
| **Session URL validation** | Session-check URLs validated against allowed domain patterns per environment |
| **Zero npm vulnerabilities** | `npm audit` reports 0 vulnerabilities |

---

## Auto-Update System

Uses [`electron-updater`](https://www.electron.build/auto-update) with GitHub Releases as the distribution channel.

**Flow:**
1. App launches → waits 5 seconds → calls `autoUpdater.checkForUpdates()`
2. If update found: downloads silently in background
3. Banner appears in the app: "Update vX.Y.Z available — downloading…"
4. When download completes: banner shows "Restart & Install" button
5. If user doesn't click it: update installs automatically on next app quit (`autoInstallOnAppQuit = true`)

**To publish an update:**
1. Bump `version` in `package.json`
2. Run `npm run build:mac` or `npm run build:win` (build number auto-increments)
3. Create a GitHub Release tagged `v<version>` and attach the DMG/EXE + blockmap files

**Publisher config** in `package.json` (update `owner` before publishing):
```json
"publish": {
  "provider": "github",
  "owner": "YOUR_GITHUB_USERNAME",
  "repo": "delivery-zone-manager"
}
```

---

## Build & Versioning

Two separate version concepts:

| Concept | Where set | Example | Purpose |
|---------|-----------|---------|---------|
| **Product version** | `package.json` → `"version"` | `1.9.5` | Shown in DMG filename, About dialog, update checks |
| **Build number** | `build-number.json` → `buildNumber` | `13` | Auto-incremented on every build; shown in the app as `v1.9.5 (build 13)` |

Build number is auto-incremented by `scripts/increment-build.js`, which runs via npm `prebuild:mac` / `prebuild:win` hooks before every `electron-builder` run.

**Build commands:**

```bash
# macOS (Intel x64 + Apple Silicon arm64)
rm -rf dist && npm run build:mac

# Windows (x64 NSIS installer)
rm -rf dist && npm run build:win

# Both platforms
rm -rf dist && npm run build:all
```

> Always delete `dist/` before rebuilding to prevent electron-builder from serving cached packaged files.

**DMG sizes (build 13):**
| Target | Size |
|--------|------|
| macOS x64 (Intel) | ~105 MB |
| macOS arm64 (Apple Silicon) | ~99 MB |

Size reduction achieved via `electronLanguages: ["en"]` (prunes non-English Chromium locales) and exclusion of test directories, `*.md`, `*.map`, and other non-runtime files from node_modules.

---

## Running the App

### Option A — Electron desktop app (end users)
Install from the DMG (macOS) or EXE (Windows) in `dist/`. No Node.js required.

### Option B — Development (with Node.js)
```bash
npm install
node server.js          # starts Express on port 3000
# open http://localhost:3000/login in a browser
```

### Option C — Electron dev mode
```bash
npm install
npm run electron:dev    # starts Electron + embedded Express
```

### Option D — macOS double-click (non-technical users, no Electron)
Double-click `Start App.command` (requires Node.js installed on the machine).
Windows equivalent: `start.bat`.

---

## Testing

Integration tests are in `tests/create-zones.test.js`. They start the real Express server on a local port and mock all outbound Fynd platform API calls with `nock`. No test framework is required beyond Node.js built-ins.

```bash
node tests/create-zones.test.js
```

**Test coverage (16 tests across 5 suites):**

| Suite | Tests |
|-------|-------|
| Create Zones — single zone | Valid zone creation |
| Create Zones — multiple zones | Bulk (5 zones), skip invalid rows, skip duplicates, API failures, `pincode` column, mixed scenario |
| File validation | Reject unsupported file types, reject blank files |
| Plan Updates | Detect region changes, no-op on identical regions, multi-zone planning, unmatched slugs |
| Authentication | No cookie, token 401, token cache reuse |

Each test uses a unique session cookie to prevent token cache bleed between tests, and `nock.cleanAll()` is called before every test case to ensure a clean mock state.
