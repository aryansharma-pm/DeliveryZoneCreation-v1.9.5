import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    import pandas as pd
except ModuleNotFoundError:
    pd = None

try:
    import requests
except ModuleNotFoundError:
    requests = None

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, simpledialog

    TK_AVAILABLE = True
except Exception:
    tk = None
    filedialog = None
    messagebox = None
    scrolledtext = None
    simpledialog = None
    TK_AVAILABLE = False

REQUEST_TIMEOUT = 30
CONFIG_PATH = Path.home() / ".delivery_zone_manager.json"
DEFAULT_ENV = "sit"
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


def enrich_zones_with_details(token, zones, api_base, verify=True, logger=None):
    enriched = []
    total = len(zones)
    for index, zone in enumerate(zones, start=1):
        zone_id = zone.get("zone_id") or zone.get("id")
        if not zone_id:
            enriched.append(zone)
            continue

        detailed = get_zone_details(token, zone_id, api_base, verify=verify)
        if detailed:
            merged = dict(zone)
            merged.update(detailed)
            enriched.append(merged)
        else:
            enriched.append(zone)

        if logger and (index % 25 == 0 or index == total):
            logger(f"Fetched detail for {index}/{total} zone(s)...")
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
        "mapping_regions",
        "channels",
    ]
    missing = []
    for field in required:
        value = row.get(field)
        if pd.isna(value) or not str(value).strip():
            missing.append(field)
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
                "regions": parse_list(row.get("mapping_regions")),
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


def create_zones_from_file(df, token, existing_zones, logger, api_base, verify=True):
    existing_slugs = {
        str(zone.get("slug", "")).strip() for zone in existing_zones if zone.get("slug")
    }
    created = 0
    skipped = 0
    failed = 0

    for idx, row in df.iterrows():
        row_data = row.to_dict()
        missing = missing_required_fields(row_data)
        if missing:
            logger(f"Skipping row {idx + 2}: missing field(s) {missing}")
            skipped += 1
            continue

        try:
            payload = build_zone_payload(row_data)
        except ValueError as exc:
            logger(f"Skipping row {idx + 2}: {exc}")
            skipped += 1
            continue

        if payload["slug"] in existing_slugs:
            logger(f"Skipping row {idx + 2}: slug '{payload['slug']}' already exists.")
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


def plan_zone_updates(df, existing_zones, logger):
    lookup = build_existing_lookup(existing_zones)
    updates_by_zone = {}
    skipped = 0
    unmatched = 0

    for idx, row in df.iterrows():
        row_data = row.to_dict()
        missing = missing_required_fields(row_data)
        if missing:
            logger(f"Skipping row {idx + 2}: missing field(s) {missing}")
            skipped += 1
            continue

        try:
            payload = build_zone_payload(row_data)
        except ValueError as exc:
            logger(f"Skipping row {idx + 2}: {exc}")
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
                f"Row {idx + 2}: no matching existing zone found for slug "
                f"'{payload['slug']}' and store IDs {payload['store_ids']}."
            )
            unmatched += 1

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


def apply_zone_updates(updates, token, logger, api_base, verify=True):
    success = 0
    failed = 0

    for update in updates:
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


