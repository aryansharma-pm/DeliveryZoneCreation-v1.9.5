const path = require("path");
const crypto = require("crypto");
const { Readable } = require("stream");

const axios = require("axios");
const express = require("express");
const multer = require("multer");
const helmet = require("helmet");
const rateLimit = require("express-rate-limit");
const ExcelJS = require("exceljs");

const ENVIRONMENTS = {
  sit: {
    label: "SIT",
    type: "Platform + Storefront",
    platformOrigin: "https://platform.jiox0.de",
    apiBase:
      "https://api.jiox0.de/service/platform/logistics/v2.0/company/1/zones",
    tokenUrl:
      "https://api.jiox0.de/service/panel/authentication/v1.0/company/1/oauth/staff/token",
    authBase:
      "https://api.jiox0.de/service/panel/authentication/v1.0",
    sessionDomainPattern: /(\.|^)jiox0\.de$/i,
  },
  uat: {
    label: "UAT",
    platformOrigin: "https://platform.jiox5.de",
    apiBase:
      "https://api.jiox5.de/service/platform/logistics/v2.0/company/1/zones",
    tokenUrl:
      "https://api.jiox5.de/service/panel/authentication/v1.0/company/1/oauth/staff/token",
    authBase:
      "https://api.jiox5.de/service/panel/authentication/v1.0",
    sessionDomainPattern: /(\.|^)jiox5\.de$/i,
  },
  prod: {
    label: "PROD",
    platformOrigin: "https://platform.jioretailer.com",
    apiBase:
      "https://api.jioretailer.com/service/platform/logistics/v2.0/company/1/zones",
    tokenUrl:
      "https://api.jioretailer.com/service/panel/authentication/v1.0/company/1/oauth/staff/token",
    authBase:
      "https://api.jioretailer.com/service/panel/authentication/v1.0",
    sessionDomainPattern: /(\.|^)jioretailer\.com$/i,
  },
};

const DEFAULT_ENV = "sit";
const REQUEST_TIMEOUT = 30000;
const TOKEN_FALLBACK_TTL_MS = 10 * 60 * 1000;
const TOKEN_EXPIRY_SKEW_MS = 60 * 1000;
const DETAIL_FETCH_CONCURRENCY = 6;
const ZONE_WRITE_CONCURRENCY   = 10; // parallel zone create/update API calls
const tokenCache = new Map();

const app = express();

// Security headers (relaxed CSP to allow CDN assets loaded by the UI)
app.use(helmet({
  contentSecurityPolicy: false,
  crossOriginEmbedderPolicy: false,
}));

const loginLimiter = rateLimit({
  windowMs: 15 * 60 * 1000, // 15 minutes
  max: 20,
  standardHeaders: true,
  legacyHeaders: false,
  message: { ok: false, error: "Too many requests. Please wait before trying again." },
});

const upload = multer({
  storage: multer.memoryStorage(),
  limits: { fileSize: 10 * 1024 * 1024 }, // 10 MB
  fileFilter(_req, file, cb) {
    const allowed = [".csv", ".xls", ".xlsx"];
    const ext = path.extname(file.originalname || "").toLowerCase();
    if (allowed.includes(ext)) return cb(null, true);
    cb(new Error("Only CSV, XLS, and XLSX files are accepted."));
  },
});

app.use(express.json({ limit: "2mb" }));
app.use(express.urlencoded({ extended: true }));
app.use(express.static(path.join(__dirname, "public")));

function getEnvConfig(env) {
  const normalized = String(env || "").trim().toLowerCase();
  const config = ENVIRONMENTS[normalized] || ENVIRONMENTS[DEFAULT_ENV];
  return { ...config, key: normalized in ENVIRONMENTS ? normalized : DEFAULT_ENV };
}

function getCookieCacheKey(rawCookie, envKey) {
  return crypto
    .createHash("sha256")
    .update(`${envKey}:${rawCookie}`)
    .digest("hex");
}

function decodeJwtExpiryMs(token) {
  try {
    const tokenParts = String(token || "").split(".");
    if (tokenParts.length < 2) return null;
    const payloadPart = tokenParts[1];
    const base64 = payloadPart.replace(/-/g, "+").replace(/_/g, "/");
    const json = Buffer.from(base64, "base64").toString("utf8");
    const payload = JSON.parse(json);
    const exp = Number(payload.exp);
    if (!Number.isFinite(exp)) return null;
    return exp * 1000;
  } catch (_) {
    return null;
  }
}

function clearExpiredTokens() {
  const now = Date.now();
  for (const [cacheKey, entry] of tokenCache.entries()) {
    if (!entry || typeof entry.expiresAt !== "number" || entry.expiresAt <= now) {
      tokenCache.delete(cacheKey);
    }
  }
}

