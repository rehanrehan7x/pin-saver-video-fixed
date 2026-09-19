"""
Core logic for finding and downloading the original photo or video behind a
Pinterest pin. Adapted from pinterest_downloader.py, reworked so a web app can
call it (no printing, structured errors, safer URL handling).
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Only talk to Pinterest's own domains (www.pinterest.com, in.pinterest.com,
# pinterest.co.uk, ...), its short-link domain, and its image/video CDN.
PINTEREST_HOST = re.compile(r"^(?:[a-z0-9-]+\.)*pinterest\.[a-z]{2,3}(?:\.[a-z]{2})?$", re.I)
SHORT_HOST = "pin.it"
CDN_HOST = re.compile(r"^(?:[a-z0-9-]+\.)*pinimg\.com$", re.I)
PIN_ID = re.compile(r"/pin/(?:[^/?#]*?--)?(\d+)")

CACHE_TTL_SECONDS = 600

log = logging.getLogger("pinsaver")


class PinError(Exception):
    """An error with a message that is safe to show to the user."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class PinInfo:
    pin_id: str
    url: str
    kind: str  # "image" or "video"
    title: str
    thumbnail: str | None
    filename: str
    image_urls: list[str] = field(default_factory=list)
    video_urls: list[str] = field(default_factory=list)  # direct .mp4 files, best first
    hls_urls: list[str] = field(default_factory=list)    # .m3u8 streams (fallback)

    def public(self) -> dict:
        """The fields the browser needs (no internal candidate lists)."""
        d = asdict(self)
        for internal in ("image_urls", "video_urls", "hls_urls"):
            d.pop(internal)
        return d


# --------------------------------------------------------------------------
# URL handling
# --------------------------------------------------------------------------

def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _is_pinterest(url: str) -> bool:
    host = _host(url)
    return bool(PINTEREST_HOST.match(host)) or host == SHORT_HOST


def _follow_short_link(url: str) -> str:
    """Resolve pin.it links, checking every redirect hop stays on Pinterest."""
    current = url
    for _ in range(5):
        try:
            resp = requests.get(current, headers=HEADERS, allow_redirects=False,
                                timeout=15, stream=True)
            resp.close()
        except requests.RequestException:
            raise PinError("Couldn't open that short link. Check your connection and try again.", 502)
        location = resp.headers.get("Location")
        if 300 <= resp.status_code < 400 and location:
            nxt = urljoin(current, location)
            if not _is_pinterest(nxt):
                raise PinError("That short link doesn't lead to a Pinterest pin.")
            current = nxt
            if _host(current) != SHORT_HOST and PIN_ID.search(urlparse(current).path):
                return current
            continue
        return current
    raise PinError("That short link redirects too many times.")


def resolve_pin_url(raw: str) -> tuple[str, str]:
    """Validate user input. Returns (canonical_pin_url, pin_id)."""
    raw = (raw or "").strip()
    if not raw:
        raise PinError("Paste a Pinterest pin link.")
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    if not _is_pinterest(raw):
        raise PinError("That isn't a Pinterest link.")

    url = _follow_short_link(raw) if _host(raw) == SHORT_HOST else raw
    match = PIN_ID.search(urlparse(url).path)
    if not match:
        raise PinError("That link doesn't point to a single pin. Open the pin and copy its address.")
    pin_id = match.group(1)
    return f"https://www.pinterest.com/pin/{pin_id}/", pin_id


def upscale_image_url(url: str) -> str:
    """Rewrite Pinterest's resized path segment (/236x/, /736x/ ...) to /originals/."""
    if not CDN_HOST.match(_host(url)):
        return url
    return re.sub(r"/\d+x(?:\d+)?/", "/originals/", url, count=1)


# --------------------------------------------------------------------------
# Page parsing
# --------------------------------------------------------------------------

def fetch_html(url: str) -> str:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
    except requests.Timeout:
        raise PinError("Pinterest took too long to respond. Try again in a moment.", 504)
    except requests.RequestException:
        raise PinError("Couldn't reach Pinterest. Check your internet connection.", 502)
    if resp.status_code == 404:
        raise PinError("Pin not found. It may have been deleted or made private.", 404)
    if resp.status_code in (403, 429):
        raise PinError("Pinterest is limiting requests right now. Wait a minute and try again.", 429)
    if not resp.ok:
        raise PinError(f"Pinterest returned an error ({resp.status_code}).", 502)
    return resp.text


