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
    "last_updated": None
}

def fetch_gametora_data():
    """Scrapes GameTora for current banners and events."""
    url = "https://gametora.com/umamusume"
    gacha_url = "https://gametora.com/umamusume/gacha"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Cookie": "umamusume_server=gl"  # Force Global server
    }
    
    try:
        logger.info(f"Fetching data from {url}...")
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')

        # The /umamusume landing page doesn't include human-readable banner names.
        # We build a best-effort map from the /umamusume/gacha listing instead.
        gacha_title_by_image: dict[str, str] = {}
        try:
            gacha_resp = requests.get(gacha_url, headers=headers, timeout=30)
            gacha_resp.raise_for_status()
            gacha_soup = BeautifulSoup(gacha_resp.content, 'html.parser')

            for img in gacha_soup.find_all('img'):
                src = img.get('src') or ''
                if '/images/umamusume/gacha/img_bnr_gacha_' not in src:
                    continue

                image_abs = src
                if not image_abs.startswith('http'):
                    image_abs = f"https://gametora.com{image_abs}"

                # Find the nearest card container and extract the label (usually "Character Gacha" or "Support Card Gacha").
                card = img.find_parent('div')
                title_text = ""
                if card:
                    # Prefer a short label ending with "Gacha".
                    label = card.find(lambda t: t.name == 'div' and (t.get_text(strip=True) or '').endswith('Gacha'))
                    if label:
                        title_text = label.get_text(' ', strip=True)
                    else:
                        # Fallback: take the first non-empty text chunk within the card.
                        for t in card.find_all(['div', 'span'], recursive=True):
                            txt = t.get_text(' ', strip=True)
                            if txt:
                                title_text = txt
                                break

                if title_text:
                    gacha_title_by_image[image_abs] = title_text
        except Exception as e:
            logger.warning(f"Failed to fetch gacha listing for banner titles: {e}")
        
        new_data = {
            "banners": [],
            "events": []
        }
        
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

        # Parse Banners
        banner_header = soup.find(lambda tag: tag.name == "h2" and "Current Gacha Banners" in tag.text)
        if banner_header:
            # The structure is h2 + div > div > (a + div)
            container = banner_header.find_next_sibling('div')
            if container:
                for item_div in container.find_all('div', recursive=False):
                    # Link and Image
                    link_tag = item_div.find('a')
                    if not link_tag:
                        continue
                        
                    link = link_tag.get('href')
                    if not link.startswith('http'):
                        link = f"https://gametora.com{link}"
                    
                    img = link_tag.find('img')
                    image_url = ""
                    if img:
                        image_url = img.get('src')
                        if not image_url.startswith('http'):
                            image_url = f"https://gametora.com{image_url}"
                    
                    # Date/Text
                    text_div = item_div.find('div', class_=lambda x: x and 'text' in x)
                    text_content = ""
                    if text_div:
                        text_content = text_div.get_text(strip=True)
                    
                    if image_url:
                        banner_title = gacha_title_by_image.get(image_url) or "Gacha Banner"
                        new_data["banners"].append({
                            "imageUrl": image_url,
                            "url": link,
                            "title": banner_title,
                            "subtitle": text_content
                        })

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
        events_cache["last_updated"] = datetime.now().isoformat()
        logger.info(f"Updated cache: {len(new_data['banners'])} banners, {len(new_data['events'])} events")
        
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
                "rightTitle": "Mission Events",
                "rightSubtitle": "Limited Time",
                "rightItems": events_cache["events"]
            }
        ],
        "last_updated": events_cache["last_updated"]
    }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8003)
