import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import requests
from bs4 import BeautifulSoup
import threading
import time
import json
import os
from datetime import datetime, timedelta, timezone
import logging
import re
from threading import Lock

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

_refresh_lock = Lock()
_refresh_in_progress = False


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
    # Prefer OG/Twitter image when present (more stable than CSS-rendered images).
    for sel in [
        lambda: event_page_soup.find('meta', attrs={'property': 'og:image'}),
        lambda: event_page_soup.find('meta', attrs={'name': 'twitter:image'}),
    ]:
        try:
            meta = sel()
        except Exception:
            meta = None
        if meta:
            content = (meta.get('content') or '').strip()
            if content and '/images/umamusume/events/' in content:
                if content.startswith('http'):
                    return content
                return f"https://gametora.com{content}"

    # Fallback: Event pages usually include a banner image like:
    # /images/umamusume/events/2025/07_summer_days_graffiti_banner.png
    img = event_page_soup.find('img', src=lambda s: s and '/images/umamusume/events/' in s)
    if not img:
        return ""
    src = (img.get('src') or '').strip()
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


def _parse_game8_utc_date_range(text: str) -> tuple[int | None, int | None, str]:
    """Parse Game8 banner availability strings.

    Examples:
      - "Dec 1 - Dec. 10, 2025"
      - "Dec. 28, 2025 - Jan. 7, 2026"
      - "Early January 2026" (estimate)

    Returns (start_ts, end_ts, label).
    Timestamps are seconds since epoch, interpreted as UTC.
    """
    raw = (text or "").strip()
    if not raw:
        return None, None, ""

    # Normalize punctuation and whitespace
    s = raw.replace(".", "").replace("–", "-")
    s = re.sub(r"\s+", " ", s)

    # Approximate formats: Early/Mid/Late Month YYYY
    m = re.match(r"^(Early|Mid|Late) ([A-Za-z]+) (\d{4})$", s)
    if m:
        when, month_name, year = m.group(1), m.group(2), int(m.group(3))
        # Use a deterministic estimate to allow sorting, but keep subtitle as estimate.
        day = {"Early": 5, "Mid": 15, "Late": 25}.get(when, 15)
        try:
            dt = datetime.strptime(f"{month_name} {day} {year}", "%B %d %Y")
        except ValueError:
            # Some pages use short month names
            dt = datetime.strptime(f"{month_name} {day} {year}", "%b %d %Y")
        # Treat as UTC
        ts = int(dt.replace(tzinfo=None).timestamp())
        return ts, None, raw

    # Range formats.
    # "Dec 1 - Dec 10, 2025" -> infer year on the left from trailing year.
    m = re.match(r"^([A-Za-z]+) (\d{1,2}) - ([A-Za-z]+) (\d{1,2}), (\d{4})$", s)
    if m:
        m1, d1, m2, d2, y = m.group(1), int(m.group(2)), m.group(3), int(m.group(4)), int(m.group(5))
        try:
            start_dt = datetime.strptime(f"{m1} {d1} {y}", "%b %d %Y")
        except ValueError:
            start_dt = datetime.strptime(f"{m1} {d1} {y}", "%B %d %Y")
        try:
            end_dt = datetime.strptime(f"{m2} {d2} {y}", "%b %d %Y")
        except ValueError:
            end_dt = datetime.strptime(f"{m2} {d2} {y}", "%B %d %Y")
        return int(start_dt.timestamp()), int(end_dt.timestamp()), raw

    # "Dec 28, 2025 - Jan 7, 2026"
    m = re.match(r"^([A-Za-z]+) (\d{1,2}), (\d{4}) - ([A-Za-z]+) (\d{1,2}), (\d{4})$", s)
    if m:
        m1, d1, y1, m2, d2, y2 = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4), int(m.group(5)), int(m.group(6))
        try:
            start_dt = datetime.strptime(f"{m1} {d1} {y1}", "%b %d %Y")
        except ValueError:
            start_dt = datetime.strptime(f"{m1} {d1} {y1}", "%B %d %Y")
        try:
            end_dt = datetime.strptime(f"{m2} {d2} {y2}", "%b %d %Y")
        except ValueError:
            end_dt = datetime.strptime(f"{m2} {d2} {y2}", "%B %d %Y")
        return int(start_dt.timestamp()), int(end_dt.timestamp()), raw

    return None, None, raw