def _json_blobs(soup: BeautifulSoup):
    """Yield every JSON blob embedded in the page's <script> tags."""
    for tag in soup.find_all("script"):
        looks_like_json = (
            tag.get("type") == "application/json"
            or tag.get("id") in ("__PWS_DATA__", "initial-state")
        )
        if not looks_like_json:
            continue
        text = tag.string or tag.get_text()
        if not text:
            continue
        try:
            yield json.loads(text)
        except ValueError:
            continue


def find_pin_objects(data, pin_id: str) -> list[dict]:
    """
    Find every dict describing *this* pin (matching id, with media fields).

    The page JSON also contains related pins, so we must match on the pin id
    rather than grabbing the first URL we see.
    """
    found: list[dict] = []
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if str(cur.get("id")) == pin_id and (
                cur.get("images") or cur.get("videos") or cur.get("story_pin_data")
            ):
                found.append(cur)
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return found


def _og(soup: BeautifulSoup, prop: str) -> str | None:
    tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
    return tag.get("content") if tag else None


def _area(entry: dict) -> int:
    return (entry.get("width") or 0) * (entry.get("height") or 0)


def _image_candidates(pin: dict) -> list[str]:
    images = pin.get("images") or {}
    if not isinstance(images, dict):
        return []
    urls: list[str] = []
    orig = images.get("orig")
    if isinstance(orig, dict) and orig.get("url"):
        urls.append(orig["url"])
    others = sorted(
        (v for k, v in images.items() if k != "orig" and isinstance(v, dict) and v.get("url")),
        key=_area,
        reverse=True,
    )
    urls.extend(v["url"] for v in others)
    return urls


def _walk_video_lists(node):
    """Yield every {"video_list": {...}} payload nested inside node."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            video_list = cur.get("video_list")
            if isinstance(video_list, dict):
                yield video_list
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _video_sources(pin: dict) -> tuple[list[str], list[str]]:
    """Return (mp4_urls, m3u8_urls) for a pin, best quality first."""
    mp4s: list[dict] = []
    streams: list[dict] = []
    # "videos" is a normal video pin; "story_pin_data" holds Idea/Story pin videos.
    for source in (pin.get("videos"), pin.get("story_pin_data")):
        if not source:
            continue
        for video_list in _walk_video_lists(source):
            for entry in video_list.values():
                if not isinstance(entry, dict):
                    continue
                url = entry.get("url")
                if not isinstance(url, str) or not url.startswith("http"):
                    continue
                path = url.lower().split("?")[0]
                if path.endswith(".mp4"):
                    mp4s.append(entry)
                elif path.endswith(".m3u8"):
                    streams.append(entry)
    mp4s.sort(key=_area, reverse=True)
    streams.sort(key=_area, reverse=True)
    return [e["url"] for e in mp4s], [e["url"] for e in streams]


def _mp4_from_hls(url: str) -> str | None:
    """
    Pinterest often lists only the .m3u8 stream, but the matching plain .mp4
    lives at the same path under /720p/. Worth a try before slower fallbacks.
    """
    guess = re.sub(r"\.m3u8(?:\?.*)?$", ".mp4", url.replace("/hls/", "/720p/"))
    return guess if guess != url and guess.endswith(".mp4") else None



def _html_media_urls(html: str) -> tuple[list[str], list[str]]:
    """Find Pinterest CDN video URLs even when Pinterest's JSON is not exposed."""
    mp4s: list[str] = []
    streams: list[str] = []
    # Pinterest commonly serves video from v1.pinimg.com or i.pinimg.com.
    pattern = re.compile(
        r'https?://[^"\'<>\s]+?\.(?:mp4|m3u8)(?:\?[^"\'<>\s]*)?',
        re.I,
    )
    for raw in pattern.findall(html):
        url = raw.replace("\\/", "/").replace("\\u0026", "&")
        if not CDN_HOST.match(_host(url)):
            continue
        path = urlparse(url).path.lower()
        if path.endswith(".mp4"):
            mp4s.append(url)
        elif path.endswith(".m3u8"):
            streams.append(url)
    return _dedupe(mp4s), _dedupe(streams)

