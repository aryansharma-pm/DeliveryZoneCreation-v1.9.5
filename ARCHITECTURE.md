# Delivery Zone Manager — Architecture & Flow

**Version:** 1.9.6 (build 13)
**Stack:** Electron 41 · Express 4 · ExcelJS · Axios · Nock (tests)

---

## Table of Contents

1. [Project Structure](#1-project-structure)
2. [High-Level Architecture](#2-high-level-architecture)
3. [Electron Layer](#3-electron-layer)
4. [Express Server](#4-express-server)
5. [API Endpoints](#5-api-endpoints)
6. [Core Functions](#6-core-functions)
7. [Frontend](#7-frontend)
8. [Authentication Flow](#8-authentication-flow)
9. [Zone Operation Flows](#9-zone-operation-flows)
10. [Data Model](#10-data-model)
11. [Concurrency & Performance](#11-concurrency--performance)
12. [Security](#12-security)
13. [Build & Distribution](#13-build--distribution)
14. [Testing](#14-testing)
15. [Configuration Reference](#15-configuration-reference)

---

## 1. Project Structure

```
DeliveryZoneCreation/
├── electron/
│   ├── main.js              # App lifecycle, window, auto-updater, IPC
│   └── preload.js           # Context bridge → exposes electronAPI to renderer
│
├── public/                  # Static frontend (served by Express)
│   ├── index.html           # Main app UI
│   ├── login.html           # OTP login page
│   ├── app.js               # Main UI logic
│   ├── login.js             # Login flow
│   ├── updater.js           # Auto-update UI
│   ├── styles.css           # Global design system
│   └── logo_black.svg       # Fynd brand asset
│
├── scripts/
│   └── increment-build.js   # Bumps buildNumber before each DMG/EXE build
│
├── tests/
│   ├── full-audit.test.js   # 80-test integration audit (all endpoints)
│   ├── dmg-load.test.js     # DMG integrity + 50k-row load/perf tests
│   └── create-zones.test.js # Unit tests for zone creation logic
│
├── server.js                # Express backend (all API + static serving)
├── package.json             # Dependencies, build scripts, electron-builder config
├── build-number.json        # Auto-incremented build counter { "buildNumber": 13 }
├── zonetesting2.py          # Python GUI testing utility (tkinter)
├── zonefile.csv             # Sample zone data
├── Start App.command        # macOS double-click launcher
└── start.bat                # Windows double-click launcher
```

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        ELECTRON SHELL                           │
│                                                                 │
│  ┌────────────────────┐        ┌──────────────────────────┐    │
│  │   BrowserWindow    │◄──────►│      IPC Bridge          │    │
│  │  (Chromium renderer│  IPC   │  get-app-version         │    │
│  │   localhost:PORT)  │        │  install-update          │    │
│  └────────┬───────────┘        │  check-for-updates       │    │
│           │ HTTP               └──────────┬───────────────┘    │
│           │                              │                      │
│  ┌────────▼───────────────────────────────▼───────────────┐    │
│  │                  EXPRESS SERVER (server.js)             │    │
│  │                                                         │    │
│  │  Static: /          → public/index.html                 │    │
│  │  Static: /login     → public/login.html                 │    │
│  │  API:    /api/*     → JSON responses                    │    │
│  │  Health: /health    → { ok: true }                      │    │
│  └────────────────────────────┬────────────────────────────┘    │
└───────────────────────────────│─────────────────────────────────┘
                                │ HTTPS / Axios
                                ▼
              ┌─────────────────────────────────────┐
              │         FYND PLATFORM API            │
              │                                      │
              │  auth:  api.jiox0.de (SIT)           │
              │  zones: api.jiox0.de (SIT)           │
              │  auth:  api.jiox5.de (UAT)           │
              │  zones: api.jiox5.de (UAT)           │
              │  auth:  api.jioretailer.com (PROD)   │
              │  zones: api.jioretailer.com (PROD)   │
              └─────────────────────────────────────┘
```

The app is a **self-contained desktop tool**: Electron spawns a local Express server, and the Chromium renderer communicates with it over `localhost`. No cloud backend is needed — the app talks directly to Fynd's platform APIs on behalf of the logged-in user.

---

## 3. Electron Layer

**File:** `electron/main.js`

### Window

| Property | Value |
|---|---|
| Default size | 1300 × 840 px |
| Minimum size | 920 × 620 px |
| nodeIntegration | `false` |
| contextIsolation | `true` |
| sandbox | `true` |
| Preload | `electron/preload.js` |

### Startup Sequence

```
1. app.whenReady()
2.   → startServer(0)          // Express binds to first free port
3.   → createWindow()          // Chromium loads http://127.0.0.1:{port}/login
4.   → setTimeout(5000)        // Delay to let window settle
5.   → autoUpdater.checkForUpdates()
```

### IPC Channels

| Channel | Direction | Handler |
|---|---|---|
| `get-app-version` | renderer → main | Returns `{ version, buildNumber }` |
| `install-update` | renderer → main | Calls `autoUpdater.quitAndInstall()` |
| `check-for-updates` | renderer → main | Calls `autoUpdater.checkForUpdates()` |
| `updater-status` | main → renderer | Emits `{ type, message, version, percent }` |

### Auto-Updater Events

```
checking-for-update   → updater-status { type: "checking" }
update-available      → updater-status { type: "available", version }
update-not-available  → updater-status { type: "up-to-date" }
download-progress     → updater-status { type: "downloading", percent }
update-downloaded     → updater-status { type: "downloaded", version }
error                 → updater-status { type: "error", message }
```

Provider: GitHub Releases (`owner/repo` from `package.json build.publish`).

### Context Bridge (`electron/preload.js`)

Exposes `window.electronAPI` to the renderer with only the needed methods — renderer cannot access Node.js APIs directly.

---

## 4. Express Server

**File:** `server.js`

### Middleware Stack (in order)

```
helmet()                   // Security headers (CSP disabled for CDN assets)
express.json()             // Parse JSON bodies
express.static("public")   // Serve frontend assets
multer (per-route)         // File upload: CSV/XLS/XLSX only, max 10 MB
loginLimiter               // Rate limit: 20 req / 15 min on login endpoints
```

### Port Selection

```javascript
// Prefers port 3000; if taken, OS assigns a free port
await startServer(0);      // 0 = OS-chosen
await startServer(3000);   // specific port
```

### Token Cache

Tokens are cached in a `Map` keyed by `SHA256(cookieString + envKey)`:

```
tokenCache: Map<sha256Key, { token, expiresAt }>
```

- JWT `exp` claim is decoded to set `expiresAt`
- 60-second skew buffer prevents using nearly-expired tokens
- 10-minute fallback TTL if JWT decode fails
- Expired entries are pruned on each cache access

---

## 5. API Endpoints

### Public / Utility

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | None | Health check → `{ ok: true }` |
| `GET` | `/api/version` | None | App version + build number |
| `GET` | `/api/environments` | None | List environments with labels and types |
| `POST` | `/api/clear-cookie-cache` | None | Evict cached token for a cookie/env pair |
| `POST` | `/api/parse-session-curl` | None | Parse cURL command → extract cookies/headers |
| `POST` | `/api/session-check` | Cookie | Validate session by attempting token fetch |

### Authentication (rate-limited: 20 req / 15 min)

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/login/send-otp` | Send OTP to email via Fynd auth API |
| `POST` | `/api/login/verify-otp` | Verify OTP → return session cookies |

### Zone Operations (require valid session cookie)

| Method | Path | File Required | Description |
|---|---|---|---|
| `POST` | `/api/fetch-zones` | No | Fetch all zones from platform (paginated) |
| `POST` | `/api/create-zones` | Yes | Create zones from uploaded file |
| `POST` | `/api/plan-updates` | Yes | Diff file vs. existing zones, return plan |
| `POST` | `/api/apply-updates` | Yes | Execute planned region updates |

---

## 6. Core Functions

### `getBearerToken(cookieString, envConfig)`

```
Input:  session cookies (string), environment config
Output: OAuth bearer token (string)

1. Normalize cookie string
2. Compute cache key = SHA256(cookie + env.key)
3. Check tokenCache — return cached if not expired
4. GET envConfig.tokenUrl with Cookie header
5. Decode JWT payload → extract exp claim
6. Store in cache with expiresAt = (exp - skew) * 1000
7. Return token
```

### `getZones(token, envConfig)`

```
Input:  bearer token, environment config
Output: zone[] (all pages combined)

1. page_no = 1, page_size = 500
2. Loop:
   GET apiBase?page_no=N&page_size=500
   Append items to result
   Stop when items.length < page_size
3. Return all zones
```

### `readRowsFromUploadedFile(file)`

```
Input:  multer file object (buffer)
Output: normalized row objects[]

1. Stream buffer through ExcelJS CSV/XLS/XLSX parser
2. Row 1 → headers (normalize: lowercase, trim, remove BOM)
3. Rows 2+ → { [header]: String(cell.value).trim() }
4. Filter blank rows
5. Throw if no usable rows found
```

### `buildZonePayload(row)`

```
Input:  normalized CSV row object
Output: Fynd zone creation payload

Validates: slug, store_ids, region_type, mapping_country,
           mapping_regions (or pincode), channels

Builds:
{
  name, slug, store_ids: [int],
  region_type: "pincode"|"state"|"country",
  is_active: bool,
  product: { type, tags: [] },
  channels: [{ name, type }],
  mapping: [{ country, regions: [] }]
}
```

### `createZonesFromRows(rows, token, existingZones, envConfig)`

```
Input:  parsed rows, bearer token, existing zones, env config
Output: { created, skipped, failed, logs }

Phase 1 — Synchronous validation:
  For each row:
    - Check required fields → skip if missing
    - buildZonePayload() → skip if invalid
    - Check existingSlugs Set → skip if duplicate
    - Reserve slug in Set (prevents within-CSV duplicates)
    - Add to toCreate[]

Phase 2 — Concurrent API calls (ZONE_WRITE_CONCURRENCY = 10):
  runConcurrent(toCreate, async ({ payload }) => {
    POST apiBase with payload
    return { slug, status, data }
  })

Tally results → return summary
```

### `planZoneUpdates(rows, existingZones)`

```
Input:  parsed rows, existing zones[]
Output: { updates[], logs[] }

1. buildExistingLookup(existingZones)
   → Map<"slug::storeId", { zoneId, oldRegions: Set }>

2. For each row:
   - Parse new regions from mapping_regions / pincode
   - Look up zone by slug + store_id
   - Compare newRegions vs oldRegions (Set equality)
   - If different → add to updates[]
   - If no match  → log skip

3. Deduplicate updates by zoneId
4. Return updates (no API calls made here)
```

### `applyZoneUpdates(updates, token, envConfig)`

```
Input:  planned updates[], bearer token, env config
Output: { updated, failed, logs }

Concurrent API calls (ZONE_WRITE_CONCURRENCY = 10):
  For each update:
    Build PUT payload with zone_id + sorted new regions
    PUT apiBase/{zoneId}
    Tally success / failure
```

### `runConcurrent(items, fn, concurrency)`

```
Worker-pool pattern — keeps exactly N tasks in-flight at all times:

workers = min(concurrency, items.length)
Each worker: while(items remaining) → pick next → await fn() → loop

Avoids batching lag (one slow item doesn't block a whole batch).
```

---

## 7. Frontend

### Pages

| Page | File | Script |
|---|---|---|
| Login | `login.html` | `login.js` |
| Main app | `index.html` | `app.js`, `updater.js` |

### `app.js` — State & Responsibilities

```
State:
  currentEnv   string        Active environment key ("sit" / "uat" / "prod")
  envMeta      object        { sit: { label, type }, ... }  ← from /api/environments
  appState     { zones[], updates[] }

Key functions:
  initAccountDisplay()   Verify session or redirect to /login
  loadEnvironments()     Fetch /api/environments → populate envMeta + type pill
  applyEnv(env)          Switch env → update badge, type pill, env tabs, hint
  runAction(fn, opts)    Wraps async ops: setBusy → fn() → clearBusy + toast
  renderZonesTable()     Virtual render with search + sort
  renderUpdatesTable()   Virtual render with search
  showConfirm(title, msg) Promise-based confirm modal
  showToast(msg, type)   Ephemeral notification (max 4 simultaneous)
```

### `login.js` — OTP Flow

```
State:
  currentEnv     Active environment
  loginEnvMeta   { sit: { type }, ... }
  requestId      OTP request ID from send-otp
  sessionCookies Pre-existing cookies (optional for some endpoints)

Flow:
  Step 1: Email input → POST /api/login/send-otp
  Step 2: OTP input   → POST /api/login/verify-otp
  Step 3: Success     → store cookie, email → redirect to /

Footer: dynamically shows "v1.9.6 · Platform + Storefront" for SIT env
```

### `updater.js` — Auto-Update UI

Listens for `window.electronAPI.onUpdaterStatus` events and shows the update banner with progress / install button. No-op in browser mode (`window.electronAPI` absent).

---

## 8. Authentication Flow

```
┌──────────────┐     POST /api/login/send-otp          ┌──────────────────┐
│  User enters │ ──────────────────────────────────────►│  Fynd Auth API   │
│  email addr  │     { email, env }                     │  POST .../otp/   │
│              │ ◄──────────────────────────────────────│  send?origin=    │
│              │     { ok, requestId, resendTimer }      │  platform        │
└──────┬───────┘                                        └──────────────────┘
       │
       │  User receives OTP in email
       ▼
┌──────────────┐     POST /api/login/verify-otp         ┌──────────────────┐
│  User enters │ ──────────────────────────────────────►│  Fynd Auth API   │
│  OTP code    │     { email, otp, requestId, env }      │  POST .../otp/   │
│              │ ◄──────────────────────────────────────│  verify?origin=  │
│              │     { ok, cookieString }                │  platform        │
└──────┬───────┘                                        └──────────────────┘
       │
       │  cookieString stored in localStorage ("dzm.cookie")
       │  email stored in localStorage ("dzm.email")
       │
       ▼  redirect to /
┌──────────────┐
│   Main app   │  All subsequent requests include cookieString as credential
│  (app.js)    │  Server exchanges it for bearer token on first use per env
└──────────────┘

OTP Endpoint Fallback:
  1. Try POST .../email/otp/verify?origin=platform
  2. If 404 only → retry POST .../otp/verify?origin=platform
  Any other 4xx (400 = wrong OTP) surfaces immediately without fallback.
```

---

## 9. Zone Operation Flows

### Fetch Zones

```
User clicks "Fetch Zones"
    │
    ▼
POST /api/fetch-zones  { cookieString, env, includeDetails }
    │
    ├─ getBearerToken(cookie, env)
    │      └─ GET tokenUrl → cache → return bearer
    │
    ├─ getZones(token, env)
    │      └─ paginated GET apiBase?page_no=1&page_size=500
    │         repeat until items < 500
    │
    └─ if includeDetails:
           enrichZonesWithDetails(zones, token, env)
               └─ GET apiBase/{zone_id} × N  (DETAIL_FETCH_CONCURRENCY = 6)
    │
    ▼
Response: { ok, zones[], total }
    │
    ▼
app.js renders zones table (search + sort)
```

### Create Zones

```
User selects CSV → clicks "Create From File"
    │
    ▼
POST /api/create-zones (multipart: file + cookieString + env)
    │
    ├─ multer: validate type (csv/xls/xlsx), size (≤ 10 MB)
    ├─ getBearerToken()
    ├─ getZones() → existingZones
    ├─ readRowsFromUploadedFile() → rows[]
    │
    ├─ createZonesFromRows(rows, token, existingZones, env)
    │      Phase 1 (sync):
    │        for each row:
    │          validate fields → skip
    │          build payload   → skip on error
    │          check slug dupe → skip
    │          reserve slug
    │          push to toCreate[]
    │
    │      Phase 2 (concurrent × 10):
    │        POST apiBase × toCreate.length
    │
    ▼
Response: { ok, summary: { created, skipped, failed, totalRows }, logs[] }
```

### Plan + Apply Updates

```
                   CSV FILE
                      │
          ┌───────────┴───────────┐
          │                       │
          ▼                       ▼
  POST /api/plan-updates    (stores plan in client state)
          │
          ├─ getBearerToken()
          ├─ getZones() → existingZones
          ├─ readRowsFromUploadedFile() → rows[]
          ├─ planZoneUpdates(rows, existingZones)
          │      build lookup: slug::storeId → { zoneId, oldRegions }
          │      diff each row's newRegions vs oldRegions
          │      collect updates[]
          ▼
  Response: { ok, updates[], summary: { plannedUpdates }, logs[] }
          │
          │  User reviews plan in "Planned Updates" table
          │
          ▼
  POST /api/apply-updates (same file)
          │
          ├─ getBearerToken()
          ├─ getZones() → existingZones
          ├─ readRowsFromUploadedFile() → rows[]
          ├─ planZoneUpdates() → recompute updates (fresh)
          ├─ applyZoneUpdates(updates, token, env)
          │      concurrent × 10:
          │        PUT apiBase/{zoneId} with new regions
          ▼
  Response: { ok, summary: { updated, failed, totalRows }, logs[] }
```

---

## 10. Data Model

### CSV Input Row (normalized)

```
slug             string   Zone identifier (unique per platform)
name             string   Display name
store_ids        string   Comma-separated store IDs  "101,102,103"
region_type      string   "pincode" | "state" | "country"
mapping_country  string   ISO code  "IN"
mapping_regions  string   Comma-separated region codes  "110001,110002"
channels         string   Comma-separated channel identifiers
company_id       string   Company identifier
is_active        string   "true" | "false"
product_type     string   "all" | "specific"
product_tags     string   Comma-separated tags (optional)
```

### Zone Payload (sent to Fynd API)

```json
{
  "name": "Zone Name",
  "slug": "zone-slug",
  "store_ids": [101, 102],
  "region_type": "pincode",
  "is_active": true,
  "product": {
    "type": "all",
    "tags": []
  },
  "channels": [
    { "name": "ch-1", "type": "channel" }
  ],
  "mapping": [
    {
      "country": "IN",
      "regions": ["110001", "110002"]
    }
  ]
}
```

### Environment Config

```javascript
{
  label:                 "SIT",
  type:                  "Platform + Storefront",   // SIT only
  platformOrigin:        "https://platform.jiox0.de",
  apiBase:               "https://api.jiox0.de/.../zones",
  tokenUrl:              "https://api.jiox0.de/.../oauth/staff/token",
  authBase:              "https://api.jiox0.de/.../authentication/v1.0",
  sessionDomainPattern:  /(\.|^)jiox0\.de$/i,
}
```

---

## 11. Concurrency & Performance

### Constants

| Constant | Value | Applies To |
|---|---|---|
| `ZONE_WRITE_CONCURRENCY` | 10 | Zone create (POST) + apply (PUT) |
| `DETAIL_FETCH_CONCURRENCY` | 6 | Zone detail enrichment (GET per zone) |
| `REQUEST_TIMEOUT` | 30 000 ms | All Axios requests |

### Throughput Benchmarks (mocked API, ~3 ms/req)

| Operation | Rows | Time | Throughput |
|---|---|---|---|
| Create zones | 1 000 | ~1.2 s | ~800 rows/sec |
| Create zones | 10 000 | ~37 s | ~270 rows/sec |
| Create zones | 50 000 | ~207 s | ~241 rows/sec |
| Plan updates | 50 000 | ~34 s | ~1 500 rows/sec |
| Apply updates | 50 000 | ~200 s | ~250 rows/sec |

### Real-API Estimate

At ~200 ms per Fynd API call with concurrency 10:

```
50 000 zones × 200 ms / 10 parallel ≈ 17 minutes
```

> **Practical recommendation:** Keep batches ≤ 5 000 rows per session for interactive use. Use Plan → Apply workflow to verify before committing large changes.

### Worker-Pool Pattern

```javascript
async function runConcurrent(items, fn, concurrency) {
  let next = 0;
  async function worker() {
    while (next < items.length) {
      const idx = next++;
      results[idx] = await fn(items[idx], idx);
    }
  }
  await Promise.all(
    Array.from({ length: Math.min(concurrency, items.length) }, worker)
  );
}
```

Unlike batch-based approaches, this keeps exactly `concurrency` calls in-flight at all times — a slow request does not block peers.

---

## 12. Security

| Layer | Mechanism |
|---|---|
| HTTP headers | `helmet()` — sets HSTS, X-Frame-Options, X-Content-Type-Options, etc. |
| Login rate limit | `express-rate-limit` — 20 requests / 15 min on `/api/login/*` |
| File uploads | `multer` — allows only `.csv`, `.xls`, `.xlsx`; rejects files > 10 MB |
| Token storage | Server-side in-memory `Map` only; never written to disk |
| Session cookies | Passed by client on each request; validated against Fynd API |
| Token expiry | JWT `exp` decoded; 60-second skew buffer prevents stale tokens |
| Renderer isolation | Electron: `contextIsolation: true`, `sandbox: true`, `nodeIntegration: false` |
| IPC surface | Only 3 handlers exposed via preload context bridge |
| OTP fallback | Only retries on HTTP 404 (endpoint not found), not on 400 (wrong OTP) |
| CSV cell trimming | All cell values trimmed on read — prevents whitespace mismatches |

---

## 13. Build & Distribution

### Scripts

| Command | Action |
|---|---|
| `npm start` | Run Express server only (browser / web mode) |
| `npm run electron:dev` | Run full Electron app in development mode |
| `npm run build:mac` | Increment build# → produce `.dmg` (x64 + arm64) |
| `npm run build:win` | Increment build# → produce `.exe` (NSIS installer, x64) |
| `npm run build:all` | Build both platforms |

### Build Number

`scripts/increment-build.js` runs as a `prebuild:*` hook — reads `build-number.json`, increments `buildNumber`, writes back. This counter is bundled into the ASAR and exposed via `/api/version`.

### ASAR Bundle

Files included in the ASAR:

```
electron/**/*
public/**/*
server.js
package.json
build-number.json
node_modules/**/*   (excluding: electron, electron-builder, sharp, .cache, test dirs)
```

Files excluded: source maps, markdown files, changelogs, CI configs.

### Targets

| Platform | Format | Architectures |
|---|---|---|
| macOS | `.dmg` | `x64`, `arm64` |
| Windows | `.exe` (NSIS one-click) | `x64` |

### Auto-Update

Provider: **GitHub Releases** — configure `owner` and `repo` in `package.json build.publish` before publishing.

---

## 14. Testing

### Test Files

| File | Tests | Focus |
|---|---|---|
| `tests/full-audit.test.js` | 80 | All 11 API endpoints, edge cases, error paths |
| `tests/dmg-load.test.js` | 28 | DMG integrity, 50k-row load, memory, latency |
| `tests/create-zones.test.js` | — | Zone creation unit tests |

### Running Tests

```bash
# Full API audit (source server)
node tests/full-audit.test.js

# DMG load + performance (requires extracted ASAR)
npx @electron/asar extract dist/mac/DeliveryZoneManager.app/Contents/Resources/app.asar /tmp/dzm-extracted
node tests/dmg-load.test.js
```

### Test Strategy

- **No real network calls** — `nock` intercepts all outbound HTTP
- `nock.disableNetConnect()` + `nock.enableNetConnect("127.0.0.1")`
- Fresh cookie per test prevents token-cache bleed
- `nock.cleanAll()` before every test case
- `.times(N)` used instead of `.persist()` (nock v14 API)

### Key Test Scenarios

| Suite | Scenario |
|---|---|
| Auth | Send OTP, verify OTP, wrong OTP (400 not retried), 404 fallback |
| Create | Single row, 50k rows, missing slug, duplicate slug, BOM headers |
| Plan | Match existing, no match, mixed |
| Apply | All updated, some failed, 50k concurrent |
| File | Empty file, header-only, .txt rejected, 10 MB accepted, 11 MB rejected |
| Perf | Latency distribution (p50/p95/p99), memory delta < 300 MB at 50k |

---

## 15. Configuration Reference

### Environment Keys

| Key | SIT | UAT | PROD |
|---|---|---|---|
| API domain | `api.jiox0.de` | `api.jiox5.de` | `api.jioretailer.com` |
| Platform URL | `platform.jiox0.de` | `platform.jiox5.de` | `platform.jioretailer.com` |
| Type label | Platform + Storefront | — | — |

### Server Constants (`server.js`)

| Constant | Value | Description |
|---|---|---|
| `DEFAULT_ENV` | `"sit"` | Fallback environment if none specified |
| `REQUEST_TIMEOUT` | `30 000` ms | Axios timeout for all outbound requests |
| `TOKEN_FALLBACK_TTL_MS` | `600 000` ms | Token cache TTL when JWT decode fails |
| `TOKEN_EXPIRY_SKEW_MS` | `60 000` ms | Early-refresh buffer before token expires |
| `DETAIL_FETCH_CONCURRENCY` | `6` | Parallel GET calls for zone detail enrichment |
| `ZONE_WRITE_CONCURRENCY` | `10` | Parallel POST/PUT calls for create/apply |
| File size limit | `10 MB` | multer `limits.fileSize` |
| Login rate limit | `20 / 15 min` | Per IP on `/api/login/*` |
| Zones page size | `500` | Items per page in `getZones()` pagination |

### Electron Window (`electron/main.js`)

| Property | Value |
|---|---|
| Default size | `1300 × 840` px |
| Minimum size | `920 × 620` px |
| Auto-download updates | `true` |
| Auto-install on quit | `true` |
| Update check delay | `5 000` ms after launch |