function normalizeCookieString(rawCookie) {
  return String(rawCookie || "")
    .replace(/^cookie\s*:\s*/i, "")
    .replace(/\r?\n/g, "; ")
    .split(";")
    .map((part) => part.trim())
    .filter(Boolean)
    .join("; ");
}

function parseCurlCommand(curlCommand) {
  const source = String(curlCommand || "").trim();
  if (!source) {
    throw new Error("cURL command is required.");
  }

  const normalized = source.replace(/\\\r?\n/g, " ").replace(/\s+/g, " ").trim();
  if (!/^curl(\s|$)/i.test(normalized)) {
    throw new Error("Input does not look like a cURL command.");
  }

  const urlMatch = normalized.match(/(?:^|\s)(['"])(https?:\/\/[^'"]+)\1/i);
  const url = urlMatch ? urlMatch[2].trim() : "";

  const methodMatch = normalized.match(/(?:^|\s)-X\s+([A-Za-z]+)/);
  const method = methodMatch ? methodMatch[1].toUpperCase() : "GET";

  const headers = {};
  const headerRegex = /(?:^|\s)-H\s+(['"])(.*?)\1/g;
  for (const match of normalized.matchAll(headerRegex)) {
    const rawHeader = match[2];
    const splitIndex = rawHeader.indexOf(":");
    if (splitIndex < 0) continue;
    const name = rawHeader.slice(0, splitIndex).trim().toLowerCase();
    const value = rawHeader.slice(splitIndex + 1).trim();
    if (!name || !value) continue;
    headers[name] = value;
  }

  const cookieMatch = normalized.match(/(?:^|\s)(?:-b|--cookie)\s+(['"])(.*?)\1/);
  if (cookieMatch && cookieMatch[2]) {
    headers.cookie = normalizeCookieString(cookieMatch[2]);
  } else if (headers.cookie) {
    headers.cookie = normalizeCookieString(headers.cookie);
  }

  return { url, method, headers };
}

function buildSessionCheckRequest(parsedCurl) {
  const safeHeaders = {};
  for (const [name, value] of Object.entries(parsedCurl.headers || {})) {
    const lower = name.toLowerCase();
    if (
      lower === "cookie" ||
      lower === "authorization" ||
      lower === "accept" ||
      lower === "accept-language" ||
      lower === "cache-control" ||
      lower === "pragma" ||
      lower === "priority" ||
      lower === "user-agent" ||
      lower.startsWith("x-")
    ) {
      safeHeaders[lower] = value;
    }
  }

  return {
    url: parsedCurl.url || "",
    method: parsedCurl.method || "GET",
    headers: safeHeaders,
  };
}

function validateSessionCheckUrl(rawUrl, envConfig) {
  let parsedUrl;
  try {
    parsedUrl = new URL(rawUrl);
  } catch (_) {
    throw new Error("Invalid session-check URL.");
  }

  if (!["http:", "https:"].includes(parsedUrl.protocol)) {
    throw new Error("Only http/https session-check URLs are allowed.");
  }

  const hostname = parsedUrl.hostname;
  const allPatterns = Object.values(ENVIRONMENTS).map((e) => e.sessionDomainPattern);
  const allowed = allPatterns.some((pattern) => pattern.test(hostname));
  if (!allowed) {
    const envLabel = envConfig ? envConfig.label : "current";
    throw new Error(
      `Session-check URL hostname "${hostname}" is not allowed for the ${envLabel} environment.`
    );
  }
}

async function runSessionCheck(sessionRequest) {
  const url = String(sessionRequest?.url || "").trim();
  const method = String(sessionRequest?.method || "GET").toUpperCase();
  const headers = { ...(sessionRequest?.headers || {}) };
  if (headers.cookie) {
    headers.cookie = normalizeCookieString(headers.cookie);
  }

  if (!url) {
    throw new Error("Session-check URL is required.");
  }
  validateSessionCheckUrl(url, null);
  if (!headers.cookie) {
    throw new Error("Cookie header is required for session check.");
  }
  if (!headers.authorization) {
    throw new Error("Authorization header is required for session check.");
  }

  try {
    const response = await axios({
      url,
      method,
      headers,
      timeout: REQUEST_TIMEOUT,
      validateStatus: () => true,
    });

    const responseData =
      typeof response.data === "string"
        ? response.data.slice(0, 2000)
        : response.data;
    return {
      status: response.status,
      okStatus: response.status >= 200 && response.status < 300,
      data: responseData,
    };
  } catch (error) {
    throw new Error(`Session check failed: ${error.message}`);
  }
}

function parseList(cell) {
  if (cell === undefined || cell === null) return [];
  const text = String(cell).trim();
  if (!text) return [];

  try {
    const parsed = JSON.parse(text);
    if (Array.isArray(parsed)) {
      return parsed
        .map((item) => String(item).trim())
        .filter((item) => item.length > 0);
    }
  } catch (_) {
    // Ignore JSON parse errors and continue with split parser.
  }

  const cleaned = text.replace(/\[|\]|"|'/g, "");
  return cleaned
    .split(",")
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
}

function parseStoreIds(cell) {
  const values = parseList(cell);
  const parsed = [];
  for (const value of values) {
    if (/^\d+$/.test(value)) {
      parsed.push(Number(value));
      continue;
    }
    const floatVal = Number(value);
    if (!Number.isNaN(floatVal) && Number.isInteger(floatVal)) {
      parsed.push(floatVal);
      continue;
    }
    parsed.push(value);
  }
  return parsed;
}

function parseBool(cell, defaultValue = true) {
  if (cell === undefined || cell === null) return defaultValue;
  const text = String(cell).trim().toLowerCase();
  if (["true", "1", "yes", "y"].includes(text)) return true;
  if (["false", "0", "no", "n"].includes(text)) return false;
  return defaultValue;
}

function normalizeRegionType(value) {
  const text = String(value || "pincode").trim().toLowerCase();
  return ["pincode", "non-pincode"].includes(text) ? text : "pincode";
}

function normalizeProductType(value) {
  const text = String(value || "all").trim().toLowerCase();
  return ["all", "explicit"].includes(text) ? text : "all";
}

async function getBearerToken(cookieString, envConfig) {
  const rawCookie = normalizeCookieString(cookieString);
  if (!rawCookie) {
    throw new Error("Cookie string is required.");
  }

  clearExpiredTokens();
  const cacheKey = getCookieCacheKey(rawCookie, envConfig.key);
  const cachedToken = tokenCache.get(cacheKey);
  if (
    cachedToken &&
    cachedToken.token &&
    typeof cachedToken.expiresAt === "number" &&
    cachedToken.expiresAt > Date.now() + TOKEN_EXPIRY_SKEW_MS
  ) {
    return cachedToken.token;
  }

  let response;
  try {
    response = await axios.get(envConfig.tokenUrl, {
      headers: {
        accept: "application/json, text/plain, */*",
        origin: envConfig.platformOrigin,
        "user-agent": "Mozilla/5.0",
        cookie: rawCookie,
      },
      timeout: REQUEST_TIMEOUT,
    });
  } catch (error) {
    if (error.response) {
      throw new Error(
        `Failed to fetch token (${error.response.status}): ${String(error.response.data)}`
      );
    }
    throw new Error(`Token request failed: ${error.message}`);
  }

  const token = response.data && response.data.access_token;
  if (!token) {
    throw new Error(
      `Access token not found in response: ${JSON.stringify(response.data)}`
    );
  }

  const expiryMs = decodeJwtExpiryMs(token);
  const expiresAt = expiryMs
    ? Math.max(expiryMs - TOKEN_EXPIRY_SKEW_MS, Date.now() + 30 * 1000)
    : Date.now() + TOKEN_FALLBACK_TTL_MS;
  tokenCache.set(cacheKey, { token, expiresAt });

  return token;
}

async function getZones(token, envConfig) {
  const zones = [];
  const pageSize = 500;
  let pageNo = 1;

  while (true) {
    let response;
    try {
      response = await axios.get(envConfig.apiBase, {
        headers: { authorization: `Bearer ${token}` },
        params: { page_no: pageNo, page_size: pageSize },
        timeout: REQUEST_TIMEOUT,
      });
    } catch (error) {
      if (error.response) {
        throw new Error(
          `Error fetching zones (${error.response.status}): ${String(error.response.data)}`
        );
      }
      throw new Error(`Failed to fetch zones: ${error.message}`);
    }

    const items = Array.isArray(response.data?.items) ? response.data.items : [];
    zones.push(...items);
    if (items.length < pageSize) break;
    pageNo += 1;
  }
  return zones;
}

function extractZoneFromResponse(payload) {
  if (!payload || typeof payload !== "object") return null;
  if (payload.item && typeof payload.item === "object") return payload.item;
  if (payload.data && typeof payload.data === "object") return payload.data;
  return payload;
}

async function getZoneDetails(token, zoneId, envConfig) {
  try {
    const response = await axios.get(`${envConfig.apiBase}/${zoneId}`, {
      headers: { authorization: `Bearer ${token}` },
      timeout: REQUEST_TIMEOUT,
    });
    return extractZoneFromResponse(response.data);
  } catch (_) {
    return null;
  }
}

function zoneHasMappingData(zone) {
  return Boolean(zone.mapping || zone.mappings || zone.region_type || zone.regionType);
}

async function enrichZonesWithDetails(token, zones, onProgress, envConfig) {
  const enriched = new Array(zones.length);
  let completed = 0;

  for (let i = 0; i < zones.length; i += DETAIL_FETCH_CONCURRENCY) {
    const batch = zones.slice(i, i + DETAIL_FETCH_CONCURRENCY);
    const results = await Promise.all(
      batch.map(async (zone) => {
        const zoneId = zone.zone_id || zone.id;
        if (!zoneId) return zone;
        const detailed = await getZoneDetails(token, zoneId, envConfig);
        return detailed && typeof detailed === "object" ? { ...zone, ...detailed } : zone;
      })
    );
    for (let j = 0; j < results.length; j++) {
      enriched[i + j] = results[j];
    }
    completed += batch.length;
    if (onProgress && (completed % 25 === 0 || completed === zones.length)) {
      onProgress(`Fetched detail for ${completed}/${zones.length} zone(s)...`);
    }
  }
  return enriched;
}

function normalizeHeaders(row) {
  const out = {};
  for (const [key, value] of Object.entries(row || {})) {
    const normalized = String(key).replace(/\uFEFF/g, "").trim().toLowerCase();
    out[normalized] = value;
  }
  return out;
}

function isBlankRow(row) {
  return Object.values(row).every((value) => String(value ?? "").trim() === "");
}

async function readRowsFromUploadedFile(file) {
  if (!file || !file.buffer) {
    throw new Error("Please upload a CSV/XLS/XLSX file.");
  }

  const workbook = new ExcelJS.Workbook();
  const filename = String(file.originalname || "").toLowerCase();
  const isCSV = filename.endsWith(".csv");

  let worksheet;
  try {
    if (isCSV) {
      const readable = Readable.from(file.buffer);
      worksheet = await workbook.csv.read(readable);
    } else {
      await workbook.xlsx.load(file.buffer);
      worksheet = workbook.worksheets[0];
    }
  } catch (error) {
    throw new Error(`Failed to read file: ${error.message}`);
  }

  if (!worksheet) {
    throw new Error("Uploaded file has no sheets/data.");
  }

  // Extract header names from row 1
  const headerRow = worksheet.getRow(1);
  const headers = {};
  headerRow.eachCell((cell, col) => {
    const val = cell.value;
    headers[col] = val !== null && val !== undefined ? String(val) : "";
  });

  // Build plain objects for each data row
  const rows = [];
  worksheet.eachRow((row, rowNumber) => {
    if (rowNumber === 1) return;
    const obj = {};
    row.eachCell({ includeEmpty: true }, (cell, col) => {
      const header = headers[col];
      if (!header) return;
      const val = cell.value;
      obj[header] = val !== null && val !== undefined ? String(val).trim() : "";
    });
    rows.push(obj);
  });

  const normalizedRows = rows.map(normalizeHeaders).filter((row) => !isBlankRow(row));
  if (!normalizedRows.length) {
    throw new Error("Selected file has no usable rows.");
  }
  return normalizedRows;
}

function missingRequiredFields(row) {
  const required = ["slug", "store_ids", "region_type", "mapping_country", "channels"];
  const missing = required.filter((field) => !String(row[field] ?? "").trim());
  // Accept either column name — mapping_regions (legacy) or pincode (new)
  if (!String(row.mapping_regions ?? "").trim() && !String(row.pincode ?? "").trim()) {
    missing.push("mapping_regions");
  }
  return missing;
}

function buildZonePayload(row) {
  const slug = String(row.slug || "").trim();
  if (!slug) throw new Error("slug is required");

  const name = String(row.name || slug).trim() || slug;
  const companyRaw = String(row.company_id || "1").trim();
  const companyNum = Number(companyRaw);
  const companyId = Number.isInteger(companyNum) ? companyNum : 1;

  const productTags =
    String(row.product_tags || "").trim() || String(row.product_tag || "").trim();

  const channelIds = parseList(row.channels);
  const channels = channelIds.map((channelId) => ({
    channel_id: String(channelId),
    channel_type: "application",
  }));
  if (!channels.length) throw new Error("channels is required");

  const payload = {
    is_active: parseBool(row.is_active, true),
    slug,
    name,
    company_id: companyId,
    store_ids: parseStoreIds(row.store_ids),
    region_type: normalizeRegionType(row.region_type),
    mapping: [
      {
        country: String(row.mapping_country || "").trim(),
        regions: parseList(row.mapping_regions || row.pincode || ""),
      },
    ],
    product: {
      type: normalizeProductType(row.product_type || "all"),
      tags: parseList(productTags),
    },
    channels,
  };

  if (!payload.store_ids.length) throw new Error("store_ids is required");
  if (!payload.mapping[0].country) throw new Error("mapping_country is required");
  if (!payload.mapping[0].regions.length) throw new Error("mapping_regions is required");

  return payload;
}

function extractMapping(zone) {
  let mapping = zone.mapping || zone.mappings || [];
  if (!Array.isArray(mapping) && mapping && typeof mapping === "object") {
    mapping = [mapping];
  }
  return Array.isArray(mapping) ? mapping : [];
}

function summarizeZone(zone) {
  const mapping = extractMapping(zone);
  const countries = new Set();
  const regions = new Set();

  for (const item of mapping) {
    const country = String(item.country || "").trim();
    if (country) countries.add(country);

    let regionValues = item.regions || item.region_ids || [];
    if (typeof regionValues === "string") {
      regionValues = parseList(regionValues);
    }
    for (const regionId of regionValues) {
      regions.add(String(regionId));
    }
  }

  const storeIds = Array.isArray(zone.store_ids)
    ? zone.store_ids.map((storeId) => String(storeId))
    : [];

  return {
    zoneId: String(zone.zone_id || zone.id || "N/A"),
    slug: String(zone.slug || ""),
    name: String(zone.name || ""),
    regionType: String(zone.region_type || zone.regionType || "N/A"),
    storesCount: storeIds.length,
    storesPreview:
      storeIds.length <= 6
        ? storeIds.join(", ")
        : `${storeIds.slice(0, 6).join(", ")} +${storeIds.length - 6} more`,
    regionsCount: regions.size,
    regionsPreview:
      regions.size <= 6
        ? Array.from(regions).join(", ")
        : `${Array.from(regions).slice(0, 6).join(", ")} +${regions.size - 6} more`,
    countries: Array.from(countries),
    countriesPreview:
      countries.size <= 4
        ? Array.from(countries).join(", ")
        : `${Array.from(countries).slice(0, 4).join(", ")} +${countries.size - 4} more`,
  };
}

async function postOrPut(url, token, payload, method) {
  try {
    const response = await axios({
      method,
      url,
      data: payload,
      headers: {
        authorization: `Bearer ${token}`,
        "content-type": "application/json",
      },
      timeout: REQUEST_TIMEOUT,
    });
    return { status: response.status, data: response.data };
  } catch (error) {
    if (error.response) {
      return { status: error.response.status, data: error.response.data };
    }
    return { status: 0, data: `Network error: ${error.message}` };
  }
}

// Runs `fn(item, index)` over `items` with at most `concurrency` in-flight at once.
async function runConcurrent(items, fn, concurrency) {
  const results = new Array(items.length);
  let next = 0;
  async function worker() {
    while (next < items.length) {
      const idx = next++;
      results[idx] = await fn(items[idx], idx);
    }
  }
  await Promise.all(Array.from({ length: Math.min(concurrency, items.length) }, worker));
  return results;
}

async function createZonesFromRows(rows, token, existingZones, envConfig) {
  const existingSlugs = new Set(
    existingZones
      .map((zone) => String(zone.slug || "").trim())
      .filter((slug) => slug.length > 0)
  );

  const logs = [];
  let created = 0;
  let skipped = 0;
  let failed = 0;

  // --- Pass 1: validate rows synchronously, collect work items -----------------
  const toCreate = [];
  for (let i = 0; i < rows.length; i++) {
    const row = rows[i];
    const rowNumber = i + 2;
    const missing = missingRequiredFields(row);
    if (missing.length) {
      logs.push(`Skipping row ${rowNumber}: missing ${missing.join(", ")}`);
      skipped += 1;
      continue;
    }

    let payload;
    try {
      payload = buildZonePayload(row);
    } catch (error) {
      logs.push(`Skipping row ${rowNumber}: ${error.message}`);
      skipped += 1;
      continue;
    }

    if (existingSlugs.has(payload.slug)) {
      logs.push(`Skipping row ${rowNumber}: slug '${payload.slug}' already exists.`);
      skipped += 1;
      continue;
    }

    // Reserve the slug now to prevent within-CSV duplicates from racing.
    existingSlugs.add(payload.slug);
    toCreate.push({ payload, rowNumber });
  }

  // --- Pass 2: fire API calls concurrently ------------------------------------
  const createResults = await runConcurrent(toCreate, async ({ payload }) => {
    const { status, data } = await postOrPut(envConfig.apiBase, token, payload, "post");
    return { slug: payload.slug, status, data };
  }, ZONE_WRITE_CONCURRENCY);

  for (const { slug, status, data } of createResults) {
    if ([200, 201].includes(status)) {
      logs.push(`Created zone '${slug}' successfully.`);
      created += 1;
    } else {
      logs.push(`Failed to create '${slug}'. Status: ${status}, Response: ${JSON.stringify(data)}`);
      failed += 1;
    }
  }

  return { created, skipped, failed, logs };
}

function buildExistingLookup(existingZones) {
  const lookup = new Map();
  for (const zone of existingZones) {
    const slug = String(zone.slug || "").trim();
    const zoneId = zone.zone_id || zone.id;
    if (!slug || !zoneId) continue;

    const oldRegions = new Set();
    for (const mappingItem of extractMapping(zone)) {
      let regions = mappingItem.regions || mappingItem.region_ids || [];
      if (typeof regions === "string") {
        regions = parseList(regions);
      }
      for (const regionId of regions) {
        oldRegions.add(String(regionId));
      }
    }

    for (const storeId of zone.store_ids || []) {
      lookup.set(`${slug}::${storeId}`, { zoneId: String(zoneId), oldRegions });
    }
  }
  return lookup;
}

function setEquals(a, b) {
  if (a.size !== b.size) return false;
  for (const item of a) {
    if (!b.has(item)) return false;
  }
  return true;
}

function planZoneUpdates(rows, existingZones) {
  const lookup = buildExistingLookup(existingZones);
  const updatesByZone = new Map();
  const logs = [];

  for (let i = 0; i < rows.length; i += 1) {
    const row = rows[i];
    const rowNumber = i + 2;
    const missing = missingRequiredFields(row);
    if (missing.length) {
      logs.push(`Skipping row ${rowNumber}: missing ${missing.join(", ")}`);
      continue;
    }

    let payload;
    try {
      payload = buildZonePayload(row);
    } catch (error) {
      logs.push(`Skipping row ${rowNumber}: ${error.message}`);
      continue;
    }

    const newRegions = new Set(payload.mapping[0].regions.map((item) => String(item)));
    let matched = false;

    for (const storeId of payload.store_ids) {
      const key = `${payload.slug}::${storeId}`;
      const entry = lookup.get(key);
      if (!entry) continue;
      matched = true;

      if (setEquals(newRegions, entry.oldRegions)) continue;

      if (!updatesByZone.has(entry.zoneId)) {
        updatesByZone.set(entry.zoneId, {
          zoneId: entry.zoneId,
          slug: payload.slug,
          storeIds: new Set(),
          oldRegions: entry.oldRegions,
          newRegions,
          payload,
        });
      }
      updatesByZone.get(entry.zoneId).storeIds.add(String(storeId));
    }

    if (!matched) {
      logs.push(
        `Row ${rowNumber}: no existing zone matched for slug '${payload.slug}' and store IDs ${payload.store_ids.join(", ")}.`
      );
    }
  }

  const updates = Array.from(updatesByZone.values());
  for (const update of updates) {
    logs.push(
      `Planned update -> slug=${update.slug}, zone_id=${update.zoneId}, stores=[${Array.from(update.storeIds).join(", ")}], old_regions=${update.oldRegions.size}, new_regions=${update.newRegions.size}`
    );
  }

  return { updates, logs };
}

async function applyZoneUpdates(updates, token, envConfig) {
  const logs = [];
  let updated = 0;
  let failed = 0;

  const applyResults = await runConcurrent(updates, async (update) => {
    const payload = {
      ...update.payload,
      zone_id: update.zoneId,
      mapping: [
        {
          ...update.payload.mapping[0],
          regions: Array.from(update.newRegions).sort(),
        },
      ],
    };
    const { status, data } = await postOrPut(
      `${envConfig.apiBase}/${update.zoneId}`,
      token,
      payload,
      "put"
    );
    return { update, status, data };
  }, ZONE_WRITE_CONCURRENCY);

  for (const { update, status, data } of applyResults) {
    if ([200, 201].includes(status)) {
      logs.push(
        `Updated zone '${update.slug}' (zone_id=${update.zoneId}) for stores [${Array.from(update.storeIds).join(", ")}].`
      );
      updated += 1;
    } else {
      logs.push(
        `Failed to update zone '${update.slug}' (zone_id=${update.zoneId}). Status: ${status}, Response: ${JSON.stringify(data)}`
      );
      failed += 1;
    }
  }

  return { updated, failed, logs };
}

function apiError(res, error) {
  res.status(400).json({
    ok: false,
    error: error instanceof Error ? error.message : String(error),
  });
}

app.get("/api/environments", (_req, res) => {
  const envList = Object.entries(ENVIRONMENTS).map(([key, cfg]) => ({
    key,
    label: cfg.label,
    type: cfg.type || null,
  }));
  res.json({ ok: true, environments: envList, default: DEFAULT_ENV });
});

app.post("/api/clear-cookie-cache", (req, res) => {
  try {
    const rawCookie = normalizeCookieString(req.body.cookieString || "");
    const envConfig = getEnvConfig(req.body.env);
    if (!rawCookie) {
      return res.json({ ok: true, cleared: 0 });
    }
    const cacheKey = getCookieCacheKey(rawCookie, envConfig.key);
    const removed = tokenCache.delete(cacheKey) ? 1 : 0;
    res.json({ ok: true, cleared: removed });
  } catch (error) {
    apiError(res, error);
  }
});

app.post("/api/parse-session-curl", (req, res) => {
  try {
    const parsed = parseCurlCommand(req.body.curlCommand || "");
    const sessionRequest = buildSessionCheckRequest(parsed);
    res.json({
      ok: true,
      cookieString: normalizeCookieString(parsed.headers.cookie || ""),
      authorizationHeader: parsed.headers.authorization || "",
      sessionRequest,
    });
  } catch (error) {
    apiError(res, error);
  }
});

app.post("/api/session-check", async (req, res) => {
  try {
    let sessionRequest = req.body.sessionRequest || null;
    if (!sessionRequest && req.body.curlCommand) {
      const parsed = parseCurlCommand(req.body.curlCommand);
      sessionRequest = buildSessionCheckRequest(parsed);
    }
    const result = await runSessionCheck(sessionRequest);
    res.json({ ok: true, ...result });
  } catch (error) {
    apiError(res, error);
  }
});

app.post("/api/fetch-zones", async (req, res) => {
  try {
    const cookieString = req.body.cookieString || "";
    const includeDetails = req.body.includeDetails !== false;
    const envConfig = getEnvConfig(req.body.env);

    const token = await getBearerToken(cookieString, envConfig);
    let zones = await getZones(token, envConfig);
    const logs = [
      `[${envConfig.label}] Fetched ${zones.length} zone(s) from list API.`,
    ];

    if (includeDetails && zones.length) {
      logs.push("Fetching per-zone details for mapping/regions (parallel)...");
      zones = await enrichZonesWithDetails(token, zones, (msg) => logs.push(msg), envConfig);
    } else if (zones.length && zones.every((zone) => !zoneHasMappingData(zone))) {
      logs.push(
        "List API does not include mapping/region fields. Enable details to fetch them."
      );
    }

    const zoneSummaries = zones.map(summarizeZone);
    res.json({ ok: true, total: zoneSummaries.length, zones: zoneSummaries, logs });
  } catch (error) {
    apiError(res, error);
  }
});

app.post("/api/create-zones", upload.single("file"), async (req, res) => {
  try {
    const cookieString = req.body.cookieString || "";
    const envConfig = getEnvConfig(req.body.env);
    const token = await getBearerToken(cookieString, envConfig);
    const rows = await readRowsFromUploadedFile(req.file);
    const existingZones = await getZones(token, envConfig);
    const result = await createZonesFromRows(rows, token, existingZones, envConfig);

    res.json({
      ok: true,
      summary: {
        totalRows: rows.length,
        created: result.created,
        skipped: result.skipped,
        failed: result.failed,
      },
      logs: result.logs,
    });
  } catch (error) {
    apiError(res, error);
  }
});

async function loadZonesForUpdate(token, envConfig) {
  let zones = await getZones(token, envConfig);
  if (zones.length && zones.every((zone) => !zoneHasMappingData(zone))) {
    zones = await enrichZonesWithDetails(token, zones, null, envConfig);
  }
  if (zones.length && zones.every((zone) => !zoneHasMappingData(zone))) {
    throw new Error(
      "Could not load mapping details for zones. Cannot safely plan/apply updates."
    );
  }
  return zones;
}

app.post("/api/plan-updates", upload.single("file"), async (req, res) => {
  try {
    const cookieString = req.body.cookieString || "";
    const envConfig = getEnvConfig(req.body.env);
    const token = await getBearerToken(cookieString, envConfig);
    const rows = await readRowsFromUploadedFile(req.file);
    const existingZones = await loadZonesForUpdate(token, envConfig);

    const { updates, logs } = planZoneUpdates(rows, existingZones);
    const planned = updates.map((update) => ({
      zoneId: update.zoneId,
      slug: update.slug,
      storeIds: Array.from(update.storeIds).sort(),
      oldRegionsCount: update.oldRegions.size,
      newRegionsCount: update.newRegions.size,
      oldRegionsPreview:
        update.oldRegions.size <= 5
          ? Array.from(update.oldRegions)
          : Array.from(update.oldRegions).slice(0, 5),
      newRegionsPreview:
        update.newRegions.size <= 5
          ? Array.from(update.newRegions)
          : Array.from(update.newRegions).slice(0, 5),
    }));

    res.json({
      ok: true,
      summary: { totalRows: rows.length, plannedUpdates: planned.length },
      updates: planned,
      logs,
    });
  } catch (error) {
    apiError(res, error);
  }
});

app.post("/api/apply-updates", upload.single("file"), async (req, res) => {
  try {
    const cookieString = req.body.cookieString || "";
    const envConfig = getEnvConfig(req.body.env);
    const token = await getBearerToken(cookieString, envConfig);
    const rows = await readRowsFromUploadedFile(req.file);
    const existingZones = await loadZonesForUpdate(token, envConfig);

    const { updates, logs: planningLogs } = planZoneUpdates(rows, existingZones);
    if (!updates.length) {
      return res.json({
        ok: true,
        summary: { totalRows: rows.length, plannedUpdates: 0, updated: 0, failed: 0 },
        logs: [...planningLogs, "No updates needed."],
      });
    }

    const result = await applyZoneUpdates(updates, token, envConfig);
    res.json({
      ok: true,
      summary: {
        totalRows: rows.length,
        plannedUpdates: updates.length,
        updated: result.updated,
        failed: result.failed,
      },
      logs: [...planningLogs, ...result.logs],
    });
  } catch (error) {
    apiError(res, error);
  }
});

function extractCookiesFromResponse(response) {
  const rawHeaders = response.headers["set-cookie"];
  if (!rawHeaders || !rawHeaders.length) return "";
  return rawHeaders
    .map((h) => h.split(";")[0].trim())
    .filter(Boolean)
    .join("; ");
}

app.post("/api/login/send-otp", loginLimiter, async (req, res) => {
  try {
    const email = String(req.body.email || "").trim();
    const envConfig = getEnvConfig(req.body.env);

    if (!email) throw new Error("Email is required.");
    if (!email.includes("@")) throw new Error("Invalid email address.");

    const response = await axios.post(
      `${envConfig.authBase}/auth/login/email/otp/send`,
      { email },
      {
        params: { origin: "platform" },
        headers: {
          accept: "application/json, text/plain, */*",
          "content-type": "application/json",
          origin: envConfig.platformOrigin,
          referer: `${envConfig.platformOrigin}/`,
          "user-agent": "Mozilla/5.0",
        },
        timeout: REQUEST_TIMEOUT,
        validateStatus: () => true,
      }
    );

    if (response.status >= 400) {
      const msg =
        response.data?.message ||
        response.data?.error ||
        JSON.stringify(response.data);
      throw new Error(`OTP send failed (${response.status}): ${msg}`);
    }

    const sessionCookies = extractCookiesFromResponse(response);
    const requestId =
      response.data?.request_id ||
      response.data?.data?.request_id ||
      null;

    res.json({
      ok: true,
      requestId,
      sessionCookies,
      resendTimer: response.data?.resend_timer || 30,
      message: response.data?.message || `OTP sent to ${email}.`,
    });
  } catch (error) {
    apiError(res, error);
  }
});

app.post("/api/login/verify-otp", loginLimiter, async (req, res) => {
  try {
    const email          = String(req.body.email         || "").trim();
    const otp            = String(req.body.otp           || "").trim();
    const requestId      = req.body.requestId            || null;
    const sessionCookies = String(req.body.sessionCookies || "").trim();
    const envConfig      = getEnvConfig(req.body.env);

    if (!email) throw new Error("Email is required.");
    if (!otp)   throw new Error("OTP is required.");

    const body = { email, otp };
    if (requestId) body.request_id = requestId;

    const reqHeaders = {
      accept: "application/json, text/plain, */*",
      "content-type": "application/json",
      origin: envConfig.platformOrigin,
      referer: `${envConfig.platformOrigin}/`,
      "user-agent": "Mozilla/5.0",
    };
    if (sessionCookies) reqHeaders.cookie = sessionCookies;

    // Try /verify first; fall back to bare /otp endpoint only if the first
    // returns 404 (endpoint not found). Any other 4xx (e.g. 400 = wrong OTP)
    // should be surfaced immediately without retrying on the fallback URL.
    let response;
    for (const suffix of ["/verify", ""]) {
      response = await axios.post(
        `${envConfig.authBase}/auth/login/email/otp${suffix}`,
        body,
        {
          params: { origin: "platform" },
          headers: reqHeaders,
          timeout: REQUEST_TIMEOUT,
          validateStatus: () => true,
        }
      );
      if (response.status !== 404) break;
    }

    const cookieString = extractCookiesFromResponse(response);

    if (response.status >= 400 || !cookieString) {
      const msg =
        response.data?.message ||
        response.data?.error ||
        JSON.stringify(response.data);
      throw new Error(
        cookieString
          ? `Unexpected status ${response.status}: ${msg}`
          : `Login failed (${response.status}): ${msg || "No session cookies returned."}`
      );
    }

    res.json({
      ok: true,
      cookieString,
      email,
      env: envConfig.key,
      message: "Login successful.",
    });
  } catch (error) {
    apiError(res, error);
  }
});

app.get("/api/version", (_req, res) => {
  // eslint-disable-next-line global-require
  const { version }     = require("./package.json");
  // eslint-disable-next-line global-require
  const { buildNumber } = require("./build-number.json");
  res.json({ ok: true, version, buildNumber });
});

app.get("/login", (_req, res) => {
  res.sendFile(path.join(__dirname, "public", "login.html"));
});

app.get("/health", (_req, res) => {
  res.json({ ok: true });
});

if (require.main === module) {
  // Running directly: node server.js
  const PORT = Number(process.env.PORT || 3000);
  app.listen(PORT, () => {
    // eslint-disable-next-line no-console
    console.log(`Delivery Zone web app running at http://localhost:${PORT}`);
  });
} else {
  // Required by Electron — caller controls the port
  module.exports = {
    startServer: (port) =>
      new Promise((resolve, reject) => {
        const srv = app.listen(port || 3000, (err) => {
          if (err) return reject(err);
          resolve(srv.address().port);
        });
        srv.on("error", reject);
      }),
  };
}