def _dedupe(items) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def fetch_pin_api(pin_id: str) -> dict | None:
    """
    Ask Pinterest's own JSON endpoint about this pin. It reliably includes the
    video list even when the HTML page doesn't. Returns None on any failure.
    """
    options = {"options": {"id": pin_id, "field_set_key": "detailed", "noCache": True}, "context": {}}
    headers = {
        **HEADERS,
        "Accept": "application/json, text/javascript, */*, q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "X-Pinterest-AppState": "active",
        "Referer": f"https://www.pinterest.com/pin/{pin_id}/",
    }
    try:
        resp = requests.get(
            "https://www.pinterest.com/resource/PinResource/get/",
            params={"source_url": f"/pin/{pin_id}/", "data": json.dumps(options)},
            headers=headers,
            timeout=20,
        )
        if not resp.ok:
            log.info("pin %s: API returned HTTP %s", pin_id, resp.status_code)
            return None
        data = resp.json()["resource_response"]["data"]
    except Exception:  # noqa: BLE001 - this lookup is a bonus; it must never break the page parse
        log.info("pin %s: API lookup failed", pin_id)
        return None
    return data if isinstance(data, dict) else None


def _collect_media(pins: list[dict]):
    """Gather image / mp4 / hls candidates from every dict describing the pin."""
    images: list[str] = []
    mp4s: list[str] = []
    streams: list[str] = []
    thumb = None
    for pin in pins:
        images.extend(_image_candidates(pin))
        m, h = _video_sources(pin)
        mp4s.extend(m)
        streams.extend(h)
        sizes = pin.get("images") if isinstance(pin.get("images"), dict) else {}
        for key in ("736x", "474x"):
            if not thumb and isinstance(sizes.get(key), dict) and sizes[key].get("url"):
                thumb = sizes[key]["url"]
    # Real originals first, then the largest remaining sizes.
    images = sorted(_dedupe(images), key=lambda u: 0 if "/originals/" in u else 1)
    return images, _dedupe(mp4s), _dedupe(streams), thumb


_cache: dict[str, tuple[float, PinInfo]] = {}
_cache_lock = threading.Lock()


def get_pin_info(raw_url: str) -> PinInfo:
    """Look up a pin and work out where its original media lives."""
    url, pin_id = resolve_pin_url(raw_url)

    with _cache_lock:
        hit = _cache.get(pin_id)
        if hit and time.time() - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]

    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")

    pins: list[dict] = []
    for blob in _json_blobs(soup):
        pins.extend(find_pin_objects(blob, pin_id))

    og_image = _og(soup, "og:image")
    og_video = _og(soup, "og:video") or _og(soup, "og:video:url")

    images, mp4s, streams, thumb = _collect_media(pins)

    # Pinterest sometimes renders media into JavaScript without putting it in
    # application/json script tags. Scan the raw HTML as a second extraction
    # path so video pins are not incorrectly treated as photos.
    html_mp4s, html_streams = _html_media_urls(html)
    mp4s = _dedupe(mp4s + html_mp4s)
    streams = _dedupe(streams + html_streams)

    if og_video:
        path = og_video.lower().split("?")[0]
        if path.endswith(".mp4"):
            mp4s = _dedupe(mp4s + [og_video])
        elif path.endswith(".m3u8"):
            streams = _dedupe(streams + [og_video])

    html_saw_video = bool(mp4s or streams or og_video)
    used_api = False
    if not html_saw_video:
        # The page didn't reveal a video. Double-check with Pinterest's JSON
        # endpoint so video pins aren't mistaken for photos.
        api_pin = fetch_pin_api(pin_id)
        if api_pin:
            used_api = True
            api_images, api_mp4s, api_streams, api_thumb = _collect_media([api_pin])
            images = _dedupe(images + api_images)
            mp4s = _dedupe(mp4s + api_mp4s)
            streams = _dedupe(streams + api_streams)
            thumb = thumb or api_thumb

    is_video = bool(mp4s or streams or og_video)

    if og_image:
        for candidate in (upscale_image_url(og_image), og_image):
            if candidate not in images:
                images.append(candidate)

    # Plain .mp4 twins of the HLS streams (no ffmpeg needed) go after the real ones.
    video_urls = _dedupe(mp4s + [g for g in map(_mp4_from_hls, streams) if g])

    log.info(
        "pin %s: page_pins=%d api=%s og_video=%s mp4=%d hls=%d images=%d -> %s",
        pin_id, len(pins), used_api, bool(og_video), len(mp4s), len(streams),
        len(images), "VIDEO" if is_video else "photo",
    )

    if not is_video and not images:
        raise PinError(
            "Couldn't find an image or video on this pin. It may be private, "
            "or Pinterest may have changed its page layout.",
            422,
        )

    thumb = thumb or og_image or (images[0] if images else None)
    title = (
        _og(soup, "og:title")
        or next((p.get("grid_title") or p.get("title") for p in pins if p.get("grid_title") or p.get("title")), None)
        or f"Pin {pin_id}"
    ).strip()

    if is_video:
        ext = ".mp4"
    else:
        ext = os.path.splitext(urlparse(images[0]).path)[1] or ".jpg"

    info = PinInfo(
        pin_id=pin_id,
        url=url,
        kind="video" if is_video else "image",
        title=title[:200],
        thumbnail=thumb,
        filename=f"{pin_id}{ext}",
        image_urls=images,
        video_urls=video_urls,
        hls_urls=streams,
    )
    with _cache_lock:
        _cache[pin_id] = (time.time(), info)
    return info


