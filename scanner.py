#!/usr/bin/env python3
"""Boston Rental Scanner — polls Craigslist for matching rentals."""

import json
import math
import os
import re
import time
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

import requests

# ── Config ──────────────────────────────────────────────────────────────────

TARGET_LAT = 42.3467  # 1001 Boylston St
TARGET_LON = -71.0872
WALK_DISTANCE_MI = 1.0  # max walking distance to work
STOP_RADIUS_MI = 0.3  # max distance from a Green Line stop

MAX_RENT = 2500
MIN_BEDS = 1

# Zillow — disabled for now, set ENABLE_ZILLOW=1 to turn on
ENABLE_ZILLOW = os.environ.get("ENABLE_ZILLOW", "") == "1"
ZILLOW_INTERVAL_HOURS = 24
ZILLOW_API_URL = "https://www.searchapi.io/api/v1/search"

CL_SEARCH_URL = (
    "https://boston.craigslist.org/search/apa"
    "?max_price=2500&min_bedrooms=1"
)

STATE_DIR = Path("state")
SEEN_FILE = STATE_DIR / "seen.json"
ZILLOW_LAST_RUN_FILE = STATE_DIR / "zillow_last_run.txt"

SEARCHAPI_KEY = os.environ.get("SEARCHAPI_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Green Line stops within ~15 min of Hynes ────────────────────────────────

GREEN_LINE_STOPS = [
    # Core (all branches)
    ("Copley", 42.3500, -71.0773),
    ("Hynes", 42.3479, -71.0840),
    ("Kenmore", 42.3489, -71.0952),
    # B Line
    ("BU East", 42.3494, -71.1001),
    ("BU Central", 42.3503, -71.1057),
    ("BU West", 42.3510, -71.1083),
    ("Babcock St", 42.3517, -71.1122),
    ("Packards Corner", 42.3515, -71.1166),
    ("Harvard Ave", 42.3503, -71.1313),
    ("Griggs St", 42.3488, -71.1351),
    # C Line
    ("St Marys", 42.3457, -71.1068),
    ("Hawes St", 42.3441, -71.1116),
    ("Kent St", 42.3421, -71.1145),
    ("Coolidge Corner", 42.3420, -71.1222),
    ("Summit Ave", 42.3410, -71.1269),
    ("Washington Sq", 42.3396, -71.1367),
    # D Line
    ("Fenway", 42.3453, -71.1040),
    ("Longwood D", 42.3418, -71.1105),
    ("Brookline Village", 42.3328, -71.1167),
    ("Brookline Hills", 42.3314, -71.1267),
    # E Line
    ("Prudential", 42.3462, -71.0820),
    ("Symphony", 42.3425, -71.0849),
    ("Northeastern", 42.3395, -71.0888),
    ("MFA", 42.3378, -71.0946),
    ("Longwood Medical", 42.3371, -71.1005),
    ("Brigham Circle", 42.3349, -71.1043),
]

# ── Keywords ────────────────────────────────────────────────────────────────

LAUNDRY_KEYWORDS = [
    "laundry", "w/d", "washer", "dryer", "in-unit", "in unit",
    "laundry in building", "laundry in bldg", "laundry on site",
    "washing machine", "coin laundry", "coin-op laundry",
]
PET_POSITIVE = [
    "cat friendly", "cats ok", "cats allowed", "cat ok",
    "pet friendly", "pet-friendly", "pets ok", "pets allowed",
    "cats welcome", "pets welcome", "small pets",
]
PET_NEGATIVE = [
    "no pets", "no cats", "no animals", "no pet policy", "no pet",
]
RED_FLAG_KEYWORDS = [
    ("garden level", "Garden/basement level"),
    ("basement", "Garden/basement level"),
    ("lower level", "Garden/basement level"),
    ("no living room", "No living room"),
    ("no lr", "No living room"),
    ("no separate living", "No living room"),
]


# ── Utilities ───────────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    R = 3959
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.asin(math.sqrt(a))


def nearest_green_line_stop(lat, lon):
    best_dist, best_name = 999, None
    for name, slat, slon in GREEN_LINE_STOPS:
        d = haversine(lat, lon, slat, slon)
        if d < best_dist:
            best_dist, best_name = d, name
    return best_name, best_dist


def load_state():
    STATE_DIR.mkdir(exist_ok=True)
    if SEEN_FILE.exists():
        try:
            with open(SEEN_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def save_state(seen):
    STATE_DIR.mkdir(exist_ok=True)
    cutoff = datetime.now(timezone.utc).timestamp() - (30 * 86400)
    pruned = {k: v for k, v in seen.items() if v > cutoff}
    with open(SEEN_FILE, "w") as f:
        json.dump(pruned, f, indent=2)


def strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text)
    return unescape(text).strip()


# ── Craigslist ──────────────────────────────────────────────────────────────

CL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}


