#!/usr/bin/env python3
"""Boston Rental Scanner — polls Craigslist RSS + Zillow API for matching rentals."""

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

MAX_RENT = 2500
MIN_BEDS = 1
ZILLOW_INTERVAL_HOURS = 24

CL_SEARCH_URL = (
    "https://boston.craigslist.org/search/apa"
    "?max_price=2500&min_bedrooms=1"
)
ZILLOW_API_URL = "https://www.searchapi.io/api/v1/search"

STATE_DIR = Path("state")
SEEN_FILE = STATE_DIR / "seen.json"
ZILLOW_LAST_RUN_FILE = STATE_DIR / "zillow_last_run.txt"

SEARCHAPI_KEY = os.environ.get("SEARCHAPI_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Neighborhoods ───────────────────────────────────────────────────────────

TIER1 = [
    "back bay", "fenway", "kenmore", "south end", "prudential",
    "copley", "hynes", "bay village",
]
TIER2 = [
    "brookline", "allston", "brighton", "longwood", "mission hill",
    "jamaica plain", "symphony", "roxbury crossing", "northeastern",
    "coolidge corner", "washington square", "cleveland circle",
    "chestnut hill",
]
TIER3 = [
    "cambridge", "somerville", "porter", "harvard", "central sq",
    "central square", "davis", "downtown", "dorchester", "charlestown",
    "medford", "kendall", "inman",
]

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
PET_NEGATIVE = ["no pets", "no cats", "no animals"]


# ── Utilities ───────────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    """Distance in miles between two coordinates."""
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


def should_run_zillow():
    if not SEARCHAPI_KEY:
        return False
    if not ZILLOW_LAST_RUN_FILE.exists():
        return True
    try:
        with open(ZILLOW_LAST_RUN_FILE) as f:
            last_run = float(f.read().strip())
        return (time.time() - last_run) >= ZILLOW_INTERVAL_HOURS * 3600
    except (ValueError, OSError):
        return True


def mark_zillow_run():
    STATE_DIR.mkdir(exist_ok=True)
    with open(ZILLOW_LAST_RUN_FILE, "w") as f:
        f.write(str(time.time()))


def extract_neighborhood(title):
    """Extract neighborhood from CL title: '$2100 / 1br - apt (Back Bay)'."""
    match = re.search(r"\(([^)]+)\)\s*$", title)
    return match.group(1) if match else ""


def strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text)
    return unescape(text).strip()


# ── Fetchers ────────────────────────────────────────────────────────────────

def fetch_craigslist():
    """Fetch CL listings by parsing the HTML search page + embedded JSON-LD."""
    listings = []
    try:
        resp = requests.get(
            CL_SEARCH_URL,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            },
            timeout=30,
        )
        resp.raise_for_status()
        html = resp.text

        # Parse JSON-LD for lat/lon and structured data
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
                    "locality": (item.get("address") or {}).get("addressLocality", ""),
                }

        # Parse HTML listing cards
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
            price = int(price_match.group(1).replace(",", "")) if price_match else None

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
                "sqft": None,
                "link": link,
                "lat": geo.get("lat"),
                "lon": geo.get("lon"),
                "thumbnail": None,
            })

    except Exception as e:
        print(f"[CL] Error: {e}")

    print(f"[CL] Fetched {len(listings)} listings")
    return listings


def fetch_zillow():
    if not should_run_zillow():
        print("[Zillow] Skipping (interval not reached)")
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

            # Build description from tags for keyword scoring
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
                "sqft": prop.get("sqft"),
                "link": prop.get("link", ""),
                "lat": prop.get("latitude"),
                "lon": prop.get("longitude"),
                "thumbnail": prop.get("thumbnail"),
                "building_name": prop.get("building_name"),
                "days_on_zillow": prop.get("days_on_zillow"),
            })

        mark_zillow_run()
        print(f"[Zillow] Fetched {len(listings)} listings")

    except Exception as e:
        print(f"[Zillow] Error: {e}")

    return listings


# ── Scoring ─────────────────────────────────────────────────────────────────

def score_listing(listing):
    """Returns (score, classification, reasons)."""
    score = 0
    reasons = []
    text = f"{listing['title']} {listing['description']} {listing['neighborhood']}".lower()

    # --- Location ---
    geo_scored = False
    if listing.get("lat") and listing.get("lon"):
        dist = haversine(TARGET_LAT, TARGET_LON, listing["lat"], listing["lon"])
        geo_scored = True
        if dist <= 0.5:
            score += 30
            reasons.append(f"{dist:.1f} mi — walking distance")
        elif dist <= 1.0:
            score += 25
            reasons.append(f"{dist:.1f} mi — close")
        elif dist <= 2.0:
            score += 15
            reasons.append(f"{dist:.1f} mi — nearby")
        elif dist <= 5.0:
            score += 5
            reasons.append(f"{dist:.1f} mi — moderate distance")
        else:
            reasons.append(f"{dist:.1f} mi — far")

    if not geo_scored:
        matched_tier = False
        for tier_kws, tier_score, tier_label in [
            (TIER1, 25, "Tier 1 — walking distance"),
            (TIER2, 15, "Tier 2 — Green Line"),
            (TIER3, 10, "Tier 3 — Red Line area"),
        ]:
            if any(kw in text for kw in tier_kws):
                score += tier_score
                reasons.append(tier_label)
                matched_tier = True
                break
        if not matched_tier:
            reasons.append("Unknown neighborhood")

    # --- Laundry ---
    if any(kw in text for kw in LAUNDRY_KEYWORDS):
        score += 15
        reasons.append("Laundry mentioned")

    # --- Pets ---
    if any(kw in text for kw in PET_POSITIVE):
        score += 10
        reasons.append("Cat/pet friendly")
    elif any(kw in text for kw in PET_NEGATIVE):
        score -= 20
        reasons.append("No pets allowed")

    # --- Classify ---
    if score >= 40:
        classification = "HOT"
    elif score >= 25:
        classification = "GOOD"
    elif score >= 10:
        classification = "MATCH"
    else:
        classification = "SKIP"

    return score, classification, reasons


