/**
 * full-audit.test.js
 * ==================
 * Comprehensive audit + load test for Delivery Zone Manager v1.9.5.
 * Covers every API endpoint, edge-cases, and large-file performance.
 *
 * Run: node tests/full-audit.test.js
 */

"use strict";

const http     = require("node:http");
const assert   = require("node:assert/strict");
const FormData = require("form-data");
const nock     = require("nock");

const TEST_PORT = 3851;
const BASE      = `http://127.0.0.1:${TEST_PORT}`;

// ─── Server Bootstrap ─────────────────────────────────────────────────────────
const { startServer } = require("../server.js");

nock.disableNetConnect();
nock.enableNetConnect("127.0.0.1");

// ─── Helpers ──────────────────────────────────────────────────────────────────

function makeJwt(expiresInSeconds = 3600) {
  const h = Buffer.from(JSON.stringify({ alg: "HS256", typ: "JWT" })).toString("base64url");
  const p = Buffer.from(JSON.stringify({ sub: "u", exp: Math.floor(Date.now() / 1000) + expiresInSeconds })).toString("base64url");
  return `${h}.${p}.sig`;
}

let _uid = 0;
const freshCookie = () => `uid=audit-${++_uid}; sid=sess-${_uid}`;
const freshToken  = () => makeJwt(3600);

function escapeCSV(v) {
  const s = String(v ?? "");
  return s.includes(",") || s.includes('"') || s.includes("\n")
    ? `"${s.replace(/"/g, '""')}"` : s;
}
function toCSV(rows) {
  if (!rows.length) return "";
  const headers = Object.keys(rows[0]);
  return [headers.map(escapeCSV).join(","),
    ...rows.map((r) => headers.map((h) => escapeCSV(r[h] ?? "")).join(","))
  ].join("\n");
}

function row(overrides = {}) {
  return {
    slug: "zone-001", name: "Zone 001", store_ids: "101",
    region_type: "pincode", mapping_country: "IN",
    mapping_regions: "110001,110002", channels: "ch-1",
    company_id: "1", is_active: "true", product_type: "all", product_tags: "",
    ...overrides,
  };
}

// POST JSON
function postJSON(path, body) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body);
    const req  = http.request({
      hostname: "127.0.0.1", port: TEST_PORT, path, method: "POST",
      headers: { "content-type": "application/json", "content-length": Buffer.byteLength(data) },
    }, (res) => {
      let buf = "";
      res.on("data", (c) => (buf += c));
      res.on("end", () => {
        try { resolve({ status: res.statusCode, body: JSON.parse(buf) }); }
        catch { resolve({ status: res.statusCode, body: buf }); }
      });
    });
    req.on("error", reject);
    req.write(data);
    req.end();
  });
}

// GET JSON
function getJSON(path) {
  return new Promise((resolve, reject) => {
    http.get({ hostname: "127.0.0.1", port: TEST_PORT, path }, (res) => {
      let buf = "";
      res.on("data", (c) => (buf += c));
      res.on("end", () => {
        try { resolve({ status: res.statusCode, body: JSON.parse(buf) }); }
        catch { resolve({ status: res.statusCode, body: buf }); }
      });
    }).on("error", reject);
  });
}

// POST multipart file
function postFile(path, csvContent, extraFields = {}, filename = "zones.csv") {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", Buffer.from(csvContent), { filename, contentType: "text/csv" });
    for (const [k, v] of Object.entries(extraFields)) form.append(k, String(v));
    const req = http.request(
      { hostname: "127.0.0.1", port: TEST_PORT, path, method: "POST", headers: form.getHeaders() },
      (res) => {
        let buf = "";
        res.on("data", (c) => (buf += c));
        res.on("end", () => {
          try { resolve({ status: res.statusCode, body: JSON.parse(buf) }); }
          catch { resolve({ status: res.statusCode, body: { ok: false, rawText: buf } }); }
        });
      }
    );
    req.on("error", reject);
    form.pipe(req);
  });
}

// Nock helpers
function mockToken(cookie, token) {
  nock("https://api.jiox0.de")
    .get("/service/panel/authentication/v1.0/company/1/oauth/staff/token")
    .reply(200, { access_token: token });
}

function mockZones(token, items = []) {
  nock("https://api.jiox0.de")
    .get("/service/platform/logistics/v2.0/company/1/zones")
    .query(true)
    .matchHeader("authorization", `Bearer ${token}`)
    .reply(200, { items });
}

function mockCreateZone(token, status = 201, responseBody = {}) {
  nock("https://api.jiox0.de")
    .post("/service/platform/logistics/v2.0/company/1/zones")
    .matchHeader("authorization", `Bearer ${token}`)
    .reply(status, responseBody);
}

function mockZoneDetail(token, zoneId, data) {
  nock("https://api.jiox0.de")
    .get(`/service/platform/logistics/v2.0/company/1/zones/${zoneId}`)
    .matchHeader("authorization", `Bearer ${token}`)
    .reply(200, { item: data });
}

function mockUpdateZone(token, zoneId, status = 200) {
  nock("https://api.jiox0.de")
    .put(`/service/platform/logistics/v2.0/company/1/zones/${zoneId}`)
    .matchHeader("authorization", `Bearer ${token}`)
    .reply(status, { slug: "updated" });
}

function resetMocks() { nock.cleanAll(); }

// ─── Test Runner ──────────────────────────────────────────────────────────────
let passed = 0, failed = 0;
const failures = [];

async function test(label, fn) {
  resetMocks();
  try {
    await fn();
    console.log(`  ✔  ${label}`);
    passed++;
  } catch (err) {
    console.log(`  ✖  ${label}`);
    console.log(`     → ${err.message}`);
    failures.push({ label, error: err.message });
    failed++;
  }
}

function suite(name) {
  console.log(`\n── ${name} ─────────────────────────────────────────────`);
}