def fetch_cl_detail(url):
    """Fetch full description + attributes from a CL listing page."""
    try:
        resp = requests.get(url, headers=CL_HEADERS, timeout=15)
        resp.raise_for_status()
        html = resp.text
        parts = []
        body_match = re.search(
            r'id="postingbody"[^>]*>(.*?)</section>', html, re.DOTALL
        )
        if body_match:
            parts.append(strip_html(body_match.group(1)))
        for attr in re.findall(r'class="attrgroup"[^>]*>(.*?)</p>', html, re.DOTALL):
            clean = strip_html(attr)
            if clean:
                parts.append(clean)
        return " ".join(parts)
    except Exception as e:
        print(f"[CL] Detail fetch failed for {url}: {e}")
        return ""


def fetch_craigslist():
    listings = []
    try:
        resp = requests.get(CL_SEARCH_URL, headers=CL_HEADERS, timeout=30)
        resp.raise_for_status()
        html = resp.text

        geo_data = {}
        ld_match = re.search(
            r'id="ld_searchpage_results"[^>]*>(.*?)</script>', html, re.DOTALL
        )
        if ld_match:
            ld_json = json.loads(ld_match.group(1))
            for i, entry in enumerate(ld_json.get("itemListElement", [])):
                item = entry.get("item", {})
                geo_data[i] = {
                    "lat": item.get("latitude"),
                    "lon": item.get("longitude"),
                    "beds": item.get("numberOfBedrooms"),
                    "baths": item.get("numberOfBathroomsTotal"),
                    "locality": (item.get("address") or {}).get(
                        "addressLocality", ""
                    ),
                }

        card_pattern = re.compile(
            r'<li\s+class="cl-static-search-result"[^>]*title="([^"]*)"[^>]*>\s*'
            r'<a\s+href="([^"]+)"[^>]*>.*?'
            r'<div\s+class="price">([^<]*)</div>.*?'
            r'<div\s+class="location">\s*(.*?)\s*</div>',
            re.DOTALL,
        )

        for i, m in enumerate(card_pattern.finditer(html)):
            title = unescape(m.group(1))
            link = m.group(2)
            price_text = m.group(3).strip()
            location = m.group(4).strip()

            price_match = re.search(r"\$([0-9,]+)", price_text)
            price = (
                int(price_match.group(1).replace(",", "")) if price_match else None
            )

            geo = geo_data.get(i, {})
            listing_id = re.search(r"/(\d+)\.html", link)
            lid = f"cl_{listing_id.group(1)}" if listing_id else f"cl_{link}"

            listings.append({
                "id": lid,
                "source": "craigslist",
                "title": title,
                "description": f"{title} {location}",
                "neighborhood": geo.get("locality") or location,
                "price": price,
                "beds": geo.get("beds"),
                "baths": geo.get("baths"),
                "link": link,
                "lat": geo.get("lat"),
                "lon": geo.get("lon"),
            })

    except Exception as e:
        print(f"[CL] Error: {e}")

    print(f"[CL] Fetched {len(listings)} listings")
    return listings


