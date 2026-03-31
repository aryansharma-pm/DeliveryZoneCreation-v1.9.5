/**
 * Integration tests — zone creation & update logic
 * Run: node tests/create-zones.test.js
 */

"use strict";

const assert   = require("node:assert/strict");
const http     = require("node:http");
const FormData = require("form-data");
const nock     = require("nock");

// ── Config ────────────────────────────────────────────────────────────────────
const TEST_PORT  = 3799;
const SIT_API    = "https://api.jiox0.de";
const TOKEN_PATH = "/service/panel/authentication/v1.0/company/1/oauth/staff/token";
const ZONES_PATH = "/service/platform/logistics/v2.0/company/1/zones";

// Each test uses a unique cookie so the token cache never bleeds between tests
let testId = 0;
const freshCookie = () => `uid=test-session-${++testId}`;

const mkToken = () =>
  "eyJhbGciOiJIUzI1NiJ9." +
  Buffer.from(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 3600 })).toString("base64url") +
  ".sig";

// ── HTTP helper ───────────────────────────────────────────────────────────────
function postFile(path, csvContent, filename, extraFields = {}) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", Buffer.from(csvContent), {
      filename: filename || "zones.csv",
      contentType: "text/csv",
    });
    for (const [k, v] of Object.entries(extraFields)) form.append(k, v);

    const req = http.request(
      { hostname: "127.0.0.1", port: TEST_PORT, path, method: "POST", headers: form.getHeaders() },
      (res) => {
        let raw = "";
        res.on("data", (c) => { raw += c; });
        res.on("end", () => {
          try {
            resolve({ status: res.statusCode, body: JSON.parse(raw) });
          } catch (_) {
            // Non-JSON response (e.g. multer HTML error) — treat as generic failure
            resolve({ status: res.statusCode, body: { ok: false, rawText: raw } });
          }
        });
      }
    );
    req.on("error", reject);
    form.pipe(req);
  });
}

// ── Nock helpers ──────────────────────────────────────────────────────────────
/** Call at the start of each test to wipe stale mocks */
function resetMocks() { nock.cleanAll(); }

function mockToken(token) {
  nock(SIT_API).get(TOKEN_PATH).reply(200, { access_token: token || mkToken() });
}
function mockZones(items = []) {
  nock(SIT_API).get(ZONES_PATH).query(true).reply(200, { items });
}
function mockSetup(items = []) { mockToken(); mockZones(items); }

// ── CSV helpers ───────────────────────────────────────────────────────────────
/** Properly quote CSV values that contain commas, quotes, or newlines */
function escapeCSV(v) {
  const s = String(v ?? "");
  return (s.includes(",") || s.includes('"') || s.includes("\n"))
    ? `"${s.replace(/"/g, '""')}"`
    : s;
}

function toCSV(rows) {
  if (!rows.length) return "";
  const headers = Object.keys(rows[0]);
  return [
    headers.map(escapeCSV).join(","),
    ...rows.map((r) => headers.map((h) => escapeCSV(r[h] ?? "")).join(",")),
  ].join("\n");
}

function row(overrides = {}) {
  return {
    slug:            "test-zone-01",
    name:            "Test Zone 01",
    company_id:      "1",
    store_ids:       "101,102",
    region_type:     "pincode",
    mapping_country: "India",
    mapping_regions: "400001,400002,400003",
    channels:        "app001",
    is_active:       "true",
    product_type:    "all",
    product_tags:    "",
    ...overrides,
  };
}

// ── Minimal test runner ───────────────────────────────────────────────────────
let passed = 0, failed = 0;
const results = [];

async function test(label, fn) {
  resetMocks(); // clean slate before every test
  try {
    await fn();
    console.log(`  ✔  ${label}`);
    passed++;
    results.push({ label, ok: true });
  } catch (err) {
    console.log(`  ✖  ${label}`);
    console.log(`     → ${err.message}`);
    failed++;
    results.push({ label, ok: false, error: err.message });
  }
}

function suite(name) { console.log(`\n▶ ${name}`); }

