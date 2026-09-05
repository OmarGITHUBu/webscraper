1. Run it

Open PowerShell in C:\Users\omarh\Documents\py.webscraper and point it at a shop's category page (not a single product page — a listing page with a grid):

.\.venv\Scripts\python.exe scraper.py https://shop.example.com/collections/all

Use .\.venv\Scripts\python.exe, not python — the venv is where the dependencies live.

2. Read the log

It tells you what worked:

INFO [page 1] https://shop.example.com/collections/all
INFO   json-ld   -> 24        <- found 24 products from structured data
INFO   total so far: 24
INFO downloading 24 images -> ...\output\images
INFO done: 24 products (24 with a price)