# ── Zillow (disabled by default) ───────────────────────────────────────────

def should_run_zillow():
    if not ENABLE_ZILLOW or not SEARCHAPI_KEY:
        return False
    if not ZILLOW_LAST_RUN_FILE.exists():
        return True
    try:
        with open(ZILLOW_LAST_RUN_FILE) as f:
            last_run = float(f.read().strip())
        return (time.time() - last_run) >= ZILLOW_INTERVAL_HOURS * 3600
    except (ValueError, OSError):
        return True


def fetch_zillow():
    if not should_run_zillow():
        print("[Zillow] Disabled or interval not reached")
        return []

    listings = []
    try:
        resp = requests.get(
            ZILLOW_API_URL,
            params={
                "engine": "zillow",
                "q": "Boston, MA",
                "listing_status": "for_rent",
                "rent_max": MAX_RENT,
                "beds_min": MIN_BEDS,
                "sort_by": "newest",
                "days_on_zillow": "1",
                "api_key": SEARCHAPI_KEY,
            },
            timeout=90,
        )
        resp.raise_for_status()
        data = resp.json()

        if "error" in data and not data.get("properties"):
            print(f"[Zillow] API error: {data['error']}")
            return []

        for prop in data.get("properties", []):
            zpid = prop.get("zpid", "")
            price = prop.get("extracted_price") or prop.get("min_base_rent")
            tag_texts = []
            if prop.get("tag"):
                tag_texts.append(prop["tag"].get("text", ""))
            for t in prop.get("tags", []):
                tag_texts.append(t.get("text", ""))
            description = " ".join(
                filter(None, [prop.get("status_text", "")] + tag_texts)
            )
            listings.append({
                "id": f"zillow_{zpid}",
                "source": "zillow",
                "title": prop.get("address", ""),
                "description": description,
                "neighborhood": prop.get("address", ""),
                "price": price,
                "beds": prop.get("beds"),
                "baths": prop.get("baths"),
                "link": prop.get("link", ""),
                "lat": prop.get("latitude"),
                "lon": prop.get("longitude"),
            })

        STATE_DIR.mkdir(exist_ok=True)
        with open(ZILLOW_LAST_RUN_FILE, "w") as f:
            f.write(str(time.time()))
        print(f"[Zillow] Fetched {len(listings)} listings")

    except Exception as e:
        print(f"[Zillow] Error: {e}")

    return listings


# ── Filter ──────────────────────────────────────────────────────────────────

def check_listing(listing):
    """Pass/fail filter. Returns (pass, location_info, red_flags) or (False, ..., ...)."""
    text = f"{listing['title']} {listing['description']} {listing['neighborhood']}".lower()

    # Must have coordinates for geo filtering
    if not listing.get("lat") or not listing.get("lon"):
        return False, "", []

    # ── Hard exclusions ──

    # No studios
    beds = listing.get("beds")
    if beds is not None and beds < 1:
        return False, "", []
    if any(kw in text for kw in ["studio", "0br"]):
        return False, "", []

    # No sublets
    if any(kw in text for kw in [
        "sublet", "sub-let", "sublease", "sub-lease",
        "short term", "short-term", "temporary",
    ]):
        return False, "", []
    if re.search(
        r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d+\S*"
        r"\s*[-\u2013\u2014]\s*"
        r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d+",
        text,
    ):
        return False, "", []

    # No explicit pet bans (unless cats specifically allowed)
    has_pet_pos = any(kw in text for kw in PET_POSITIVE)
    has_pet_neg = any(kw in text for kw in PET_NEGATIVE)
    if has_pet_neg and not has_pet_pos:
        return False, "", []

    # Must mention laundry
    if not any(kw in text for kw in LAUNDRY_KEYWORDS):
        return False, "", []

    # ── Location: walking distance OR near a Green Line stop ──

    dist_work = haversine(TARGET_LAT, TARGET_LON, listing["lat"], listing["lon"])
    stop_name, stop_dist = nearest_green_line_stop(listing["lat"], listing["lon"])

    walking = dist_work <= WALK_DISTANCE_MI
    near_green = stop_dist <= STOP_RADIUS_MI

    if not walking and not near_green:
        return False, "", []

    if walking:
        location_info = f"{dist_work:.1f} mi to work (walking distance)"
    else:
        location_info = f"{dist_work:.1f} mi to work | {stop_dist:.1f} mi to {stop_name}"

    # ── Red flags (auto-exclude) ──

    red_flags = []
    seen_flags = set()
    for keyword, flag_label in RED_FLAG_KEYWORDS:
        if keyword in text and flag_label not in seen_flags:
            red_flags.append(flag_label)
            seen_flags.add(flag_label)

    if red_flags:
        return False, location_info, red_flags

    return True, location_info, []


