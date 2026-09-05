#!/usr/bin/env python3
"""
Product image + purchase-link scraper.

Scrapes product listing/category pages and produces, for every product it finds:

    image (downloaded locally) -> product/purchase URL

which is exactly the mapping a product-recognition service needs: match the
customer's photo against the local images, then forward them to `product_url`.

Extraction runs three strategies, best first:

  1. JSON-LD   (schema.org/Product) - most reliable, used by most shops
  2. Microdata (itemtype=schema.org/Product)
  3. DOM heuristics - find repeated "cards" that contain both an <img> and an <a>

You can also bypass all of that with explicit CSS selectors (--card-selector etc.)
when you know the page's markup.

Usage:
    python scraper.py https://shop.example.com/collections/all --pages 5
    python scraper.py https://shop.example.com -o data --render
    python scraper.py --url-file urls.txt --card-selector ".product-card"
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import mimetypes
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("scraper")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# Images that are almost never a product photo.
JUNK_IMAGE_RE = re.compile(
    r"(sprite|logo|favicon|/icons?/|icon[-_.]|placeholder|blank\.|spacer|1x1|"
    r"pixel|loader|loading|avatar|payment|visa|mastercard|paypal|trustpilot|"
    r"social|facebook|instagram|twitter|tiktok|youtube|whatsapp)",
    re.I,
)
# Links that are clearly not a product page.
JUNK_LINK_RE = re.compile(
    r"(/cart|/checkout|/account|/login|/register|/wishlist|/compare|/search"
    r"|/blog|/pages/|/policies/|/faq|javascript:|mailto:|tel:)",
    re.I,
)
PRICE_RE = re.compile(
    r"(?P<cur>[$€£¥₹₺]|USD|EUR|GBP|AED|SAR|CAD|AUD|CHF|SEK|NOK|PLN|TRY|MAD|EGP)"
    r"\s?(?P<val>\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{1,2})?)"
    r"|(?P<val2>\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{1,2})?)\s?"
    r"(?P<cur2>[$€£¥₹₺]|USD|EUR|GBP|AED|SAR|CAD|AUD|CHF|SEK|NOK|PLN|TRY|kr|DH)",
    re.I,
)
# Attributes lazy-loading scripts hide the real image URL in.
LAZY_ATTRS = (
    "src", "data-src", "data-original", "data-lazy-src", "data-lazy",
    "data-image", "data-img", "data-fallback-src", "data-zoom-image",
    "data-large_image", "data-thumb", "data-echo",
)
SRCSET_ATTRS = ("srcset", "data-srcset", "data-lazy-srcset")
NEXT_TEXT_RE = re.compile(
    r"^\s*(next|suivant|siguiente|weiter|»|›|>)\s*$", re.I)


def html_parser() -> str:
    try:
        import lxml  # noqa: F401
        return "lxml"
    except ImportError:
        return "html.parser"


# --------------------------------------------------------------------------- #
# data model
# --------------------------------------------------------------------------- #

@dataclass
class Product:
    id: str = ""
    title: str = ""
    product_url: str = ""          # the purchase link the customer is forwarded to
    image_url: str = ""
    image_path: str = ""           # local file, filled in after download
    price: str = ""
    currency: str = ""
    source_page: str = ""
    method: str = ""               # jsonld | microdata | selector | heuristic
    scraped_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def key(self) -> tuple[str, str]:
        return (strip_query(self.product_url), strip_query(self.image_url))

    def finalize(self) -> "Product":
        seed = f"{strip_query(self.product_url)}|{strip_query(self.image_url)}"
        self.id = hashlib.sha1(seed.encode()).hexdigest()[:16]
        return self


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def strip_query(url: str) -> str:
    """Normalise a URL for dedup: drop fragment and cache-busting query."""
    if not url:
        return ""
    url, _ = urldefrag(url)
    return url.split("?")[0].rstrip("/").lower()


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def absolutize(url: str | None, base: str) -> str:
    url = (url or "").strip()
    if not url or url.startswith(("data:", "javascript:", "mailto:", "tel:", "#")):
        return ""
    if url.startswith("//"):
        url = urlparse(base).scheme + ":" + url
    return urljoin(base, url)


def best_from_srcset(value: str) -> str:
    """Pick the highest-resolution candidate out of a srcset attribute."""
    best, best_w = "", -1.0
    for part in value.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        url = bits[0]
        width = 1.0
        if len(bits) > 1:
            m = re.match(r"([\d.]+)([wx])", bits[1])
            if m:
                width = float(m.group(1)) * (1000 if m.group(2) == "x" else 1)
        if width > best_w:
            best, best_w = url, width
    return best


def image_url_from(node, base: str) -> str:
    """Extract the largest usable image URL from an <img>/<source>/<div> node."""
    if node is None:
        return ""

    # <picture><source srcset> beats the <img> fallback
    if node.name == "img":
        picture = node.find_parent("picture")
        if picture is not None:
            for source in picture.find_all("source"):
                for attr in SRCSET_ATTRS:
                    if source.get(attr):
                        url = absolutize(best_from_srcset(source[attr]), base)
                        if url and not JUNK_IMAGE_RE.search(url):
                            return url

    for attr in SRCSET_ATTRS:
        if node.get(attr):
            url = absolutize(best_from_srcset(node[attr]), base)
            if url:
                return url
    for attr in LAZY_ATTRS:
        if node.get(attr):
            url = absolutize(node[attr], base)
            if url:
                return url

    # inline background-image
    style = node.get("style") or ""
    m = re.search(r"background-image\s*:\s*url\((['\"]?)(.+?)\1\)", style, re.I)
    if m:
        return absolutize(m.group(2), base)
    return ""


def price_from(text: str) -> tuple[str, str]:
    m = PRICE_RE.search(text or "")
    if not m:
        return "", ""
    value = m.group("val") or m.group("val2") or ""
    currency = m.group("cur") or m.group("cur2") or ""
    return clean(value), clean(currency).upper()


def is_product_image(url: str) -> bool:
    return bool(url) and not JUNK_IMAGE_RE.search(url)


def is_product_link(url: str) -> bool:
    return bool(url) and not JUNK_LINK_RE.search(url)


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #

class Fetcher:
    def __init__(self, args: argparse.Namespace):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": args.user_agent,
            "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                       "image/avif,image/webp,*/*;q=0.8"),
            "Accept-Language": "en-US,en;q=0.9",
        })
        for header in args.header or []:
            name, _, value = header.partition(":")
            self.session.headers[name.strip()] = value.strip()
        if args.cookie:
            self.session.headers["Cookie"] = args.cookie
        self.timeout = args.timeout
        self.delay = args.delay
        self.retries = args.retries
        self.ignore_robots = args.ignore_robots
        self.render = args.render
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_request = 0.0
        self._browser_ctx = None
        self._pw = None

    # -- politeness ------------------------------------------------------- #

    def allowed(self, url: str) -> bool:
        if self.ignore_robots:
            return True
        parts = urlparse(url)
        root = f"{parts.scheme}://{parts.netloc}"
        if root not in self._robots:
            rp = RobotFileParser()
            rp.set_url(root + "/robots.txt")
            try:
                rp.read()
            except Exception:
                rp = None  # unreachable robots.txt -> don't block on it
            self._robots[root] = rp
        rp = self._robots[root]
        if rp is None:
            return True
        return rp.can_fetch(self.session.headers["User-Agent"], url)

    def _throttle(self) -> None:
        wait = self.delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    # -- html ------------------------------------------------------------- #

    def get_html(self, url: str) -> str | None:
        if not self.allowed(url):
            log.warning("robots.txt disallows %s (use --ignore-robots to override)", url)
            return None
        if self.render:
            return self._render(url)

        for attempt in range(1, self.retries + 1):
            self._throttle()
            try:
                r = self.session.get(url, timeout=self.timeout)
                if r.status_code == 429 or r.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                r.raise_for_status()
                ctype = r.headers.get("Content-Type", "text/html")
                if "html" not in ctype:
                    log.warning("%s is not HTML", url)
                    return None
                # requests falls back to ISO-8859-1 when the header omits a
                # charset, which mangles currency symbols and accents.
                if "charset" not in ctype.lower():
                    r.encoding = r.apparent_encoding or r.encoding
                return r.text
            except Exception as exc:
                log.warning("fetch failed (%s/%s) %s: %s", attempt, self.retries, url, exc)
                time.sleep(min(2 ** attempt, 15))
        return None

    def _render(self, url: str) -> str | None:
        """JS-rendered pages: pip install playwright && playwright install chromium."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.error("--render needs playwright: "
                      "pip install playwright && playwright install chromium")
            sys.exit(2)

        if self._browser_ctx is None:
            self._pw = sync_playwright().start()
            browser = self._pw.chromium.launch(headless=True)
            self._browser_ctx = browser.new_context(
                user_agent=self.session.headers["User-Agent"],
                viewport={"width": 1440, "height": 1000},
            )
        page = self._browser_ctx.new_page()
        try:
            page.goto(url, timeout=self.timeout * 1000, wait_until="domcontentloaded")
            # scroll to the bottom so lazy-loaded product images actually load
            for _ in range(12):
                page.mouse.wheel(0, 2200)
                page.wait_for_timeout(350)
            page.wait_for_timeout(1200)
            return page.content()
        except Exception as exc:
            log.warning("render failed %s: %s", url, exc)
            return None
        finally:
            page.close()

    def close(self) -> None:
        if self._browser_ctx is not None:
            try:
                self._browser_ctx.close()
                self._pw.stop()
            except Exception:
                pass
        self.session.close()