def fetch_game8_upcoming_banners(limit: int = 5) -> list[dict]:
    """Scrape Game8's upcoming banner list (Global/EN oriented).

    Note: Game8 explicitly states parts of the schedule are estimates based on JP.
    We'll surface the dates as-is and treat only exact date ranges as hard.
    """
    url = "https://game8.co/games/Umamusume-Pretty-Derby/archives/537125"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    }

    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"Failed to fetch Game8 upcoming banners: {e}")
        return []

    soup = BeautifulSoup(resp.content, 'html.parser')

    # Prefer the first table under "December 2025 Banners" (exact UTC ranges),
    # then fall back to "Expected Banner Schedule" (estimated releases).
    tables: list[BeautifulSoup] = []
    for heading_text in ["December 2025 Banners", "Expected Banner Schedule"]:
        h = soup.find(lambda tag: tag.name in ("h2", "h3") and heading_text.lower() in tag.get_text(" ", strip=True).lower())
        if not h:
            continue
        # Next table after the heading
        t = h.find_next("table")
        if t:
            tables.append(t)

    rows: list[dict] = []
    now_ts = int(time.time())

    def _iter_table_rows(t):
        for tr in t.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            texts = [c.get_text(" ", strip=True) for c in cells]
            # Skip header rows
            if texts and texts[0].lower() in ("banner", "character"):
                continue
            yield texts

    for t in tables:
        for texts in _iter_table_rows(t):
            banner_text = ""
            avail_text = ""

            # Two column tables:
            #   Banner | Availability (UTC)
            if len(texts) >= 2 and any("availability" in x.lower() for x in texts[:2]):
                # It's the header row; already skipped above, but keep safe.
                continue

            if len(texts) == 2:
                banner_text, avail_text = texts[0], texts[1]
            # Three column table (Expected Banner Schedule):
            #   Character | Support Cards | Est. Release Date
            elif len(texts) >= 3:
                banner_text = texts[0]
                supports = texts[1]
                avail_text = texts[2]
                if supports:
                    banner_text = f"{banner_text} — {supports}"

            banner_text = (banner_text or "").strip()
            avail_text = (avail_text or "").strip()
            if not banner_text or not avail_text:
                continue

            start_ts, end_ts, label = _parse_game8_utc_date_range(avail_text)

            # Only show as "upcoming" if it is in the future.
            # Exact ranges that already ended are NOT upcoming.
            is_upcoming = False
            sort_ts = None
            if start_ts and start_ts > now_ts:
                is_upcoming = True
                sort_ts = start_ts
            elif start_ts and end_ts and start_ts <= now_ts <= end_ts:
                # currently running; keep it out of Upcoming to avoid duplication with GameTora current
                is_upcoming = False
            elif start_ts and end_ts and now_ts > end_ts:
                # ended in the past
                is_upcoming = False
            else:
                # Estimated entries (e.g., Early January 2026) should be shown as upcoming.
                # If we couldn't parse an actual date, still include it but sort after dated entries.
                if not start_ts and label and any(tok in label for tok in ("Early ", "Mid ", "Late ")):
                    is_upcoming = True
                    sort_ts = start_ts or (now_ts + 10**9)
                elif not start_ts and label and re.search(r"\b20\d{2}\b", label):
                    # Year-only-ish label; include as a last resort.
                    is_upcoming = True
                    sort_ts = start_ts or (now_ts + 10**9)

            if not is_upcoming:
                continue

            title = banner_text
            subtitle = f"{label} (UTC)" if label else ""
            rows.append({
                "title": title,
                "subtitle": subtitle,
                "url": url,
                "imageUrl": "",
                "_sort": sort_ts or (now_ts + 10**9),
            })

    rows.sort(key=lambda x: x.get("_sort") or 0)
    for r in rows:
        r.pop("_sort", None)

    # De-dupe by title
    seen = set()
    out = []
    for r in rows:
        key = (r.get("title") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= limit:
            break

    return out


def _abs_uma_moe_asset(path: str) -> str:
    p = (path or "").strip()
    if not p:
        return ""
    if p.startswith("http"):
        return p
    if not p.startswith("/"):
        p = "/" + p
    return "https://uma.moe" + p


def _parse_iso_z_to_ts(value: str) -> int | None:
    s = (value or "").strip()
    if not s:
        return None
    try:
        # Example: 2021-03-02T03:00:00.000Z
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return int(dt.timestamp())
    except Exception:
        return None


def _parse_uma_moe_human_dt_to_ts(value: str) -> int | None:
    s = (value or "").strip()
    if not s:
        return None
    # Example: "30 May 2025, 5:00" or "15 Sept 2022, 5:00"
    # uma.moe parses these via JS Date() and then normalizes to UTC date-only.
    # We follow that behavior: ignore the clock time and keep only the date.
    s = s.replace("Sept ", "Sep ")
    for fmt in ("%d %b %Y, %H:%M", "%d %B %Y, %H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
            dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
            return int(dt.replace(tzinfo=timezone.utc).timestamp())
        except Exception:
            continue
    return None


def _uma_moe_extract_utc_date(js: str, var_name: str) -> datetime | None:
    """Extract a constant like: ee=new Date(Date.UTC(2021,1,24))"""
    pattern = re.escape(var_name) + r"=new Date\(Date\.UTC\((\d+),(\d+),(\d+)\)\)"
    m = re.search(pattern, js)
    if not m:
        return None
    y, mo0, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return datetime(y, mo0 + 1, d, tzinfo=timezone.utc)


def _uma_moe_extract_number(js: str, var_name: str) -> float | None:
    # Example: me=1.6
    pattern = r"\b" + re.escape(var_name) + r"=([0-9]+(?:\.[0-9]+)?)"
    m = re.search(pattern, js)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def _uma_moe_extract_map(js: str, var_name: str) -> dict[str, datetime]:
    """Extract a Map literal like: Qt=new Map([["x",new Date(Date.UTC(...))], ...])"""
    out: dict[str, datetime] = {}
    # Matches: Qt=new Map([ ["k",new Date(Date.UTC(...))], ... ])
    # i.e. the whole thing ends with "]])".
    pattern = re.escape(var_name) + r"=new Map\(\[(.*?)\]\]\)"
    m = re.search(pattern, js, re.S)
    if not m:
        return out
    blob = m.group(1)
    for k, y, mo0, d, hh, mm, ss in re.findall(
        r'\["([^"]+)",new Date\(Date\.UTC\((\d+),(\d+),(\d+),(\d+),(\d+),(\d+)\)\)\]',
        blob,
    ):
        try:
            out[k] = datetime(int(y), int(mo0) + 1, int(d), int(hh), int(mm), int(ss), tzinfo=timezone.utc)
        except Exception:
            continue
    return out


def _uma_moe_days_between(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 86400)


def _uma_moe_calculate_recent_acceleration_rate(
    confirmed_pairs: list[dict],
    catchup_rate: float,
    multiplier: float = 1.0,
) -> float:
    # Ported from uma.moe chunk: calculateRecentAccelerationRate
    if len(confirmed_pairs) < 2:
        return catchup_rate

    pairs = sorted(confirmed_pairs, key=lambda p: p["global"].timestamp())
    last = pairs[-1]["global"]
    cutoff = last - timedelta(days=30)
    recent = [p for p in pairs if cutoff <= p["global"] <= last]
    window = recent if len(recent) >= 2 else pairs[-4:]
    if len(window) < 2:
        return catchup_rate

    jp_sum = 0
    gl_sum = 0
    for i in range(1, len(window)):
        jp_days = _uma_moe_days_between(window[i - 1]["jp"], window[i]["jp"])
        gl_days = _uma_moe_days_between(window[i - 1]["global"], window[i]["global"])
        if gl_days > 0:
            jp_sum += jp_days
            gl_sum += gl_days

    if gl_sum == 0:
        return catchup_rate

    rate = (jp_sum / gl_sum) * multiplier
    # clamp to [1.2, 2]
    return max(1.2, min(rate, 2.0))


def _uma_moe_calculate_global_date(
    jp_date: datetime,
    confirmed_pairs: list[dict],
    jp_launch: datetime,
    global_launch: datetime,
    catchup_rate: float,
) -> datetime:
    """Ported from uma.moe chunk: calculateGlobalDate + calculateGlobalDateWithFallback."""

    pairs = sorted(confirmed_pairs, key=lambda p: p["jp"].timestamp())
    if not pairs:
        # Fallback: global = globalLaunch + floor(daysSinceLaunch / catchupRate)
        days_since = _uma_moe_days_between(jp_launch, jp_date)
        days_global = int(days_since // catchup_rate)
        out = global_launch + timedelta(days=days_global)
        return out.replace(hour=22, minute=0, second=0, microsecond=0)

    prev_pair = None
    next_pair = None
    for p in pairs:
        if p["jp"].timestamp() <= jp_date.timestamp():
            prev_pair = p
        elif next_pair is None:
            next_pair = p
            break

    accel = _uma_moe_calculate_recent_acceleration_rate(pairs, catchup_rate, 1.0)

    if prev_pair and next_pair:
        jp_span = (next_pair["jp"] - prev_pair["jp"]).total_seconds()
        gl_span = (next_pair["global"] - prev_pair["global"]).total_seconds()
        jp_off = (jp_date - prev_pair["jp"]).total_seconds()
        ratio = (gl_span / jp_span) if jp_span else 0
        out = prev_pair["global"] + timedelta(seconds=(ratio * jp_off))
    elif prev_pair:
        days = _uma_moe_days_between(prev_pair["jp"], jp_date) / accel
        out = prev_pair["global"] + timedelta(days=days)
    elif next_pair:
        days = _uma_moe_days_between(jp_date, next_pair["jp"]) / accel
        out = next_pair["global"] - timedelta(days=days)
    else:
        days_since = _uma_moe_days_between(jp_launch, jp_date)
        days_global = int(days_since // catchup_rate)
        out = global_launch + timedelta(days=days_global)

    return out.replace(hour=22, minute=0, second=0, microsecond=0)


def _get_uma_moe_timeline_chunk_url() -> str:
    """Resolve the current Timeline JS chunk URL from the uma.moe timeline page."""
    headers = {"User-Agent": "Mozilla/5.0"}
    html = requests.get("https://uma.moe/timeline", headers=headers, timeout=30).text
    main_scripts = re.findall(r'<script[^>]+src="([^"]*main-[^"]+\.js)"', html, re.I)
    if not main_scripts:
        return ""
    main_src = main_scripts[0]
    if not main_src.startswith("http"):
        main_src = "https://uma.moe/" + main_src.lstrip("/")
    main_js = requests.get(main_src, headers=headers, timeout=30).text
    # Extract the TimelineComponent chunk import like: import("./chunk-XXXX.js")
    m = re.search(r'path:"timeline".*?import\("\./(chunk-[A-Z0-9]+\.js)"\)', main_js)
    if not m:
        return ""
    return "https://uma.moe/" + m.group(1)


def fetch_uma_moe_upcoming(limit_banners: int = 5, limit_events: int = 5) -> tuple[list[dict], list[dict]]:
    """Upcoming items from uma.moe timeline.

    uma.moe embeds JP timelines (story events, champions meetings, etc.) and
    calculates estimated *Global* dates client-side. This function ports the same
    mapping logic so we can list upcoming items relative to *Global* dates.
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        chunk_url = _get_uma_moe_timeline_chunk_url()
        if not chunk_url:
            return [], []
        js = requests.get(chunk_url, headers=headers, timeout=30).text
    except Exception as e:
        logger.warning(f"Failed to fetch uma.moe timeline chunk: {e}")
        return [], []

    now_ts = int(time.time())
    upcoming_banners: list[dict] = []
    upcoming_events: list[dict] = []

    def _normalize_event_title(s: str) -> str:
        t = (s or "").strip().lower()
        if t.startswith("champions meeting:"):
            t = t.split(":", 1)[1].strip()
        t = re.sub(r"[^a-z0-9]+", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        return t

    def _try_resolve_gametora_event_image(title: str) -> str:
        """Best-effort lookup for an event's banner image on GameTora.

        Used for upcoming items that come from uma.moe (and might not include an image).
        """
        want = _normalize_event_title(title)
        if not want:
            return ""

        try:
            idx_html = requests.get("https://gametora.com/umamusume/events", headers=headers, timeout=30).text
            idx_soup = BeautifulSoup(idx_html, 'html.parser')
        except Exception:
            return ""

        slugs: list[str] = []
        seen: set[str] = set()
        for a in idx_soup.find_all('a', href=True):
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

        # Cap work to avoid hammering the site. This runs on a daily schedule.
        for slug in slugs[:80]:
            page_url = f"https://gametora.com/umamusume/events/{slug}"
            try:
                ev_html = requests.get(page_url, headers=headers, timeout=30).text
                ev_soup = BeautifulSoup(ev_html, 'html.parser')
                pp = _parse_next_data(ev_soup)
                ev = pp.get('eventData') or {}
                if not isinstance(ev, dict):
                    continue
                name = (ev.get('name_en') or "").strip() or (ev.get('name_jp') or "").strip()
                got = _normalize_event_title(name)
                if not got:
                    continue
                if want in got or got in want:
                    img = _extract_event_banner_image_url(ev_soup)
                    if img:
                        return img
            except Exception:
                continue

        return ""

    def _get_gametora_champions_meeting_image() -> str:
        try:
            html = requests.get("https://gametora.com/umamusume/events/champions-meeting", headers=headers, timeout=30).text
            soup = BeautifulSoup(html, 'html.parser')
            img = soup.find('img', src=lambda s: s and '/images/umamusume/events/' in s)
            if not img:
                return ""
            src = (img.get('src') or '').strip()
            if not src:
                return ""
            if src.startswith('http'):
                return src
            return f"https://gametora.com{src}"
        except Exception:
            return ""

    jp_launch = _uma_moe_extract_utc_date(js, "ee")
    global_launch = _uma_moe_extract_utc_date(js, "_e")
    catchup_rate = _uma_moe_extract_number(js, "me")
    if not (jp_launch and global_launch and catchup_rate):
        return [], []

    story_confirmed = _uma_moe_extract_map(js, "Qt")
    champions_confirmed = _uma_moe_extract_map(js, "Xt")

    # --- Upcoming Story Events (compute Global start dates) ---
    story_m = re.search(r"var Vt=\[(.*?)\];", js, re.S)
    if story_m:
        story_blob = story_m.group(1)
        story_rows = re.findall(
            r'\{event_name:"(.*?)",image:"(.*?)",start_date:"(.*?)",end_date:"(.*?)"\}',
            story_blob,
            re.S,
        )

        story_items = []
        story_pairs = []
        for name, image, start_s, end_s in story_rows:
            start_ts = _parse_uma_moe_human_dt_to_ts(start_s)
            if not start_ts:
                continue
            jp_start = datetime.fromtimestamp(start_ts, tz=timezone.utc)
            story_items.append({"name": (name or "").strip(), "image": image.strip(), "jp_start": jp_start})

        story_items.sort(key=lambda x: x["jp_start"].timestamp())
        for it in story_items:
            global_dt = story_confirmed.get(it["image"])
            if global_dt:
                story_pairs.append({"jp": it["jp_start"], "global": global_dt})

        for it in story_items:
            global_dt = _uma_moe_calculate_global_date(it["jp_start"], story_pairs, jp_launch, global_launch, catchup_rate)
            global_ts = int(global_dt.timestamp())
            if global_ts < now_ts:
                continue
            upcoming_events.append({
                "title": it["name"] or "Story Event",
                "subtitle": f"Starts {_format_dt(global_ts)} (est)",
                "url": "https://uma.moe/timeline",
                "imageUrl": _abs_uma_moe_asset(f"assets/images/story/{it['image']}"),
                "_sort": global_ts,
            })

    # --- Upcoming Champions Meetings (compute Global start dates) ---
    cm_m = re.search(r"var jt=\[(.*?)\];", js, re.S)
    cm_image = ""
    if cm_m:
        cm_image = _get_gametora_champions_meeting_image()
        cm_blob = cm_m.group(1)
        cm_rows = re.findall(
            r'\{name:"(.*?)",start_date:"(.*?)",end_date:"(.*?)",track:"(.*?)",distance:"(.*?)",conditions:"(.*?)"\}',
            cm_blob,
            re.S,
        )
        cm_items = []
        for name, start_s, end_s, track, distance, conditions in cm_rows:
            start_ts = _parse_uma_moe_human_dt_to_ts(start_s)
            if not start_ts:
                continue
            jp_start = datetime.fromtimestamp(start_ts, tz=timezone.utc)
            cm_items.append({
                "name": (name or "").strip(),
                "track": (track or "").strip(),
                "distance": (distance or "").strip(),
                "conditions": (conditions or "").strip(),
                "jp_start": jp_start,
            })

        cm_items.sort(key=lambda x: x["jp_start"].timestamp())
        cm_pairs = []
        for idx, it in enumerate(cm_items):
            global_dt = champions_confirmed.get(f"champions_meeting_{idx}")
            if global_dt:
                cm_pairs.append({"jp": it["jp_start"], "global": global_dt})

        for it in cm_items:
            global_dt = _uma_moe_calculate_global_date(it["jp_start"], cm_pairs, jp_launch, global_launch, catchup_rate)
            global_ts = int(global_dt.timestamp())
            if global_ts < now_ts:
                continue
            upcoming_events.append({
                "title": f"Champions Meeting: {it['name']}" if it["name"] else "Champions Meeting",
                "subtitle": f"Starts {_format_dt(global_ts)} (est)",
                "url": "https://uma.moe/timeline",
                "imageUrl": cm_image,
                "_sort": global_ts,
            })

    # Best-effort: fill missing images (e.g., Champions Meeting) using GameTora.
    missing = [ev for ev in upcoming_events if not (ev.get('imageUrl') or '').strip()]
    for ev in missing[:5]:
        img = _try_resolve_gametora_event_image(ev.get('title') or '')
        if img:
            ev['imageUrl'] = img

    upcoming_banners.sort(key=lambda x: x.get('_sort') or 0)
    upcoming_events.sort(key=lambda x: x.get('_sort') or 0)
    for lst in (upcoming_banners, upcoming_events):
        for it in lst:
            it.pop('_sort', None)

    # De-dupe upcoming events by title
    seen_titles = set()
    deduped_events = []
    for ev in upcoming_events:
        key = (ev.get("title") or "").strip().lower()
        if not key or key in seen_titles:
            continue
        seen_titles.add(key)
        deduped_events.append(ev)
        if len(deduped_events) >= limit_events:
            break

    return upcoming_banners[:limit_banners], deduped_events

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

        # Upcoming banners: GameTora doesn't expose future banners in __NEXT_DATA__.
        # Use Game8 as a best-effort fallback source.
        upcoming_banners = fetch_game8_upcoming_banners(limit=5)

        # If we still have gaps (or for upcoming events), use uma.moe timeline as an additional estimate source.
        uma_banners, uma_events = fetch_uma_moe_upcoming(limit_banners=10, limit_events=10)

        # Fill upcoming events if GameTora couldn't provide any.
        if not new_data["upcoming_events"]:
            new_data["upcoming_events"] = uma_events[:5]

        # Extend upcoming banners with uma.moe if needed.
        if len(upcoming_banners) < 5 and uma_banners:
            want = 5 - len(upcoming_banners)
            seen = {((b.get('title') or '').strip().lower()) for b in upcoming_banners}
            for b in uma_banners:
                key = ((b.get('title') or '').strip().lower())
                if not key or key in seen:
                    continue
                upcoming_banners.append(b)
                seen.add(key)
                if len(upcoming_banners) >= 5:
                    break

        new_data["upcoming_banners"] = upcoming_banners
        
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

def _run_refresh_in_background() -> None:
    global _refresh_in_progress
    with _refresh_lock:
        if _refresh_in_progress:
            return
        _refresh_in_progress = True

    try:
        fetch_gametora_data()
    finally:
        with _refresh_lock:
            _refresh_in_progress = False


@app.on_event("startup")
def startup_event():
    # Do an initial refresh once after boot.
    threading.Thread(target=_run_refresh_in_background, daemon=True).start()

    # Register service
    register_service()


@app.post("/api/refresh")
def refresh_now():
    """Trigger a background refresh (used by systemd timer)."""
    threading.Thread(target=_run_refresh_in_background, daemon=True).start()
    return {
        "status": "scheduled",
        "in_progress": _refresh_in_progress,
        "last_updated": events_cache.get("last_updated"),
    }

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
    upcoming_banners = events_cache.get("upcoming_banners", []) or []
    upcoming_events = events_cache.get("upcoming_events", []) or []

    if not upcoming_banners:
        upcoming_banners = [{
            "title": "No upcoming banners found",
            "subtitle": "No upcoming data from available sources.",
            "url": "https://gametora.com/umamusume/gacha",
            "imageUrl": "",
        }]

    if not upcoming_events:
        upcoming_events = [{
            "title": "No upcoming events found",
            "subtitle": "No upcoming data from available sources.",
            "url": "https://gametora.com/umamusume/events",
            "imageUrl": "",
        }]

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
                "items": upcoming_banners,
                "rightTitle": "Upcoming Events",
                "rightSubtitle": "Story",
                "rightItems": upcoming_events,
            }
        ],
        "last_updated": events_cache["last_updated"]
    }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8003)
