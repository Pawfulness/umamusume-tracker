import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import requests
from bs4 import BeautifulSoup
import threading
import time
import json
import os
from datetime import datetime
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("umamusume-tracker")

app = FastAPI()

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for local network access
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global storage for events
events_cache = {
    "banners": [],
    "events": [],
    "upcoming_banners": [],
    "upcoming_events": [],
    "last_updated": None
}


def _format_dt(ts_seconds: int | None) -> str:
    if not ts_seconds:
        return ""
    try:
        dt = datetime.fromtimestamp(int(ts_seconds))
        # Day without leading zero, short month, 24h time
        return dt.strftime('%d %b %Y, %H:%M').lstrip('0')
    except Exception:
        return ""


def _parse_next_data(page_soup: BeautifulSoup) -> dict:
    script = page_soup.find('script', id='__NEXT_DATA__')
    if not script or not script.string:
        return {}
    try:
        obj = json.loads(script.string)
        return obj.get('props', {}).get('pageProps', {}) or {}
    except Exception:
        return {}


def _extract_event_banner_image_url(event_page_soup: BeautifulSoup) -> str:
    # Event pages typically include a banner image like:
    # /images/umamusume/events/2025/07_summer_days_graffiti_banner.png
    img = event_page_soup.find('img', src=lambda s: s and '/images/umamusume/events/' in s and s.endswith('_banner.png'))
    if not img:
        return ""
    src = img.get('src') or ""
    if not src:
        return ""
    if src.startswith('http'):
        return src
    return f"https://gametora.com{src}"


def fetch_story_events(limit: int = 5) -> tuple[list[dict], list[dict]]:
    """Returns (current_events, upcoming_events) for the EN site.

    GameTora's Story Event list is server-rendered enough to enumerate event URLs.
    Each event page contains eventData (start/end/name_en) in __NEXT_DATA__.
    """

    list_url = "https://gametora.com/umamusume/events/story-events"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    }

    try:
        resp = requests.get(list_url, headers=headers, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"Failed to fetch story events list: {e}")
        return [], []

    soup = BeautifulSoup(resp.content, 'html.parser')

    # Collect unique event slugs from links
    slugs: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all('a', href=True):
        href = a.get('href') or ""
        if not href.startswith('/umamusume/events/'):
            continue
        if href in ('/umamusume/events', '/umamusume/events/story-events'):
            continue
        slug = href.split('/umamusume/events/', 1)[1].split('?', 1)[0].strip('/')
        if not slug or slug in seen:
            continue
        seen.add(slug)
        slugs.append(slug)

    now_ts = int(time.time())
    current: list[dict] = []
    upcoming: list[dict] = []

    # Cap requests to avoid hammering the site.
    # The list is fairly complete; scanning the first ~120 is usually enough.
    for slug in slugs[:120]:
        page_url = f"https://gametora.com/umamusume/events/{slug}"
        try:
            ev_resp = requests.get(page_url, headers=headers, timeout=30)
            ev_resp.raise_for_status()
            ev_soup = BeautifulSoup(ev_resp.content, 'html.parser')
            pp = _parse_next_data(ev_soup)
            ev = pp.get('eventData') or {}
            if not isinstance(ev, dict):
                continue

            start = int(ev.get('start') or 0)
            end = int(ev.get('end') or 0)
            name = (ev.get('name_en') or "").strip() or (ev.get('name_jp') or "").strip() or slug.replace('-', ' ').title()
            image_url = _extract_event_banner_image_url(ev_soup)

            item = {
                "title": name,
                "subtitle": "",
                "url": page_url,
                "imageUrl": image_url,
            }

            if start and end and start <= now_ts <= end:
                item["subtitle"] = f"Ends {_format_dt(end)}"
                item["_sort"] = end
                current.append(item)
            elif start and start > now_ts:
                item["subtitle"] = f"Starts {_format_dt(start)}"
                item["_sort"] = start
                upcoming.append(item)

        except Exception:
            continue

    current.sort(key=lambda x: x.get('_sort') or 0)
    upcoming.sort(key=lambda x: x.get('_sort') or 0)

    # Remove sort helper keys
    for lst in (current, upcoming):
        for it in lst:
            it.pop('_sort', None)

    return current[:limit], upcoming[:limit]