# --------------------------------------------------------------------------- #
# extraction strategies
# --------------------------------------------------------------------------- #

def _flatten_jsonld(node) -> Iterator[dict]:
    if isinstance(node, list):
        for item in node:
            yield from _flatten_jsonld(item)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "itemListElement", "item", "hasVariant",
                    "isSimilarTo", "mainEntity"):
            if key in node:
                yield from _flatten_jsonld(node[key])


def _jsonld_blocks(soup: BeautifulSoup) -> Iterator[dict]:
    for tag in soup.find_all("script", type=re.compile(r"ld\+json", re.I)):
        raw = clean(tag.string or tag.get_text())
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            try:  # tolerate trailing commas, a very common CMS bug
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw))
            except Exception:
                continue
        yield from _flatten_jsonld(data)


def _as_str(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value:
        return _as_str(value[0])
    if isinstance(value, dict):
        for key in ("url", "contentUrl", "@id", "name"):
            if key in value:
                return _as_str(value[key])
    return ""


def _has_type(node: dict, wanted: str) -> bool:
    types = node.get("@type") or node.get("type") or ""
    if isinstance(types, str):
        types = [types]
    return any(wanted.lower() in str(t).lower() for t in types)


def extract_jsonld(soup: BeautifulSoup, page_url: str) -> list[Product]:
    products: list[Product] = []
    for node in _jsonld_blocks(soup):
        if not _has_type(node, "Product"):
            continue
        image = absolutize(_as_str(node.get("image")), page_url)
        url = absolutize(_as_str(node.get("url")) or _as_str(node.get("@id")), page_url)
        offers = node.get("offers")
        price = currency = ""
        if offers:
            offer = offers[0] if isinstance(offers, list) and offers else offers
            if isinstance(offer, dict):
                price = str(offer.get("price") or offer.get("lowPrice") or "")
                currency = str(offer.get("priceCurrency") or "")
                url = url or absolutize(_as_str(offer.get("url")), page_url)
        if not image or not url:
            continue
        products.append(Product(
            title=clean(_as_str(node.get("name"))),
            product_url=url, image_url=image,
            price=price, currency=currency.upper(),
            source_page=page_url, method="jsonld",
        ))
    return products


def extract_microdata(soup: BeautifulSoup, page_url: str) -> list[Product]:
    products: list[Product] = []
    scopes = soup.find_all(attrs={"itemtype": re.compile(r"schema\.org/Product", re.I)})
    for scope in scopes:
        def prop(name: str):
            return scope.find(attrs={"itemprop": name})

        image_node = prop("image")
        image = ""
        if image_node is not None:
            image = (absolutize(image_node.get("content"), page_url)
                     or image_url_from(image_node, page_url))
        if not image:
            image = image_url_from(scope.find("img"), page_url)

        url_node = prop("url")
        url = ""
        if url_node is not None:
            url = absolutize(url_node.get("href") or url_node.get("content"), page_url)
        if not url:
            a = scope.find("a", href=True)
            url = absolutize(a["href"], page_url) if a else page_url

        name_node = prop("name")
        title = clean(_node_value(name_node))
        price = clean(_node_value(prop("price")))
        cur_node = prop("priceCurrency")
        currency = clean(cur_node.get("content") if cur_node else "")

        if image and url:
            products.append(Product(
                title=title, product_url=url, image_url=image,
                price=price, currency=currency.upper(),
                source_page=page_url, method="microdata",
            ))
    return products


def _node_value(node) -> str:
    if node is None:
        return ""
    return node.get("content") or node.get_text(" ")


def _nearest_link(img, max_up: int = 8):
    """The <a> a product image belongs to: an ancestor link, or one nearby."""
    node = img
    for _ in range(max_up):
        node = node.parent
        if node is None or node.name in ("body", "html", "[document]"):
            break
        if node.name == "a" and node.get("href"):
            return node, node.parent
        a = node.find("a", href=True)
        if a is not None:
            return a, node
    return None, None


def _signature(node) -> str:
    """A rough 'what kind of box is this' fingerprint, used to group repeats."""
    if node is None:
        return "?"
    classes = re.sub(r"\d+", "#", " ".join(node.get("class") or []))
    tokens = sorted(set(classes.split()))[:6]
    if tokens:
        return node.name + "." + ".".join(tokens)
    parent = node.parent.name if node.parent else ""
    return f"{node.name}>{parent}"


def _card_scope(container, max_up: int = 3):
    """
    Widen from the element holding the link out to the real product card.

    The nearest common ancestor of an image and its link is often just the
    image wrapper, which excludes the title and price sitting beside it. Climb
    until a price appears, stopping before we swallow the whole grid.
    """
    node = container
    best = container
    for _ in range(max_up):
        if node is None or node.parent is None:
            break
        if node.name in ("body", "html", "[document]"):
            break
        text = clean(node.get_text(" "))
        if len(text) > 600:          # too big to still be one card
            break
        best = node
        if price_from(text)[0]:
            break
        node = node.parent
    return best


def _heading_text(container) -> str:
    if container is None:
        return ""
    h = container.find(["h1", "h2", "h3", "h4", "h5"])
    return clean(h.get_text(" ")) if h else ""


def extract_heuristic(soup: BeautifulSoup, page_url: str) -> list[Product]:
    groups: dict[str, list[tuple]] = {}
    for img in soup.find_all("img"):
        image = image_url_from(img, page_url)
        if not is_product_image(image):
            continue
        a, container = _nearest_link(img)
        if a is None:
            continue
        url = absolutize(a.get("href"), page_url)
        if not is_product_link(url) or strip_query(url) == strip_query(page_url):
            continue
        groups.setdefault(_signature(container), []).append(
            (container, a, img, url, image))

    if not groups:
        return []

    largest = max(len(v) for v in groups.values())
    # Keep every repeated card group (a page can have a grid *and* a carousel);
    # if nothing repeats at all, fall back to whatever we found.
    threshold = 2 if largest >= 2 else 1

    products: list[Product] = []
    for members in groups.values():
        if len(members) < threshold:
            continue
        for container, a, img, url, image in members:
            card = _card_scope(container) if container is not None else a
            price, currency = price_from(clean(card.get_text(" ")))
            title = (clean(img.get("alt")) or clean(a.get("title"))
                     or clean(a.get_text(" ")) or _heading_text(card))
            products.append(Product(
                title=title[:200], product_url=url, image_url=image,
                price=price, currency=currency,
                source_page=page_url, method="heuristic",
            ))
    return products


def extract_with_selectors(soup: BeautifulSoup, page_url: str, args) -> list[Product]:
    products: list[Product] = []
    for card in soup.select(args.card_selector):
        if args.link_selector:
            a = card.select_one(args.link_selector)
        else:
            a = card if card.name == "a" and card.get("href") else card.find("a", href=True)
        img = card.select_one(args.image_selector) if args.image_selector else card.find("img")

        url = absolutize(a.get("href") if a else "", page_url)
        image = image_url_from(img, page_url)
        if not url or not image:
            continue

        title = ""
        if args.title_selector:
            node = card.select_one(args.title_selector)
            title = clean(node.get_text(" ")) if node else ""
        title = title or clean(img.get("alt")) or _heading_text(card)

        if args.price_selector:
            node = card.select_one(args.price_selector)
            price, currency = price_from(clean(node.get_text(" ")) if node else "")
        else:
            price, currency = price_from(clean(card.get_text(" ")))

        products.append(Product(
            title=title[:200], product_url=url, image_url=image,
            price=price, currency=currency,
            source_page=page_url, method="selector",
        ))
    return products


def extract_products(html: str, page_url: str, args) -> list[Product]:
    soup = BeautifulSoup(html, html_parser())

    if args.card_selector:
        found = extract_with_selectors(soup, page_url, args)
        log.info("  selector  -> %d", len(found))
        return found

    found = extract_jsonld(soup, page_url)
    log.info("  json-ld   -> %d", len(found))
    if len(found) < args.min_products:
        micro = extract_microdata(soup, page_url)
        log.info("  microdata -> %d", len(micro))
        if len(micro) > len(found):
            found = micro
    if len(found) < args.min_products:
        heur = extract_heuristic(soup, page_url)
        log.info("  heuristic -> %d", len(heur))
        if len(heur) > len(found):
            found = heur
    return found


def find_next_page(html: str, page_url: str) -> str:
    soup = BeautifulSoup(html, html_parser())
    for link in soup.find_all("link", rel=True):
        rel = " ".join(link.get("rel") or []).lower()
        if "next" in rel and link.get("href"):
            return absolutize(link["href"], page_url)
    for a in soup.find_all("a", href=True):
        rel = " ".join(a.get("rel") or []).lower()
        classes = " ".join(a.get("class") or []).lower()
        label = (a.get("aria-label") or "").lower()
        if ("next" in rel or "next" in classes or "next" in label
                or NEXT_TEXT_RE.match(a.get_text() or "")):
            url = absolutize(a["href"], page_url)
            if url and strip_query(url) != strip_query(page_url):
                return url
    return ""


# --------------------------------------------------------------------------- #
# image download
# --------------------------------------------------------------------------- #

def _extension(url: str, content_type: str) -> str:
    mime = (content_type or "").split(";")[0].strip().lower()
    # A generic octet-stream tells us nothing; trust the URL's suffix instead.
    ext = "" if mime in ("", "application/octet-stream") else (mimetypes.guess_extension(mime) or "")
    if ext in (".jpe", ".jpeg"):
        ext = ".jpg"
    if not ext:
        ext = Path(urlparse(url).path).suffix.lower()
        if ext == ".jpeg":
            ext = ".jpg"
    if ext not in (".jpg", ".png", ".webp", ".gif", ".avif", ".bmp"):
        ext = ".jpg"
    return ext


def _dimensions_ok(path: Path, min_px: int) -> bool:
    """Drop thumbnails/tracking pixels. Needs Pillow; without it, always passes."""
    try:
        from PIL import Image
    except ImportError:
        return True
    try:
        with Image.open(path) as im:
            width, height = im.size
        return width >= min_px and height >= min_px
    except Exception:
        return False


def download_image(product: Product, fetcher: Fetcher, image_dir: Path, args) -> Product:
    url = product.image_url
    try:
        r = fetcher.session.get(url, timeout=args.timeout, stream=True)
        r.raise_for_status()
        ctype = r.headers.get("Content-Type", "")
        if not ctype.startswith("image/") and "octet-stream" not in ctype:
            log.debug("not an image: %s (%s)", url, ctype)
            return product

        path = image_dir / f"{product.id}{_extension(url, ctype)}"
        if path.exists() and path.stat().st_size > 0:
            product.image_path = path.relative_to(args.out).as_posix()
            return product

        size = 0
        too_large = False
        with open(path, "wb") as fh:
            for chunk in r.iter_content(64 * 1024):
                size += len(chunk)
                if size > args.max_image_bytes:
                    too_large = True
                    break
                fh.write(chunk)

        if too_large:
            path.unlink(missing_ok=True)
            log.debug("too large, skipped: %s", url)
            return product
        if size < args.min_image_bytes:
            path.unlink(missing_ok=True)
            log.debug("too small (%d B), skipped: %s", size, url)
            return product
        if not _dimensions_ok(path, args.min_image_px):
            path.unlink(missing_ok=True)
            log.debug("below %dpx, skipped: %s", args.min_image_px, url)
            return product

        product.image_path = path.relative_to(args.out).as_posix()
    except Exception as exc:
        log.debug("download failed %s: %s", url, exc)
    return product


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #

FIELDNAMES = ["id", "title", "product_url", "image_url", "image_path",
              "price", "currency", "source_page", "method", "scraped_at"]


def write_outputs(products: list[Product], out: Path) -> None:
    rows = [asdict(p) for p in products]

    (out / "products.json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

    with open(out / "products.csv", "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    # The lookup table the recognition service actually consumes:
    # local image file -> purchase link.
    index = {
        p.image_path: {
            "product_url": p.product_url,
            "title": p.title,
            "price": p.price,
            "currency": p.currency,
        }
        for p in products if p.image_path
    }
    (out / "image_index.json").write_text(
        json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- #
# crawl + CLI
# --------------------------------------------------------------------------- #

def crawl(start_urls: Iterable[str], fetcher: Fetcher, args) -> list[Product]:
    seen: set[tuple[str, str]] = set()
    visited: set[str] = set()
    products: list[Product] = []

    for start in start_urls:
        url = start
        for page_no in range(1, args.pages + 1):
            if not url or strip_query(url) in visited:
                break
            visited.add(strip_query(url))
            log.info("[page %d] %s", page_no, url)

            html = fetcher.get_html(url)
            if not html:
                break

            for product in extract_products(html, url, args):
                product.finalize()
                if product.key() in seen:
                    continue
                seen.add(product.key())
                products.append(product)
                if args.max_products and len(products) >= args.max_products:
                    log.info("reached --max-products (%d)", args.max_products)
                    return products

            log.info("  total so far: %d", len(products))
            if args.pages > 1:
                url = find_next_page(html, url)
                if not url:
                    log.info("  no next page found")
            else:
                url = ""
    return products


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Scrape product images and their purchase links from a webpage.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("urls", nargs="*", help="one or more listing/category page URLs")
    p.add_argument("--url-file", help="text file with one URL per line")
    p.add_argument("-o", "--out", default="output", help="output directory")
    p.add_argument("--pages", type=int, default=1,
                   help="max pages to follow per start URL")
    p.add_argument("--max-products", type=int, default=0,
                   help="stop after N products (0 = no limit)")
    p.add_argument("--no-download", action="store_true",
                   help="collect URLs only, don't fetch the image files")
    p.add_argument("--workers", type=int, default=8, help="parallel image downloads")

    p.add_argument("--render", action="store_true",
                   help="render with a headless browser (JS-built shops); needs playwright")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between page requests")
    p.add_argument("--timeout", type=int, default=25, help="request timeout in seconds")
    p.add_argument("--retries", type=int, default=3, help="retries per page")
    p.add_argument("--user-agent", default=DEFAULT_UA)
    p.add_argument("--header", action="append",
                   help="extra header 'Name: value' (repeatable)")
    p.add_argument("--cookie", help="raw Cookie header, for pages behind a login")
    p.add_argument("--ignore-robots", action="store_true",
                   help="skip the robots.txt check")

    p.add_argument("--card-selector",
                   help="CSS selector for one product card (bypasses auto-detection)")
    p.add_argument("--link-selector", help="CSS selector for the link inside a card")
    p.add_argument("--image-selector", help="CSS selector for the image inside a card")
    p.add_argument("--title-selector", help="CSS selector for the title inside a card")
    p.add_argument("--price-selector", help="CSS selector for the price inside a card")
    p.add_argument("--min-products", type=int, default=3,
                   help="if a strategy finds fewer than this, try the next one")

    p.add_argument("--min-image-px", type=int, default=200,
                   help="drop images smaller than this (needs Pillow)")
    p.add_argument("--min-image-bytes", type=int, default=1024,
                   help="safety net for tracking pixels; real filtering is --min-image-px")
    p.add_argument("--max-image-bytes", type=int, default=15 * 1024 * 1024)
    p.add_argument("-v", "--verbose", action="store_true")

    args = p.parse_args(argv)
    if args.url_file:
        lines = Path(args.url_file).read_text(encoding="utf-8").splitlines()
        args.urls += [ln.strip() for ln in lines
                      if ln.strip() and not ln.strip().startswith("#")]
    if not args.urls:
        p.error("give at least one URL (or --url-file)")
    args.out = Path(args.out).resolve()
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    args.out.mkdir(parents=True, exist_ok=True)
    image_dir = args.out / "images"
    image_dir.mkdir(exist_ok=True)

    fetcher = Fetcher(args)
    try:
        products = crawl(args.urls, fetcher, args)

        if products and not args.no_download:
            log.info("downloading %d images -> %s", len(products), image_dir)
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = [pool.submit(download_image, p, fetcher, image_dir, args)
                           for p in products]
                done = 0
                for f in as_completed(futures):
                    f.result()
                    done += 1
                    if done % 25 == 0:
                        log.info("  %d/%d", done, len(products))
            found, products = len(products), [p for p in products if p.image_path]
            if products and len(products) < found:
                log.info("kept %d/%d images (rest were filtered; -v shows why)",
                         len(products), found)
            elif not products:
                log.error("found %d products but every image was rejected - most "
                          "likely they are smaller than --min-image-px %d. "
                          "Re-run with -v to see the reason per image.",
                          found, args.min_image_px)
                return 1
    finally:
        fetcher.close()

    if not products:
        log.error("no products found. Try --render for a JS shop, or pass "
                  "--card-selector once you've inspected the page markup.")
        return 1

    write_outputs(products, args.out)
    with_price = sum(1 for p in products if p.price)
    log.info("done: %d products (%d with a price) -> %s",
             len(products), with_price, args.out)
    log.info("  products.json / products.csv   full records")
    log.info("  image_index.json               image file -> purchase link")
    return 0


if __name__ == "__main__":
    sys.exit(main())