# --------------------------------------------------------------------------
# Downloading
# --------------------------------------------------------------------------

_CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "video/mp4": ".mp4",
}


def _download_direct(url: str, out_dir: str, base: str) -> str:
    """Stream a file from Pinterest's CDN into out_dir. Raises requests errors."""
    if not CDN_HOST.match(_host(url)):
        raise PinError("Unexpected media host.", 502)
    with requests.get(url, headers=HEADERS, stream=True, timeout=30) as resp:
        resp.raise_for_status()
        ext = os.path.splitext(urlparse(url).path)[1]
        if not ext:
            ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            ext = _CONTENT_TYPE_EXT.get(ctype, ".jpg")
        dest = os.path.join(out_dir, f"{base}{ext}")
        with open(dest + ".part", "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)
    os.replace(dest + ".part", dest)
    return dest


def _download_with_ytdlp(pin_url: str, out_dir: str, base: str) -> str:
    try:
        import yt_dlp
    except ImportError:
        raise PinError("Video fallback needs yt-dlp. Run: pip install yt-dlp", 500)

    has_ffmpeg = shutil.which("ffmpeg") is not None
    options = {
        "outtmpl": os.path.join(out_dir, f"{base}.%(ext)s"),
        # Prefer a complete MP4 when available. If Pinterest exposes separate
        # video/audio streams, yt-dlp will merge them when ffmpeg is installed.
        "format": "bestvideo+bestaudio/best" if has_ffmpeg else "best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "http_headers": HEADERS,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([pin_url])
    except yt_dlp.utils.DownloadError as exc:
        message = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).replace("ERROR: ", "")
        raise PinError(f"Couldn't download the video: {message}", 502)

    files = [
        f for f in glob.glob(os.path.join(out_dir, f"{base}.*"))
        if not f.endswith((".part", ".ytdl"))
    ]
    if not files:
        raise PinError("The video download finished but produced no file.", 502)
    return files[0]


def download_media(info: PinInfo, out_dir: str) -> str:
    """Download the pin's media into out_dir and return the file path."""
    base = info.pin_id

    if info.kind == "video":
        # 1) Direct .mp4 files: fast and need no ffmpeg.
        for video_url in info.video_urls:
            try:
                return _download_direct(video_url, out_dir, base)
            except (requests.RequestException, PinError):
                log.info("pin %s: direct video failed: %s", info.pin_id, video_url)
        # 2) yt-dlp on the pin page, then on the raw stream. Never hand back the
        #    poster photo in place of a video.
        first_error: PinError | None = None
        for target in [info.url, *info.hls_urls]:
            try:
                return _download_with_ytdlp(target, out_dir, base)
            except PinError as exc:
                first_error = first_error or exc
        raise first_error or PinError("Couldn't download the video.", 502)

    for image_url in info.image_urls:
        try:
            return _download_direct(image_url, out_dir, base)
        except (requests.RequestException, PinError):
            continue  # e.g. /originals/ missing -> fall back to the next size
    raise PinError("Pinterest wouldn't serve the image file. Try again in a moment.", 502)