# ── Notifications ───────────────────────────────────────────────────────────

def _tg_request(method, payload):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TG] Not configured — would send: "
              f"{payload.get('text', payload.get('caption', ''))[:120]}...")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        resp = requests.post(url, json=payload, timeout=30)
        if resp.status_code != 200:
            print(f"[TG] Error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[TG] Send failed: {e}")


def notify_match(listing, location_info):
    price_str = f"${listing['price']:,}/mo" if listing.get("price") else "Price N/A"
    beds_str = f"{listing['beds']}BR" if listing.get("beds") else ""
    baths_str = f" / {listing['baths']}BA" if listing.get("baths") else ""

    text = (
        f"<b>New Match</b>\n\n"
        f"<b>{listing['title']}</b>\n"
        f"{price_str}  {beds_str}{baths_str}\n"
        f"{location_info}\n\n"
        f"<a href=\"{listing['link']}\">View Listing</a>"
    )

    _tg_request("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    })
    time.sleep(1)


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now(timezone.utc)
    print(f"=== Boston Rental Scanner — {now.isoformat()} ===")

    seen = load_state()
    print(f"State: {len(seen)} previously seen listings")

    # Fetch
    all_listings = fetch_craigslist() + fetch_zillow()

    # Deduplicate
    new_listings = [l for l in all_listings if l["id"] not in seen]
    print(f"New: {len(new_listings)} of {len(all_listings)} total")

    # Quick location pre-check to decide which listings to enrich
    to_enrich = []
    for listing in new_listings:
        if not listing.get("lat") or not listing.get("lon"):
            continue
        dist_work = haversine(TARGET_LAT, TARGET_LON, listing["lat"], listing["lon"])
        _, stop_dist = nearest_green_line_stop(listing["lat"], listing["lon"])
        if dist_work <= WALK_DISTANCE_MI or stop_dist <= STOP_RADIUS_MI:
            to_enrich.append(listing)

    # Enrich with full CL descriptions
    enriched = 0
    for listing in to_enrich:
        if listing["source"] == "craigslist" and listing.get("link"):
            detail = fetch_cl_detail(listing["link"])
            if detail:
                listing["description"] = f"{listing['description']} {detail}"
                enriched += 1
                time.sleep(1)
    if enriched:
        print(f"[CL] Enriched {enriched} listings with full descriptions")

    # Filter
    matches = []
    for listing in new_listings:
        seen[listing["id"]] = now.timestamp()
        passed, location_info, red_flags = check_listing(listing)
        if passed:
            matches.append((listing, location_info))
            print(f"  MATCH: {listing['title'][:65]}")
        elif red_flags:
            print(f"  SKIP (red flag: {', '.join(red_flags)}): {listing['title'][:50]}")

    print(f"Results: {len(matches)} matches of {len(new_listings)} new listings")

    # Notify
    for listing, location_info in matches:
        notify_match(listing, location_info)

    if not matches:
        print("No matches — no notifications sent.")

    save_state(seen)
    print("Done.")


if __name__ == "__main__":
    main()
