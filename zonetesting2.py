import argparse
import base64
import csv
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

try:
    import pandas as pd
except ModuleNotFoundError:
    pd = None

try:
    import requests
except ModuleNotFoundError:
    requests = None

try:
    import browser_cookie3
except ModuleNotFoundError:
    browser_cookie3 = None

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

    TK_AVAILABLE = True
except Exception:
    tk = None
    filedialog = None
    messagebox = None
    scrolledtext = None
    simpledialog = None
    ttk = None
    TK_AVAILABLE = False

REQUEST_TIMEOUT = 30
try:
    DETAIL_FETCH_MAX_WORKERS = max(1, int(os.getenv("DZ_DETAIL_WORKERS", "6")))
except ValueError:
    DETAIL_FETCH_MAX_WORKERS = 6
CONFIG_PATH = Path.home() / ".delivery_zone_manager.json"
DEFAULT_ENV = "sit"
LOGO_URL = "https://cdn.pixelbin.io/v2/fynd-console/original/console-frontend/console_logo_new_white.svg"
ENVIRONMENTS = {
    "sit": {
        "label": "SIT",
        "platform_origin": "https://platform.jiox0.de",
        "api_base": (
            "https://api.jiox0.de/service/platform/logistics/v2.0/company/1/zones"
        ),
        "token_url": (
            "https://api.jiox0.de/service/panel/authentication/v1.0/"
            "company/1/oauth/staff/token"
        ),
        "auth_base": "https://api.jiox0.de/service/panel/authentication/v1.0",
    },
    "uat": {
        "label": "UAT",
        "platform_origin": "https://platform.jiox5.de",
        "api_base": (
            "https://api.jiox5.de/service/platform/logistics/v2.0/company/1/zones"
        ),
        "token_url": (
            "https://api.jiox5.de/service/panel/authentication/v1.0/"
            "company/1/oauth/staff/token"
        ),
        "auth_base": "https://api.jiox5.de/service/panel/authentication/v1.0",
    },
    "prod": {
        "label": "PROD",
        "platform_origin": "https://platform.jioretailer.com",
        "api_base": (
            "https://api.jioretailer.com/service/platform/logistics/v2.0/company/1/zones"
        ),
        "token_url": (
            "https://api.jioretailer.com/service/panel/authentication/v1.0/"
            "company/1/oauth/staff/token"
        ),
        "auth_base": "https://api.jioretailer.com/service/panel/authentication/v1.0",
    },
}


def normalize_env_name(value):
    text = str(value or "").strip().lower()
    if text in {"sit", "uat", "prod"}:
        return text
    return ""


def get_env_config(env_name):
    normalized = normalize_env_name(env_name)
    if not normalized:
        raise ValueError("Environment must be one of: sit, uat, prod.")
    return ENVIRONMENTS[normalized]


def parse_true_false_text(value, default=False):
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def build_tls_error_message(exc):
    return (
        "TLS/SSL certificate verification failed. "
        "For PROD/UAT behind enterprise proxy, provide CA bundle via "
        "--ca-bundle /path/to/ca.pem (or set DZ_CA_BUNDLE). "
        "Temporary workaround (not recommended): --insecure-skip-tls-verify. "
        f"Original error: {exc}"
    )


def normalize_ca_bundle_path(path_value):
    text = str(path_value or "").strip()
    if not text:
        return ""
    path = Path(text).expanduser()
    if not path.exists():
        raise ValueError(f"CA bundle file does not exist: {path}")
    return str(path)


def resolve_tls_settings(args, config):
    insecure = bool(getattr(args, "insecure_skip_tls_verify", False))
    insecure = insecure or parse_true_false_text(
        os.getenv("DZ_INSECURE_SKIP_TLS_VERIFY", ""), default=False
    )
    insecure = insecure or bool(config.get("insecure_skip_tls_verify", False))

    ca_bundle_raw = getattr(args, "ca_bundle", None) or ""
    if not ca_bundle_raw:
        ca_bundle_raw = os.getenv("DZ_CA_BUNDLE", "")
    if not ca_bundle_raw:
        ca_bundle_raw = str(config.get("ca_bundle", "")).strip()

    ca_bundle = ""
    if not insecure and ca_bundle_raw:
        ca_bundle = normalize_ca_bundle_path(ca_bundle_raw)
    verify = False if insecure else (ca_bundle if ca_bundle else True)
    mode = "INSECURE (verify disabled)" if insecure else (
        f"CA bundle: {ca_bundle}" if ca_bundle else "System CA store"
    )
    return {
        "verify": verify,
        "insecure": insecure,
        "ca_bundle": ca_bundle,
        "mode": mode,
    }


def ensure_dependencies():
    missing = []
    if pd is None:
        missing.append("pandas")
    if requests is None:
        missing.append("requests")
    if missing:
        raise RuntimeError(
            "Missing required package(s): "
            + ", ".join(missing)
            + ". Install with: pip3 install pandas requests openpyxl"
        )


def normalize_cookie_header(cookie_string):
    no_prefix = str(cookie_string or "").strip().replace("\n", "; ")
    if no_prefix.lower().startswith("cookie:"):
        no_prefix = no_prefix.split(":", 1)[1]
    parts = [chunk.strip() for chunk in no_prefix.split(";") if chunk.strip()]
    return "; ".join(parts)


def parse_cookie_string(cookie_string):
    cookies = {}
    normalized = normalize_cookie_header(cookie_string)
    for item in normalized.split(";"):
        if "=" not in item:
            continue
        key, value = item.strip().split("=", 1)
        if key and value:
            cookies[key] = value
    return cookies


def _host_from_url(url):
    try:
        return str(urlparse(str(url or "")).hostname or "").strip().lower()
    except Exception:
        return ""


def _environment_cookie_domains(env_config):
    domains = []
    for key in ("platform_origin", "token_url", "api_base"):
        host = _host_from_url(env_config.get(key, ""))
        if not host:
            continue
        if host not in domains:
            domains.append(host)
        parts = host.split(".")
        if len(parts) > 2:
            parent = ".".join(parts[-2:])
            if parent and parent not in domains:
                domains.append(parent)
    return domains


def _cookiejar_to_header(cookiejar):
    if cookiejar is None:
        return ""

    # Keep deterministic order and avoid duplicates by cookie name.
    seen = set()
    cookie_parts = []
    for cookie in cookiejar:
        name = str(getattr(cookie, "name", "") or "").strip()
        value = str(getattr(cookie, "value", "") or "").strip()
        if not name or not value or name in seen:
            continue
        seen.add(name)
        cookie_parts.append(f"{name}={value}")
    return normalize_cookie_header("; ".join(cookie_parts))


def try_load_browser_cookie_header(env_config):
    if browser_cookie3 is None:
        return "", "browser_cookie3 not installed (pip3 install browser-cookie3)"

    domains = _environment_cookie_domains(env_config)
    if not domains:
        return "", "Could not infer environment domains"

    loaders = []
    for attr, label in (
        ("chrome", "Chrome"),
        ("brave", "Brave"),
        ("edge", "Edge"),
        ("firefox", "Firefox"),
        ("opera", "Opera"),
        ("chromium", "Chromium"),
    ):
        loader = getattr(browser_cookie3, attr, None)
        if callable(loader):
            loaders.append((label, loader))

    if not loaders:
        return "", "No browser loaders available"

    for domain in domains:
        for label, loader in loaders:
            try:
                cookie_jar = loader(domain_name=domain)
            except Exception:
                continue
            cookie_header = _cookiejar_to_header(cookie_jar)
            if cookie_header:
                return cookie_header, f"{label} ({domain})"

    return "", (
        "No matching browser cookie found for selected environment. "
        "Login to that environment in browser and retry."
    )


def parse_list(cell):
    if pd.isna(cell):
        return []
    text = str(cell).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if str(x).strip()]
    except Exception:
        pass
    cleaned = (
        text.replace("[", "")
        .replace("]", "")
        .replace("'", "")
        .replace('"', "")
    )
    return [x.strip() for x in cleaned.split(",") if x.strip()]


def parse_store_ids(cell):
    values = parse_list(cell)
    parsed_ids = []
    for value in values:
        if value.isdigit():
            parsed_ids.append(int(value))
            continue
        try:
            float_val = float(value)
            if float_val.is_integer():
                parsed_ids.append(int(float_val))
            else:
                parsed_ids.append(value)
        except Exception:
            parsed_ids.append(value)
    return parsed_ids


def parse_bool(cell, default=True):
    if pd.isna(cell):
        return default
    text = str(cell).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return default


def normalize_region_type(value):
    text = str(value or "pincode").strip().lower()
    return text if text in {"pincode", "non-pincode"} else "pincode"


def normalize_product_type(value):
    text = str(value or "all").strip().lower()
    return text if text in {"all", "explicit"} else "all"


def safe_json(response):
    try:
        return response.json()
    except ValueError:
        return response.text


def get_bearer_token(
    cookie_string,
    token_url,
    verify=True,
    platform_origin="",
    env_label="",
):
    normalized_cookie = normalize_cookie_header(cookie_string)
    if not normalized_cookie:
        raise ValueError("Cookie string is required.")

    headers = {
        "accept": "application/json, text/plain, */*",
        "user-agent": "Mozilla/5.0",
    }
    if platform_origin:
        headers["origin"] = platform_origin
        headers["referer"] = f"{platform_origin}/"
    try:
        response = requests.get(
            token_url,
            headers=headers,
            cookies=parse_cookie_string(normalized_cookie),
            timeout=REQUEST_TIMEOUT,
            verify=verify,
        )
    except requests.exceptions.SSLError as exc:
        raise RuntimeError(build_tls_error_message(exc)) from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"Token request failed: {exc}") from exc

    if response.status_code == 401:
        env_hint = f" for {env_label}" if env_label else ""
        raise RuntimeError(
            "Failed to fetch token (401 unauthenticated). "
            f"Your cookie is likely invalid{env_hint} or copied from another env. "
            "Please login to the same environment in browser and copy fresh Cookie header."
        )
    if response.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch token ({response.status_code}): {response.text}"
        )

    data = safe_json(response)
    if not isinstance(data, dict):
        raise RuntimeError("Token response was not valid JSON.")

    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"Access token not found in response: {data}")
    return token


def send_otp(email, env_config, verify=True):
    """Send OTP to email address. Returns (request_id, response_data)."""
    auth_base = env_config.get("auth_base", "")
    if not auth_base:
        raise RuntimeError("auth_base not configured for this environment.")
    url = f"{auth_base}/auth/login/email/otp/send?origin=platform"
    try:
        response = requests.post(
            url,
            json={"email": email},
            timeout=REQUEST_TIMEOUT,
            verify=verify,
        )
    except requests.exceptions.SSLError as exc:
        raise RuntimeError(build_tls_error_message(exc)) from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"OTP send failed: {exc}") from exc

    if response.status_code not in {200, 201}:
        raise RuntimeError(
            f"OTP send failed ({response.status_code}): {response.text}"
        )

    data = safe_json(response)
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected OTP send response: {data}")

    # request_id may be top-level or nested under "data"
    request_id = (
        data.get("request_id")
        or data.get("requestId")
        or (data.get("data") or {}).get("request_id")
        or (data.get("data") or {}).get("requestId")
        or ""
    )
    return str(request_id), data


