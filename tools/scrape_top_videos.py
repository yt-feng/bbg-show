#!/usr/bin/env python3
"""Scrape Bloomberg /videos Top Videos links."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from download_bloomberg_video import (  # noqa: E402
    DEFAULT_PROXY_CACHE,
    DEFAULT_PROXY_TEST_URL,
    DEFAULT_SUBSCRIPTION,
    DEFAULT_SUBSCRIPTION_URL_FILE,
    FetchError,
    build_proxy_fetcher,
    bloomberg_brp_url,
    ensure_subscription,
    fetch_text_direct,
    safe_file_part,
)
from trump_filter import is_trump_related  # noqa: E402


DEFAULT_URL = "https://www.bloomberg.com/videos"
TOP_VIDEOS_XPATH = "/html/body/div[2]/div/div[2]/div[3]/main/section/section[1]/div"
VIDEO_PATH_RE = re.compile(r"/news/videos/\d{4}-\d{2}-\d{2}/[^\"'<>\s?#]+", re.IGNORECASE)


def log(message: str) -> None:
    print(f"[top-videos] {message}", flush=True)


def chrome_binary() -> str | None:
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ]
    for candidate in candidates:
        if candidate.startswith("/") and Path(candidate).exists():
            return candidate
        if not candidate.startswith("/") and shutil_which(candidate):
            return candidate
    return None


def shutil_which(name: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        path = Path(directory) / name
        if path.exists() and os.access(path, os.X_OK):
            return str(path)
    return None


def clean_text(value: str) -> str:
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_url(base_url: str, href: str) -> str:
    href = href.strip().rstrip("\\")
    url = urljoin(base_url, html.unescape(href))
    parts = urlsplit(url)
    return urlunsplit((parts.scheme or "https", parts.netloc or "www.bloomberg.com", parts.path, "", ""))


def add_link(links: list[dict[str, str]], seen: set[str], url: str, title: str) -> None:
    normalized = normalize_url(DEFAULT_URL, url)
    if "/news/videos/" not in urlsplit(normalized).path:
        return
    clean_title = clean_text(title) or title_from_url(normalized)
    if is_trump_related(normalized, clean_title):
        log(f"Skipping sensitive-topic top video: {clean_title or normalized}")
        return
    if normalized in seen:
        return
    seen.add(normalized)
    links.append({
        "url": normalized,
        "title": clean_title,
        "slug": safe_file_part(title_from_url(normalized)),
    })


def title_from_url(url: str) -> str:
    slug = Path(urlsplit(url).path).name
    slug = re.sub(r"-?video$", "", slug, flags=re.IGNORECASE)
    return clean_text(slug.replace("-", " ")).title()


def direct_top_video_slice(links: list[dict[str, str]], max_videos: int, skip_leading: int) -> list[dict[str, str]]:
    if skip_leading > 0 and len(links) >= max_videos + skip_leading:
        return links[skip_leading:skip_leading + max_videos]
    return links[:max_videos]


def extract_links_from_html(
    text: str,
    base_url: str,
    max_videos: int,
    *,
    skip_leading: int = 4,
) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    scan_limit = max_videos + max(0, skip_leading)

    anchor_re = re.compile(
        r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for match in anchor_re.finditer(text):
        href = html.unescape(match.group(1))
        if not VIDEO_PATH_RE.search(href):
            continue
        label = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", match.group(2))
        label = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", label)
        label = re.sub(r"(?s)<[^>]+>", " ", label)
        add_link(links, seen, normalize_url(base_url, href), label)
        if len(links) >= scan_limit:
            return direct_top_video_slice(links, max_videos, skip_leading)

    for match in VIDEO_PATH_RE.finditer(html.unescape(text)):
        add_link(links, seen, normalize_url(base_url, match.group(0)), "")
        if len(links) >= scan_limit:
            break
    return direct_top_video_slice(links, max_videos, skip_leading)


class WebSocketClient:
    def __init__(self, ws_url: str) -> None:
        parts = urlsplit(ws_url)
        if parts.scheme != "ws" or not parts.hostname or not parts.port:
            raise RuntimeError(f"Unsupported websocket URL: {ws_url}")
        self.path = parts.path + (f"?{parts.query}" if parts.query else "")
        self.sock = socket.create_connection((parts.hostname, parts.port), timeout=20)
        self.sock.settimeout(30)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {parts.hostname}:{parts.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        response = self._read_http_response()
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"WebSocket upgrade failed: {response[:200]!r}")
        self.next_id = 1

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def _read_http_response(self) -> bytes:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)

    def _recv_exact(self, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = self.sock.recv(size - len(chunks))
            if not chunk:
                raise RuntimeError("WebSocket closed")
            chunks.extend(chunk)
        return bytes(chunks)

    def send_json(self, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        header = bytearray([0x81])
        length = len(raw)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(raw))
        self.sock.sendall(bytes(header) + masked)

    def recv_json(self) -> dict[str, Any]:
        while True:
            first, second = self._recv_exact(2)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length)
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x8:
                raise RuntimeError("WebSocket closed")
            if opcode == 0x9:
                continue
            if opcode in {0x1, 0x2}:
                return json.loads(payload.decode("utf-8"))

    def command(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        command_id = self.next_id
        self.next_id += 1
        self.send_json({"id": command_id, "method": method, "params": params or {}})
        while True:
            message = self.recv_json()
            if message.get("id") == command_id:
                if "error" in message:
                    raise RuntimeError(f"CDP {method} failed: {message['error']}")
                return message.get("result", {})


def chrome_page_ws(debug_port: int, timeout: int = 30) -> str:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{debug_port}/json/list", timeout=2) as response:
                pages = json.loads(response.read())
            for page in pages:
                if page.get("type") == "page" and page.get("webSocketDebuggerUrl"):
                    return str(page["webSocketDebuggerUrl"])
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError(f"Could not locate Chrome page websocket: {last_error}")


def allocate_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def extract_links_with_headless_chrome(url: str, xpath: str, max_videos: int, wait_seconds: int) -> list[dict[str, str]]:
    binary = chrome_binary()
    if not binary:
        raise RuntimeError("No Chrome/Chromium binary found")

    profile_seed = hashlib.sha1(f"{url}-{time.time()}".encode()).hexdigest()[:12]
    profile_dir = Path("/tmp") / f"bbg-top-videos-chrome-{profile_seed}"
    debug_port = allocate_local_port()
    command = [
        binary,
        "--headless=new",
        "--disable-gpu",
        "--disable-background-networking",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-sync",
        "--disable-translate",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1440,1200",
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={profile_dir}",
        url,
    ]
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ws = WebSocketClient(chrome_page_ws(debug_port))
        try:
            ws.command("Page.enable")
            ws.command("Runtime.enable")
            time.sleep(wait_seconds)
            expression = top_videos_js(xpath, max_videos)
            result = ws.command("Runtime.evaluate", {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
            })
        finally:
            ws.close()
        value = result.get("result", {}).get("value", "{}")
        parsed = json.loads(value)
        return normalize_browser_links(parsed.get("links", []), max_videos)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def top_videos_js(xpath: str, max_videos: int) -> str:
    return f"""
