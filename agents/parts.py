"""find_parts(job) -> list[Part].   Owner: Person B.

For each likely part, find the cheapest reasonable listing and return it as a
link + price. Links only: NEVER auto-buy.

Lookup order per part: eBay Browse API -> SerpAPI Google Shopping ->
FALLBACK_CATALOG (hardcoded) -> a Home Depot search link with no price.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from urllib.parse import quote_plus

import httpx

from shared.db import log_event
from shared.schema import Job, Part

HTTP_TIMEOUT = 3.0

# FALLBACK: approximate big-box retail prices (estimates, not live) used when the APIs fail.
# Links are store searches, not specific products.
FALLBACK_CATALOG: dict[str, tuple[float, str]] = {
    "1-1/2 in P-trap kit": (8.97, "https://www.homedepot.com/s/p-trap%201-1%2F2"),
    "1-1/4 in P-trap kit": (7.98, "https://www.homedepot.com/s/p-trap%201-1%2F4"),
    "1-1/2 in slip-joint washer": (2.48, "https://www.homedepot.com/s/slip%20joint%20washer"),
    "2 in toilet flapper": (6.97, "https://www.homedepot.com/s/toilet%20flapper"),
    "3 in toilet flapper": (9.98, "https://www.homedepot.com/s/3%20in%20toilet%20flapper"),
    "universal toilet fill valve": (11.97, "https://www.homedepot.com/s/toilet%20fill%20valve"),
    "1/4 in x 25 ft drain auger": (24.97, "https://www.homedepot.com/s/drain%20auger%2025%20ft"),
    "single-handle faucet cartridge": (19.98, "https://www.homedepot.com/s/faucet%20cartridge"),
    "faucet O-ring kit": (4.98, "https://www.homedepot.com/s/faucet%20o-ring%20kit"),
    "3/8 in compression x 1/2 in FIP braided supply line": (
        8.48, "https://www.homedepot.com/s/braided%20faucet%20supply%20line"),
    "40 gal water heater thermocouple": (14.97, "https://www.homedepot.com/s/water%20heater%20thermocouple"),
    "3/4 in water heater flex connector": (13.98, "https://www.homedepot.com/s/water%20heater%20connector"),
}

_cache: dict[str, tuple[Optional[float], Optional[str], str]] = {}
_ebay_token: tuple[str, float] | None = None  # (token, expires_at)


# ---------- eBay Browse API ----------

def _get_ebay_token() -> Optional[str]:
    global _ebay_token
    if _ebay_token and _ebay_token[1] > time.time() + 60:
        return _ebay_token[0]
    cid, secret = os.getenv("EBAY_CLIENT_ID"), os.getenv("EBAY_CLIENT_SECRET")
    if not (cid and secret):
        return None
    r = httpx.post("https://api.ebay.com/identity/v1/oauth2/token",
                   auth=(cid, secret), timeout=HTTP_TIMEOUT,
                   data={"grant_type": "client_credentials",
                         "scope": "https://api.ebay.com/oauth/api_scope"})
    r.raise_for_status()
    body = r.json()
    _ebay_token = (body["access_token"], time.time() + int(body.get("expires_in", 7200)))
    return _ebay_token[0]


def _search_ebay(name: str) -> Optional[tuple[float, str]]:
    token = _get_ebay_token()
    if not token:
        return None
    r = httpx.get("https://api.ebay.com/buy/browse/v1/item_summary/search",
                  headers={"Authorization": f"Bearer {token}",
                           "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
                  params={"q": name, "limit": 10,
                          "filter": "buyingOptions:{FIXED_PRICE},conditions:{NEW}"},
                  timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    items = [(float(i["price"]["value"]), i["itemWebUrl"])
             for i in r.json().get("itemSummaries", [])
             if i.get("price", {}).get("currency") == "USD" and i.get("itemWebUrl")]
    # Cheapest among the top relevance matches (sort=price surfaces junk listings).
    return min(items) if items else None


# ---------- SerpAPI Google Shopping ----------

def _search_serpapi(name: str) -> Optional[tuple[float, str]]:
    key = os.getenv("SERPAPI_KEY")
    if not key:
        return None
    r = httpx.get("https://serpapi.com/search.json",
                  params={"engine": "google_shopping", "q": name, "api_key": key, "num": 10},
                  timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    items = []
    for i in r.json().get("shopping_results", [])[:5]:
        price, link = i.get("extracted_price"), i.get("product_link") or i.get("link")
        if isinstance(price, (int, float)) and link:
            items.append((float(price), link))
    return min(items) if items else None


# ---------- lookup ----------

def _lookup(name: str) -> tuple[Optional[float], Optional[str], str, list[str]]:
    """Return (price, url, source, errors) for one part name."""
    if name in _cache:
        price, url, source = _cache[name]
        return price, url, f"{source}, cached", []
    errors = []
    for source, fn in (("eBay", _search_ebay), ("Google Shopping", _search_serpapi)):
        try:
            hit = fn(name)
        except Exception as e:
            errors.append(f"{source} {e.__class__.__name__}")
            continue
        if hit:
            _cache[name] = (hit[0], hit[1], source)
            return hit[0], hit[1], source, errors
    if name in FALLBACK_CATALOG:
        price, url = FALLBACK_CATALOG[name]
        return price, url, "FALLBACK catalog", errors
    return None, f"https://www.homedepot.com/s/{quote_plus(name)}", "search link only", errors


def find_parts(job: Job) -> list[Part]:
    if not job.parts:
        log_event(job.id, "parts", "no likely parts to look up")
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(job.parts))) as pool:
        results = list(pool.map(_lookup, [p.name for p in job.parts]))

    out = []
    for p, (price, url, source, errors) in zip(job.parts, results):
        out.append(p.model_copy(update={"price": price, "url": url}))
        price_txt = f"${price:.2f}" if price is not None else "no price"
        err_txt = f" ({'; '.join(errors)} failed)" if errors else ""
        log_event(job.id, "parts", f"{p.name}: {price_txt} via {source}{err_txt}")
    log_event(job.id, "parts",
              f"matched {sum(p.price is not None for p in out)}/{len(out)} parts (links only, nothing ordered)")
    return out