def verify_otp(email, otp, request_id, env_config, verify=True):
    """Verify OTP code. Returns (cookie_header, response_data)."""
    auth_base = env_config.get("auth_base", "")
    if not auth_base:
        raise RuntimeError("auth_base not configured for this environment.")

    # Try primary endpoint, fall back to legacy path
    endpoints = [
        f"{auth_base}/auth/login/email/otp/verify?origin=platform",
        f"{auth_base}/auth/login/otp/verify?origin=platform",
    ]
    payload = {"email": email, "otp": str(otp), "request_id": request_id}

    for url in endpoints:
        try:
            response = requests.post(
                url,
                json=payload,
                timeout=REQUEST_TIMEOUT,
                verify=verify,
            )
        except requests.exceptions.SSLError as exc:
            raise RuntimeError(build_tls_error_message(exc)) from exc
        except requests.RequestException as exc:
            raise RuntimeError(f"OTP verify network error: {exc}") from exc

        if response.status_code == 404:
            continue  # try fallback

        if response.status_code not in {200, 201}:
            raise RuntimeError(
                f"OTP verification failed ({response.status_code}): {response.text}"
            )

        # Build cookie header from response cookies
        cookie_parts = [
            f"{name}={val}" for name, val in response.cookies.items()
        ]
        cookie_header = normalize_cookie_header("; ".join(cookie_parts))
        return cookie_header, safe_json(response)

    raise RuntimeError("OTP verification failed on all endpoints (404).")


def get_zones(token, api_base, verify=True):
    headers = {"authorization": f"Bearer {token}"}
    zones = []
    page_no = 1
    page_size = 500

    while True:
        try:
            response = requests.get(
                api_base,
                headers=headers,
                params={"page_no": page_no, "page_size": page_size},
                timeout=REQUEST_TIMEOUT,
                verify=verify,
            )
        except requests.exceptions.SSLError as exc:
            raise RuntimeError(build_tls_error_message(exc)) from exc
        except requests.RequestException as exc:
            raise RuntimeError(f"Failed to fetch zones: {exc}") from exc

        if response.status_code != 200:
            raise RuntimeError(
                f"Error fetching zones ({response.status_code}): {response.text}"
            )

        payload = safe_json(response)
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected zones response type: {type(payload)}")

        items = payload.get("items", [])
        zones.extend(items)
        if len(items) < page_size:
            break
        page_no += 1

    return zones


def extract_zone_from_response(payload):
    if isinstance(payload, dict):
        if "item" in payload and isinstance(payload["item"], dict):
            return payload["item"]
        if "data" in payload and isinstance(payload["data"], dict):
            return payload["data"]
    return payload if isinstance(payload, dict) else None


def get_zone_details(token, zone_id, api_base, verify=True):
    headers = {"authorization": f"Bearer {token}"}
    url = f"{api_base}/{zone_id}"
    try:
        response = requests.get(
            url, headers=headers, timeout=REQUEST_TIMEOUT, verify=verify
        )
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return None
    payload = safe_json(response)
    return extract_zone_from_response(payload)


def zone_has_mapping_data(zone):
    return bool(
        zone.get("mapping")
        or zone.get("mappings")
        or zone.get("region_type")
        or zone.get("regionType")
    )


def enrich_zones_with_details(
    token,
    zones,
    api_base,
    verify=True,
    logger=None,
    progress_cb=None,
):
    if not zones:
        if progress_cb:
            progress_cb(1, 1)
        return zones

    # Fetch details only for zones where mapping fields are missing.
    pending = []
    for index, zone in enumerate(zones):
        if zone_has_mapping_data(zone):
            continue
        zone_id = zone.get("zone_id") or zone.get("id")
        if zone_id:
            pending.append((index, str(zone_id)))

    if not pending:
        if progress_cb:
            progress_cb(1, 1)
        return zones

    total = len(pending)
    workers = min(DETAIL_FETCH_MAX_WORKERS, total)
    details_by_index = {}
    completed = 0

    if logger:
        logger(
            f"Fetching per-zone details for {total} zone(s) "
            f"using {workers} worker(s)..."
        )

    def _fetch(index, zone_id):
        return index, get_zone_details(token, zone_id, api_base, verify=verify)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_fetch, idx, zid): idx
            for idx, zid in pending
        }
        for future in as_completed(futures):
            index = futures[future]
            detailed = None
            try:
                _resolved_index, detailed = future.result()
                index = _resolved_index
            except Exception:
                detailed = None
            if detailed:
                details_by_index[index] = detailed
            completed += 1
            if progress_cb:
                progress_cb(completed, total)

    enriched = []
    for index, zone in enumerate(zones):
        detailed = details_by_index.get(index)
        if detailed:
            merged = dict(zone)
            merged.update(detailed)
            enriched.append(merged)
        else:
            enriched.append(zone)
    return enriched


def post_or_put(url, token, payload, method="POST", verify=True):
    headers = {
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
    }
    try:
        if method == "POST":
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT,
                verify=verify,
            )
        else:
            response = requests.put(
                url,
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT,
                verify=verify,
            )
    except requests.exceptions.SSLError as exc:
        return 0, build_tls_error_message(exc)
    except requests.RequestException as exc:
        return 0, f"Network error: {exc}"

    return response.status_code, safe_json(response)


def missing_required_fields(row):
    required = [
        "slug",
        "store_ids",
        "region_type",
        "mapping_country",
        "channels",
    ]
    missing = []
    for field in required:
        value = row.get(field)
        if pd.isna(value) or not str(value).strip():
            missing.append(field)
    # Accept either mapping_regions or pincode (legacy column name)
    regions_val = row.get("mapping_regions")
    pincode_val = row.get("pincode")
    regions_empty = (regions_val is None or pd.isna(regions_val) or not str(regions_val).strip())
    pincode_empty = (pincode_val is None or pd.isna(pincode_val) or not str(pincode_val).strip())
    if regions_empty and pincode_empty:
        missing.append("mapping_regions")
    return missing


def build_zone_payload(row):
    slug = str(row.get("slug", "")).strip()
    if not slug:
        raise ValueError("slug is required")

    name = str(row.get("name", slug)).strip() or slug

    company_id_raw = row.get("company_id", 1)
    try:
        company_id = int(float(company_id_raw))
    except Exception:
        company_id = 1

    product_tags = row.get("product_tags")
    if product_tags is None or pd.isna(product_tags) or not str(product_tags).strip():
        product_tags = row.get("product_tag", "")

    channel_ids = parse_list(row.get("channels"))
    channels = [
        {"channel_id": str(channel_id), "channel_type": "application"}
        for channel_id in channel_ids
    ]
    if not channels:
        raise ValueError("channels is required")

    # Accept either mapping_regions or pincode (legacy column name)
    regions_raw = row.get("mapping_regions") or row.get("pincode") or ""

    payload = {
        "is_active": parse_bool(row.get("is_active"), default=True),
        "slug": slug,
        "name": name,
        "company_id": company_id,
        "store_ids": parse_store_ids(row.get("store_ids")),
        "region_type": normalize_region_type(row.get("region_type")),
        "mapping": [
            {
                "country": str(row.get("mapping_country", "")).strip(),
                "regions": parse_list(regions_raw),
            }
        ],
        "product": {
            "type": normalize_product_type(row.get("product_type", "all")),
            "tags": parse_list(product_tags),
        },
        "channels": channels,
    }

    if not payload["store_ids"]:
        raise ValueError("store_ids is required")
    if not payload["mapping"][0]["country"]:
        raise ValueError("mapping_country is required")
    if not payload["mapping"][0]["regions"]:
        raise ValueError("mapping_regions is required")

    return payload


def read_zone_file(file_path):
    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        df = pd.read_excel(path, dtype=str)
    elif suffix == ".csv":
        df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    else:
        raise ValueError("Unsupported file type. Use CSV, XLS, or XLSX.")

    df = df.dropna(how="all")
    if df.empty:
        raise ValueError("Selected file has no usable rows.")

    df.columns = [str(col).replace("\ufeff", "").strip().lower() for col in df.columns]
    return df


def create_zones_from_file(
    df,
    token,
    existing_zones,
    logger,
    api_base,
    verify=True,
    progress_cb=None,
):
    existing_slugs = {
        str(zone.get("slug", "")).strip() for zone in existing_zones if zone.get("slug")
    }
    created = 0
    skipped = 0
    failed = 0

    records = df.to_dict(orient="records")
    total_rows = len(records)
    for idx, row_data in enumerate(records):
        row_num = idx + 2
        try:
            missing = missing_required_fields(row_data)
            if missing:
                logger(f"Skipping row {row_num}: missing field(s) {missing}")
                skipped += 1
                continue

            try:
                payload = build_zone_payload(row_data)
            except ValueError as exc:
                logger(f"Skipping row {row_num}: {exc}")
                skipped += 1
                continue

            if payload["slug"] in existing_slugs:
                logger(f"Skipping row {row_num}: slug '{payload['slug']}' already exists.")
                skipped += 1
                continue

            status, resp = post_or_put(api_base, token, payload, "POST", verify=verify)
            if status in {200, 201}:
                logger(f"Created zone '{payload['slug']}' successfully.")
                created += 1
                existing_slugs.add(payload["slug"])
            else:
                logger(
                    f"Failed to create '{payload['slug']}'. "
                    f"Status: {status}, Response: {resp}"
                )
                failed += 1
        finally:
            if progress_cb:
                progress_cb(idx + 1, total_rows)

    summary = {
        "total_rows": len(df),
        "created": created,
        "skipped": skipped,
        "failed": failed,
    }
    logger(
        "Create summary -> "
        f"Rows: {summary['total_rows']}, Created: {created}, "
        f"Skipped: {skipped}, Failed: {failed}"
    )
    return summary


def build_existing_lookup(existing_zones):
    lookup = {}
    for zone in existing_zones:
        slug = str(zone.get("slug", "")).strip()
        zone_id = zone.get("zone_id") or zone.get("id")
        if not slug or not zone_id:
            continue

        old_regions = set()
        for mapping_item in zone.get("mapping", []):
            old_regions.update(str(x) for x in mapping_item.get("regions", []))

        for store_id in zone.get("store_ids", []):
            lookup[(slug, str(store_id))] = {
                "zone_id": zone_id,
                "old_regions": old_regions,
            }
    return lookup


