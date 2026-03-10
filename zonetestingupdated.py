import json
from pathlib import Path

import pandas as pd
import requests
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext

API_BASE = "https://api.jiox0.de/service/platform/logistics/v2.0/company/1/zones"
TOKEN_URL = "https://api.jiox0.de/service/panel/authentication/v1.0/company/1/oauth/staff/token"
REQUEST_TIMEOUT = 30


def parse_cookie_string(cookie_string):
    cookies = {}
    for item in cookie_string.split(";"):
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


def get_bearer_token(cookie_string):
    if not cookie_string.strip():
        raise ValueError("Cookie string is required.")
    headers = {
        "accept": "application/json, text/plain, */*",
        "origin": "https://platform.jiox0.de",
        "user-agent": "Mozilla/5.0",
    }
    try:
        response = requests.get(
            TOKEN_URL,
            headers=headers,
            cookies=parse_cookie_string(cookie_string),
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Token request failed: {exc}") from exc

    if response.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch token ({response.status_code}): {response.text}"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("Token response was not valid JSON.") from exc

    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"Access token not found in response: {data}")
    return token


def get_zones(token):
    headers = {"authorization": f"Bearer {token}"}
    zones = []
    page_no = 1
    page_size = 500

    while True:
        try:
            response = requests.get(
                API_BASE,
                headers=headers,
                params={"page_no": page_no, "page_size": page_size},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Failed to fetch zones: {exc}") from exc

        if response.status_code != 200:
            raise RuntimeError(
                f"Error fetching zones ({response.status_code}): {response.text}"
            )

        payload = response.json()
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


def get_zone_details(token, zone_id):
    headers = {"authorization": f"Bearer {token}"}
    url = f"{API_BASE}/{zone_id}"
    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    return extract_zone_from_response(payload)


def zone_has_mapping_data(zone):
    return bool(
        zone.get("mapping")
        or zone.get("mappings")
        or zone.get("region_type")
        or zone.get("regionType")
    )


def enrich_zones_with_details(token, zones, logger=None):
    enriched = []
    total = len(zones)
    for index, zone in enumerate(zones, start=1):
        zone_id = zone.get("zone_id") or zone.get("id")
        if not zone_id:
            enriched.append(zone)
            continue

        detailed = get_zone_details(token, zone_id)
        if detailed:
            merged = dict(zone)
            merged.update(detailed)
            enriched.append(merged)
        else:
            enriched.append(zone)

        if logger and (index % 25 == 0 or index == total):
            logger(f"Fetched detail for {index}/{total} zone(s)...")
    return enriched


def post_or_put(url, token, payload, method="POST"):
    headers = {
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
    }
    try:
        if method == "POST":
            response = requests.post(
                url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT
            )
        else:
            response = requests.put(
                url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT
            )
    except requests.RequestException as exc:
        return 0, f"Network error: {exc}"

    try:
        parsed = response.json()
    except ValueError:
        parsed = response.text
    return response.status_code, parsed


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


def create_zones_from_file(df, token, existing_zones, logger):
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

        status, resp = post_or_put(API_BASE, token, payload, "POST")
        if status in {200, 201}:
            logger(f"Created zone '{payload['slug']}' successfully.")
            created += 1
            existing_slugs.add(payload["slug"])
        else:
            logger(f"Failed to create '{payload['slug']}'. Status: {status}, Response: {resp}")
            failed += 1

    logger(f"Create summary -> Created: {created}, Skipped: {skipped}, Failed: {failed}")


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

    for idx, row in df.iterrows():
        row_data = row.to_dict()
        missing = missing_required_fields(row_data)
        if missing:
            logger(f"Skipping row {idx + 2}: missing field(s) {missing}")
            continue

        try:
            payload = build_zone_payload(row_data)
        except ValueError as exc:
            logger(f"Skipping row {idx + 2}: {exc}")
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

    updates = list(updates_by_zone.values())
    for update in updates:
        logger(
            f"Update planned -> slug: {update['slug']}, zone_id: {update['zone_id']}, "
            f"store_ids: {sorted(update['store_ids'])}, "
            f"old_regions: {sorted(update['old_regions'])}, "
            f"new_regions: {sorted(update['new_regions'])}"
        )
    return updates


def apply_zone_updates(updates, token, logger):
    success = 0
    failed = 0

    for update in updates:
        payload = update["payload"]
        payload["zone_id"] = update["zone_id"]
        payload["mapping"][0]["regions"] = sorted(update["new_regions"])
        url = f"{API_BASE}/{update['zone_id']}"
        status, resp = post_or_put(url, token, payload, "PUT")
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

    logger(f"Update summary -> Updated: {success}, Failed: {failed}")


class DeliveryZoneApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Delivery Zone Manager")
        self.root.geometry("950x700")
        self.root.minsize(840, 620)

        self.cookie_value = tk.StringVar()
        self.file_path_value = tk.StringVar()
        self.include_details_var = tk.BooleanVar(value=True)
        self.last_cookie = None
        self.token = None
        self.existing_zones = []

        self._build_ui()

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

        cookie_frame = tk.LabelFrame(
            container, text="Authentication", padx=10, pady=10
        )
        cookie_frame.pack(fill="x", pady=(12, 8))

        tk.Label(cookie_frame, text="Cookie Header:").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 4)
        )
        cookie_entry = tk.Entry(cookie_frame, textvariable=self.cookie_value)
        cookie_entry.grid(row=0, column=1, sticky="ew", pady=(0, 4))
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

        self.update_button = tk.Button(
            action_frame, text="Update Zones From File", command=self.update_from_file
        )
        self.update_button.grid(row=0, column=2, pady=4, sticky="ew")

        details_toggle = tk.Checkbutton(
            action_frame,
            text="Include full zone details (regions/countries, slower)",
            variable=self.include_details_var,
            onvalue=True,
            offvalue=False,
            anchor="w",
        )
        details_toggle.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

        action_frame.grid_columnconfigure(0, weight=1)
        action_frame.grid_columnconfigure(1, weight=1)
        action_frame.grid_columnconfigure(2, weight=1)

        log_frame = tk.LabelFrame(container, text="Logs", padx=10, pady=10)
        log_frame.pack(fill="both", expand=True)

        self.log_text = scrolledtext.ScrolledText(
            log_frame, wrap=tk.WORD, font=("Menlo", 11), state="disabled"
        )
        self.log_text.pack(fill="both", expand=True)

    def set_busy(self, busy):
        state = tk.DISABLED if busy else tk.NORMAL
        self.fetch_button.config(state=state)
        self.create_button.config(state=state)
        self.update_button.config(state=state)

    def log(self, message):
        self.log_text.config(state="normal")
        self.log_text.insert(tk.END, f"{message}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state="disabled")
        self.root.update_idletasks()

    def run_action(self, action, label):
        self.set_busy(True)
        self.log(f"\n--- {label} ---")
        try:
            action()
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

    def ensure_token(self):
        cookie = self.cookie_value.get().strip()
        if not cookie:
            raise ValueError("Please paste your browser cookie string first.")

        if self.token and self.last_cookie == cookie:
            return self.token

        self.log("Requesting bearer token...")
        self.token = get_bearer_token(cookie)
        self.last_cookie = cookie
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
        self.log("Fetching existing zones from API...")
        zones = get_zones(token)
        include_details = self.include_details_var.get()
        missing_mapping_in_list = zones and all(
            not zone_has_mapping_data(zone) for zone in zones
        )
        if include_details and zones:
            self.log("Fetching per-zone details (regions/countries)...")
            zones = enrich_zones_with_details(token, zones, self.log)
        elif missing_mapping_in_list:
            self.log(
                "List API does not include mapping/region fields. "
                "Enable 'Include full zone details' to load them."
            )
        self.existing_zones = zones
        self.log(f"Fetched {len(self.existing_zones)} zone(s).")

    def log_zone_details(self, zones):
        if not zones:
            self.log("No zones returned by API.")
            return

        def shorten(text, width):
            text = str(text)
            if len(text) <= width:
                return text
            if width <= 3:
                return text[:width]
            return f"{text[:width - 3]}..."

        def preview(items, max_items=4):
            values = [str(x) for x in items]
            if not values:
                return "-"
            if len(values) <= max_items:
                return ",".join(values)
            return f"{','.join(values[:max_items])} +{len(values) - max_items} more"

        self.log("Zone details (structured):")
        header = (
            f"{'No':>3} | {'Name':<26} | {'Slug':<26} | {'Zone ID':<24} | "
            f"{'Type':<11} | {'Stores':>6} | {'Regions':>7} | {'Countries'}"
        )
        self.log(header)
        self.log("-" * len(header))

        for index, zone in enumerate(zones, start=1):
            zone_id = zone.get("zone_id") or zone.get("id") or "N/A"
            slug = str(zone.get("slug", "")).strip() or "N/A"
            name = str(zone.get("name", "")).strip() or "N/A"
            region_type = (
                str(zone.get("region_type") or zone.get("regionType") or "").strip()
                or "N/A"
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

            self.log(
                f"{index:>3} | {shorten(name, 26):<26} | {shorten(slug, 26):<26} | "
                f"{shorten(zone_id, 24):<24} | {shorten(region_type, 11):<11} | "
                f"{len(store_ids):>6} | {len(mapping_regions):>7} | "
                f"{shorten(preview(sorted(mapping_countries), 3), 30)}"
            )
            self.log(
                f"    stores: {shorten(preview(store_ids, 6), 90)}"
            )

    def fetch_current_zones(self):
        def action():
            token = self.ensure_token()
            self.refresh_existing_zones(token)
            self.log_zone_details(self.existing_zones)

        self.run_action(action, "Fetch Current Zones")

    def create_from_file(self):
        def action():
            token = self.ensure_token()
            self.refresh_existing_zones(token)
            df = self.load_input_dataframe()
            create_zones_from_file(df, token, self.existing_zones, self.log)
            self.refresh_existing_zones(token)

        self.run_action(action, "Create Zones From File")

    def update_from_file(self):
        def action():
            token = self.ensure_token()
            self.refresh_existing_zones(token)
            df = self.load_input_dataframe()
            updates = plan_zone_updates(df, self.existing_zones, self.log)
            if not updates:
                self.log("No updates needed.")
                return

            proceed = messagebox.askyesno(
                "Confirm Updates",
                f"{len(updates)} zone update(s) are ready. Proceed?",
                parent=self.root,
            )
            if not proceed:
                self.log("Update cancelled by user.")
                return

            apply_zone_updates(updates, token, self.log)
            self.refresh_existing_zones(token)

        self.run_action(action, "Update Zones From File")


def main():
    root = tk.Tk()
    app = DeliveryZoneApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
