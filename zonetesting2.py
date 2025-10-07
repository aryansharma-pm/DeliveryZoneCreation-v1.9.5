import pandas as pd
import json
import requests
from tqdm import tqdm
import tkinter as tk
from tkinter import filedialog, simpledialog

API_BASE = "https://api.jiox0.de/service/platform/logistics/v2.0/company/1/zones"
TOKEN_URL = "https://api.jiox0.de/service/panel/authentication/v1.0/company/1/oauth/staff/token"

def get_cookie_string_gui():
    root = tk.Tk()
    root.withdraw()
    cookie_string = simpledialog.askstring(
        "Paste Cookies",
        "Paste your browser cookie string (copy from DevTools 'Cookie' header):",
        parent=root)
    root.destroy()
    return cookie_string or ""

def get_bearer_token(cookie_string):
    headers = {
        "accept": "application/json, text/plain, */*",
        "origin": "https://platform.jiox0.de",
        "user-agent": "Mozilla/5.0"
    }
    cookies = {}
    for item in cookie_string.split(";"):
        if "=" in item:
            k, v = item.strip().split("=", 1)
            cookies[k] = v
    resp = requests.get(TOKEN_URL, headers=headers, cookies=cookies)
    if resp.status_code == 200:
        data = resp.json()
        token = data.get("access_token")
        if not token:
            print("❌ Access token not found. Response:", data)
            exit(1)
        return token
    else:
        print("❌ Failed to fetch token:", resp.status_code, resp.text)
        exit(1)

def get_zones(token):
    print("\nFetching current delivery zones...")
    headers = {"authorization": f"Bearer {token}"}
    resp = requests.get(API_BASE, headers=headers, params={"page_no":1,"page_size":1000})
    if resp.status_code == 200:
        data = resp.json()
        zones = data.get("items", [])
        print(f"\nTotal zones found: {len(zones)}")
        for zone in zones:
            print(f"Slug: {zone.get('slug')}, Store IDs: {zone.get('store_ids')}, Mapping Regions: {zone.get('mapping')[0]['regions'] if zone.get('mapping') else []}")
        return zones
    else:
        print("❌ Error fetching zones:", resp.status_code, resp.text)
        return []

def parse_list(cell):
    if pd.isna(cell): return []
    if isinstance(cell, list): return [str(x) for x in cell]
    try:
        parsed = json.loads(cell)
        if isinstance(parsed, list): return [str(x) for x in parsed]
    except: pass
    return [x.strip() for x in str(cell).replace("[","").replace("]","").replace("'","").replace('"',"").split(',') if x.strip()]

def parse_store_ids(cell):
    if pd.isna(cell): return []
    ids = []
    for x in str(cell).replace('[','').replace(']','').replace('"','').replace("'",'').split(','):
        x = x.strip()
        if not x: continue
        try:
            float_val = float(x)
            if float_val.is_integer():
                ids.append(int(float_val))
            else:
                ids.append(str(x))
        except:
            ids.append(str(x))
    return ids

def post_or_put(url, token, payload, method="POST"):
    headers = {
        "authorization": f"Bearer {token}",
        "content-type": "application/json"
    }
    if method == "POST":
        r = requests.post(url, headers=headers, json=payload, timeout=20)
    else:
        r = requests.put(url, headers=headers, json=payload, timeout=20)
    try:
        return r.status_code, r.json()
    except:
        return r.status_code, r.text

def build_zone_payload(row):
    company_id_val = row.get('company_id')
    try:
        company_id = int(float(company_id_val))
    except Exception:
        company_id = 1

    ptype = str(row.get('product_type', 'all')).strip().lower()
    if ptype not in ['all', 'explicit']:
        ptype = 'all'

    region_type = str(row.get('region_type', 'pincode')).lower()
    if region_type not in ['pincode', 'non-pincode']:
        region_type = 'pincode'

    return {
        "is_active": True,
        "slug": str(row['slug']),
        "name": str(row['name']),
        "company_id": company_id,
        "store_ids": parse_store_ids(row['store_ids']),
        "region_type": region_type,
        "mapping": [{
            "country": str(row['mapping_country']),
            "regions": parse_list(row['mapping_regions'])
        }],
        "product": {
            "type": ptype,
            "tags": parse_list(row.get('product_tags', "")),
        },
        "channels": [{"channel_id": str(row['channels']), "channel_type": "application"}]
    }

def select_file():
    root = tk.Tk()
    root.withdraw()
    file_path = filedialog.askopenfilename(
        title="Select your Delivery Zone CSV/XLSX file",
        filetypes=[("Excel files", "*.xlsx *.xls"), ("CSV files", "*.csv"), ("All files", "*.*")]
    )
    root.destroy()
    return file_path