def plan_zone_updates(df, existing_zones, logger, progress_cb=None):
    lookup = build_existing_lookup(existing_zones)
    updates_by_zone = {}
    skipped = 0
    unmatched = 0

    records = df.to_dict(orient="records")
    total_rows = len(records)
    for idx, row_data in enumerate(records):
        row_num = idx + 2
        try:
            missing = missing_required_fields(row_data)
            if missing:
                logger(f"Skipping row {row_num}: missing field(s) {missing}")
                skipped += 1
                continue

            try:
                payload = build_zone_payload(row_data)
            except ValueError as exc:
                logger(f"Skipping row {row_num}: {exc}")
                skipped += 1
                continue

            new_regions = set(payload["mapping"][0]["regions"])
            matched = False

            for store_id in payload["store_ids"]:
                key = (payload["slug"], str(store_id))
                if key not in lookup:
                    continue

                matched = True
                match = lookup[key]
                if new_regions == match["old_regions"]:
                    continue

                zone_id = match["zone_id"]
                if zone_id not in updates_by_zone:
                    updates_by_zone[zone_id] = {
                        "zone_id": zone_id,
                        "slug": payload["slug"],
                        "store_ids": set(),
                        "old_regions": match["old_regions"],
                        "new_regions": new_regions,
                        "payload": payload,
                    }
                updates_by_zone[zone_id]["store_ids"].add(str(store_id))

            if not matched:
                logger(
                    f"Row {row_num}: no matching existing zone found for slug "
                    f"'{payload['slug']}' and store IDs {payload['store_ids']}."
                )
                unmatched += 1
        finally:
            if progress_cb:
                progress_cb(idx + 1, total_rows)

    updates = list(updates_by_zone.values())
    for update in updates:
        logger(
            f"Update planned -> slug: {update['slug']}, zone_id: {update['zone_id']}, "
            f"store_ids: {sorted(update['store_ids'])}, "
            f"old_regions: {sorted(update['old_regions'])}, "
            f"new_regions: {sorted(update['new_regions'])}"
        )

    stats = {
        "total_rows": len(df),
        "planned_updates": len(updates),
        "skipped_rows": skipped,
        "unmatched_rows": unmatched,
    }
    logger(
        "Plan summary -> "
        f"Rows: {stats['total_rows']}, Planned: {stats['planned_updates']}, "
        f"Skipped: {stats['skipped_rows']}, Unmatched: {stats['unmatched_rows']}"
    )
    return updates, stats


def apply_zone_updates(
    updates,
    token,
    logger,
    api_base,
    verify=True,
    progress_cb=None,
):
    success = 0
    failed = 0

    total_updates = len(updates)
    for idx, update in enumerate(updates, start=1):
        payload = update["payload"]
        payload["zone_id"] = update["zone_id"]
        payload["mapping"][0]["regions"] = sorted(update["new_regions"])
        url = f"{api_base}/{update['zone_id']}"
        status, resp = post_or_put(url, token, payload, "PUT", verify=verify)
        if status in {200, 201}:
            logger(
                f"Updated zone '{update['slug']}' (zone_id={update['zone_id']}) "
                f"for store IDs {sorted(update['store_ids'])}."
            )
            success += 1
        else:
            logger(
                f"Failed to update zone '{update['slug']}' (zone_id={update['zone_id']}). "
                f"Status: {status}, Response: {resp}"
            )
            failed += 1
        if progress_cb:
            progress_cb(idx, total_updates)

    summary = {"updated": success, "failed": failed}
    logger(f"Update summary -> Updated: {success}, Failed: {failed}")
    return summary


def load_preferences(path=CONFIG_PATH):
    try:
        if not path.exists():
            return {}
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_preferences(data, path=CONFIG_PATH):
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def shorten(text, width):
    value = str(text)
    if len(value) <= width:
        return value
    if width <= 3:
        return value[:width]
    return f"{value[: width - 3]}..."


def preview(items, max_items=4):
    values = [str(x) for x in items]
    if not values:
        return "-"
    if len(values) <= max_items:
        return ",".join(values)
    return f"{','.join(values[:max_items])} +{len(values) - max_items} more"


def zone_to_table_row(zone):
    zone_id = zone.get("zone_id") or zone.get("id") or "N/A"
    slug = str(zone.get("slug", "")).strip() or "N/A"
    name = str(zone.get("name", "")).strip() or "N/A"
    region_type = (
        str(zone.get("region_type") or zone.get("regionType") or "").strip() or "N/A"
    )
    store_ids = [str(x) for x in zone.get("store_ids", [])]

    mapping_regions = set()
    mapping_countries = set()
    mapping = zone.get("mapping") or zone.get("mappings") or []
    if isinstance(mapping, dict):
        mapping = [mapping]
    for mapping_item in mapping:
        country = str(mapping_item.get("country", "")).strip()
        if country:
            mapping_countries.add(country)
        regions = mapping_item.get("regions", [])
        if not regions:
            regions = mapping_item.get("region_ids", [])
        if isinstance(regions, str):
            regions = parse_list(regions)
        mapping_regions.update(str(x) for x in regions)

    return {
        "name": name,
        "slug": slug,
        "zone_id": str(zone_id),
        "region_type": region_type,
        "stores_count": len(store_ids),
        "regions_count": len(mapping_regions),
        "countries_preview": preview(sorted(mapping_countries), 3),
        "stores_preview": preview(store_ids, 6),
    }


def zones_to_export_rows(zones):
    rows = []
    for index, zone in enumerate(zones, start=1):
        zone_id = zone.get("zone_id") or zone.get("id") or ""
        slug = str(zone.get("slug", "")).strip()
        name = str(zone.get("name", "")).strip()
        region_type = str(zone.get("region_type") or zone.get("regionType") or "").strip()
        company_id = zone.get("company_id", "")
        is_active = zone.get("is_active", "")
        store_ids = [str(x) for x in (zone.get("store_ids") or [])]

        mapping = zone.get("mapping") or zone.get("mappings") or []
        if isinstance(mapping, dict):
            mapping = [mapping]

        countries = set()
        regions = set()
        for mapping_item in mapping:
            country = str(mapping_item.get("country", "")).strip()
            if country:
                countries.add(country)
            region_ids = mapping_item.get("regions", [])
            if not region_ids:
                region_ids = mapping_item.get("region_ids", [])
            if isinstance(region_ids, str):
                region_ids = parse_list(region_ids)
            regions.update(str(x) for x in region_ids)

        channels_raw = zone.get("channels") or []
        channel_ids = []
        if isinstance(channels_raw, list):
            for channel in channels_raw:
                if isinstance(channel, dict):
                    channel_id = channel.get("channel_id")
                    if channel_id is not None:
                        channel_ids.append(str(channel_id))
                else:
                    channel_ids.append(str(channel))

        product = zone.get("product") or {}
        product_type = ""
        product_tags = []
        if isinstance(product, dict):
            product_type = str(product.get("type", "")).strip()
            product_tags_raw = product.get("tags") or []
            if isinstance(product_tags_raw, list):
                product_tags = [str(x) for x in product_tags_raw]
            elif product_tags_raw:
                product_tags = [str(product_tags_raw)]

        rows.append(
            {
                "no": index,
                "name": name,
                "slug": slug,
                "zone_id": str(zone_id),
                "region_type": region_type,
                "is_active": is_active,
                "company_id": company_id,
                "store_count": len(store_ids),
                "store_ids": ",".join(store_ids),
                "region_count": len(regions),
                "regions": ",".join(sorted(regions)),
                "country_count": len(countries),
                "countries": ",".join(sorted(countries)),
                "channels": ",".join(channel_ids),
                "product_type": product_type,
                "product_tags": ",".join(product_tags),
            }
        )
    return rows


def format_zone_table_lines(zones, max_rows=None):
    if not zones:
        return ["No zones returned by API."]

    lines = ["Zone details (structured):"]
    header = (
        f"{'No':>3} | {'Name':<26} | {'Slug':<26} | {'Zone ID':<24} | "
        f"{'Type':<11} | {'Stores':>6} | {'Regions':>7} | {'Countries'}"
    )
    lines.append(header)
    lines.append("-" * len(header))

    rows = zones
    truncated = 0
    if max_rows is not None and max_rows >= 0 and len(zones) > max_rows:
        rows = zones[:max_rows]
        truncated = len(zones) - max_rows

    for index, zone in enumerate(rows, start=1):
        view = zone_to_table_row(zone)
        lines.append(
            f"{index:>3} | {shorten(view['name'], 26):<26} | "
            f"{shorten(view['slug'], 26):<26} | {shorten(view['zone_id'], 24):<24} | "
            f"{shorten(view['region_type'], 11):<11} | "
            f"{view['stores_count']:>6} | {view['regions_count']:>7} | "
            f"{shorten(view['countries_preview'], 30)}"
        )
        lines.append(f"    stores: {shorten(view['stores_preview'], 90)}")

    if truncated > 0:
        lines.append(f"... {truncated} more zone(s) not shown.")
    return lines


def fetch_existing_zones(
    token,
    include_details,
    logger,
    api_base,
    verify=True,
    progress_cb=None,
):
    logger("Fetching existing zones from API...")
    zones = get_zones(token, api_base, verify=verify)
    missing_mapping_in_list = zones and all(
        not zone_has_mapping_data(zone) for zone in zones
    )
    if include_details and zones:
        logger("Fetching per-zone details (regions/countries)...")
        zones = enrich_zones_with_details(
            token,
            zones,
            api_base,
            verify=verify,
            logger=logger,
            progress_cb=progress_cb,
        )
    elif missing_mapping_in_list:
        logger(
            "List API does not include mapping/region fields. "
            "Enable include-details to load them."
        )
    logger(f"Fetched {len(zones)} zone(s).")
    if progress_cb and (not include_details or not zones):
        progress_cb(1, 1)
    return zones


def parse_environment_input(value):
    text = str(value or "").strip().lower()
    index_map = {"1": "sit", "2": "uat", "3": "prod"}
    if text in index_map:
        return index_map[text]
    return normalize_env_name(text)


def prompt_environment_cli(default_env):
    default_label = ENVIRONMENTS[default_env]["label"]
    print("Select environment:")
    print("  1) SIT")
    print("  2) UAT")
    print("  3) PROD")
    answer = input(f"Enter choice [1/2/3 or sit/uat/prod] (default {default_label}): ")
    selected = parse_environment_input(answer)
    if not selected:
        return default_env
    return selected


def resolve_environment(args, config, ask_if_missing=True):
    from_arg = parse_environment_input(getattr(args, "env", ""))
    if from_arg:
        return from_arg

    from_env_var = parse_environment_input(os.getenv("DZ_ENV", ""))
    from_config = parse_environment_input(config.get("environment", ""))
    default_env = from_env_var or from_config or DEFAULT_ENV

    if ask_if_missing and sys.stdin.isatty():
        return prompt_environment_cli(default_env)
    return default_env


def resolve_cookie(args, config):
    cookie = ""
    if getattr(args, "cookie", None):
        cookie = args.cookie
    elif getattr(args, "cookie_file", None):
        cookie = Path(args.cookie_file).read_text(encoding="utf-8")
    elif os.getenv("DZ_COOKIE"):
        cookie = os.getenv("DZ_COOKIE", "")
    elif config.get("cookie"):
        cookie = config["cookie"]

    normalized = normalize_cookie_header(cookie)
    if not normalized:
        raise ValueError(
            "Cookie is required. Pass --cookie, --cookie-file, set DZ_COOKIE, "
            "or save it once in GUI mode."
        )
    return normalized