# ── Notifications ───────────────────────────────────────────────────────────

def _tg_request(method, payload):
    """Make a Telegram Bot API request."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TG] Not configured — would send: {payload.get('text', payload.get('caption', ''))[:120]}...")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        resp = requests.post(url, json=payload, timeout=30)
        if resp.status_code != 200:
            print(f"[TG] Error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[TG] Send failed: {e}")


def _format_listing_line(listing, score, classification):
    """One-line summary for grouped messages."""
    price = f"${listing['price']:,}" if listing.get("price") else "?"
    beds = f"{listing['beds']}BR" if listing.get("beds") else ""
    label = listing["title"][:55]
    icon = "+" if classification == "GOOD" else "-"
    return f"  {icon} {price} {beds} — {label}"


def notify_hot(listing, score, reasons):
    """Individual Telegram message for a HOT match."""
    price_str = f"${listing['price']:,}/mo" if listing.get("price") else "Price N/A"
    beds_str = f"{listing['beds']}BR" if listing.get("beds") else ""
    baths_str = f" / {listing['baths']}BA" if listing.get("baths") else ""
    sqft_str = f" / {listing['sqft']} sqft" if listing.get("sqft") else ""

    text = (
        f"<b>HOT MATCH</b> (score {score})\n\n"
        f"<b>{listing['title']}</b>\n"
        f"{price_str}  {beds_str}{baths_str}{sqft_str}\n\n"
        + "\n".join(f"  - {r}" for r in reasons)
        + f"\n\n<a href=\"{listing['link']}\">View Listing</a>"
        f"  ({listing['source'].title()})"
    )

    if listing.get("thumbnail"):
        _tg_request("sendPhoto", {
            "chat_id": TELEGRAM_CHAT_ID,
            "photo": listing["thumbnail"],
            "caption": text,
            "parse_mode": "HTML",
        })
    else:
        _tg_request("sendMessage", {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        })
    time.sleep(1)


def notify_summary(matches):
    """Grouped Telegram summary for GOOD + MATCH listings."""
    if not matches:
        return

    lines = [f"<b>{len(matches)} New Listings</b>\n"]

    for listing, score, classification, reasons in matches:
        price = f"${listing['price']:,}" if listing.get("price") else "?"
        beds = f"{listing['beds']}BR" if listing.get("beds") else ""
        icon = "[GOOD]" if classification == "GOOD" else "[MATCH]"

        lines.append(
            f"<b>{icon}</b> {price} {beds} — {listing['title'][:60]}\n"
            f"  {' | '.join(reasons)}\n"
            f"  <a href=\"{listing['link']}\">View</a> ({listing['source']})\n"
        )

    message = "\n".join(lines)

    # Telegram 4096 char limit — split if needed
    if len(message) <= 4000:
        _tg_request("sendMessage", {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        return

    chunk_lines = []
    chunk_len = 0
    for line in lines:
        if chunk_len + len(line) > 3800 and chunk_lines:
            _tg_request("sendMessage", {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": "\n".join(chunk_lines),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            time.sleep(1)
            chunk_lines = []
            chunk_len = 0
        chunk_lines.append(line)
        chunk_len += len(line)

    if chunk_lines:
        _tg_request("sendMessage", {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": "\n".join(chunk_lines),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })


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

    # Score
    hot = []
    other = []

    for listing in new_listings:
        score, classification, reasons = score_listing(listing)
        seen[listing["id"]] = now.timestamp()

        if classification == "HOT":
            hot.append((listing, score, classification, reasons))
            print(f"  HOT  ({score}): {listing['title'][:70]}")
        elif classification in ("GOOD", "MATCH"):
            other.append((listing, score, classification, reasons))
            print(f"  {classification:5} ({score}): {listing['title'][:70]}")

    print(f"Results: {len(hot)} HOT, {len(other)} GOOD/MATCH, "
          f"{len(new_listings) - len(hot) - len(other)} skipped")

    # Notify
    for listing, score, classification, reasons in hot:
        notify_hot(listing, score, reasons)

    if not hot:
        print("No HOT matches — no notifications sent.")

    # Persist
    save_state(seen)
    print("Done.")


if __name__ == "__main__":
    main()