def fetch_existing_zones(token, include_details, logger, api_base, verify=True):
    logger("Fetching existing zones from API...")
    zones = get_zones(token, api_base, verify=verify)
    missing_mapping_in_list = zones and all(
        not zone_has_mapping_data(zone) for zone in zones
    )
    if include_details and zones:
        logger("Fetching per-zone details (regions/countries)...")
        zones = enrich_zones_with_details(
            token, zones, api_base, verify=verify, logger=logger
        )
    elif missing_mapping_in_list:
        logger(
            "List API does not include mapping/region fields. "
            "Enable include-details to load them."
        )
    logger(f"Fetched {len(zones)} zone(s).")
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
            self.root.geometry("1000x760")
            self.root.minsize(900, 650)

            self.cookie_value = tk.StringVar()
            self.file_path_value = tk.StringVar()
            self.include_details_var = tk.BooleanVar(value=True)
            self.remember_cookie_var = tk.BooleanVar(value=False)
            self.environment_value = tk.StringVar(value=DEFAULT_ENV)
            self.ca_bundle_value = tk.StringVar()
            self.insecure_tls_var = tk.BooleanVar(value=False)
            self.last_cookie = None
            self.last_env = None
            self.last_tls_signature = None
            self.token = None
            self.existing_zones = []

            self._build_ui()
            self.load_preferences()
            self.prompt_environment_on_startup()

        def _build_ui(self):
            container = tk.Frame(self.root, padx=14, pady=14)
            container.pack(fill="both", expand=True)

            header = tk.Label(
                container,
                text="Delivery Zone Management",
                font=("Helvetica", 17, "bold"),
                anchor="w",
            )
            header.pack(fill="x")

            subtitle = tk.Label(
                container,
                text=(
                    "Fetch, create, plan and update zones from CSV/XLSX with "
                    "clear logs and structured output."
                ),
                anchor="w",
                fg="#444",
            )
            subtitle.pack(fill="x", pady=(2, 8))

            env_frame = tk.LabelFrame(container, text="Environment", padx=10, pady=8)
            env_frame.pack(fill="x", pady=(0, 8))
            tk.Label(
                env_frame,
                text="Select target environment (affects both Zone API and Token API):",
            ).grid(row=0, column=0, sticky="w")

            radio_wrap = tk.Frame(env_frame)
            radio_wrap.grid(row=1, column=0, sticky="w", pady=(4, 0))
            tk.Radiobutton(
                radio_wrap,
                text="SIT",
                variable=self.environment_value,
                value="sit",
                command=self.on_environment_changed,
            ).pack(side="left", padx=(0, 12))
            tk.Radiobutton(
                radio_wrap,
                text="UAT",
                variable=self.environment_value,
                value="uat",
                command=self.on_environment_changed,
            ).pack(side="left", padx=(0, 12))
            tk.Radiobutton(
                radio_wrap,
                text="PROD",
                variable=self.environment_value,
                value="prod",
                command=self.on_environment_changed,
            ).pack(side="left")

            cookie_frame = tk.LabelFrame(
                container, text="Authentication", padx=10, pady=10
            )
            cookie_frame.pack(fill="x", pady=(6, 8))

            tk.Label(cookie_frame, text="Cookie Header:").grid(
                row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 6)
            )
            cookie_entry = tk.Entry(cookie_frame, textvariable=self.cookie_value)
            cookie_entry.grid(row=0, column=1, sticky="ew", pady=(0, 6))

            normalize_button = tk.Button(
                cookie_frame,
                text="Normalize",
                command=self.normalize_cookie_entry,
                width=11,
            )
            normalize_button.grid(row=0, column=2, padx=(8, 0), pady=(0, 6))

            remember_cb = tk.Checkbutton(
                cookie_frame,
                text="Remember cookie locally",
                variable=self.remember_cookie_var,
                onvalue=True,
                offvalue=False,
                anchor="w",
            )
            remember_cb.grid(row=1, column=1, sticky="w")
            cookie_frame.grid_columnconfigure(1, weight=1)

            file_frame = tk.LabelFrame(container, text="Input File", padx=10, pady=10)
            file_frame.pack(fill="x", pady=(0, 8))

            tk.Label(file_frame, text="CSV/XLSX Path:").grid(
                row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 4)
            )
            file_entry = tk.Entry(file_frame, textvariable=self.file_path_value)
            file_entry.grid(row=0, column=1, sticky="ew", pady=(0, 4))
            browse_button = tk.Button(file_frame, text="Browse", command=self.select_file)
            browse_button.grid(row=0, column=2, padx=(8, 0), pady=(0, 4))
            file_frame.grid_columnconfigure(1, weight=1)

            options_frame = tk.LabelFrame(container, text="Options", padx=10, pady=8)
            options_frame.pack(fill="x", pady=(0, 8))

            details_toggle = tk.Checkbutton(
                options_frame,
                text="Include full zone details (regions/countries, slower)",
                variable=self.include_details_var,
                onvalue=True,
                offvalue=False,
                anchor="w",
            )
            details_toggle.grid(row=0, column=0, sticky="w")

            tk.Label(options_frame, text="CA Bundle (optional):").grid(
                row=1, column=0, sticky="w", pady=(6, 0)
            )
            ca_entry = tk.Entry(options_frame, textvariable=self.ca_bundle_value)
            ca_entry.grid(row=1, column=1, sticky="ew", pady=(6, 0))
            ca_entry.bind("<FocusOut>", lambda _event: self.on_tls_options_changed())
            ca_browse_button = tk.Button(
                options_frame, text="Browse", command=self.select_ca_bundle
            )
            ca_browse_button.grid(row=1, column=2, padx=(8, 0), pady=(6, 0))

            insecure_cb = tk.Checkbutton(
                options_frame,
                text="Skip TLS certificate verification (not recommended)",
                variable=self.insecure_tls_var,
                onvalue=True,
                offvalue=False,
                anchor="w",
                command=self.on_tls_options_changed,
            )
            insecure_cb.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
            options_frame.grid_columnconfigure(1, weight=1)

            action_frame = tk.LabelFrame(container, text="Actions", padx=10, pady=10)
            action_frame.pack(fill="x", pady=(0, 8))

            self.fetch_button = tk.Button(
                action_frame, text="Fetch Current Zones", command=self.fetch_current_zones
            )
            self.fetch_button.grid(row=0, column=0, padx=(0, 8), pady=4, sticky="ew")

            self.create_button = tk.Button(
                action_frame, text="Create Zones From File", command=self.create_from_file
            )
            self.create_button.grid(row=0, column=1, padx=(0, 8), pady=4, sticky="ew")

            self.plan_button = tk.Button(
                action_frame, text="Plan Updates", command=self.plan_updates_from_file
            )
            self.plan_button.grid(row=0, column=2, padx=(0, 8), pady=4, sticky="ew")

            self.update_button = tk.Button(
                action_frame, text="Apply Updates", command=self.update_from_file
            )
            self.update_button.grid(row=0, column=3, pady=4, sticky="ew")

            self.clear_log_button = tk.Button(
                action_frame, text="Clear Logs", command=self.clear_logs
            )
            self.clear_log_button.grid(row=1, column=0, pady=(6, 0), sticky="ew")

            self.copy_log_button = tk.Button(
                action_frame, text="Copy Logs", command=self.copy_logs
            )
            self.copy_log_button.grid(row=1, column=1, pady=(6, 0), sticky="ew")

            self.save_pref_button = tk.Button(
                action_frame, text="Save Preferences", command=self.save_preferences
            )
            self.save_pref_button.grid(row=1, column=2, pady=(6, 0), sticky="ew")

            action_frame.grid_columnconfigure(0, weight=1)
            action_frame.grid_columnconfigure(1, weight=1)
            action_frame.grid_columnconfigure(2, weight=1)
            action_frame.grid_columnconfigure(3, weight=1)

            log_frame = tk.LabelFrame(container, text="Logs", padx=10, pady=10)
            log_frame.pack(fill="both", expand=True)

            self.log_text = scrolledtext.ScrolledText(
                log_frame, wrap=tk.WORD, font=("Menlo", 10), state="disabled"
            )
            self.log_text.pack(fill="both", expand=True)

        def set_busy(self, busy):
            state = tk.DISABLED if busy else tk.NORMAL
            self.fetch_button.config(state=state)
            self.create_button.config(state=state)
            self.plan_button.config(state=state)
            self.update_button.config(state=state)
            self.clear_log_button.config(state=state)
            self.copy_log_button.config(state=state)
            self.save_pref_button.config(state=state)

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
            self.log("Logs cleared.")

        def copy_logs(self):
            text = self.log_text.get("1.0", tk.END).strip()
            if not text:
                self.log("No logs to copy.")
                return
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.log("Logs copied to clipboard.")

        def run_action(self, action, label):
            self.set_busy(True)
            self.log(f"--- {label} ---")
            try:
                self.save_preferences()
                action()
                self.log(f"{label}: completed.")
            except Exception as exc:
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
            ca_bundle = ""
            if not insecure:
                ca_bundle = normalize_ca_bundle_path(self.ca_bundle_value.get())
            verify = False if insecure else (ca_bundle if ca_bundle else True)
            mode = "INSECURE (verify disabled)" if insecure else (
                f"CA bundle: {ca_bundle}" if ca_bundle else "System CA store"
            )
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
            self.log(f"Selected environment: {env_config['label']}")
            self.log(f"API base: {env_config['api_base']}")
            self.save_preferences()

        def on_environment_changed(self):
            key, env_config = self.get_environment_config()
            self.log(f"Environment changed to: {env_config['label']}")
            self.log(f"API base: {env_config['api_base']}")
            self.last_env = None
            self.last_tls_signature = None
            self.token = None
            self.save_preferences()

        def on_tls_options_changed(self):
            self.last_tls_signature = None
            self.token = None
            try:
                tls = self.get_tls_settings()
                self.log(f"TLS mode: {tls['mode']}")
            except ValueError as exc:
                self.log(f"TLS config warning: {exc}")
            self.save_preferences()

        def save_preferences(self):
            cookie = normalize_cookie_header(self.cookie_value.get())
            data = {
                "file_path": self.file_path_value.get().strip(),
                "include_details": bool(self.include_details_var.get()),
                "remember_cookie": bool(self.remember_cookie_var.get()),
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
            if remember:
                self.cookie_value.set(str(data.get("cookie", "")).strip())
            else:
                self.cookie_value.set("")

        def ensure_token(self):
            cookie = normalize_cookie_header(self.cookie_value.get())
            if not cookie:
                raise ValueError("Please paste your browser cookie string first.")
            self.cookie_value.set(cookie)
            env_key, env_config = self.get_environment_config()
            tls = self.get_tls_settings()
            tls_signature = (tls["insecure"], tls["ca_bundle"])

            if (
                self.token
                and self.last_cookie == cookie
                and self.last_env == env_key
                and self.last_tls_signature == tls_signature
            ):
                return self.token

            self.log(f"Using environment: {env_config['label']}")
            self.log(f"Token URL: {env_config['token_url']}")
            self.log(f"TLS mode: {tls['mode']}")
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

        def refresh_existing_zones(self, token):
            _env_key, env_config = self.get_environment_config()
            tls = self.get_tls_settings()
            self.existing_zones = fetch_existing_zones(
                token,
                self.include_details_var.get(),
                self.log,
                env_config["api_base"],
                verify=tls["verify"],
            )

        def log_zone_details(self, zones):
            for line in format_zone_table_lines(zones):
                self.log(line)

        def fetch_current_zones(self):
            def action():
                token = self.ensure_token()
                self.refresh_existing_zones(token)
                self.log_zone_details(self.existing_zones)

            self.run_action(action, "Fetch Current Zones")

        def create_from_file(self):
            def action():
                token = self.ensure_token()
                _env_key, env_config = self.get_environment_config()
                tls = self.get_tls_settings()
                self.refresh_existing_zones(token)
                df = self.load_input_dataframe()
                create_zones_from_file(
                    df,
                    token,
                    self.existing_zones,
                    self.log,
                    env_config["api_base"],
                    verify=tls["verify"],
                )
                self.refresh_existing_zones(token)

            self.run_action(action, "Create Zones From File")

        def plan_updates_from_file(self):
            def action():
                token = self.ensure_token()
                self.refresh_existing_zones(token)
                df = self.load_input_dataframe()
                updates, _stats = plan_zone_updates(df, self.existing_zones, self.log)
                if not updates:
                    self.log("No updates needed.")

            self.run_action(action, "Plan Updates")

        def update_from_file(self):
            def action():
                token = self.ensure_token()
                _env_key, env_config = self.get_environment_config()
                tls = self.get_tls_settings()
                self.refresh_existing_zones(token)
                df = self.load_input_dataframe()
                updates, stats = plan_zone_updates(df, self.existing_zones, self.log)
                if not updates:
                    self.log("No updates needed.")
                    return

                proceed = messagebox.askyesno(
                    "Confirm Updates",
                    f"{stats['planned_updates']} zone update(s) are ready. Proceed?",
                    parent=self.root,
                )
                if not proceed:
                    self.log("Update cancelled by user.")
                    return

                apply_zone_updates(
                    updates,
                    token,
                    self.log,
                    env_config["api_base"],
                    verify=tls["verify"],
                )
                self.refresh_existing_zones(token)

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