class CliLogger:
    def __init__(self, use_timestamps=True):
        self.use_timestamps = use_timestamps

    def __call__(self, message):
        if self.use_timestamps:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] {message}")
        else:
            print(message)


def run_cli_command(args):
    ensure_dependencies()
    config = load_preferences()
    logger = CliLogger(use_timestamps=True)
    include_details = args.include_details
    env_key = resolve_environment(args, config, ask_if_missing=True)
    env_config = get_env_config(env_key)

    cookie = resolve_cookie(args, config)
    tls = resolve_tls_settings(args, config)
    logger(f"Environment selected: {env_config['label']}")
    logger(f"API base: {env_config['api_base']}")
    logger(f"Token URL: {env_config['token_url']}")
    logger(f"TLS mode: {tls['mode']}")
    logger("Requesting bearer token...")
    token = get_bearer_token(
        cookie,
        env_config["token_url"],
        verify=tls["verify"],
        platform_origin=env_config.get("platform_origin", ""),
        env_label=env_config["label"],
    )
    logger("Token fetched successfully.")

    config["environment"] = env_key
    config["ca_bundle"] = tls["ca_bundle"]
    config["insecure_skip_tls_verify"] = tls["insecure"]
    save_preferences(config)

    if args.command == "fetch":
        zones = fetch_existing_zones(
            token,
            include_details,
            logger,
            env_config["api_base"],
            verify=tls["verify"],
        )
        for line in format_zone_table_lines(zones, max_rows=args.max_rows):
            print(line)
        return 0

    if args.command == "create":
        df = read_zone_file(args.file)
        logger(f"Loaded {len(df)} row(s) from file.")
        existing_zones = fetch_existing_zones(
            token,
            include_details,
            logger,
            env_config["api_base"],
            verify=tls["verify"],
        )
        create_zones_from_file(
            df,
            token,
            existing_zones,
            logger,
            env_config["api_base"],
            verify=tls["verify"],
        )
        return 0

    if args.command == "plan":
        df = read_zone_file(args.file)
        logger(f"Loaded {len(df)} row(s) from file.")
        existing_zones = fetch_existing_zones(
            token,
            include_details,
            logger,
            env_config["api_base"],
            verify=tls["verify"],
        )
        updates, _stats = plan_zone_updates(df, existing_zones, logger)
        if not updates:
            logger("No updates needed.")
        return 0

    if args.command == "update":
        df = read_zone_file(args.file)
        logger(f"Loaded {len(df)} row(s) from file.")
        existing_zones = fetch_existing_zones(
            token,
            include_details,
            logger,
            env_config["api_base"],
            verify=tls["verify"],
        )
        updates, stats = plan_zone_updates(df, existing_zones, logger)
        if not updates:
            logger("No updates needed.")
            return 0

        if not args.yes:
            answer = input(
                f"{stats['planned_updates']} update(s) planned. Proceed? [y/N]: "
            ).strip()
            if answer.lower() not in {"y", "yes"}:
                logger("Update cancelled.")
                return 0

        apply_zone_updates(
            updates,
            token,
            logger,
            env_config["api_base"],
            verify=tls["verify"],
        )
        return 0

    raise ValueError(f"Unknown command: {args.command}")