def fetch_gametora_data():
    """Scrapes GameTora for current banners and events."""
    url = "https://gametora.com/umamusume"
    gacha_url = "https://gametora.com/umamusume/gacha"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    }
    
    try:
        logger.info(f"Fetching data from {url}...")
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        new_data = {
            "banners": [],
            "events": []
        }

        # IMPORTANT:
        # GameTora is a Next.js app. The server-rendered HTML defaults to JP, and switching to Global
        # happens client-side (JS). Since this service doesn't execute JS, we must read __NEXT_DATA__
        # and explicitly select the EN (Global) region.

        try:
            gacha_resp = requests.get(gacha_url, headers=headers, timeout=30)
            gacha_resp.raise_for_status()
            gacha_soup = BeautifulSoup(gacha_resp.content, 'html.parser')
            gacha_props = _parse_next_data(gacha_soup)

            region = "en"  # Global server / English

            char_cards = {c.get('id'): c for c in (gacha_props.get('charCardData', {}).get(region) or []) if isinstance(c, dict)}
            support_cards = {c.get('id'): c for c in (gacha_props.get('supportCardData', {}).get(region) or []) if isinstance(c, dict)}

            char_banners = (gacha_props.get('currentCharBanners', {}).get(region) or [])
            support_banners = (gacha_props.get('currentSupportBanners', {}).get(region) or [])

            def _pickup_names(pickups, cards_by_id):
                ids = []
                for p in pickups or []:
                    if isinstance(p, (list, tuple)) and p:
                        ids.append(p[0])
                names = []
                for pid in ids:
                    card = cards_by_id.get(pid) or {}
                    nm = card.get('name')
                    if nm:
                        names.append(nm)
                # keep unique order
                seen = set()
                uniq = []
                for n in names:
                    if n in seen:
                        continue
                    seen.add(n)
                    uniq.append(n)
                return uniq

            def _add_banner(banner_id: int, end_ts: int | None, kind: str, pickups, cards_by_id):
                names = _pickup_names(pickups, cards_by_id)
                title = kind
                if names:
                    title = f"{kind} — {' / '.join(names[:2])}"

                new_data["banners"].append({
                    "imageUrl": f"https://gametora.com/images/umamusume/gacha/img_bnr_gacha_{banner_id}.png",
                    "url": gacha_url,
                    "title": title,
                    "subtitle": f"Ends {_format_dt(end_ts)}" if end_ts else "",
                })

            for b in char_banners:
                if isinstance(b, dict) and b.get('id'):
                    _add_banner(int(b['id']), b.get('end'), "Character Gacha", b.get('pickups'), char_cards)

            for b in support_banners:
                if isinstance(b, dict) and b.get('id'):
                    _add_banner(int(b['id']), b.get('end'), "Support Card Gacha", b.get('pickups'), support_cards)

        except Exception as e:
            logger.warning(f"Failed to build EN/Global banners from gacha data: {e}")

        # Current + Upcoming story events (best-effort)
        current_events, upcoming_events = fetch_story_events(limit=5)
        new_data["events"] = current_events
        new_data["upcoming_events"] = upcoming_events
        new_data["upcoming_banners"] = []
        
        # Helper to parse sections
        def parse_section(header_text, target_list):
            header = soup.find(lambda tag: tag.name == "h2" and header_text in tag.text)
            if not header:
                logger.warning(f"Header '{header_text}' not found.")
                return

            # Iterate through siblings until the next header
            current_element = header.find_next_sibling()
            while current_element and current_element.name != "h2":
                if current_element.name == "a":
                    # Found a link, likely an image link
                    link = current_element.get('href')
                    if not link.startswith('http'):
                        link = f"https://gametora.com{link}"
                    
                    img = current_element.find('img')
                    image_url = ""
                    if img:
                        image_url = img.get('src')
                        if not image_url.startswith('http'):
                            image_url = f"https://gametora.com{image_url}"
                    
                    # The text usually follows the link or is inside a div nearby
                    # In the fetch output we saw "Ends 29 Dec 2025..."
                    # Let's look at the text content of the container or next sibling text node
                    
                    # GameTora structure is often: <a><img></a> TextNode <br>
                    end_time_text = ""
                    next_node = current_element.next_sibling
                    if next_node and isinstance(next_node, str):
                        end_time_text = next_node.strip()
                    
                    target_list.append({
                        "title": "Current Banner" if "Banner" in header_text else "Current Event", # Placeholder title
                        "image": image_url,
                        "link": link,
                        "time": end_time_text
                    })
                
                current_element = current_element.find_next_sibling()

        # NOTE: banners are now sourced from /umamusume/gacha __NEXT_DATA__ (EN region).

        # Parse Events
        # Try finding header first
        event_header = soup.find(lambda tag: tag.name == "h2" and "Current Mission Events" in tag.text)
        if event_header:
            container = event_header.find_next_sibling('div')
            if container:
                for item_div in container.find_all('div', recursive=False):
                    link_tag = item_div.find('a')
                    if not link_tag:
                        continue

                    link = link_tag.get('href')
                    if not link.startswith('http'):
                        link = f"https://gametora.com{link}"
                    
                    # Title is often in the link text or a sibling span/div
                    title = link_tag.get_text(strip=True)
                    
                    img = link_tag.find('img')
                    image_url = ""
                    if img:
                        image_url = img.get('src')
                        if not image_url.startswith('http'):
                            image_url = f"https://gametora.com{image_url}"
                            
                    # Date
                    text_div = item_div.find('div', class_=lambda x: x and 'text' in x)
                    time_text = ""
                    if text_div:
                        time_text = text_div.get_text(strip=True)
                    
                    if not title and time_text:
                        title = "Mission Event"

                    new_data["events"].append({
                        "title": title,
                        "imageUrl": image_url,
                        "url": link,
                        "subtitle": time_text
                    })
        else:
            # Fallback: Look for links with /missions in href that are not in the nav
            # This is a bit risky but better than nothing if header is missing
            pass

        global events_cache
        events_cache["banners"] = new_data["banners"]
        events_cache["events"] = new_data["events"]
        events_cache["upcoming_banners"] = new_data.get("upcoming_banners", [])
        events_cache["upcoming_events"] = new_data.get("upcoming_events", [])
        events_cache["last_updated"] = datetime.now().isoformat()
        logger.info(
            f"Updated cache: {len(new_data['banners'])} banners, {len(new_data['events'])} current events, "
            f"{len(new_data.get('upcoming_banners', []))} upcoming banners, {len(new_data.get('upcoming_events', []))} upcoming events"
        )
        
    except Exception as e:
        logger.error(f"Error fetching data: {e}")