// ── Tests ─────────────────────────────────────────────────────────────────────
async function runTests() {

  // ── SUITE 1: Single zone ───────────────────────────────────────────────────
  suite("Create Zones — single zone");

  await test("creates one valid zone successfully", async () => {
    const cookie = freshCookie();
    mockSetup([]);
    nock(SIT_API).post(ZONES_PATH).reply(201, { zone_id: "z001" });

    const res = await postFile("/api/create-zones", toCSV([row()]), "zones.csv", {
      cookieString: cookie, env: "sit",
    });
    assert.equal(res.body.ok,               true,  "ok=true");
    assert.equal(res.body.summary.created,  1,     "created=1");
    assert.equal(res.body.summary.skipped,  0,     "skipped=0");
    assert.equal(res.body.summary.failed,   0,     "failed=0");
  });

  // ── SUITE 2: Multiple zones ────────────────────────────────────────────────
  suite("Create Zones — multiple zones in one file");

  await test("creates 5 zones in sequence, all successful", async () => {
    const cookie = freshCookie();
    mockSetup([]);
    for (let i = 1; i <= 5; i++) nock(SIT_API).post(ZONES_PATH).reply(201, { zone_id: `z00${i}` });

    const rows = Array.from({ length: 5 }, (_, i) =>
      row({ slug: `bulk-zone-0${i + 1}`, mapping_regions: `40000${i + 1}` })
    );
    const res = await postFile("/api/create-zones", toCSV(rows), "bulk.csv", {
      cookieString: cookie, env: "sit",
    });
    assert.equal(res.body.summary.created, 5, "all 5 created");
    assert.equal(res.body.summary.skipped, 0);
    assert.equal(res.body.summary.failed,  0);
  });

  await test("skips rows with missing required fields, creates valid ones", async () => {
    const cookie = freshCookie();
    mockSetup([]);
    nock(SIT_API).post(ZONES_PATH).times(2).reply(201, { zone_id: "zX" });

    const rows = [
      row({ slug: "valid-01" }),
      row({ slug: "" }),                                         // missing slug
      row({ slug: "valid-02" }),
      row({ slug: "no-regions", mapping_regions: "", pincode: "" }), // missing regions
    ];
    const res = await postFile("/api/create-zones", toCSV(rows), "mixed.csv", {
      cookieString: cookie, env: "sit",
    });
    assert.equal(res.body.summary.created, 2, "2 valid rows created");
    assert.equal(res.body.summary.skipped, 2, "2 invalid rows skipped");
  });

  await test("skips slugs that already exist on the platform", async () => {
    const cookie = freshCookie();
    mockSetup([{
      zone_id: "z-existing", slug: "already-exists",
      store_ids: [101], mapping: [{ country: "India", regions: ["400001"] }],
    }]);
    nock(SIT_API).post(ZONES_PATH).reply(201, { zone_id: "z-new" });

    const rows = [
      row({ slug: "already-exists" }),
      row({ slug: "brand-new" }),
    ];
    const res = await postFile("/api/create-zones", toCSV(rows), "dupes.csv", {
      cookieString: cookie, env: "sit",
    });
    assert.equal(res.body.summary.created, 1, "only new slug created");
    assert.equal(res.body.summary.skipped, 1, "existing slug skipped");
  });

  await test("records platform 400 failures while continuing other rows", async () => {
    const cookie = freshCookie();
    mockSetup([]);
    nock(SIT_API).post(ZONES_PATH).reply(201, { zone_id: "z001" });
    nock(SIT_API).post(ZONES_PATH).reply(400, { message: "Invalid regions" });
    nock(SIT_API).post(ZONES_PATH).reply(201, { zone_id: "z003" });

    const rows = [
      row({ slug: "ok-zone-01" }),
      row({ slug: "bad-zone-02" }),
      row({ slug: "ok-zone-03" }),
    ];
    const res = await postFile("/api/create-zones", toCSV(rows), "failures.csv", {
      cookieString: cookie, env: "sit",
    });
    assert.equal(res.body.summary.created, 2, "2 succeeded");
    assert.equal(res.body.summary.failed,  1, "1 failed");
    assert.ok(res.body.logs.some((l) => l.includes("bad-zone-02")), "failure logged");
  });

  await test("accepts 'pincode' column as alternative to 'mapping_regions'", async () => {
    const cookie = freshCookie();
    mockSetup([]);
    nock(SIT_API).post(ZONES_PATH).reply(201, { zone_id: "zP01" });

    const r = { ...row(), pincode: "486881,487441" };
    delete r.mapping_regions;

    const res = await postFile("/api/create-zones", toCSV([r]), "pincode-col.csv", {
      cookieString: cookie, env: "sit",
    });
    assert.equal(res.body.summary.created, 1, "created with pincode column");
    assert.equal(res.body.summary.skipped, 0);
  });

  await test("mixed scenario: valid / duplicate / missing-field / API-failure", async () => {
    const cookie = freshCookie();
    mockSetup([{
      zone_id: "ze1", slug: "dupe-zone",
      store_ids: [1], mapping: [{ country: "India", regions: ["111"] }],
    }]);
    nock(SIT_API).post(ZONES_PATH).reply(201, { zone_id: "zN1" });  // valid-new-01 OK
    nock(SIT_API).post(ZONES_PATH).reply(500, { message: "Error" }); // valid-new-02 fails

    const rows = [
      row({ slug: "valid-new-01" }),   // created ✓
      row({ slug: "dupe-zone" }),      // skipped (exists)
      row({ slug: "" }),               // skipped (missing slug)
      row({ slug: "valid-new-02" }),   // failed (API 500)
    ];
    const res = await postFile("/api/create-zones", toCSV(rows), "comprehensive.csv", {
      cookieString: cookie, env: "sit",
    });
    assert.equal(res.body.summary.totalRows, 4, "4 total rows");
    assert.equal(res.body.summary.created,  1, "1 created");
    assert.equal(res.body.summary.skipped,  2, "2 skipped");
    assert.equal(res.body.summary.failed,   1, "1 failed");
  });

  // ── SUITE 3: File validation ───────────────────────────────────────────────
  suite("Create Zones — file validation");

  await test("rejects unsupported file type (.txt)", async () => {
    const cookie = freshCookie();
    const res = await postFile("/api/create-zones", "col1,col2\nv1,v2", "data.txt", {
      cookieString: cookie, env: "sit",
    });
    // Multer rejects before JSON is sent — either JSON ok:false or raw HTML error
    assert.equal(res.body.ok, false, "txt file rejected");
  });

  await test("rejects file with only blank/empty rows", async () => {
    const cookie = freshCookie();
    mockSetup([]);
    const content = "slug,store_ids,region_type,mapping_country,mapping_regions,channels\n,,,,,";
    const res = await postFile("/api/create-zones", content, "blank.csv", {
      cookieString: cookie, env: "sit",
    });
    assert.equal(res.body.ok, false, "blank-row file should error");
  });

  // ── SUITE 4: Plan Updates ──────────────────────────────────────────────────
  suite("Plan Updates");

  await test("detects region change for an existing zone", async () => {
    const cookie = freshCookie();
    mockSetup([{
      zone_id: "z-abc", slug: "my-zone", store_ids: [101, 102], region_type: "pincode",
      mapping: [{ country: "India", regions: ["400001", "400002"] }],
    }]);

    const r = row({ slug: "my-zone", store_ids: "101,102", mapping_regions: "400001,400002,400099" });
    const res = await postFile("/api/plan-updates", toCSV([r]), "update.csv", {
      cookieString: cookie, env: "sit",
    });

    assert.equal(res.body.ok, true);
    assert.equal(res.body.summary.plannedUpdates,      1, "1 update planned");
    assert.equal(res.body.updates[0].zoneId,           "z-abc");
    assert.equal(res.body.updates[0].newRegionsCount,  3, "3 new regions");
    assert.equal(res.body.updates[0].oldRegionsCount,  2, "2 old regions");
  });

  await test("no updates when regions are already identical", async () => {
    const cookie = freshCookie();
    mockSetup([{
      zone_id: "z-abc", slug: "my-zone", store_ids: [101],
      mapping: [{ country: "India", regions: ["400001", "400002"] }],
    }]);

    const r = row({ slug: "my-zone", store_ids: "101", mapping_regions: "400001,400002" });
    const res = await postFile("/api/plan-updates", toCSV([r]), "no-change.csv", {
      cookieString: cookie, env: "sit",
    });
    assert.equal(res.body.summary.plannedUpdates, 0, "no updates needed");
  });

  await test("plans updates for multiple zones in one file", async () => {
    const cookie = freshCookie();
    mockSetup([
      { zone_id: "z1", slug: "zone-alpha", store_ids: [10], mapping: [{ country: "India", regions: ["111"] }] },
      { zone_id: "z2", slug: "zone-beta",  store_ids: [20], mapping: [{ country: "India", regions: ["222"] }] },
      { zone_id: "z3", slug: "zone-gamma", store_ids: [30], mapping: [{ country: "India", regions: ["333"] }] },
    ]);

    const rows = [
      row({ slug: "zone-alpha", store_ids: "10", mapping_regions: "111,999" }), // changed
      row({ slug: "zone-beta",  store_ids: "20", mapping_regions: "222" }),     // unchanged
      row({ slug: "zone-gamma", store_ids: "30", mapping_regions: "333,888" }), // changed
    ];
    const res = await postFile("/api/plan-updates", toCSV(rows), "multi.csv", {
      cookieString: cookie, env: "sit",
    });
    assert.equal(res.body.summary.plannedUpdates, 2, "2 of 3 zones planned");
  });

  await test("reports no updates for slugs not found on platform", async () => {
    const cookie = freshCookie();
    mockSetup([]);
    const res = await postFile("/api/plan-updates", toCSV([row({ slug: "ghost-zone" })]), "ghost.csv", {
      cookieString: cookie, env: "sit",
    });
    assert.equal(res.body.summary.plannedUpdates, 0);
    assert.ok(res.body.logs.some((l) => l.includes("ghost-zone")), "unmatched slug logged");
  });

  // ── SUITE 5: Authentication ────────────────────────────────────────────────
  suite("Authentication");

  await test("returns error when no cookie is provided", async () => {
    const res = await postFile("/api/create-zones", toCSV([row()]), "zones.csv", {
      cookieString: "", env: "sit",
    });
    assert.equal(res.body.ok, false);
    assert.ok(res.body.error.toLowerCase().includes("cookie"), "error mentions cookie");
  });

  await test("returns error when platform token endpoint returns 401", async () => {
    // Use a fresh cookie so there's no cached token for this session
    const cookie = freshCookie();
    nock(SIT_API).get(TOKEN_PATH).reply(401, { message: "Unauthorized" });

    const res = await postFile("/api/create-zones", toCSV([row()]), "zones.csv", {
      cookieString: cookie, env: "sit",
    });
    assert.equal(res.body.ok, false, "should fail");
    assert.ok(
      res.body.error.includes("401") || res.body.error.toLowerCase().includes("token"),
      `error should mention 401 or token, got: ${res.body.error}`
    );
  });

  await test("cached token reused across two requests (no extra token call)", async () => {
    const cookie = freshCookie();
    const token  = mkToken();

    // First request — token fetched and cached
    nock(SIT_API).get(TOKEN_PATH).reply(200, { access_token: token });
    mockZones([]);
    nock(SIT_API).post(ZONES_PATH).reply(201, { zone_id: "z-r1" });
    const r1 = await postFile("/api/create-zones", toCSV([row({ slug: "cache-test-01" })]), "c1.csv", {
      cookieString: cookie, env: "sit",
    });
    assert.equal(r1.body.ok, true, "first request ok");

    // Second request — token mock NOT set up; server must use cache
    resetMocks();
    mockZones([]);
    nock(SIT_API).post(ZONES_PATH).reply(201, { zone_id: "z-r2" });
    const r2 = await postFile("/api/create-zones", toCSV([row({ slug: "cache-test-02" })]), "c2.csv", {
      cookieString: cookie, env: "sit",
    });
    assert.equal(r2.body.ok, true, "second request ok — token from cache");
  });
}

// ── Bootstrap ─────────────────────────────────────────────────────────────────
(async () => {
  console.log("\n━━━ Delivery Zone Manager — Integration Tests ━━━\n");

  const { startServer } = require("../server");
  await startServer(TEST_PORT);

  // Block outbound calls to real platform APIs; keep localhost open
  nock.disableNetConnect();
  nock.enableNetConnect("127.0.0.1");

  try {
    await runTests();
  } finally {
    nock.enableNetConnect();
    nock.cleanAll();
  }

  console.log(`\n━━━ Results: ${passed} passed, ${failed} failed out of ${passed + failed} ━━━\n`);
  if (failed > 0) {
    console.log("Failed:");
    results.filter((r) => !r.ok).forEach((r) => console.log(`  ✖  ${r.label}\n     ${r.error}\n`));
    process.exit(1);
  }
})();