if TK_AVAILABLE:

    class DeliveryZoneApp:
        def __init__(self, root):
            self.root = root
            self.root.title("Delivery Zone Manager")
            self.root.geometry("1220x860")
            self.root.minsize(1020, 720)

            self.cookie_value = tk.StringVar()
            self.file_path_value = tk.StringVar()
            self.include_details_var = tk.BooleanVar(value=True)
            self.remember_cookie_var = tk.BooleanVar(value=False)
            self.auto_cookie_from_browser_var = tk.BooleanVar(value=True)
            self.environment_value = tk.StringVar(value=DEFAULT_ENV)
            self.ca_bundle_value = tk.StringVar()
            self.insecure_tls_var = tk.BooleanVar(value=False)
            self.status_value = tk.StringVar(value="Ready")
            self.last_action_value = tk.StringVar(value="None")
            self.zone_count_value = tk.StringVar(value="0")
            self.plan_count_value = tk.StringVar(value="0")
            self.tls_mode_value = tk.StringVar(value="Secure")
            self.preview_info_value = tk.StringVar(value="No preview loaded.")
            self.progress_percent_value = tk.StringVar(value="0%")
            self.preview_rows_value = tk.IntVar(value=12)
            self.preview_autofit_var = tk.BooleanVar(value=True)
            self.last_cookie = None
            self.last_env = None
            self.last_tls_signature = None
            self.token = None
            self.existing_zones = []
            self.current_planned_updates = []
            self._busy_animation_job = None
            self._busy_frame = 0
            self.logo_image = None
            self.otp_email_var = tk.StringVar()
            self.otp_code_var = tk.StringVar()
            self._otp_request_id = ""
            self._otp_resend_job = None
            self._otp_resend_seconds = 0

            self._init_theme()
            self._build_ui()
            self.load_preferences()
            self.prompt_environment_on_startup()

        def _init_theme(self):
            style = ttk.Style(self.root)
            try:
                style.theme_use("clam")
            except Exception:
                pass
            style.configure("Main.TFrame", background="#f3f6fb")
            style.configure(
                "Card.TLabelframe",
                background="#ffffff",
                borderwidth=1,
                relief="solid",
                padding=10,
            )
            style.configure("Card.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
            style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"), background="#f3f6fb")
            style.configure("SubTitle.TLabel", font=("Segoe UI", 10), background="#f3f6fb", foreground="#4d5a70")
            style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"), padding=7)
            style.configure("Secondary.TButton", font=("Segoe UI", 10), padding=7)
            style.configure(
                "ActionBlue.Horizontal.TProgressbar",
                troughcolor="#e2e8f0",
                background="#2563eb",
                bordercolor="#e2e8f0",
                lightcolor="#2563eb",
                darkcolor="#2563eb",
            )
            style.configure(
                "ActionGreen.Horizontal.TProgressbar",
                troughcolor="#e2e8f0",
                background="#16a34a",
                bordercolor="#e2e8f0",
                lightcolor="#16a34a",
                darkcolor="#16a34a",
            )
            style.configure(
                "ActionRed.Horizontal.TProgressbar",
                troughcolor="#e2e8f0",
                background="#dc2626",
                bordercolor="#e2e8f0",
                lightcolor="#dc2626",
                darkcolor="#dc2626",
            )
            style.configure(
                "Preview.Treeview",
                font=("Segoe UI", 10),
                rowheight=24,
                foreground="#0f172a",
                background="#ffffff",
                fieldbackground="#ffffff",
            )
            style.configure(
                "Preview.Treeview.Heading",
                font=("Segoe UI", 10, "bold"),
                foreground="#0f172a",
                background="#e2e8f0",
            )
            style.map(
                "Preview.Treeview",
                background=[("selected", "#dbeafe")],
                foreground=[("selected", "#0f172a")],
            )

        def _build_ui(self):
            container = ttk.Frame(self.root, padding=14, style="Main.TFrame")
            container.pack(fill="both", expand=True)

            header_wrap = ttk.Frame(container, style="Main.TFrame")
            header_wrap.pack(fill="x", pady=(0, 8))
            title_left = ttk.Frame(header_wrap, style="Main.TFrame")
            title_left.pack(side="left", fill="x", expand=True)
            ttk.Label(title_left, text="Delivery Zone Command Center", style="Title.TLabel").pack(
                anchor="w"
            )
            ttk.Label(
                title_left,
                text="Fynd Delivery Zone extension for v1.9.5",
                style="SubTitle.TLabel",
            ).pack(anchor="w", pady=(2, 0))

            logo_badge = tk.Frame(
                header_wrap,
                bg="#0f172a",
                bd=0,
                relief="flat",
                padx=10,
                pady=6,
            )
            logo_badge.pack(side="right", padx=(8, 0))
            self._attach_logo_widget(logo_badge)

            stats_wrap = tk.Frame(container, bg="#f3f6fb")
            stats_wrap.pack(fill="x", pady=(0, 8))

            def make_card(parent, title, variable):
                card = tk.Frame(parent, bg="#ffffff", bd=1, relief="solid")
                card.pack(side="left", fill="x", expand=True, padx=(0, 8))
                tk.Label(
                    card,
                    text=title,
                    bg="#ffffff",
                    fg="#64748b",
                    font=("Segoe UI", 9, "bold"),
                ).pack(anchor="w", padx=10, pady=(8, 2))
                tk.Label(
                    card,
                    textvariable=variable,
                    bg="#ffffff",
                    fg="#0f172a",
                    font=("Segoe UI", 12, "bold"),
                ).pack(anchor="w", padx=10, pady=(0, 8))
                return card

            make_card(stats_wrap, "Status", self.status_value)
            make_card(stats_wrap, "Last Action", self.last_action_value)
            make_card(stats_wrap, "Zones Loaded", self.zone_count_value)
            make_card(stats_wrap, "Planned Updates", self.plan_count_value)
            make_card(stats_wrap, "TLS", self.tls_mode_value)

            for child in stats_wrap.winfo_children()[:-1]:
                child.pack_configure(padx=(0, 8))
            stats_wrap.winfo_children()[-1].pack_configure(padx=(0, 0))

            status_row = ttk.Frame(container, style="Main.TFrame")
            status_row.pack(fill="x", pady=(0, 8))
            ttk.Label(status_row, text="Activity", font=("Segoe UI", 10, "bold")).pack(
                side="left"
            )
            self.progress = ttk.Progressbar(
                status_row,
                mode="determinate",
                maximum=100,
                length=330,
                style="ActionBlue.Horizontal.TProgressbar",
            )
            self.progress.pack(side="left", padx=(12, 10))
            ttk.Label(
                status_row,
                textvariable=self.progress_percent_value,
                font=("Segoe UI", 10, "bold"),
            ).pack(side="left", padx=(0, 10))
            self.activity_hint_label = ttk.Label(
                status_row,
                text="Idle",
                foreground="#475569",
                font=("Segoe UI", 10),
            )
            self.activity_hint_label.pack(side="left")

            config_wrap = ttk.Frame(container, style="Main.TFrame")
            config_wrap.pack(fill="x", pady=(0, 8))

            top_config_row = ttk.Frame(config_wrap, style="Main.TFrame")
            top_config_row.pack(fill="x", pady=(0, 8))
            env_frame = ttk.LabelFrame(
                top_config_row, text="Environment", style="Card.TLabelframe"
            )
            env_frame.pack(side="left", fill="both", expand=True, padx=(0, 8))
            ttk.Label(
                env_frame,
                text="Select target environment (applies to token + zones APIs)",
            ).grid(row=0, column=0, sticky="w")
            radio_wrap = ttk.Frame(env_frame)
            radio_wrap.grid(row=1, column=0, sticky="w", pady=(4, 0))
            ttk.Radiobutton(
                radio_wrap,
                text="SIT",
                variable=self.environment_value,
                value="sit",
                command=self.on_environment_changed,
            ).pack(side="left", padx=(0, 14))
            ttk.Radiobutton(
                radio_wrap,
                text="UAT",
                variable=self.environment_value,
                value="uat",
                command=self.on_environment_changed,
            ).pack(side="left", padx=(0, 14))
            ttk.Radiobutton(
                radio_wrap,
                text="PROD",
                variable=self.environment_value,
                value="prod",
                command=self.on_environment_changed,
            ).pack(side="left")

            options_frame = ttk.LabelFrame(top_config_row, text="Options", style="Card.TLabelframe")
            options_frame.pack(side="left", fill="both", expand=True)
            ttk.Checkbutton(
                options_frame,
                text="Include full zone details (regions/countries, slower)",
                variable=self.include_details_var,
            ).grid(row=0, column=0, sticky="w")
            ttk.Label(
                options_frame,
                text="TLS (default: secure verification)",
            ).grid(row=1, column=0, sticky="w", pady=(8, 0))
            ttk.Checkbutton(
                options_frame,
                text="Skip TLS verification (advanced)",
                variable=self.insecure_tls_var,
                command=self.on_tls_options_changed,
            ).grid(row=2, column=0, sticky="w", pady=(4, 0))

            mid_config_row = ttk.Frame(config_wrap, style="Main.TFrame")
            mid_config_row.pack(fill="x", pady=(0, 8))
            auth_frame = ttk.LabelFrame(
                mid_config_row, text="Authentication", style="Card.TLabelframe"
            )
            auth_frame.pack(side="left", fill="both", expand=True, padx=(0, 8))
            ttk.Label(auth_frame, text="Cookie Header").grid(
                row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 6)
            )
            cookie_entry = ttk.Entry(auth_frame, textvariable=self.cookie_value)
            cookie_entry.grid(row=0, column=1, sticky="ew", pady=(0, 6))
            ttk.Button(
                auth_frame,
                text="Normalize",
                style="Secondary.TButton",
                command=self.normalize_cookie_entry,
            ).grid(row=0, column=2, padx=(8, 0), pady=(0, 6))
            ttk.Checkbutton(
                auth_frame,
                text="Remember cookie locally",
                variable=self.remember_cookie_var,
            ).grid(row=1, column=1, sticky="w", pady=(0, 2))
            self.fetch_cookie_button = ttk.Button(
                auth_frame,
                text="Fetch Browser Cookie",
                style="Secondary.TButton",
                command=lambda: self.try_auto_load_cookie_for_selected_env(force=True),
            )
            self.fetch_cookie_button.grid(row=1, column=2, padx=(8, 0), pady=(0, 2))
            ttk.Checkbutton(
                auth_frame,
                text="Auto-load browser cookie when environment changes",
                variable=self.auto_cookie_from_browser_var,
            ).grid(row=2, column=1, columnspan=2, sticky="w")

            ttk.Separator(auth_frame, orient="horizontal").grid(
                row=3, column=0, columnspan=3, sticky="ew", pady=(8, 4)
            )
            ttk.Label(
                auth_frame,
                text="Or login via Email OTP",
                font=("Segoe UI", 9, "bold"),
                foreground="#2563eb",
            ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(0, 4))

            ttk.Label(auth_frame, text="Email").grid(
                row=5, column=0, sticky="w", padx=(0, 8), pady=(0, 4)
            )
            ttk.Entry(auth_frame, textvariable=self.otp_email_var, width=32).grid(
                row=5, column=1, sticky="ew", pady=(0, 4)
            )
            self.send_otp_button = ttk.Button(
                auth_frame,
                text="Send OTP",
                style="Secondary.TButton",
                command=self._gui_send_otp,
            )
            self.send_otp_button.grid(row=5, column=2, padx=(8, 0), pady=(0, 4))

            ttk.Label(auth_frame, text="OTP Code").grid(
                row=6, column=0, sticky="w", padx=(0, 8), pady=(0, 4)
            )
            self._otp_code_entry = ttk.Entry(
                auth_frame, textvariable=self.otp_code_var, width=16, state="disabled"
            )
            self._otp_code_entry.grid(row=6, column=1, sticky="w", pady=(0, 4))
            self.verify_otp_button = ttk.Button(
                auth_frame,
                text="Verify & Login",
                style="Secondary.TButton",
                command=self._gui_verify_otp,
                state="disabled",
            )
            self.verify_otp_button.grid(row=6, column=2, padx=(8, 0), pady=(0, 4))

            self._otp_status_var = tk.StringVar(value="")
            self._otp_status_label = ttk.Label(
                auth_frame,
                textvariable=self._otp_status_var,
                foreground="#475569",
                font=("Segoe UI", 9),
            )
            self._otp_status_label.grid(
                row=7, column=1, columnspan=2, sticky="w", pady=(0, 2)
            )

            auth_frame.grid_columnconfigure(1, weight=1)

            file_frame = ttk.LabelFrame(mid_config_row, text="Input File", style="Card.TLabelframe")
            file_frame.pack(side="left", fill="both", expand=True)
            ttk.Label(file_frame, text="CSV/XLSX Path").grid(
                row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 4)
            )
            file_entry = ttk.Entry(file_frame, textvariable=self.file_path_value)
            file_entry.grid(row=0, column=1, sticky="ew", pady=(0, 4))
            ttk.Button(
                file_frame,
                text="Browse",
                style="Secondary.TButton",
                command=self.select_file,
            ).grid(row=0, column=2, padx=(8, 0), pady=(0, 4))
            file_frame.grid_columnconfigure(1, weight=1)

            action_frame = ttk.LabelFrame(config_wrap, text="Actions", style="Card.TLabelframe")
            action_frame.pack(fill="x", pady=(0, 8))
            self.fetch_button = ttk.Button(
                action_frame,
                text="Fetch Current Zones",
                style="Primary.TButton",
                command=self.fetch_current_zones,
            )
            self.fetch_button.grid(row=0, column=0, padx=(0, 8), pady=4, sticky="ew")
            self.create_button = ttk.Button(
                action_frame,
                text="Create From File",
                style="Primary.TButton",
                command=self.create_from_file,
            )
            self.create_button.grid(row=0, column=1, padx=(0, 8), pady=4, sticky="ew")
            self.plan_button = ttk.Button(
                action_frame,
                text="Plan Updates",
                style="Primary.TButton",
                command=self.plan_updates_from_file,
            )
            self.plan_button.grid(row=0, column=2, padx=(0, 8), pady=4, sticky="ew")
            self.update_button = ttk.Button(
                action_frame,
                text="Apply Updates",
                style="Primary.TButton",
                command=self.update_from_file,
            )
            self.update_button.grid(row=0, column=3, pady=4, sticky="ew")
            self.clear_log_button = ttk.Button(
                action_frame,
                text="Clear Logs",
                style="Secondary.TButton",
                command=self.clear_logs,
            )
            self.clear_log_button.grid(row=1, column=0, pady=(6, 0), sticky="ew")
            self.copy_log_button = ttk.Button(
                action_frame,
                text="Copy Logs",
                style="Secondary.TButton",
                command=self.copy_logs,
            )
            self.copy_log_button.grid(row=1, column=1, pady=(6, 0), sticky="ew")
            self.save_pref_button = ttk.Button(
                action_frame,
                text="Save Preferences",
                style="Secondary.TButton",
                command=self.save_preferences,
            )
            self.save_pref_button.grid(row=1, column=2, pady=(6, 0), sticky="ew")
            for i in range(4):
                action_frame.grid_columnconfigure(i, weight=1)

            results_pane = ttk.Panedwindow(container, orient=tk.VERTICAL)
            results_pane.pack(fill="both", expand=True, pady=(0, 0))
            preview_frame = ttk.LabelFrame(results_pane, text="Zone Preview", style="Card.TLabelframe")
            preview_head = ttk.Frame(preview_frame)
            preview_head.pack(fill="x", pady=(0, 6))
            ttk.Label(
                preview_head,
                textvariable=self.preview_info_value,
                foreground="#475569",
            ).pack(side="left")
            ttk.Label(preview_head, text="Rows:").pack(side="left", padx=(12, 4))
            preview_rows_spin = ttk.Spinbox(
                preview_head,
                from_=6,
                to=40,
                width=4,
                textvariable=self.preview_rows_value,
                command=self.on_preview_rows_changed,
            )
            preview_rows_spin.pack(side="left")
            preview_rows_spin.bind("<KeyRelease>", lambda _e: self.on_preview_rows_changed())
            ttk.Checkbutton(
                preview_head,
                text="Auto-fit columns",
                variable=self.preview_autofit_var,
                command=self.on_preview_resize,
            ).pack(side="left", padx=(10, 0))
            self.download_button = ttk.Button(
                preview_head,
                text="Download",
                style="Secondary.TButton",
                command=self.download_fetched_zones,
            )
            self.download_button.pack(side="left", padx=(12, 0))
            cols = (
                "no",
                "name",
                "slug",
                "zone_id",
                "type",
                "stores",
                "regions",
                "countries",
            )
            tree_wrap = ttk.Frame(preview_frame)
            tree_wrap.pack(fill="both", expand=True)
            self.preview_tree = ttk.Treeview(
                tree_wrap,
                columns=cols,
                show="headings",
                height=self.preview_rows_value.get(),
                style="Preview.Treeview",
            )
            headers = {
                "no": "#",
                "name": "Name",
                "slug": "Slug",
                "zone_id": "Zone ID",
                "type": "Type",
                "stores": "Stores",
                "regions": "Regions",
                "countries": "Countries",
            }
            widths = {
                "no": 45,
                "name": 190,
                "slug": 190,
                "zone_id": 180,
                "type": 90,
                "stores": 80,
                "regions": 80,
                "countries": 150,
            }
            self.preview_default_widths = widths
            for col in cols:
                self.preview_tree.heading(col, text=headers[col])
                self.preview_tree.column(
                    col,
                    width=widths[col],
                    minwidth=50,
                    stretch=True,
                    anchor="w",
                )
            self.preview_tree.tag_configure("zone_row", foreground="#0f172a", background="#ffffff")
            y_scroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.preview_tree.yview)
            x_scroll = ttk.Scrollbar(tree_wrap, orient="horizontal", command=self.preview_tree.xview)
            self.preview_tree.configure(
                yscrollcommand=y_scroll.set,
                xscrollcommand=x_scroll.set,
            )
            self.preview_tree.grid(row=0, column=0, sticky="nsew")
            y_scroll.grid(row=0, column=1, sticky="ns")
            x_scroll.grid(row=1, column=0, sticky="ew")
            tree_wrap.grid_columnconfigure(0, weight=1)
            tree_wrap.grid_rowconfigure(0, weight=1)
            self.preview_tree.bind("<Configure>", self.on_preview_resize)

            log_frame = ttk.LabelFrame(results_pane, text="Logs", style="Card.TLabelframe")
            self.log_text = scrolledtext.ScrolledText(
                log_frame,
                wrap=tk.WORD,
                font=("Consolas", 10),
                state="disabled",
                bg="#0f172a",
                fg="#dbeafe",
                insertbackground="#dbeafe",
                relief="flat",
                borderwidth=0,
            )
            self.log_text.pack(fill="both", expand=True)
            results_pane.add(preview_frame, weight=3)
            results_pane.add(log_frame, weight=2)
            try:
                results_pane.paneconfigure(preview_frame, minsize=230)
                results_pane.paneconfigure(log_frame, minsize=170)
            except Exception:
                pass
            self.root.after(160, lambda: self._set_default_results_split(results_pane))
            self.render_zone_preview([])
            self.on_preview_rows_changed()
            self.on_preview_resize()
            self.set_progress(0, "Idle")

        def _animate_busy_hint(self, label):
            frames = ["", ".", "..", "..."]
            self._busy_frame = (self._busy_frame + 1) % len(frames)
            self.activity_hint_label.config(text=f"{label}{frames[self._busy_frame]}")
            self._busy_animation_job = self.root.after(
                260, self._animate_busy_hint, label
            )

        def _attach_logo_widget(self, parent):
            self.logo_image = self._load_logo_image()
            if self.logo_image:
                logo = tk.Label(parent, image=self.logo_image, bg="#0f172a", bd=0)
                logo.pack()
                return

            fallback = tk.Label(
                parent,
                text="FYND",
                fg="#ffffff",
                bg="#0f172a",
                font=("Segoe UI", 11, "bold"),
                bd=0,
            )
            fallback.pack()

        def _load_logo_image(self):
            if requests is None:
                return None

            candidate_urls = [
                LOGO_URL,
                LOGO_URL.replace(".svg", ".png"),
                f"{LOGO_URL}?format=png",
            ]
            for url in candidate_urls:
                try:
                    response = requests.get(url, timeout=2)
                except Exception:
                    continue
                if response.status_code != 200:
                    continue
                content = response.content or b""
                if not (
                    content.startswith(b"\x89PNG")
                    or content.startswith(b"GIF8")
                    or content.startswith(b"RIFF")
                ):
                    continue
                try:
                    encoded = base64.b64encode(content).decode("ascii")
                    return tk.PhotoImage(data=encoded)
                except Exception:
                    continue
            return None

        def set_busy(self, busy, label="Working"):
            state = tk.DISABLED if busy else tk.NORMAL
            for btn in [
                self.fetch_button,
                self.create_button,
                self.plan_button,
                self.update_button,
                self.clear_log_button,
                self.copy_log_button,
                self.save_pref_button,
                self.download_button,
                self.fetch_cookie_button,
            ]:
                btn.config(state=state)
            # OTP buttons only disabled while zone ops are running; don't re-enable
            # verify button if no OTP has been sent yet
            if busy:
                self.send_otp_button.config(state=tk.DISABLED)
                self.verify_otp_button.config(state=tk.DISABLED)
            else:
                # restore send_otp unless cooldown timer is still running
                if self._otp_resend_seconds <= 0:
                    self.send_otp_button.config(state=tk.NORMAL)
                # restore verify only if an OTP has been sent
                if self._otp_request_id:
                    self.verify_otp_button.config(state=tk.NORMAL)

            if busy:
                self.status_value.set("Running")
                self.activity_hint_label.config(text=label)
                self.progress.configure(style="ActionBlue.Horizontal.TProgressbar")
                self.set_progress(6, label)
                if self._busy_animation_job is not None:
                    self.root.after_cancel(self._busy_animation_job)
                self._busy_frame = 0
                self._animate_busy_hint(label)
                return

            if self._busy_animation_job is not None:
                self.root.after_cancel(self._busy_animation_job)
                self._busy_animation_job = None
            self.activity_hint_label.config(text="Idle")

        def set_progress(self, percent, hint=None):
            safe_percent = max(0, min(100, int(percent)))
            self.progress["value"] = safe_percent
            self.progress_percent_value.set(f"{safe_percent}%")
            if hint:
                self.activity_hint_label.config(text=hint)
            self.root.update_idletasks()

        def _set_default_results_split(self, pane):
            try:
                height = pane.winfo_height()
                if height > 0:
                    pane.sashpos(0, int(height * 0.68))
            except Exception:
                pass

        def on_preview_rows_changed(self, *_args):
            if not hasattr(self, "preview_tree"):
                return

            min_rows, max_rows = 6, 40
            current = self.preview_rows_value.get()
            try:
                rows = int(current)
            except (TypeError, ValueError):
                rows = min_rows
            rows = max(min_rows, min(max_rows, rows))
            self.preview_rows_value.set(rows)
            self.preview_tree.configure(height=rows)

        def on_preview_resize(self, _event=None):
            if not hasattr(self, "preview_tree"):
                return

            columns = list(self.preview_tree["columns"])
            if not columns:
                return

            default_widths = {
                col: int(width)
                for col, width in getattr(self, "preview_default_widths", {}).items()
                if isinstance(width, (int, float))
            }
            if not default_widths:
                default_widths = {col: 90 for col in columns}

            if not bool(self.preview_autofit_var.get()):
                for col in columns:
                    width = max(50, int(default_widths.get(col, 90)))
                    self.preview_tree.column(col, width=width, minwidth=50, stretch=True)
                return

            table_width = self.preview_tree.winfo_width()
            if table_width <= 1:
                return

            pad = 26
            total_default = sum(default_widths.get(col, 90) for col in columns)
            if total_default <= 0:
                total_default = len(columns) * 90

            usable = max(300, table_width - pad)
            for idx, col in enumerate(columns):
                ratio = default_widths.get(col, 90) / total_default
                width = max(50, int(usable * ratio))
                # absorb rounding remainder in the last column
                if idx == len(columns) - 1:
                    used = sum(
                        max(50, int(usable * (default_widths.get(c, 90) / total_default)))
                        for c in columns[:-1]
                    )
                    width = max(50, usable - used)
                self.preview_tree.column(col, width=width, minwidth=50, stretch=True)

        def render_zone_preview(self, zones):
            for item in self.preview_tree.get_children():
                self.preview_tree.delete(item)

            if not zones:
                self.preview_info_value.set("No zones loaded yet.")
                self.zone_count_value.set("0")
                self.on_preview_resize()
                return

            preview_rows = zones[:220]
            for index, zone in enumerate(preview_rows, start=1):
                view = zone_to_table_row(zone)
                self.preview_tree.insert(
                    "",
                    "end",
                    values=(
                        index,
                        shorten(view["name"], 34),
                        shorten(view["slug"], 34),
                        shorten(view["zone_id"], 26),
                        view["region_type"],
                        view["stores_count"],
                        view["regions_count"],
                        shorten(view["countries_preview"], 22),
                    ),
                    tags=("zone_row",),
                )

            hidden = len(zones) - len(preview_rows)
            if hidden > 0:
                self.preview_info_value.set(
                    f"Showing {len(preview_rows)} of {len(zones)} zones."
                )
            else:
                self.preview_info_value.set(f"Showing all {len(zones)} zones.")
            self.zone_count_value.set(str(len(zones)))
            self.on_preview_resize()

        def make_progress_callback(self, start, end, hint):
            start_value = float(start)
            end_value = float(end)
            span = max(0.0, end_value - start_value)

            def _callback(done, total):
                try:
                    done_value = float(done)
                    total_value = float(total)
                except Exception:
                    self.set_progress(end_value, hint)
                    return
                if total_value <= 0:
                    self.set_progress(end_value, hint)
                    return
                ratio = max(0.0, min(1.0, done_value / total_value))
                self.set_progress(start_value + (span * ratio), hint)

            return _callback

        def timestamp(self):
            return datetime.now().strftime("%H:%M:%S")

        def log(self, message):
            self.log_text.config(state="normal")
            self.log_text.insert(tk.END, f"[{self.timestamp()}] {message}\n")
            self.log_text.see(tk.END)
            self.log_text.config(state="disabled")
            self.root.update_idletasks()

        def clear_logs(self):
            self.log_text.config(state="normal")
            self.log_text.delete("1.0", tk.END)
            self.log_text.config(state="disabled")
            self.status_value.set("Ready")
            self.last_action_value.set("Clear Logs")
            self.progress.configure(style="ActionBlue.Horizontal.TProgressbar")
            self.set_progress(0, "Idle")
            self.log("Logs cleared.")

        def copy_logs(self):
            text = self.log_text.get("1.0", tk.END).strip()
            if not text:
                self.log("No logs to copy.")
                return
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.last_action_value.set("Copy Logs")
            self.log("Logs copied to clipboard.")

        def download_fetched_zones(self):
            if not self.existing_zones:
                messagebox.showinfo(
                    "No Data",
                    "No fetched zones available. Please run 'Fetch Current Zones' first.",
                    parent=self.root,
                )
                self.log("Download skipped: no fetched zones available.")
                return

            env_key, env_config = self.get_environment_config()
            default_name = (
                f"zones_{env_key}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            )
            path = filedialog.asksaveasfilename(
                title="Download Fetched Zones",
                initialfile=default_name,
                defaultextension=".csv",
                filetypes=[
                    ("CSV files", "*.csv"),
                    ("Excel files", "*.xlsx"),
                    ("JSON files", "*.json"),
                    ("All files", "*.*"),
                ],
                parent=self.root,
            )
            if not path:
                return

            export_path = Path(path)
            suffix = export_path.suffix.lower()
            if not suffix:
                export_path = export_path.with_suffix(".csv")
                suffix = ".csv"

            rows = zones_to_export_rows(self.existing_zones)
            try:
                if suffix == ".json":
                    export_path.write_text(
                        json.dumps(self.existing_zones, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                elif suffix == ".csv":
                    if rows:
                        with export_path.open("w", newline="", encoding="utf-8-sig") as fh:
                            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                            writer.writeheader()
                            writer.writerows(rows)
                    else:
                        export_path.write_text("", encoding="utf-8")
                elif suffix == ".xlsx":
                    if pd is None:
                        raise RuntimeError(
                            "Excel export requires pandas/openpyxl. "
                            "Install with: pip3 install pandas openpyxl"
                        )
                    df = pd.DataFrame(rows)
                    df.to_excel(export_path, index=False)
                else:
                    raise ValueError(
                        "Unsupported file extension. Use .csv, .xlsx, or .json"
                    )
            except Exception as exc:
                self.log(f"ERROR: Failed to download zones: {exc}")
                messagebox.showerror(
                    "Download Failed",
                    f"Could not export zones.\n{exc}",
                    parent=self.root,
                )
                return

            self.last_action_value.set("Download Zones")
            self.log(
                f"Downloaded {len(self.existing_zones)} zone(s) to {export_path} "
                f"({env_config['label']})."
            )
            messagebox.showinfo(
                "Download Complete",
                f"Exported {len(self.existing_zones)} zone(s)\n\n{export_path}",
                parent=self.root,
            )

        def run_action(self, action, label):
            self.last_action_value.set(label)
            self.set_busy(True, label=label)
            self.log(f"--- {label} ---")
            try:
                self.save_preferences()
                action()
                self.status_value.set("Completed")
                self.progress.configure(style="ActionGreen.Horizontal.TProgressbar")
                self.set_progress(100, "Completed")
                self.log(f"{label}: completed.")
            except Exception as exc:
                self.status_value.set("Error")
                self.progress.configure(style="ActionRed.Horizontal.TProgressbar")
                self.set_progress(100, "Failed")
                self.log(f"ERROR: {exc}")
                messagebox.showerror("Error", str(exc), parent=self.root)
            finally:
                self.set_busy(False)

        def select_file(self):
            path = filedialog.askopenfilename(
                title="Select delivery zone file",
                filetypes=[
                    ("Excel files", "*.xlsx *.xls"),
                    ("CSV files", "*.csv"),
                    ("All files", "*.*"),
                ],
                parent=self.root,
            )
            if path:
                self.file_path_value.set(path)
                self.log(f"Selected file: {path}")
                self.save_preferences()

        def normalize_cookie_entry(self):
            normalized = normalize_cookie_header(self.cookie_value.get())
            self.cookie_value.set(normalized)
            self.log("Cookie normalized.")

        def _gui_send_otp(self):
            email = self.otp_email_var.get().strip()
            if not email or "@" not in email:
                messagebox.showwarning(
                    "Invalid Email",
                    "Please enter a valid email address.",
                    parent=self.root,
                )
                return

            _env_key, env_config = self.get_environment_config()
            tls = self.get_tls_settings()
            self.send_otp_button.config(state="disabled")
            self._otp_status_var.set("Sending OTP...")
            self.root.update_idletasks()

            import threading

            def _do_send():
                try:
                    request_id, _data = send_otp(email, env_config, verify=tls["verify"])
                    self._otp_request_id = request_id
                    self.root.after(
                        0,
                        lambda: self._on_otp_sent(email),
                    )
                except Exception as exc:
                    self.root.after(
                        0,
                        lambda msg=str(exc): self._on_otp_send_failed(msg),
                    )

            threading.Thread(target=_do_send, daemon=True).start()

        def _on_otp_sent(self, email):
            self._otp_status_var.set(f"OTP sent to {email}. Enter code below.")
            self._otp_code_entry.config(state="normal")
            self.verify_otp_button.config(state="normal")
            self.log(f"OTP sent to {email}.")
            self._start_otp_resend_cooldown(30)

        def _on_otp_send_failed(self, msg):
            self._otp_status_var.set(f"Send failed: {msg}")
            self.send_otp_button.config(state="normal")
            self.log(f"OTP send error: {msg}")
            messagebox.showerror("OTP Send Failed", msg, parent=self.root)

        def _start_otp_resend_cooldown(self, seconds):
            if self._otp_resend_job:
                self.root.after_cancel(self._otp_resend_job)
            self._otp_resend_seconds = seconds
            self._tick_otp_resend()

        def _tick_otp_resend(self):
            if self._otp_resend_seconds > 0:
                self.send_otp_button.config(
                    text=f"Resend ({self._otp_resend_seconds}s)", state="disabled"
                )
                self._otp_resend_seconds -= 1
                self._otp_resend_job = self.root.after(1000, self._tick_otp_resend)
            else:
                self.send_otp_button.config(text="Send OTP", state="normal")
                self._otp_resend_job = None

        def _gui_verify_otp(self):
            email = self.otp_email_var.get().strip()
            otp = self.otp_code_var.get().strip()
            if not otp:
                messagebox.showwarning(
                    "Missing OTP", "Please enter the OTP code.", parent=self.root
                )
                return

            _env_key, env_config = self.get_environment_config()
            tls = self.get_tls_settings()
            self.verify_otp_button.config(state="disabled")
            self._otp_status_var.set("Verifying OTP...")
            self.root.update_idletasks()

            import threading

            def _do_verify():
                try:
                    cookie_header, _data = verify_otp(
                        email,
                        otp,
                        self._otp_request_id,
                        env_config,
                        verify=tls["verify"],
                    )
                    self.root.after(
                        0,
                        lambda: self._on_otp_verified(cookie_header),
                    )
                except Exception as exc:
                    self.root.after(
                        0,
                        lambda msg=str(exc): self._on_otp_verify_failed(msg),
                    )

            threading.Thread(target=_do_verify, daemon=True).start()

        def _on_otp_verified(self, cookie_header):
            if cookie_header:
                self.cookie_value.set(cookie_header)
                self._otp_status_var.set("Login successful. Cookie populated above.")
                self.log("OTP login successful. Cookie populated from session.")
                # Invalidate cached token so it gets refreshed with new cookie
                self.token = None
                self.last_cookie = None
                messagebox.showinfo(
                    "Login Successful",
                    "OTP verified. Session cookie has been loaded — you can now run zone operations.",
                    parent=self.root,
                )
            else:
                self._otp_status_var.set("Verified but no cookie returned. Try pasting cookie manually.")
                self.log("OTP verified but response contained no Set-Cookie. Check browser login state.")
            self.verify_otp_button.config(state="normal")
            self.otp_code_var.set("")

        def _on_otp_verify_failed(self, msg):
            self._otp_status_var.set(f"Verification failed: {msg}")
            self.verify_otp_button.config(state="normal")
            self.log(f"OTP verify error: {msg}")
            messagebox.showerror("OTP Verification Failed", msg, parent=self.root)

        def try_auto_load_cookie_for_selected_env(self, force=False):
            if not force and not bool(self.auto_cookie_from_browser_var.get()):
                return False

            _env_key, env_config = self.get_environment_config()
            if force:
                self.last_action_value.set("Browser Cookie Fetch")
            cookie_header, source = try_load_browser_cookie_header(env_config)
            if cookie_header:
                self.cookie_value.set(cookie_header)
                self.log(f"Cookie auto-loaded from browser: {source}")
                return True

            if force:
                self.log(f"Browser cookie fetch failed: {source}")
                messagebox.showwarning(
                    "Browser Cookie Not Found",
                    (
                        f"Could not auto-fetch cookie.\n\n{source}\n\n"
                        "You can continue by pasting Cookie header manually."
                    ),
                    parent=self.root,
                )
            else:
                self.log(f"Auto cookie fetch skipped: {source}")
            return False

        def select_ca_bundle(self):
            path = filedialog.askopenfilename(
                title="Select CA bundle file",
                filetypes=[
                    ("PEM files", "*.pem *.crt *.cer"),
                    ("All files", "*.*"),
                ],
                parent=self.root,
            )
            if path:
                self.ca_bundle_value.set(path)
                self.log(f"Selected CA bundle: {path}")
                self.on_tls_options_changed()

        def get_tls_settings(self):
            insecure = bool(self.insecure_tls_var.get())
            # Keep GUI simple but still support advanced CA bundles
            # via saved prefs or DZ_CA_BUNDLE env var.
            ca_bundle = str(self.ca_bundle_value.get() or "").strip()
            if not ca_bundle:
                ca_bundle = str(os.getenv("DZ_CA_BUNDLE", "")).strip()
            if not insecure and ca_bundle:
                ca_bundle = normalize_ca_bundle_path(ca_bundle)
            else:
                ca_bundle = ""
            verify = False if insecure else (ca_bundle if ca_bundle else True)
            mode = "Skip verify" if insecure else ("Secure + CA" if ca_bundle else "Secure")
            return {
                "verify": verify,
                "ca_bundle": ca_bundle,
                "insecure": insecure,
                "mode": mode,
            }

        def get_environment_key(self):
            key = normalize_env_name(self.environment_value.get()) or DEFAULT_ENV
            self.environment_value.set(key)
            return key

        def get_environment_config(self):
            key = self.get_environment_key()
            return key, get_env_config(key)

        def prompt_environment_on_startup(self):
            default_key = self.get_environment_key()
            answer = simpledialog.askstring(
                "Select Environment",
                (
                    "Choose target environment for this run:\n"
                    "1) SIT\n2) UAT\n3) PROD\n\n"
                    f"Default: {ENVIRONMENTS[default_key]['label']}\n"
                    "Enter 1/2/3 or sit/uat/prod."
                ),
                parent=self.root,
            )
            if answer is None or not str(answer).strip():
                selected = default_key
            else:
                selected = parse_environment_input(answer)
                if not selected:
                    messagebox.showerror(
                        "Invalid Environment",
                        "Please enter one of: 1, 2, 3, sit, uat, prod.",
                        parent=self.root,
                    )
                    self.prompt_environment_on_startup()
                    return

            self.environment_value.set(selected)
            _, env_config = self.get_environment_config()
            self.last_action_value.set(
                f"Environment selected - {env_config['label']}"
            )
            self.log(f"Selected environment: {env_config['label']}")
            self.log(f"API base: {env_config['api_base']}")
            self.try_auto_load_cookie_for_selected_env(force=False)
            self.save_preferences()

        def on_environment_changed(self):
            key, env_config = self.get_environment_config()
            self.last_action_value.set(
                f"Environment selected - {env_config['label']}"
            )
            self.log(f"Environment changed to: {env_config['label']}")
            self.log(f"API base: {env_config['api_base']}")
            self.last_env = None
            self.last_tls_signature = None
            self.token = None
            self.status_value.set("Ready")
            self.progress.configure(style="ActionBlue.Horizontal.TProgressbar")
            self.set_progress(0, "Idle")
            self.try_auto_load_cookie_for_selected_env(force=False)
            self.save_preferences()

        def on_tls_options_changed(self):
            self.last_tls_signature = None
            self.token = None
            try:
                tls = self.get_tls_settings()
                self.tls_mode_value.set(tls["mode"])
                self.log(f"TLS: {tls['mode']}")
            except ValueError as exc:
                self.log(f"TLS config warning: {exc}")
            self.progress.configure(style="ActionBlue.Horizontal.TProgressbar")
            self.set_progress(0, "Idle")
            self.save_preferences()

        def save_preferences(self):
            cookie = normalize_cookie_header(self.cookie_value.get())
            data = {
                "file_path": self.file_path_value.get().strip(),
                "include_details": bool(self.include_details_var.get()),
                "remember_cookie": bool(self.remember_cookie_var.get()),
                "auto_cookie_from_browser": bool(self.auto_cookie_from_browser_var.get()),
                "cookie": cookie if self.remember_cookie_var.get() else "",
                "environment": self.get_environment_key(),
                "ca_bundle": str(self.ca_bundle_value.get() or "").strip(),
                "insecure_skip_tls_verify": bool(self.insecure_tls_var.get()),
            }
            save_preferences(data)

        def load_preferences(self):
            data = load_preferences()
            self.file_path_value.set(str(data.get("file_path", "")).strip())
            self.include_details_var.set(bool(data.get("include_details", True)))
            env_key = parse_environment_input(data.get("environment", DEFAULT_ENV))
            self.environment_value.set(env_key or DEFAULT_ENV)
            self.ca_bundle_value.set(str(data.get("ca_bundle", "")).strip())
            self.insecure_tls_var.set(bool(data.get("insecure_skip_tls_verify", False)))
            remember = bool(data.get("remember_cookie", False))
            self.remember_cookie_var.set(remember)
            self.auto_cookie_from_browser_var.set(
                bool(data.get("auto_cookie_from_browser", True))
            )
            if remember:
                self.cookie_value.set(str(data.get("cookie", "")).strip())
            else:
                self.cookie_value.set("")
            try:
                tls = self.get_tls_settings()
                self.tls_mode_value.set(tls["mode"])
            except Exception:
                self.tls_mode_value.set("Invalid TLS config")

        def ensure_token(self):
            cookie = normalize_cookie_header(self.cookie_value.get())
            if not cookie:
                raise ValueError("Please paste your browser cookie string first.")
            self.cookie_value.set(cookie)
            env_key, env_config = self.get_environment_config()
            tls = self.get_tls_settings()
            tls_signature = (tls["insecure"], tls["ca_bundle"])
            self.tls_mode_value.set(tls["mode"])

            if (
                self.token
                and self.last_cookie == cookie
                and self.last_env == env_key
                and self.last_tls_signature == tls_signature
            ):
                return self.token

            self.log(f"Using environment: {env_config['label']}")
            self.log(f"Token URL: {env_config['token_url']}")
            self.log(f"TLS: {tls['mode']}")
            self.log("Requesting bearer token...")
            self.token = get_bearer_token(
                cookie,
                env_config["token_url"],
                verify=tls["verify"],
                platform_origin=env_config.get("platform_origin", ""),
                env_label=env_config["label"],
            )
            self.last_cookie = cookie
            self.last_env = env_key
            self.last_tls_signature = tls_signature
            self.log("Token fetched successfully.")
            return self.token

        def load_input_dataframe(self):
            file_path = self.file_path_value.get().strip()
            if not file_path:
                raise ValueError("Please select a CSV/XLS/XLSX file.")
            path = Path(file_path)
            if not path.exists():
                raise ValueError(f"File does not exist: {file_path}")
            self.log(f"Reading file: {file_path}")
            df = read_zone_file(file_path)
            self.log(f"Loaded {len(df)} row(s) from file.")
            return df

        def refresh_existing_zones(self, token, progress_range=None):
            _env_key, env_config = self.get_environment_config()
            tls = self.get_tls_settings()
            progress_cb = None
            if progress_range and len(progress_range) == 2:
                progress_cb = self.make_progress_callback(
                    progress_range[0], progress_range[1], "Fetching zone details"
                )
            self.existing_zones = fetch_existing_zones(
                token,
                self.include_details_var.get(),
                self.log,
                env_config["api_base"],
                verify=tls["verify"],
                progress_cb=progress_cb,
            )
            self.render_zone_preview(self.existing_zones)
            self.log(f"Zone preview updated with {len(self.existing_zones)} zone(s).")

        def fetch_current_zones(self):
            def action():
                self.set_progress(10, "Authenticating")
                token = self.ensure_token()
                self.set_progress(24, "Fetching zones")
                self.refresh_existing_zones(token, progress_range=(30, 80))
                self.set_progress(90, "Syncing preview")
                self.set_progress(96, "Completing")

            self.run_action(action, "Fetch Current Zones")

        def create_from_file(self):
            def action():
                self.set_progress(8, "Authenticating")
                token = self.ensure_token()
                _env_key, env_config = self.get_environment_config()
                tls = self.get_tls_settings()
                self.set_progress(20, "Loading existing zones")
                self.refresh_existing_zones(token, progress_range=(24, 42))
                self.set_progress(46, "Reading input file")
                df = self.load_input_dataframe()
                create_zones_from_file(
                    df,
                    token,
                    self.existing_zones,
                    self.log,
                    env_config["api_base"],
                    verify=tls["verify"],
                    progress_cb=self.make_progress_callback(
                        48, 86, "Creating zones"
                    ),
                )
                self.set_progress(90, "Refreshing zones")
                self.refresh_existing_zones(token, progress_range=(92, 97))

            self.run_action(action, "Create Zones From File")

        def plan_updates_from_file(self):
            def action():
                self.set_progress(8, "Authenticating")
                token = self.ensure_token()
                self.set_progress(20, "Loading existing zones")
                self.refresh_existing_zones(token, progress_range=(24, 40))
                self.set_progress(44, "Reading input file")
                df = self.load_input_dataframe()
                updates, _stats = plan_zone_updates(
                    df,
                    self.existing_zones,
                    self.log,
                    progress_cb=self.make_progress_callback(
                        46, 92, "Planning updates"
                    ),
                )
                self.current_planned_updates = updates
                self.plan_count_value.set(str(len(updates)))
                if not updates:
                    self.log("No updates needed.")
                self.set_progress(96, "Completing")

            self.run_action(action, "Plan Updates")

        def update_from_file(self):
            def action():
                self.set_progress(8, "Authenticating")
                token = self.ensure_token()
                _env_key, env_config = self.get_environment_config()
                tls = self.get_tls_settings()
                self.set_progress(18, "Loading existing zones")
                self.refresh_existing_zones(token, progress_range=(22, 36))
                self.set_progress(40, "Reading input file")
                df = self.load_input_dataframe()
                updates, stats = plan_zone_updates(
                    df,
                    self.existing_zones,
                    self.log,
                    progress_cb=self.make_progress_callback(
                        42, 66, "Planning updates"
                    ),
                )
                self.current_planned_updates = updates
                self.plan_count_value.set(str(len(updates)))
                if not updates:
                    self.log("No updates needed.")
                    self.set_progress(96, "Completing")
                    return

                proceed = messagebox.askyesno(
                    "Confirm Updates",
                    f"{stats['planned_updates']} zone update(s) are ready. Proceed?",
                    parent=self.root,
                )
                if not proceed:
                    self.log("Update cancelled by user.")
                    self.set_progress(96, "Cancelled")
                    return

                apply_zone_updates(
                    updates,
                    token,
                    self.log,
                    env_config["api_base"],
                    verify=tls["verify"],
                    progress_cb=self.make_progress_callback(
                        68, 92, "Applying updates"
                    ),
                )
                self.plan_count_value.set("0")
                self.current_planned_updates = []
                self.set_progress(94, "Refreshing zones")
                self.refresh_existing_zones(token, progress_range=(95, 98))

            self.run_action(action, "Apply Updates")


def add_common_cli_args(parser):
    parser.add_argument(
        "--env",
        choices=["sit", "uat", "prod"],
        help="Target environment. If not provided, script prompts at runtime.",
    )
    parser.add_argument("--cookie", help="Raw Cookie header string.")
    parser.add_argument("--cookie-file", help="Path to file containing Cookie header.")
    parser.add_argument(
        "--ca-bundle",
        help="Path to CA bundle PEM file for TLS verification.",
    )
    parser.add_argument(
        "--insecure-skip-tls-verify",
        action="store_true",
        help="Disable TLS certificate verification (not recommended).",
    )
    parser.add_argument(
        "--include-details",
        dest="include_details",
        action="store_true",
        default=True,
        help="Fetch per-zone detail API for complete region/country data (default).",
    )
    parser.add_argument(
        "--no-include-details",
        dest="include_details",
        action="store_false",
        help="Skip detail API calls (faster, but may miss mapping fields).",
    )


def build_parser():
    parser = argparse.ArgumentParser(
        prog="zonetesting2.py",
        description=(
            "Delivery Zone Manager. Runs GUI by default when Tkinter is available, "
            "otherwise use CLI subcommands."
        ),
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Force GUI mode (fails if Tkinter is not available).",
    )

    subparsers = parser.add_subparsers(dest="command")

    fetch_parser = subparsers.add_parser("fetch", help="Fetch and display existing zones.")
    add_common_cli_args(fetch_parser)
    fetch_parser.add_argument(
        "--max-rows",
        type=int,
        default=120,
        help="Maximum number of zone rows to print in table output.",
    )

    create_parser = subparsers.add_parser("create", help="Create zones from file.")
    add_common_cli_args(create_parser)
    create_parser.add_argument("--file", required=True, help="CSV/XLS/XLSX path.")

    plan_parser = subparsers.add_parser("plan", help="Plan updates from file.")
    add_common_cli_args(plan_parser)
    plan_parser.add_argument("--file", required=True, help="CSV/XLS/XLSX path.")

    update_parser = subparsers.add_parser("update", help="Apply updates from file.")
    add_common_cli_args(update_parser)
    update_parser.add_argument("--file", required=True, help="CSV/XLS/XLSX path.")
    update_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt.",
    )

    return parser


def run_interactive_cli():
    ensure_dependencies()
    print("Tkinter is not available. Running interactive CLI mode.\n")
    action = input("Choose action [fetch/create/plan/update]: ").strip().lower()
    if action not in {"fetch", "create", "plan", "update"}:
        print("Invalid action.")
        return 1

    cookie = input("Paste cookie header: ").strip()
    include_details_input = (
        input("Include full zone details? [Y/n]: ").strip().lower() or "y"
    )
    include_details = include_details_input in {"y", "yes"}
    chosen_env = prompt_environment_cli(DEFAULT_ENV)
    ca_bundle = input("CA bundle path (optional, press Enter to skip): ").strip()
    insecure_input = input(
        "Skip TLS certificate verification? [y/N]: "
    ).strip().lower()
    insecure_skip_tls = insecure_input in {"y", "yes"}

    command_args = ["--cookie", cookie, "--env", chosen_env]
    if ca_bundle:
        command_args.extend(["--ca-bundle", ca_bundle])
    if insecure_skip_tls:
        command_args.append("--insecure-skip-tls-verify")
    if not include_details:
        command_args.append("--no-include-details")

    if action in {"create", "plan", "update"}:
        file_path = input("Enter CSV/XLS/XLSX file path: ").strip()
        command_args.extend(["--file", file_path])
        if action == "update":
            confirm = input("Skip confirmation prompt? [y/N]: ").strip().lower()
            if confirm in {"y", "yes"}:
                command_args.append("--yes")

    parser = build_parser()
    args = parser.parse_args([action] + command_args)
    return run_cli_command(args)


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.command:
        try:
            return run_cli_command(args)
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    if args.gui:
        if not TK_AVAILABLE:
            print("ERROR: Tkinter is not available in this Python build.", file=sys.stderr)
            return 1
        ensure_dependencies()
        root = tk.Tk()
        DeliveryZoneApp(root)
        root.mainloop()
        return 0

    if TK_AVAILABLE:
        ensure_dependencies()
        root = tk.Tk()
        DeliveryZoneApp(root)
        root.mainloop()
        return 0

    return run_interactive_cli()


if __name__ == "__main__":
    raise SystemExit(main())