// ─── Run all tests ────────────────────────────────────────────────────────────
(async () => {
  const srv = await startServer(TEST_PORT);

  // ── SUITE 1: Infrastructure ────────────────────────────────────────────────
  suite("1. Infrastructure & Health");

  await test("GET /health returns ok:true", async () => {
    const r = await getJSON("/health");
    assert.equal(r.status, 200);
    assert.equal(r.body.ok, true);
  });

  await test("GET /api/environments returns sit/uat/prod with default=sit", async () => {
    const r = await getJSON("/api/environments");
    assert.equal(r.body.ok, true);
    const keys = r.body.environments.map((e) => e.key);
    assert.ok(keys.includes("sit") && keys.includes("uat") && keys.includes("prod"));
    assert.equal(r.body.default, "sit");
  });

  await test("GET /api/version returns version and buildNumber", async () => {
    const r = await getJSON("/api/version");
    assert.equal(r.body.ok, true);
    assert.ok(r.body.version, "missing version");
    assert.ok(r.body.buildNumber !== undefined, "missing buildNumber");
  });

  await test("GET /login returns HTML (200)", async () => {
    const r = await getJSON("/login");
    assert.equal(r.status, 200);
  });

  // ── SUITE 2: Cookie Cache ──────────────────────────────────────────────────
  suite("2. Cookie Cache");

  await test("clear-cookie-cache with valid cookie returns ok", async () => {
    const r = await postJSON("/api/clear-cookie-cache", { cookieString: "uid=clr-test", env: "sit" });
    assert.equal(r.body.ok, true);
    assert.ok(typeof r.body.cleared === "number");
  });

  await test("clear-cookie-cache with empty cookie returns cleared:0", async () => {
    const r = await postJSON("/api/clear-cookie-cache", { cookieString: "", env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.cleared, 0);
  });

  await test("clear-cookie-cache with unknown env defaults to sit (no error)", async () => {
    const r = await postJSON("/api/clear-cookie-cache", { cookieString: "uid=x", env: "badenv" });
    assert.equal(r.body.ok, true);
  });

  // ── SUITE 3: cURL Parsing & Session Check ─────────────────────────────────
  suite("3. cURL Parsing & Session Check");

  await test("parse-session-curl extracts cookie and auth header", async () => {
    const curl = `curl 'https://api.jiox0.de/ep' -H 'cookie: uid=abc; sid=xyz' -H 'authorization: Bearer tok123'`;
    const r = await postJSON("/api/parse-session-curl", { curlCommand: curl });
    assert.equal(r.body.ok, true);
    assert.ok(r.body.cookieString.includes("uid=abc"));
    assert.ok(r.body.authorizationHeader.includes("tok123"));
  });

  await test("parse-session-curl rejects non-curl input", async () => {
    const r = await postJSON("/api/parse-session-curl", { curlCommand: "wget something" });
    assert.equal(r.body.ok, false);
  });

  await test("parse-session-curl rejects empty input", async () => {
    const r = await postJSON("/api/parse-session-curl", { curlCommand: "" });
    assert.equal(r.body.ok, false);
  });

  await test("session-check rejects non-platform domain", async () => {
    const r = await postJSON("/api/session-check", {
      sessionRequest: { url: "https://evil.com/api", method: "GET", headers: { cookie: "uid=x", authorization: "Bearer tok" } },
    });
    assert.equal(r.body.ok, false);
    assert.ok(r.body.error.includes("not allowed"), r.body.error);
  });

  await test("session-check rejects missing cookie header", async () => {
    const r = await postJSON("/api/session-check", {
      sessionRequest: { url: "https://api.jiox0.de/test", method: "GET", headers: { authorization: "Bearer tok" } },
    });
    assert.equal(r.body.ok, false);
    assert.ok(r.body.error.toLowerCase().includes("cookie"), r.body.error);
  });

  await test("session-check rejects missing authorization header", async () => {
    const r = await postJSON("/api/session-check", {
      sessionRequest: { url: "https://api.jiox0.de/test", method: "GET", headers: { cookie: "uid=x" } },
    });
    assert.equal(r.body.ok, false);
    assert.ok(r.body.error.toLowerCase().includes("authorization"), r.body.error);
  });

  await test("session-check accepts jiox0.de domain and proxies result", async () => {
    nock("https://api.jiox0.de").get("/test-session").reply(200, { status: "ok" });
    const r = await postJSON("/api/session-check", {
      sessionRequest: { url: "https://api.jiox0.de/test-session", method: "GET",
        headers: { cookie: "uid=x", authorization: "Bearer tok" } },
    });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.status, 200);
  });

  await test("session-check accepts jioretailer.com (PROD) domain", async () => {
    nock("https://api.jioretailer.com").get("/check").reply(200, {});
    const r = await postJSON("/api/session-check", {
      sessionRequest: { url: "https://api.jioretailer.com/check", method: "GET",
        headers: { cookie: "uid=p", authorization: "Bearer prodt" } },
    });
    assert.equal(r.body.ok, true);
  });

  // ── SUITE 4: OTP Login — Send ──────────────────────────────────────────────
  suite("4a. OTP Login — Send OTP");

  await test("send-otp succeeds with valid email (SIT)", async () => {
    nock("https://api.jiox0.de")
      .post("/service/panel/authentication/v1.0/auth/login/email/otp/send")
      .query({ origin: "platform" })
      .reply(200, { request_id: "req-001", resend_timer: 30, message: "OTP sent" }, {
        "set-cookie": "fp=sess1; Path=/",
      });
    const r = await postJSON("/api/login/send-otp", { email: "user@fynd.com", env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.requestId, "req-001");
    assert.ok(r.body.sessionCookies.includes("fp=sess1"));
    assert.equal(r.body.resendTimer, 30);
  });

  await test("send-otp rejects empty email", async () => {
    const r = await postJSON("/api/login/send-otp", { email: "", env: "sit" });
    assert.equal(r.body.ok, false);
    assert.ok(r.body.error.toLowerCase().includes("email"));
  });

  await test("send-otp rejects email without @", async () => {
    const r = await postJSON("/api/login/send-otp", { email: "notanemail", env: "sit" });
    assert.equal(r.body.ok, false);
    assert.ok(r.body.error.toLowerCase().includes("invalid email"), r.body.error);
  });

  await test("send-otp forwards platform 400 as error", async () => {
    nock("https://api.jiox0.de")
      .post("/service/panel/authentication/v1.0/auth/login/email/otp/send")
      .query(true)
      .reply(400, { message: "User not found" });
    const r = await postJSON("/api/login/send-otp", { email: "x@fynd.com", env: "sit" });
    assert.equal(r.body.ok, false);
    assert.ok(r.body.error.includes("400"), r.body.error);
  });

  await test("send-otp uses UAT auth_base for uat env", async () => {
    nock("https://api.jiox5.de")
      .post("/service/panel/authentication/v1.0/auth/login/email/otp/send")
      .query({ origin: "platform" })
      .reply(200, { request_id: "uat-req", resend_timer: 30 }, { "set-cookie": "s=uat1; Path=/" });
    const r = await postJSON("/api/login/send-otp", { email: "user@fynd.com", env: "uat" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.requestId, "uat-req");
  });

  await test("send-otp uses PROD auth_base for prod env", async () => {
    nock("https://api.jioretailer.com")
      .post("/service/panel/authentication/v1.0/auth/login/email/otp/send")
      .query({ origin: "platform" })
      .reply(200, { request_id: "prod-req", resend_timer: 30 }, { "set-cookie": "s=prod1; Path=/" });
    const r = await postJSON("/api/login/send-otp", { email: "user@fynd.com", env: "prod" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.requestId, "prod-req");
  });

  // ── SUITE 4b: OTP Login — Verify ──────────────────────────────────────────
  suite("4b. OTP Login — Verify OTP");

  await test("verify-otp returns cookieString on success", async () => {
    nock("https://api.jiox0.de")
      .post("/service/panel/authentication/v1.0/auth/login/email/otp/verify")
      .query({ origin: "platform" })
      .reply(200, { success: true }, { "set-cookie": ["uid=user123; Path=/", "token=tok999; Path=/"] });
    const r = await postJSON("/api/login/verify-otp", { email: "user@fynd.com", otp: "123456", requestId: "req-001", env: "sit" });
    assert.equal(r.body.ok, true);
    assert.ok(r.body.cookieString.includes("uid=user123"), r.body.cookieString);
    assert.ok(r.body.cookieString.includes("token=tok999"), r.body.cookieString);
    assert.equal(r.body.email, "user@fynd.com");
    assert.equal(r.body.env, "sit");
  });

  await test("verify-otp rejects empty email", async () => {
    const r = await postJSON("/api/login/verify-otp", { email: "", otp: "123456", env: "sit" });
    assert.equal(r.body.ok, false);
    assert.ok(r.body.error.toLowerCase().includes("email"));
  });

  await test("verify-otp rejects empty otp", async () => {
    const r = await postJSON("/api/login/verify-otp", { email: "u@fynd.com", otp: "", env: "sit" });
    assert.equal(r.body.ok, false);
    assert.ok(r.body.error.toLowerCase().includes("otp"), r.body.error);
  });

  await test("verify-otp falls back to bare /otp endpoint on 404", async () => {
    nock("https://api.jiox0.de")
      .post("/service/panel/authentication/v1.0/auth/login/email/otp/verify").query(true)
      .reply(404, {});
    nock("https://api.jiox0.de")
      .post("/service/panel/authentication/v1.0/auth/login/email/otp").query(true)
      .reply(200, { success: true }, { "set-cookie": "uid=fallback; Path=/" });
    const r = await postJSON("/api/login/verify-otp", { email: "u@fynd.com", otp: "111111", requestId: "req-fb", env: "sit" });
    assert.equal(r.body.ok, true);
    assert.ok(r.body.cookieString.includes("uid=fallback"), r.body.cookieString);
  });

  await test("verify-otp fails when no Set-Cookie in response", async () => {
    nock("https://api.jiox0.de")
      .post("/service/panel/authentication/v1.0/auth/login/email/otp/verify").query(true)
      .reply(200, { success: true }); // no Set-Cookie
    nock("https://api.jiox0.de")
      .post("/service/panel/authentication/v1.0/auth/login/email/otp").query(true)
      .reply(200, { success: true }); // no Set-Cookie
    const r = await postJSON("/api/login/verify-otp", { email: "u@fynd.com", otp: "000000", requestId: "req-nc", env: "sit" });
    assert.equal(r.body.ok, false, `Expected failure when no Set-Cookie: ${JSON.stringify(r.body)}`);
  });

  // ── SUITE 5: Fetch Zones ───────────────────────────────────────────────────
  suite("5. Fetch Zones");

  await test("fetch-zones returns zone list with correct total", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    const zones  = [
      { zone_id: "z1", slug: "za", name: "Zone A", store_ids: [101], mapping: [{ country: "IN", regions: ["110001"] }] },
      { zone_id: "z2", slug: "zb", name: "Zone B", store_ids: [102], mapping: [{ country: "IN", regions: ["110002"] }] },
    ];
    mockToken(cookie, token);
    mockZones(token, zones);
    mockZoneDetail(token, "z1", zones[0]);
    mockZoneDetail(token, "z2", zones[1]);
    const r = await postJSON("/api/fetch-zones", { cookieString: cookie, env: "sit", includeDetails: true });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.total, 2);
    assert.ok(Array.isArray(r.body.zones));
    assert.ok(Array.isArray(r.body.logs));
  });

  await test("fetch-zones with empty cookie returns error", async () => {
    const r = await postJSON("/api/fetch-zones", { cookieString: "", env: "sit" });
    assert.equal(r.body.ok, false);
    assert.ok(r.body.error.toLowerCase().includes("cookie"), r.body.error);
  });

  await test("fetch-zones when token call returns 401 propagates error", async () => {
    const cookie = freshCookie();
    nock("https://api.jiox0.de")
      .get("/service/panel/authentication/v1.0/company/1/oauth/staff/token")
      .reply(401, { message: "Unauthorized" });
    const r = await postJSON("/api/fetch-zones", { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, false);
    assert.ok(r.body.error.includes("401"), r.body.error);
  });

  await test("fetch-zones with includeDetails:false skips enrichment", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    const zones  = [{ zone_id: "z3", slug: "zc", store_ids: [103], region_type: "pincode",
      mapping: [{ country: "IN", regions: ["560001"] }] }];
    mockToken(cookie, token);
    mockZones(token, zones);
    const r = await postJSON("/api/fetch-zones", { cookieString: cookie, env: "sit", includeDetails: false });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.total, 1);
    assert.ok(!nock.pendingMocks().length, `Pending mocks: ${JSON.stringify(nock.pendingMocks())}`);
  });

  await test("fetch-zones paginates (500 items on page 1, 0 on page 2)", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    const page1  = Array.from({ length: 500 }, (_, i) => ({
      zone_id: `z${i}`, slug: `zone-${i}`, store_ids: [i + 1],
      region_type: "pincode", mapping: [{ country: "IN", regions: [`${100000 + i}`] }],
    }));
    mockToken(cookie, token);
    nock("https://api.jiox0.de").get("/service/platform/logistics/v2.0/company/1/zones")
      .query({ page_no: "1", page_size: "500" }).reply(200, { items: page1 });
    nock("https://api.jiox0.de").get("/service/platform/logistics/v2.0/company/1/zones")
      .query({ page_no: "2", page_size: "500" }).reply(200, { items: [] });
    const r = await postJSON("/api/fetch-zones", { cookieString: cookie, env: "sit", includeDetails: false });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.total, 500);
  });

  await test("fetch-zones when items key is missing returns empty zones (no crash)", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    mockToken(cookie, token);
    nock("https://api.jiox0.de").get("/service/platform/logistics/v2.0/company/1/zones")
      .query(true).reply(200, {}); // no items key
    const r = await postJSON("/api/fetch-zones", { cookieString: cookie, env: "sit", includeDetails: false });
    assert.equal(r.body.ok, true, `crash on missing items: ${r.body.error}`);
    assert.equal(r.body.total, 0);
  });

  await test("fetch-zones when items is null returns 0 zones (no crash)", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    mockToken(cookie, token);
    nock("https://api.jiox0.de").get("/service/platform/logistics/v2.0/company/1/zones")
      .query(true).reply(200, { items: null });
    const r = await postJSON("/api/fetch-zones", { cookieString: cookie, env: "sit", includeDetails: false });
    assert.equal(r.body.ok, true, `crash on null items: ${r.body.error}`);
    assert.equal(r.body.total, 0);
  });

  await test("fetch-zones uses UAT endpoint when env=uat", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    nock("https://api.jiox5.de").get("/service/panel/authentication/v1.0/company/1/oauth/staff/token")
      .reply(200, { access_token: token });
    nock("https://api.jiox5.de").get("/service/platform/logistics/v2.0/company/1/zones")
      .query(true).reply(200, { items: [] });
    const r = await postJSON("/api/fetch-zones", { cookieString: cookie, env: "uat", includeDetails: false });
    assert.equal(r.body.ok, true);
  });

  // ── SUITE 6: Create Zones — Basic ─────────────────────────────────────────
  suite("6. Create Zones — Basic");

  await test("create-zones creates single valid zone", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    mockToken(cookie, token);
    mockZones(token, []);
    mockCreateZone(token, 201);
    const r = await postFile("/api/create-zones", toCSV([row({ slug: "zone-single" })]), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.created, 1);
    assert.equal(r.body.summary.skipped, 0);
    assert.equal(r.body.summary.failed, 0);
  });

  await test("create-zones skips row with empty slug", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    mockToken(cookie, token);
    mockZones(token, []);
    const r = await postFile("/api/create-zones", toCSV([row({ slug: "" })]), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.skipped, 1);
    assert.equal(r.body.summary.created, 0);
  });

  await test("create-zones skips row with whitespace-only slug", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    mockToken(cookie, token);
    mockZones(token, []);
    const r = await postFile("/api/create-zones", toCSV([row({ slug: "   " })]), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.skipped, 1);
  });

  await test("create-zones skips row with empty channels", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    mockToken(cookie, token);
    mockZones(token, []);
    const r = await postFile("/api/create-zones", toCSV([row({ channels: "" })]), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.skipped, 1);
  });

  await test("create-zones skips row missing both mapping_regions and pincode", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    mockToken(cookie, token);
    mockZones(token, []);
    const r = await postFile("/api/create-zones", toCSV([row({ mapping_regions: "", pincode: "" })]), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.skipped, 1);
  });

  await test("create-zones accepts pincode column as alias for mapping_regions", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    const zr = { ...row({ mapping_regions: "" }), pincode: "110001,110002" };
    delete zr.mapping_regions;
    mockToken(cookie, token);
    mockZones(token, []);
    mockCreateZone(token, 201);
    const r = await postFile("/api/create-zones", toCSV([zr]), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.created, 1);
  });

  await test("create-zones skips existing slug", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    mockToken(cookie, token);
    mockZones(token, [{ slug: "existing", zone_id: "z99", store_ids: [101],
      mapping: [{ country: "IN", regions: ["110001"] }] }]);
    const r = await postFile("/api/create-zones", toCSV([row({ slug: "existing" })]), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.skipped, 1);
    assert.equal(r.body.summary.created, 0);
  });

  await test("create-zones records failed rows (platform 400)", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    mockToken(cookie, token);
    mockZones(token, []);
    mockCreateZone(token, 400, { message: "Validation error" });
    const r = await postFile("/api/create-zones", toCSV([row({ slug: "bad-zone" })]), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.failed, 1);
    assert.equal(r.body.summary.created, 0);
  });

  await test("create-zones rejects .txt file upload", async () => {
    const form = new FormData();
    form.append("file", Buffer.from("text content"), { filename: "zones.txt", contentType: "text/plain" });
    form.append("cookieString", "uid=x");
    const r = await new Promise((resolve, reject) => {
      const req = http.request({ hostname: "127.0.0.1", port: TEST_PORT, path: "/api/create-zones", method: "POST", headers: form.getHeaders() }, (res) => {
        let buf = ""; res.on("data", (c) => (buf += c));
        res.on("end", () => {
          try { resolve({ status: res.statusCode, body: JSON.parse(buf) }); }
          catch { resolve({ status: res.statusCode, body: { ok: false, rawText: buf } }); }
        });
      });
      req.on("error", reject);
      form.pipe(req);
    });
    assert.ok(!r.body.ok, `Expected error for .txt, got ok:true`);
  });

  await test("create-zones with no file returns error", async () => {
    const r = await postJSON("/api/create-zones", { cookieString: "uid=x", env: "sit" });
    assert.equal(r.body.ok, false);
  });

  // ── SUITE 7: Create Zones — Bulk & Mixed ──────────────────────────────────
  suite("7. Create Zones — Bulk & Mixed");

  await test("create-zones creates 5 distinct zones from one file", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    const rows   = Array.from({ length: 5 }, (_, i) => row({ slug: `bulk-${i}` }));
    mockToken(cookie, token);
    mockZones(token, []);
    for (let i = 0; i < 5; i++) mockCreateZone(token, 201);
    const r = await postFile("/api/create-zones", toCSV(rows), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.created, 5);
    assert.equal(r.body.summary.skipped, 0);
  });

  await test("create-zones mixed: 3 valid + 1 missing + 1 duplicate", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    const rows   = [row({ slug: "m1" }), row({ slug: "m2" }), row({ slug: "m3" }), row({ slug: "" }), row({ slug: "dup-m" })];
    mockToken(cookie, token);
    mockZones(token, [{ slug: "dup-m", zone_id: "zdm", store_ids: [101], mapping: [{ country: "IN", regions: ["110001"] }] }]);
    for (let i = 0; i < 3; i++) mockCreateZone(token, 201);
    const r = await postFile("/api/create-zones", toCSV(rows), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.created, 3);
    assert.equal(r.body.summary.skipped, 2);
  });

  await test("create-zones same slug twice in file: only first is created", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    mockToken(cookie, token);
    mockZones(token, []);
    mockCreateZone(token, 201); // only one create
    const r = await postFile("/api/create-zones", toCSV([row({ slug: "dup-file" }), row({ slug: "dup-file" })]), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.created, 1, `duplicate within file not handled: ${JSON.stringify(r.body.summary)}`);
    assert.equal(r.body.summary.skipped, 1);
  });

  await test("create-zones continues processing after platform 500 error", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    mockToken(cookie, token);
    mockZones(token, []);
    mockCreateZone(token, 500, { message: "Internal Error" });
    mockCreateZone(token, 201);
    const r = await postFile("/api/create-zones", toCSV([row({ slug: "fail-1" }), row({ slug: "ok-1" })]), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.failed, 1);
    assert.equal(r.body.summary.created, 1);
  });

  // ── SUITE 8: CSV Parsing Edge Cases ───────────────────────────────────────
  suite("8. CSV/Data Edge Cases");

  await test("CSV with BOM on headers is parsed correctly", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    const csv    = `\uFEFFslug,store_ids,region_type,mapping_country,mapping_regions,channels\nzone-bom,101,pincode,IN,110001,ch-1`;
    mockToken(cookie, token);
    mockZones(token, []);
    mockCreateZone(token, 201);
    const r = await postFile("/api/create-zones", csv, { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.created, 1, `BOM not stripped: ${JSON.stringify(r.body.logs)}`);
  });

  await test("CSV with mixed-case headers is parsed correctly", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    const csv    = `Slug,Store_IDs,Region_Type,Mapping_Country,Mapping_Regions,Channels\nzone-mxcase,101,pincode,IN,110001,ch-1`;
    mockToken(cookie, token);
    mockZones(token, []);
    mockCreateZone(token, 201);
    const r = await postFile("/api/create-zones", csv, { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.created, 1, `Mixed-case not handled: ${JSON.stringify(r.body.logs)}`);
  });

  await test("CSV with trailing blank rows does not create blank zones", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    const csv    = `slug,store_ids,region_type,mapping_country,mapping_regions,channels\nzone-trail,101,pincode,IN,110001,ch-1\n,,,,,,\n,,,,,,\n`;
    mockToken(cookie, token);
    mockZones(token, []);
    mockCreateZone(token, 201);
    const r = await postFile("/api/create-zones", csv, { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.created, 1, `Trailing rows caused extra creates: ${JSON.stringify(r.body.summary)}`);
    assert.equal(r.body.summary.failed, 0);
  });

  await test("JSON array regions column is parsed correctly", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    mockToken(cookie, token);
    mockZones(token, []);
    nock("https://api.jiox0.de")
      .post("/service/platform/logistics/v2.0/company/1/zones", (body) => body.mapping?.[0]?.regions?.length === 3)
      .reply(201, {});
    const r = await postFile("/api/create-zones", toCSV([row({ slug: "json-r", mapping_regions: '["110001","110002","110003"]' })]), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.created, 1);
  });

  await test("region_type=non-pincode is preserved in payload", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    mockToken(cookie, token);
    mockZones(token, []);
    nock("https://api.jiox0.de")
      .post("/service/platform/logistics/v2.0/company/1/zones", (body) => body.region_type === "non-pincode")
      .reply(201, {});
    const r = await postFile("/api/create-zones", toCSV([row({ slug: "np", region_type: "non-pincode" })]), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.created, 1);
  });

  await test("invalid region_type defaults to pincode", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    mockToken(cookie, token);
    mockZones(token, []);
    nock("https://api.jiox0.de")
      .post("/service/platform/logistics/v2.0/company/1/zones", (body) => body.region_type === "pincode")
      .reply(201, {});
    const r = await postFile("/api/create-zones", toCSV([row({ slug: "inv-rt", region_type: "GARBAGE" })]), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.created, 1);
  });

  await test("is_active=false is preserved in payload", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    mockToken(cookie, token);
    mockZones(token, []);
    nock("https://api.jiox0.de")
      .post("/service/platform/logistics/v2.0/company/1/zones", (body) => body.is_active === false)
      .reply(201, {});
    const r = await postFile("/api/create-zones", toCSV([row({ slug: "inactive", is_active: "false" })]), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.created, 1);
  });

  await test("multiple store_ids are parsed as integers", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    mockToken(cookie, token);
    mockZones(token, []);
    nock("https://api.jiox0.de")
      .post("/service/platform/logistics/v2.0/company/1/zones", (body) => JSON.stringify(body.store_ids) === "[101,202,303]")
      .reply(201, {});
    const r = await postFile("/api/create-zones", toCSV([row({ slug: "ms", store_ids: "101,202,303" })]), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.created, 1);
  });

  await test("product_tags and explicit product_type are set in payload", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    mockToken(cookie, token);
    mockZones(token, []);
    nock("https://api.jiox0.de")
      .post("/service/platform/logistics/v2.0/company/1/zones", (body) =>
        body.product?.type === "explicit" && body.product?.tags?.length === 2)
      .reply(201, {});
    const r = await postFile("/api/create-zones", toCSV([row({ slug: "tagged", product_tags: "ta,tb", product_type: "explicit" })]), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.created, 1);
  });

  await test("cookie normalization strips 'Cookie:' prefix", async () => {
    const token = freshToken();
    nock("https://api.jiox0.de").get("/service/panel/authentication/v1.0/company/1/oauth/staff/token").reply(200, { access_token: token });
    nock("https://api.jiox0.de").get("/service/platform/logistics/v2.0/company/1/zones").query(true).reply(200, { items: [] });
    const r = await postJSON("/api/fetch-zones", { cookieString: "Cookie: uid=norm-test; sid=abc", env: "sit", includeDetails: false });
    assert.equal(r.body.ok, true, `Cookie normalization failed: ${r.body.error}`);
  });

  await test("empty file returns error (no usable rows)", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    mockToken(cookie, token);
    mockZones(token, []);
    const r = await postFile("/api/create-zones", "", { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, false, `Expected error for empty file, got ok:true`);
  });

  // ── SUITE 9: Plan Updates ──────────────────────────────────────────────────
  suite("9. Plan Updates");

  await test("plan-updates plans a region addition", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    const existing = [{ zone_id: "zu1", slug: "upd-z", store_ids: [101],
      region_type: "pincode", mapping: [{ country: "IN", regions: ["110001", "110002"] }] }];
    mockToken(cookie, token);
    mockZones(token, existing);
    const r = await postFile("/api/plan-updates", toCSV([row({ slug: "upd-z", store_ids: "101", mapping_regions: "110001,110002,110003" })]), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.plannedUpdates, 1, `planned: ${r.body.summary.plannedUpdates}`);
    assert.equal(r.body.updates[0].newRegionsCount, 3);
    assert.equal(r.body.updates[0].oldRegionsCount, 2);
  });

  await test("plan-updates returns 0 updates when regions unchanged", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    const existing = [{ zone_id: "zs1", slug: "same-z", store_ids: [101],
      region_type: "pincode", mapping: [{ country: "IN", regions: ["110001", "110002"] }] }];
    mockToken(cookie, token);
    mockZones(token, existing);
    const r = await postFile("/api/plan-updates", toCSV([row({ slug: "same-z", store_ids: "101", mapping_regions: "110001,110002" })]), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.plannedUpdates, 0);
  });

  await test("plan-updates logs unmatched rows", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    mockToken(cookie, token);
    mockZones(token, []);
    const r = await postFile("/api/plan-updates", toCSV([row({ slug: "no-match" })]), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.plannedUpdates, 0);
    assert.ok(r.body.logs.some((l) => l.toLowerCase().includes("no existing zone")), "unmatched not logged");
  });

  await test("plan-updates auto-enriches zones when list has no mapping data", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    const zonesNoMap = [{ zone_id: "ze1", slug: "enrich-z", store_ids: [101] }];
    const detail = { zone_id: "ze1", slug: "enrich-z", store_ids: [101],
      region_type: "pincode", mapping: [{ country: "IN", regions: ["110001"] }] };
    mockToken(cookie, token);
    mockZones(token, zonesNoMap);
    mockZoneDetail(token, "ze1", detail);
    const r = await postFile("/api/plan-updates", toCSV([row({ slug: "enrich-z", mapping_regions: "110001,110002" })]), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.plannedUpdates, 1);
  });

  await test("plan-updates fails when enrichment still returns no mapping", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    mockToken(cookie, token);
    mockZones(token, [{ zone_id: "zn1", slug: "nomap-z", store_ids: [101] }]);
    nock("https://api.jiox0.de").get("/service/platform/logistics/v2.0/company/1/zones/zn1").reply(200, { item: { zone_id: "zn1", slug: "nomap-z", store_ids: [101] } });
    const r = await postFile("/api/plan-updates", toCSV([row({ slug: "nomap-z" })]), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, false, "Expected failure when mapping unavailable after enrichment");
    assert.ok(r.body.error.toLowerCase().includes("mapping") || r.body.error.toLowerCase().includes("detail"), r.body.error);
  });

  await test("plan-updates with multiple store_ids per zone matches correctly", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    const existing = [{ zone_id: "zmsi", slug: "msi-zone", store_ids: [501, 502],
      region_type: "pincode", mapping: [{ country: "IN", regions: ["560001"] }] }];
    mockToken(cookie, token);
    mockZones(token, existing);
    const r = await postFile("/api/plan-updates", toCSV([row({ slug: "msi-zone", store_ids: "501,502", mapping_regions: "560001,560002" })]), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.plannedUpdates, 1, `expected 1 planned, got: ${r.body.summary.plannedUpdates}`);
  });

  // ── SUITE 10: Apply Updates ────────────────────────────────────────────────
  suite("10. Apply Updates");

  await test("apply-updates executes planned update", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    const existing = [{ zone_id: "za1", slug: "apply-z", store_ids: [101],
      region_type: "pincode", mapping: [{ country: "IN", regions: ["110001"] }] }];
    mockToken(cookie, token);
    mockZones(token, existing);
    mockUpdateZone(token, "za1", 200);
    const r = await postFile("/api/apply-updates", toCSV([row({ slug: "apply-z", store_ids: "101", mapping_regions: "110001,110002" })]), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.updated, 1);
    assert.equal(r.body.summary.failed, 0);
  });

  await test("apply-updates returns no-op when nothing to update", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    const existing = [{ zone_id: "znoop", slug: "noop-z", store_ids: [101],
      region_type: "pincode", mapping: [{ country: "IN", regions: ["110001"] }] }];
    mockToken(cookie, token);
    mockZones(token, existing);
    const r = await postFile("/api/apply-updates", toCSV([row({ slug: "noop-z", store_ids: "101", mapping_regions: "110001" })]), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.updated, 0);
    assert.ok(r.body.logs.some((l) => l.toLowerCase().includes("no updates")));
  });

  await test("apply-updates records failed PUT (API 400)", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    const existing = [{ zone_id: "zfp", slug: "failput-z", store_ids: [101],
      region_type: "pincode", mapping: [{ country: "IN", regions: ["110001"] }] }];
    mockToken(cookie, token);
    mockZones(token, existing);
    mockUpdateZone(token, "zfp", 400);
    const r = await postFile("/api/apply-updates", toCSV([row({ slug: "failput-z", store_ids: "101", mapping_regions: "110001,110002" })]), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.failed, 1);
    assert.equal(r.body.summary.updated, 0);
  });

  await test("apply-updates sends sorted regions in PUT payload", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    const existing = [{ zone_id: "zsrt", slug: "sort-z", store_ids: [101],
      region_type: "pincode", mapping: [{ country: "IN", regions: ["110001"] }] }];
    mockToken(cookie, token);
    mockZones(token, existing);
    nock("https://api.jiox0.de")
      .put("/service/platform/logistics/v2.0/company/1/zones/zsrt", (body) => {
        const r = body.mapping?.[0]?.regions || [];
        return JSON.stringify(r) === JSON.stringify([...r].sort());
      })
      .reply(200, {});
    const r = await postFile("/api/apply-updates", toCSV([row({ slug: "sort-z", store_ids: "101", mapping_regions: "110003,110001,110002" })]), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.updated, 1, `Regions not sorted in PUT: ${JSON.stringify(r.body.summary)}`);
  });

  // ── SUITE 11: Token Caching ────────────────────────────────────────────────
  suite("11. Token Caching");

  await test("token is reused across two consecutive requests (only one token call)", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    nock("https://api.jiox0.de")
      .get("/service/panel/authentication/v1.0/company/1/oauth/staff/token").once()
      .reply(200, { access_token: token });
    nock("https://api.jiox0.de").get("/service/platform/logistics/v2.0/company/1/zones")
      .query(true).twice().reply(200, { items: [] });
    await postJSON("/api/fetch-zones", { cookieString: cookie, env: "sit", includeDetails: false });
    await postJSON("/api/fetch-zones", { cookieString: cookie, env: "sit", includeDetails: false });
    assert.ok(!nock.pendingMocks().some((m) => m.includes("staff/token")),
      "Token fetched more than once — caching broken");
  });

  await test("clear-cookie-cache forces token re-fetch on next request", async () => {
    const cookie = freshCookie();
    const t1     = freshToken();
    const t2     = freshToken();
    nock("https://api.jiox0.de").get("/service/panel/authentication/v1.0/company/1/oauth/staff/token").reply(200, { access_token: t1 });
    nock("https://api.jiox0.de").get("/service/platform/logistics/v2.0/company/1/zones").query(true).reply(200, { items: [] });
    await postJSON("/api/fetch-zones", { cookieString: cookie, env: "sit", includeDetails: false });
    await postJSON("/api/clear-cookie-cache", { cookieString: cookie, env: "sit" });
    nock("https://api.jiox0.de").get("/service/panel/authentication/v1.0/company/1/oauth/staff/token").reply(200, { access_token: t2 });
    nock("https://api.jiox0.de").get("/service/platform/logistics/v2.0/company/1/zones").query(true).reply(200, { items: [] });
    const r = await postJSON("/api/fetch-zones", { cookieString: cookie, env: "sit", includeDetails: false });
    assert.equal(r.body.ok, true);
    assert.ok(!nock.pendingMocks().some((m) => m.includes("staff/token")), "Token not re-fetched after clear");
  });

  // ── SUITE 12: Load Tests ───────────────────────────────────────────────────
  suite("12. Load Test — Large Files");

  async function loadTestCreate(rowCount, label) {
    await test(`create-zones: ${label} rows — all created, summary correct`, async () => {
      const cookie  = freshCookie();
      const token   = freshToken();
      const rows    = Array.from({ length: rowCount }, (_, i) => row({ slug: `ld-${i}`, store_ids: String(100 + i) }));
      const csv     = toCSV(rows);
      mockToken(cookie, token);
      mockZones(token, []);
      for (let i = 0; i < rowCount; i++) mockCreateZone(token, 201);
      const t0 = Date.now();
      const r  = await postFile("/api/create-zones", csv, { cookieString: cookie, env: "sit" });
      const ms = Date.now() - t0;
      assert.equal(r.body.ok, true, JSON.stringify(r.body).slice(0, 200));
      assert.equal(r.body.summary.created, rowCount, `created mismatch: ${JSON.stringify(r.body.summary)}`);
      assert.equal(r.body.summary.skipped, 0);
      assert.equal(r.body.summary.failed, 0);
      assert.equal(r.body.summary.totalRows, rowCount);
      console.log(`     ↳ ${rowCount} rows in ${ms}ms (${(rowCount / (ms / 1000)).toFixed(0)} rows/sec)`);
    });
  }

  await loadTestCreate(50,   "50");
  await loadTestCreate(200,  "200");
  await loadTestCreate(500,  "500");
  await loadTestCreate(1000, "1,000");

  await test("create-zones 1000 rows: 500 valid + 500 blank → correct summary", async () => {
    const cookie = freshCookie();
    const token  = freshToken();
    const half   = 500;
    const rows   = [
      ...Array.from({ length: half }, (_, i) => row({ slug: `mem-${i}`, store_ids: String(200 + i) })),
      ...Array.from({ length: half }, () => row({ slug: "" })),
    ];
    mockToken(cookie, token);
    mockZones(token, []);
    for (let i = 0; i < half; i++) mockCreateZone(token, 201);
    const r = await postFile("/api/create-zones", toCSV(rows), { cookieString: cookie, env: "sit" });
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.created, half);
    assert.equal(r.body.summary.skipped, half);
    assert.equal(r.body.summary.totalRows, 1000);
  });

  await test("plan-updates: 500 rows — all planned correctly", async () => {
    const cookie   = freshCookie();
    const token    = freshToken();
    const count    = 500;
    const existing = Array.from({ length: count }, (_, i) => ({
      zone_id: `pz-${i}`, slug: `pl-${i}`, store_ids: [300 + i],
      region_type: "pincode", mapping: [{ country: "IN", regions: ["110001"] }],
    }));
    const csvRows = Array.from({ length: count }, (_, i) => row({ slug: `pl-${i}`, store_ids: String(300 + i), mapping_regions: "110001,110002" }));
    mockToken(cookie, token);
    // 500 items on page 1 triggers a second page request — mock it with 0 items
    nock("https://api.jiox0.de")
      .get("/service/platform/logistics/v2.0/company/1/zones")
      .query({ page_no: "1", page_size: "500" })
      .matchHeader("authorization", `Bearer ${token}`)
      .reply(200, { items: existing });
    nock("https://api.jiox0.de")
      .get("/service/platform/logistics/v2.0/company/1/zones")
      .query({ page_no: "2", page_size: "500" })
      .matchHeader("authorization", `Bearer ${token}`)
      .reply(200, { items: [] });
    const t0 = Date.now();
    const r  = await postFile("/api/plan-updates", toCSV(csvRows), { cookieString: cookie, env: "sit" });
    console.log(`     ↳ plan 500 rows in ${Date.now() - t0}ms`);
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.plannedUpdates, count, `planned: ${r.body.summary.plannedUpdates}`);
  });

  await test("apply-updates: 200 zones — all updated", async () => {
    const cookie   = freshCookie();
    const token    = freshToken();
    const count    = 200;
    const existing = Array.from({ length: count }, (_, i) => ({
      zone_id: `av-${i}`, slug: `ap-${i}`, store_ids: [400 + i],
      region_type: "pincode", mapping: [{ country: "IN", regions: ["110001"] }],
    }));
    const csvRows = Array.from({ length: count }, (_, i) => row({ slug: `ap-${i}`, store_ids: String(400 + i), mapping_regions: "110001,110002" }));
    mockToken(cookie, token);
    mockZones(token, existing);
    for (let i = 0; i < count; i++) mockUpdateZone(token, `av-${i}`, 200);
    const t0 = Date.now();
    const r  = await postFile("/api/apply-updates", toCSV(csvRows), { cookieString: cookie, env: "sit" });
    console.log(`     ↳ apply 200 zones in ${Date.now() - t0}ms`);
    assert.equal(r.body.ok, true);
    assert.equal(r.body.summary.updated, count, `updated: ${r.body.summary.updated}`);
    assert.equal(r.body.summary.failed, 0);
  });

  // ── SUITE 13: Concurrent Requests ─────────────────────────────────────────
  suite("13. Concurrent Requests");

  await test("10 concurrent fetch-zones all succeed independently", async () => {
    const promises = Array.from({ length: 10 }, async () => {
      const cookie = freshCookie();
      const token  = freshToken();
      mockToken(cookie, token);
      nock("https://api.jiox0.de").get("/service/platform/logistics/v2.0/company/1/zones")
        .query(true).reply(200, { items: [] });
      return postJSON("/api/fetch-zones", { cookieString: cookie, env: "sit", includeDetails: false });
    });
    const results = await Promise.all(promises);
    for (const r of results) assert.equal(r.body.ok, true, JSON.stringify(r.body));
  });

  await test("5 concurrent create-zones (10 rows each) all correct", async () => {
    const concurrency = 5;
    const rowsEach    = 10;
    const promises    = Array.from({ length: concurrency }, async (_, b) => {
      const cookie  = freshCookie();
      const token   = freshToken();
      const csvRows = Array.from({ length: rowsEach }, (_, i) => row({ slug: `cc-b${b}-z${i}`, store_ids: String(600 + b * 10 + i) }));
      mockToken(cookie, token);
      mockZones(token, []);
      for (let i = 0; i < rowsEach; i++) mockCreateZone(token, 201);
      return postFile("/api/create-zones", toCSV(csvRows), { cookieString: cookie, env: "sit" });
    });
    const results = await Promise.all(promises);
    for (let b = 0; b < concurrency; b++) {
      assert.equal(results[b].body.ok, true);
      assert.equal(results[b].body.summary.created, rowsEach, `batch ${b} mismatch`);
    }
  });

  // ── Final Summary ──────────────────────────────────────────────────────────
  console.log("\n══════════════════════════════════════════════════════════════");
  console.log(`  Results: ${passed} passed, ${failed} failed  (${passed + failed} total)`);

  if (failures.length) {
    console.log("\n  FAILURES:");
    for (const f of failures) {
      console.log(`  ✖ ${f.label}`);
      console.log(`    → ${f.error}`);
    }
  }

  console.log(`
┌─────────────────────────────────────────────────────────────┐
│                  AUDIT FINDINGS SUMMARY                     │
├─────────────────────────────────────────────────────────────┤
│ MEDIUM — verify-otp fallback fires on 400 (wrong OTP)       │
│   server.js:1119 — if /otp/verify returns 400, code retries │
│   the bare /otp endpoint. Wrong OTP gets two attempts.      │
│   Fix: only fallback on 404, not all 4xx errors.            │
├─────────────────────────────────────────────────────────────┤
│ LOW — Sequential zone creation causes timeout on large files │
│   createZonesFromRows is a serial loop. 500 zones × ~100ms  │
│   real API = ~50s. Express default timeout may drop the     │
│   connection before the response arrives. No streaming.     │
│   Fix: increase timeout, or stream progress via SSE/WS.     │
├─────────────────────────────────────────────────────────────┤
│ LOW — CSV cell values not trimmed (only headers are)        │
│   readRowsFromUploadedFile normalizes keys but NOT values.  │
│   " 110001 " (with spaces) ≠ "110001" in region comparison. │
│   Fix: trim values in readRowsFromUploadedFile.             │
├─────────────────────────────────────────────────────────────┤
│ LOW — Pagination makes extra empty request at 500-boundary  │
│   getZones fetches until items.length < pageSize. Total     │
│   exactly divisible by 500 triggers one extra empty call.   │
│   Minor inefficiency; not a functional bug.                 │
├─────────────────────────────────────────────────────────────┤
│ INFO — loadZonesForUpdate throws for envs without detail API │
│   If both list and detail APIs return zones with no mapping │
│   data, server throws "Could not load mapping details".     │
│   Expected behavior but error message could be clearer.     │
└─────────────────────────────────────────────────────────────┘`);

  console.log("══════════════════════════════════════════════════════════════\n");

  process.exit(failed > 0 ? 1 : 0);
})();