def background_updater():
    """Updates data every hour."""
    while True:
        fetch_gametora_data()
        time.sleep(3600) # 1 hour

@app.on_event("startup")
def startup_event():
    # Start background updater
    thread = threading.Thread(target=background_updater, daemon=True)
    thread.start()
    
    # Register service
    register_service()

def register_service():
    """Registers this service with the home-page dashboard."""
    try:
        service_def = {
            "id": "umamusume-tracker",
            "name": "Umamusume Events",
            "description": "Global server banners and events",
            "url": "http://raspberrypi.local:8003",
            "apiUrl": "http://raspberrypi.local:8003/api/events",
            "type": "split-slide", # Use the new split-slide type we added for Fortnite
            "icon": "horse-head" # FontAwesome icon name (hope it exists or generic)
        }
        
        services_path = "/home/admin/home-page/data/services.json"
        if os.path.exists(services_path):
            with open(services_path, 'r') as f:
                services = json.load(f)
            
            # Update or add
            updated = False
            for i, service in enumerate(services):
                if service["id"] == service_def["id"]:
                    services[i] = service_def
                    services[i]["lastRegistered"] = datetime.now().isoformat()
                    updated = True
                    break
            
            if not updated:
                service_def["lastRegistered"] = datetime.now().isoformat()
                services.append(service_def)
            
            with open(services_path, 'w') as f:
                json.dump(services, f, indent=2)
            
            logger.info("Service registered successfully")
        else:
            logger.warning(f"Services file not found at {services_path}")
            
    except Exception as e:
        logger.error(f"Failed to register service: {e}")

@app.get("/api/events")
def get_events():
    return {
        "slides": [
            {
                "type": "split-slide",
                "title": "Current Banners",
                "subtitle": "Gacha",
                "items": events_cache["banners"],
                "rightTitle": "Current Events",
                "rightSubtitle": "Story",
                "rightItems": events_cache["events"],
            },
            {
                "type": "split-slide",
                "title": "Upcoming Banners",
                "subtitle": "Gacha",
                "items": events_cache.get("upcoming_banners", []),
                "rightTitle": "Upcoming Events",
                "rightSubtitle": "Story",
                "rightItems": events_cache.get("upcoming_events", []),
            }
        ],
        "last_updated": events_cache["last_updated"]
    }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8003)
