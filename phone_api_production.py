"""Phone specifications API with optional Flipkart Affiliate API integration.

Run locally:
    uvicorn phone_api_production:app --reload

Required in production:
    FLIPKART_AFFILIATE_ID=...
    FLIPKART_AFFILIATE_TOKEN=...

The service deliberately fails with HTTP 503 when Flipkart credentials are
missing, rather than silently returning fake prices. Set
USE_MOCK_FLIPKART=true only for local development.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from fastapi.responses import HTMLResponse


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("phone-api")


@dataclass(frozen=True)
class Settings:
    flipkart_affiliate_id: str | None
    flipkart_affiliate_token: str | None
    flipkart_api_url: str
    timeout_seconds: float
    use_mock_flipkart: bool
    cors_origins: tuple[str, ...]

    @classmethod
    def from_environment(cls) -> "Settings":
        timeout = float(os.getenv("FLIPKART_TIMEOUT_SECONDS", "8"))
        if timeout <= 0:
            raise ValueError("FLIPKART_TIMEOUT_SECONDS must be greater than zero")

        origins = tuple(
            origin.strip()
            for origin in os.getenv("CORS_ORIGINS", "").split(",")
            if origin.strip()
        )
        return cls(
            flipkart_affiliate_id=os.getenv("FLIPKART_AFFILIATE_ID"),
            flipkart_affiliate_token=os.getenv("FLIPKART_AFFILIATE_TOKEN"),
            flipkart_api_url=os.getenv(
                "FLIPKART_API_URL",
                "https://affiliate-api.flipkart.net/affiliate/1.0/search.json",
            ),
            timeout_seconds=timeout,
            use_mock_flipkart=os.getenv("USE_MOCK_FLIPKART", "false").lower()
            in {"1", "true", "yes"},
            cors_origins=origins,
        )


settings = Settings.from_environment()


PHONE_SPECS_DB: dict[str, dict[str, str]] = {
    "samsung galaxy s24": {
        "processor": "Snapdragon 8 Gen 3",
        "ram_rom": "8GB / 12GB RAM | 256GB / 512GB ROM",
        "display": "6.2 inch Dynamic AMOLED 2X, 120Hz",
        "camera": "50 MP + 10 MP + 12 MP | 12 MP Front",
        "battery": "4000 mAh (25W Fast)",
        "os": "Android 14, One UI 6.1",
    },
    "redmi note 13 pro": {
        "processor": "Snapdragon 7s Gen 2",
        "ram_rom": "8GB / 12GB RAM | 128GB / 256GB ROM",
        "display": "6.67 inch AMOLED, 120Hz",
        "camera": "200 MP + 8 MP + 2 MP | 16 MP Front",
        "battery": "5100 mAh (67W Fast)",
        "os": "Android 13, MIUI 14",
    },
}

PHONE_ALIASES = {
    "s24": "samsung galaxy s24",
    "samsung s24": "samsung galaxy s24",
    "samsung galaxy s 24": "samsung galaxy s24",
    "redmi note 13 pro 5g": "redmi note 13 pro",
    "redmi note 13 pro 5g phone": "redmi note 13 pro",
}


class ProductInfo(BaseModel):
    name: str
    live_price: str | None
    image: str | None
    buy_url: str


class PhoneDetailsResponse(BaseModel):
    product_info: ProductInfo
    full_specifications: dict[str, str]


class FlipkartUpstreamError(Exception):
    """Raised when Flipkart cannot provide a usable product result."""


def normalize_phone_name(phone_name: str) -> str:
    return " ".join(phone_name.casefold().split())


def resolve_phone_name(phone_name: str) -> str:
    normalized = normalize_phone_name(phone_name)
    return PHONE_ALIASES.get(normalized, normalized)


def _first_image(image_urls: Any) -> str | None:
    if not isinstance(image_urls, dict):
        return None
    for key in ("400x400", "200x200", "100x100"):
        if isinstance(image_urls.get(key), str):
            return image_urls[key]
    for value in image_urls.values():
        if isinstance(value, str):
            return value
    return None


def _parse_product(product: Any, search_query: str) -> dict[str, str | None] | None:
    if not isinstance(product, dict):
        return None

    base_info = product.get("productBaseInfoV1") or product.get("productBaseInfo") or {}
    if not isinstance(base_info, dict):
        return None

    title = base_info.get("title") or product.get("title")
    if not isinstance(title, str) or not title.strip():
        return None

    price_info = base_info.get("flipkartSellingPrice") or {}
    price: str | None = None
    if isinstance(price_info, dict):
        price = price_info.get("price")
        if not price and price_info.get("amount") is not None:
            currency = price_info.get("currency", "INR")
            symbol = "₹" if currency == "INR" else currency
            price = f"{symbol}{price_info['amount']}"
    if price is not None and not isinstance(price, str):
        price = str(price)

    product_url = base_info.get("productUrl") or product.get("productUrl")
    if not isinstance(product_url, str) or not product_url:
        product_url = "https://www.flipkart.com/search?q=" + quote_plus(search_query)
    elif product_url.startswith("/"):
        product_url = "https://www.flipkart.com" + product_url

    return {
        "title": title.strip(),
        "price": price,
        "image_url": _first_image(base_info.get("imageUrls")),
        "buy_url": product_url,
    }


def _mock_product(search_query: str) -> dict[str, str | None]:
    return {
        "title": search_query.title(),
        "price": None,
        "image_url": None,
        "buy_url": "https://www.flipkart.com/search?q=" + quote_plus(search_query),
    }


async def fetch_flipkart_data(search_query: str) -> dict[str, str | None]:
    """Return the first matching Flipkart product or raise a typed error."""
    if settings.use_mock_flipkart:
        return _mock_product(search_query)

    if not settings.flipkart_affiliate_id or not settings.flipkart_affiliate_token:
        raise FlipkartUpstreamError("Flipkart credentials are not configured")

    headers = {
        "Fk-Affiliate-Id": settings.flipkart_affiliate_id,
        "Fk-Affiliate-Token": settings.flipkart_affiliate_token,
        "Accept": "application/json",
        "User-Agent": "phone-specs-api/1.0",
    }
    params = {"query": search_query, "resultCount": 1}

    try:
        async with httpx.AsyncClient(timeout=settings.timeout_seconds) as client:
            response = await client.get(
                settings.flipkart_api_url,
                params=params,
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.TimeoutException as exc:
        raise FlipkartUpstreamError("Flipkart request timed out") from exc
    except httpx.HTTPStatusError as exc:
        logger.warning("Flipkart returned HTTP %s", exc.response.status_code)
        raise FlipkartUpstreamError("Flipkart returned an HTTP error") from exc
    except (httpx.RequestError, ValueError) as exc:
        logger.warning("Flipkart request failed: %s", type(exc).__name__)
        raise FlipkartUpstreamError("Flipkart request failed") from exc

    if not isinstance(payload, dict):
        raise FlipkartUpstreamError("Flipkart returned an unexpected response shape")

    products = payload.get("productInfoList", payload.get("products", []))
    if not isinstance(products, list) or not products:
        raise FlipkartUpstreamError("No matching Flipkart product was found")

    product = _parse_product(products[0], search_query)
    if product is None:
        raise FlipkartUpstreamError("Flipkart returned an unexpected product shape")
    return product


app = FastAPI(
    title="Phone Specs & Flipkart API Server",
    version="1.0.0",
    description="Returns curated phone specifications and the first live Flipkart result.",
)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home() -> str:
    return """
    <!doctype html>
    <html lang="hi">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Phone Specs Search</title>
      <style>
        :root {
          color-scheme: light;
          font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        }
        body {
          margin: 0;
          min-height: 100vh;
          display: grid;
          place-items: center;
          padding: 24px;
          box-sizing: border-box;
          background: #f4f4f9;
          color: #1f2937;
        }
        main {
          width: min(100%, 680px);
          padding: 28px;
          background: white;
          border-radius: 14px;
          box-shadow: 0 10px 30px rgba(15, 23, 42, 0.1);
        }
        h1 { margin-top: 0; }
        form { display: flex; gap: 10px; }
        input {
          flex: 1;
          min-width: 0;
          padding: 12px;
          font-size: 16px;
          border: 1px solid #cbd5e1;
          border-radius: 7px;
        }
        button {
          padding: 12px 20px;
          font-size: 16px;
          color: white;
          background: #2563eb;
          border: 0;
          border-radius: 7px;
          cursor: pointer;
        }
        button:disabled { opacity: 0.65; cursor: wait; }
        #result {
          margin-top: 20px;
          padding: 16px;
          min-height: 24px;
          overflow-x: auto;
          white-space: pre-wrap;
          background: #f8fafc;
          border-radius: 8px;
        }
        .error { color: #b91c1c; }
        @media (max-width: 520px) {
          form { flex-direction: column; }
        }
      </style>
    </head>
    <body>
      <main>
        <h1>Phone Specs Finder</h1>
        <p>फोन का नाम लिखकर specifications और product details खोजें।</p>
        <form id="searchForm">
          <input
            id="phoneInput"
            type="search"
            placeholder="जैसे: Samsung Galaxy S24"
            autocomplete="off"
            required
          >
          <button id="searchButton" type="submit">Search</button>
        </form>
        <pre id="result" aria-live="polite"></pre>
      </main>
      <script>
        const form = document.getElementById("searchForm");
        const input = document.getElementById("phoneInput");
        const button = document.getElementById("searchButton");
        const result = document.getElementById("result");

        form.addEventListener("submit", async (event) => {
          event.preventDefault();
          const phone = input.value.trim();
          if (!phone) return;

          button.disabled = true;
          result.className = "";
          result.textContent = "Searching...";
          try {
            const response = await fetch(
              `/get-phone-details?phone_name=${encodeURIComponent(phone)}`
            );
            const data = await response.json();
            if (!response.ok) {
              throw new Error(data.detail || "Phone details could not be loaded.");
            }
            result.textContent = JSON.stringify(data, null, 2);
          } catch (error) {
            result.className = "error";
            result.textContent = error.message || "Something went wrong.";
          } finally {
            button.disabled = false;
          }
        });
      </script>
    </body>
    </html>
    """


@app.get("/health", tags=["system"])
async def health() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "flipkart_configured": bool(
            settings.flipkart_affiliate_id and settings.flipkart_affiliate_token
        ),
        "mock_mode": settings.use_mock_flipkart,
    }


@app.get(
    "/get-phone-details",
    response_model=PhoneDetailsResponse,
    tags=["phones"],
)
async def get_phone_details(
    phone_name: str = Query(..., min_length=2, max_length=100),
) -> PhoneDetailsResponse:
    clean_name = resolve_phone_name(phone_name)

    try:
        product = await fetch_flipkart_data(clean_name)
    except FlipkartUpstreamError as exc:
        raise HTTPException(
            status_code=503,
            detail="Product data is temporarily unavailable",
        ) from exc

    specifications = PHONE_SPECS_DB.get(
        clean_name,
        {
            "processor": "N/A",
            "ram_rom": "N/A",
            "display": "N/A",
            "camera": "N/A",
            "battery": "N/A",
            "os": "N/A",
        },
    )
    return PhoneDetailsResponse(
        product_info=ProductInfo(
            name=product["title"] or clean_name.title(),
            live_price=product["price"],
            image=product["image_url"],
            buy_url=product["buy_url"] or "",
        ),
        full_specifications=specifications,
    )