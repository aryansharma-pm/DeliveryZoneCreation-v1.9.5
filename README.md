# Delivery Zone Manager — v1.9.5

An internal operations console to manage platform delivery zones across SIT, UAT, and PROD environments. Supports session-based authentication, file-driven zone creation/updates, and live audit logs.

---

## Features

| Feature | Description |
|---|---|
| **Email OTP Login** | Login directly from the UI — no manual cookie copying needed |
| **Multi-Environment** | Switch between SIT / UAT / PROD with a single click |
| **Fetch Zones** | Paginated zone listing with parallel detail enrichment |
| **Create Zones** | Bulk-create zones from a CSV/XLS/XLSX file |
| **Plan Updates** | Dry-run region updates before applying — shows diff |
| **Apply Updates** | Apply planned zone region changes with confirmation |
| **Audit Logs** | Timestamped log panel with copy-to-clipboard |
| **Token Cache** | Bearer tokens are cached per-env until expiry |

---

## Quick Start

### Prerequisites

- Node.js 18+
- Python 3.9+ (for CLI tool only)

### Install & Run (Web App)

```bash
npm install
node server.js
# Open http://localhost:3000
```

### CLI Tool (Python)

```bash
pip3 install pandas requests openpyxl browser-cookie3
python3 zonetesting2.py --help
```

---

## Login Flow

Instead of manually copying cookies from DevTools, use the built-in login:

1. Click **"Login with email OTP"** in the Session Setup section
2. Select the correct environment (SIT / UAT / PROD)
3. Enter your platform email (e.g. `you@gofynd.com`)
4. Click **Send OTP** — a 6-digit code will be emailed to you
5. Enter the OTP and click **Verify & Login**
6. The cookie is auto-filled and saved if "Remember cookie" is checked

> The login flow proxies requests through the Node.js server to the platform auth API. Cookies are returned via `Set-Cookie` response headers and auto-populated in the UI.

---

## CSV / XLSX File Format

Required columns for create and update operations:

| Column | Required | Description |
|---|---|---|
| `slug` | Yes | Unique zone identifier |
| `name` | No | Display name (defaults to slug) |
| `store_ids` | Yes | Comma-separated store IDs e.g. `101,102` |
| `region_type` | Yes | `pincode` or `non-pincode` |
| `mapping_country` | Yes | Country code e.g. `IN` |
| `mapping_regions` | Yes | Comma-separated region/pincode list |
| `channels` | Yes | Comma-separated channel IDs |
| `is_active` | No | `true` / `false` (default: `true`) |
| `company_id` | No | Company ID (default: `1`) |
| `product_type` | No | `all` or `explicit` (default: `all`) |
| `product_tags` | No | Comma-separated product tags |

**Example row:**

```
slug,name,store_ids,region_type,mapping_country,mapping_regions,channels,is_active
zone-mumbai,Mumbai Zone,"101,102",pincode,IN,"400001,400002,400003",app123,true
```

---

## Environments

| Environment | API Domain | Platform |
|---|---|---|
| SIT | `api.jiox0.de` | `platform.jiox0.de` |
| UAT | `api.jiox5.de` | `platform.jiox5.de` |
| PROD | `api.jioretailer.com` | `platform.jioretailer.com` |

All environments are configurable in `server.js` under the `ENVIRONMENTS` object.

---

## Architecture

```
Browser (HTML/CSS/JS)
    │
    ├── GET  /                         → index.html
    ├── POST /api/login/send-otp       → Proxy to platform auth API
    ├── POST /api/login/verify-otp     → Proxy + capture Set-Cookie
    ├── POST /api/fetch-zones          → Get + parallel-enrich zones
    ├── POST /api/create-zones         → Bulk create from file
    ├── POST /api/plan-updates         → Dry-run update diff
    ├── POST /api/apply-updates        → Apply planned updates
    ├── POST /api/session-check        → Validate session via cURL
    ├── POST /api/parse-session-curl   → Parse cURL into session request
    ├── POST /api/clear-cookie-cache   → Evict cached bearer token
    └── GET  /api/environments         → List available environments
```

**Token caching:** Bearer tokens are cached in-memory keyed by `env + cookie hash`, and automatically evicted 60 seconds before JWT expiry.

**Parallel enrichment:** Zone detail fetching uses concurrent batches of 6 requests (matching the Python CLI's `ThreadPoolExecutor` behaviour).

---

## Python CLI

The CLI (`zonetesting2.py`) provides the same functionality with an optional Tkinter GUI and supports:

- `--env sit|uat|prod`
- `--cookie` / auto-load from browser via `browser-cookie3`
- `--ca-bundle` / `--insecure-skip-tls-verify` for enterprise proxies
- `--file` for CSV/XLSX input
- `fetch`, `create`, `plan`, `apply` subcommands

```bash
python3 zonetesting2.py fetch --env sit --cookie "your-cookie-here"
python3 zonetesting2.py create --env sit --file zones.csv --cookie "..."
python3 zonetesting2.py plan   --env sit --file updates.csv --cookie "..."
python3 zonetesting2.py apply  --env sit --file updates.csv --cookie "..."
```

---

## Local Development

```bash
# Set custom port
PORT=8080 node server.js

# Increase parallel detail workers (default: 6)
# Edit DETAIL_FETCH_CONCURRENCY in server.js
```

---

## Notes

- **PROD** changes are irreversible — always run **Plan Updates** first
- The `Apply Updates` button shows a confirmation modal that includes the environment name
- Cookies stored via "Remember cookie" are saved to `localStorage` — clear them with the **Clear Saved Cookie** button
- The session cURL test validates your auth before each action when the checkbox is enabled
