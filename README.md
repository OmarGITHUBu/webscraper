# Product image scraper

Scrapes product listing pages and produces the mapping a product-recognition
service needs:

```
downloaded image file  ->  purchase link
```

Point it at a shop's category/collection page. For every product it finds it
saves the image locally and records the product URL, title and price.

## Setup

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Then run everything through `.\.venv\Scripts\python.exe`, not a bare `python`.

```powershell
.\.venv\Scripts\python.exe scraper.py --help
```

## Usage

```powershell
# one page
.\.venv\Scripts\python.exe scraper.py https://shop.example.com/collections/all

# follow pagination, write into .\data
.\.venv\Scripts\python.exe scraper.py https://shop.example.com/collections/all --pages 20 -o data

# several starting pages
.\.venv\Scripts\python.exe scraper.py --url-file urls.txt --pages 10

# a shop that builds its grid in JavaScript
pip install playwright; playwright install chromium
.\.venv\Scripts\python.exe scraper.py https://shop.example.com --render
```

## Output

```
output/
  images/
    6123dc474cd08682.jpg      # filename is the record id
    ...
  products.json               # full records
  products.csv                # same, for spreadsheets
  image_index.json            # image file -> purchase link
```

`image_index.json` is the file your recognition service loads:

```json
{
  "images/6123dc474cd08682.jpg": {
    "product_url": "https://shop.example.com/products/blue-chair",
    "title": "Blue Chair",
    "price": "129.99",
    "currency": "$"
  }
}
```

Build your embeddings over `output/images/`, and when a customer photo matches a
file, look that path up in the index and forward them to `product_url`:

```python
import json
index = json.load(open("output/image_index.json", encoding="utf-8"))
index["images/6123dc474cd08682.jpg"]["product_url"]
```

Re-running into the same directory is safe: already-downloaded images are
skipped, and records are deduplicated on (product URL, image URL).

## How products are detected

Three strategies, tried in order until one returns enough results
(`--min-products`, default 3):

1. **JSON-LD** — `schema.org/Product` blocks. Most shops (Shopify, WooCommerce,
   Magento) emit these, and they give exact title, price, currency and URL.
2. **Microdata** — `itemtype="…schema.org/Product"` markup.
3. **DOM heuristics** — every `<img>` is matched to its nearest `<a href>`, the
   containers are fingerprinted by tag + class, and repeated fingerprints are
   treated as a product grid. Handles `srcset`, `<picture><source>`, and the
   usual lazy-load attributes (`data-src`, `data-original`, …). Logos, icons,
   payment badges and social buttons are filtered out, as are cart/login/blog
   links.

If auto-detection gets it wrong, inspect the page in devtools and drive it
manually — this skips all three strategies:

```powershell
.\.venv\Scripts\python.exe scraper.py https://shop.example.com/shop `
  --card-selector ".product-card" `
  --link-selector "a.product-link" `
  --image-selector "img.product-image" `
  --title-selector "h3" `
  --price-selector ".price"
```

## Useful flags

| Flag | What it does |
|---|---|
| `--pages N` | follow "next page" links, up to N pages per start URL |
| `--max-products N` | stop after N products |
| `--no-download` | collect URLs only, skip the image files |
| `--render` | drive a headless Chromium and scroll, for JS-built grids |
| `--delay` | seconds between page requests (default 1.0 — be polite) |
| `--cookie "…"` | raw Cookie header, for pages behind a login |
| `--header "X: y"` | extra request header, repeatable |
| `--min-image-px` | drop images smaller than this on either side (default 200) |
| `--ignore-robots` | skip the robots.txt check |
| `-v` | debug logging, including why an image was skipped |

## When it doesn't work

Run with `-v` first — it logs how many products each strategy found and why
each image was rejected.

| Symptom | Fix |
|---|---|
| `found N products but every image was rejected` | the shop uses small thumbnails; lower `--min-image-px` (e.g. `100`) |
| `json-ld -> 0`, `heuristic -> 0` | the grid is built in JavaScript — add `--render` |
| got navigation/banner images instead of products | pass `--card-selector` with the real card class from devtools |
| only a handful of products on a long page | infinite scroll — use `--render`, or find the `?page=2` URL pattern and feed pages via `--url-file` |
| 403 / challenge page | bot protection; check whether the shop offers a product feed or API instead |

## Notes

- `robots.txt` is respected by default, and requests are throttled to one per
  second. Before scraping a shop you don't own, check its terms of service —
  product images are usually copyrighted, and using them for a recognition
  index is a decision to make deliberately rather than by default.
- Shops behind Cloudflare or similar bot protection will return challenge pages.
  `--render` helps sometimes; a real API or affiliate feed is the better answer.
- The heuristic strategy is the fallback, and it can pick up a "related products"
  carousel alongside the main grid. Check `method` in `products.json` to see
  which strategy produced each record.