def create_zones_from_file(df, token, existing_zones):
    existing_slugs = set(z['slug'] for z in existing_zones)
    required_fields = ['slug', 'store_ids', 'region_type', 'mapping_country', 'mapping_regions']
    for idx, row in df.iterrows():
        missing = [f for f in required_fields if pd.isna(row.get(f)) or not str(row.get(f)).strip()]
        if missing:
            print(f"Skipping row {idx+2} - missing required field(s): {missing}")
            continue
        payload = build_zone_payload(row)
        if payload['slug'] in existing_slugs:
            print(f"Skipping create for slug '{payload['slug']}' - already exists.")
            continue
        print(f"\n[CREATE] Creating delivery zone with slug '{payload['slug']}' and store_ids {payload['store_ids']}")
        status, resp = post_or_put(API_BASE, token, payload, "POST")
        if status in (200,201):
            print(f"✅ Success: {resp}")
        else:
            print(f"❌ Failed: {resp}")

def update_zones_from_file(df, token, existing_zones):
    zone_lookup = {}
    for zone in existing_zones:
        slug = zone.get('slug')
        zone_id = zone.get('zone_id') or zone.get('id')
        store_ids = [str(x) for x in zone.get('store_ids',[])]
        mapping_regions = set()
        if zone.get('mapping'):
            mapping_regions = set(str(x) for x in zone['mapping'][0].get('regions',[]))
        for sid in store_ids:
            zone_lookup[(slug, sid)] = (mapping_regions, zone_id, zone)
    updates = []
    skips = []
    required_fields = ['slug', 'store_ids', 'region_type', 'mapping_country', 'mapping_regions']
    for idx, row in df.iterrows():
        missing = [f for f in required_fields if pd.isna(row.get(f)) or not str(row.get(f)).strip()]
        if missing:
            print(f"Skipping row {idx+2} - missing required field(s): {missing}")
            continue
        slug = str(row['slug'])
        store_ids = parse_store_ids(row['store_ids'])
        mapping_regions_new = set(parse_list(row['mapping_regions']))
        for sid in store_ids:
            key = (slug, str(sid))
            if key in zone_lookup:
                mapping_regions_old, zone_id, zone_obj = zone_lookup[key]
                if not zone_id:
                    print(f"Skipping update for slug '{slug}', store_id '{sid}' - zone_id not found in API response.")
                    continue
                if mapping_regions_new != mapping_regions_old:
                    updates.append({
                        "slug": slug, "store_id": sid,
                        "old_regions": list(mapping_regions_old),
                        "new_regions": list(mapping_regions_new),
                        "zone_id": zone_id,
                        "row": row
                    })
                else:
                    skips.append((slug, sid))
    print("\n--- Delivery Zones to Update ---")
    for upd in updates:
        print(f"\nZone: {upd['slug']}, Store ID: {upd['store_id']}")
        print(f"Existing Mapping Regions: {upd['old_regions']}")
        print(f"New Mapping Regions (from your file): {upd['new_regions']}")
    if not updates:
        print("\nNo updates needed (all mapping regions already up to date).")
        return
    confirm = input("\nDo you want to proceed with these updates? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("❌ Update cancelled.")
        return
    for upd in updates:
        payload = build_zone_payload(upd['row'])
        payload['mapping'][0]['regions'] = upd['new_regions']
        payload['zone_id'] = upd['zone_id']   # REQUIRED for update!
        url = f"{API_BASE}/{upd['zone_id']}"
        status, resp = post_or_put(url, token, payload, "PUT")
        if status in (200,201):
            print(f"✅ Updated mapping regions for zone '{upd['slug']}' and store_id '{upd['store_id']}'")
        else:
            print(f"❌ Failed to update zone '{upd['slug']}' and store_id '{upd['store_id']}': {resp}")

def main():
    print("Welcome to the Delivery Zone Management Script.")
    print("\n1. Would you like to fetch the current delivery zones? (yes/no)")
    q1 = input().strip().lower()
    existing_zones = []
    token = None
    if q1 == "yes":
        cookie_string = get_cookie_string_gui().strip()
        token = get_bearer_token(cookie_string)
        existing_zones = get_zones(token)
    else:
        cookie_string = get_cookie_string_gui().strip()
        token = get_bearer_token(cookie_string)

    print("\n2. Would you like to create a new delivery zone? (yes/no)")
    q2 = input().strip().lower()
    if q2 == "yes":
        print("Please select your delivery zone CSV/XLSX file (a window will open)...")
        file_path = select_file()
        print("You selected:", file_path)
        if not file_path:
            print("❌ No file selected, skipping create.")
        else:
            df = pd.read_excel(file_path) if file_path.endswith('.xlsx') else pd.read_csv(file_path)
            create_zones_from_file(df, token, existing_zones)

    print("\n3. Would you like to update an existing delivery zone? (yes/no)")
    q3 = input().strip().lower()
    if q3 == "yes":
        print("Please select your delivery zone CSV/XLSX file for update (a window will open)...")
        file_path = select_file()
        print("You selected:", file_path)
        if not file_path:
            print("❌ No file selected, skipping update.")
        else:
            df = pd.read_excel(file_path) if file_path.endswith('.xlsx') else pd.read_csv(file_path)
            if not existing_zones:
                existing_zones = get_zones(token)
            update_zones_from_file(df, token, existing_zones)

if __name__ == "__main__":
    main()