(async function () {{
  const xpath = {json.dumps(xpath)};
  const maxVideos = {int(max_videos)};
  const node = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
  const root = node || document;
  const seen = new Map();
  function addLinks() {{
    Array.from(root.querySelectorAll('a[href]')).forEach((a) => {{
      const href = a.href || '';
      if (!/\\/news\\/videos\\//.test(href)) return;
      const text = (a.innerText || a.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
      if (!seen.has(href)) seen.set(href, {{ url: href, title: text }});
    }});
  }}
  function scrollCandidates() {{
    const nodes = [root].concat(Array.from(root.querySelectorAll('*')));
    return nodes.filter((item) => item && item.scrollWidth > item.clientWidth + 20);
  }}
  addLinks();
  const candidates = scrollCandidates();
  for (let step = 0; step < 10 && seen.size < maxVideos; step += 1) {{
    candidates.forEach((item) => {{
      item.scrollLeft = Math.min(item.scrollWidth, item.scrollLeft + Math.max(300, item.clientWidth * 0.8));
      item.dispatchEvent(new Event('scroll', {{ bubbles: true }}));
    }});
    await new Promise((resolve) => setTimeout(resolve, 500));
    addLinks();
  }}
  return JSON.stringify({{
    url: location.href,
    title: document.title,
    ready: document.readyState,
    foundXPath: Boolean(node),
    links: Array.from(seen.values()).slice(0, maxVideos)
  }});
}})()
"""


def normalize_browser_links(items: list[dict[str, Any]], max_videos: int) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        add_link(links, seen, str(item.get("url", "")), str(item.get("title", "")))
        if len(links) >= max_videos:
            break
    return links


def fetch_text_proxy(url: str, work_dir: Path) -> str:
    args = argparse.Namespace(
        subscription=DEFAULT_SUBSCRIPTION,
        subscription_url="",
        subscription_url_file=DEFAULT_SUBSCRIPTION_URL_FILE,
        refresh_subscription=False,
        proxy_cache=DEFAULT_PROXY_CACHE,
        proxy_test_url=DEFAULT_PROXY_TEST_URL,
        google_doh=True,
        url=url,
    )
    subscription = ensure_subscription(args)
    fetcher, _proxy = build_proxy_fetcher(args, subscription, work_dir)
    return fetcher(url, "top_videos_page", timeout=120)


def scrape(
    url: str,
    xpath: str,
    max_videos: int,
    method: str,
    wait_seconds: int,
    direct_skip_leading: int,
    work_dir: Path,
) -> tuple[str, list[dict[str, str]]]:
    errors: list[str] = []
    if method in {"auto", "direct"}:
        try:
            log(f"Trying direct page fetch: {url}")
            text = fetch_text_direct(url, timeout=90)
            links = extract_links_from_html(text, url, max_videos, skip_leading=direct_skip_leading)
            if links:
                log(f"Direct page fetch found {len(links)} video link(s)")
                return "direct", links
            errors.append("direct fetch returned no video links")
            log("Direct page fetch returned no video links")
        except FetchError as exc:
            errors.append(f"direct failed: {exc}")
            log(f"Direct page fetch failed: {exc}")

    if method in {"auto", "brp"}:
        brp_url = bloomberg_brp_url(url)
        if brp_url != url:
            try:
                log(f"Trying BRP background page fetch: {brp_url}")
                text = fetch_text_direct(brp_url, timeout=45)
                links = extract_links_from_html(text, url, max_videos, skip_leading=direct_skip_leading)
                if links:
                    log(f"BRP background page fetch found {len(links)} video link(s)")
                    return "brp", links
                errors.append("brp fetch returned no video links")
                log("BRP background page fetch returned no video links")
            except FetchError as exc:
                errors.append(f"brp failed: {exc}")
                log(f"BRP background page fetch failed: {exc}")

    if method in {"auto", "proxy"}:
        try:
            log("Trying proxy page fetch fallback")
            text = fetch_text_proxy(url, work_dir)
            links = extract_links_from_html(text, url, max_videos, skip_leading=direct_skip_leading)
            if links:
                log(f"Proxy page fetch found {len(links)} video link(s)")
                return "proxy", links
            errors.append("proxy fetch returned no video links")
            log("Proxy page fetch returned no video links")
        except (Exception, SystemExit) as exc:
            errors.append(f"proxy failed: {exc}")
            log(f"Proxy page fetch failed: {exc}")

    if method in {"auto", "chrome"}:
        try:
            log("Trying headless Chrome XPath fallback")
            links = extract_links_with_headless_chrome(url, xpath, max_videos, wait_seconds)
            if links:
                log(f"Headless Chrome found {len(links)} video link(s)")
                return "chrome", links
            errors.append("headless Chrome returned no video links")
            log("Headless Chrome returned no video links")
        except Exception as exc:  # noqa: BLE001 - surface all scrape diagnostics
            errors.append(f"chrome failed: {exc}")
            log(f"Headless Chrome failed: {exc}")

    raise SystemExit("Could not scrape Top Videos links: " + " | ".join(errors))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--xpath", default=TOP_VIDEOS_XPATH)
    parser.add_argument("--max-videos", type=int, default=9)
    parser.add_argument("--method", choices=("auto", "direct", "brp", "proxy", "chrome"), default="auto")
    parser.add_argument("--wait-seconds", type=int, default=8)
    parser.add_argument("--work-dir", type=Path, default=Path("work/top-videos/scrape"))
    parser.add_argument(
        "--direct-skip-leading",
        type=int,
        default=4,
        help="When direct HTML/RSC extraction sees enough links, skip leading hero videos before Top Videos.",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    method, links = scrape(
        args.url,
        args.xpath,
        args.max_videos,
        args.method,
        args.wait_seconds,
        args.direct_skip_leading,
        args.work_dir,
    )
    payload = {
        "source_url": args.url,
        "source_xpath": args.xpath,
        "scrape_method": method,
        "scraped_at": int(time.time()),
        "count": len(links),
        "videos": links,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Scraped {len(links)} Bloomberg Top Videos via {method}: {args.out}", flush=True)
    for index, item in enumerate(links, start=1):
        print(f"  {index:02d}. {item['title']} -> {item['url']}", flush=True)


if __name__ == "__main__":
    main()
