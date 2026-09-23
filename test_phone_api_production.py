import pytest
from fastapi.testclient import TestClient

import phone_api_production as api


@pytest.fixture()
def client() -> TestClient:
    return TestClient(api.app)


def test_health_endpoint_reports_service_status(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "flipkart_configured" in body
    assert "mock_mode" in body


def test_homepage_contains_search_ui(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "Phone Specs Finder" in response.text
    assert "/get-phone-details?phone_name=" in response.text
    assert "result.textContent" in response.text


def test_phone_alias_returns_curated_specifications(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch(search_query: str) -> dict[str, str | None]:
        assert search_query == "samsung galaxy s24"
        return {
            "title": "Samsung Galaxy S24",
            "price": "₹79,999",
            "image_url": "https://example.com/s24.jpg",
            "buy_url": "https://www.flipkart.com/samsung-galaxy-s24",
        }

    monkeypatch.setattr(api, "fetch_flipkart_data", fake_fetch)

    response = client.get("/get-phone-details", params={"phone_name": "  S24 "})

    assert response.status_code == 200
    body = response.json()
    assert body["product_info"]["name"] == "Samsung Galaxy S24"
    assert body["product_info"]["live_price"] == "₹79,999"
    assert body["full_specifications"]["processor"] == "Snapdragon 8 Gen 3"


def test_unknown_phone_keeps_product_data_and_marks_specs_unknown(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch(search_query: str) -> dict[str, str | None]:
        return {
            "title": "Some Phone",
            "price": "₹10,000",
            "image_url": None,
            "buy_url": "https://www.flipkart.com/search?q=some+phone",
        }

    monkeypatch.setattr(api, "fetch_flipkart_data", fake_fetch)

    response = client.get(
        "/get-phone-details",
        params={"phone_name": "some phone"},
    )

    assert response.status_code == 200
    assert response.json()["full_specifications"]["processor"] == "N/A"


def test_invalid_phone_name_is_rejected(client: TestClient) -> None:
    response = client.get("/get-phone-details", params={"phone_name": "x"})

    assert response.status_code == 422


def test_flipkart_failure_is_not_exposed_to_the_client(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch(search_query: str) -> dict[str, str | None]:
        raise api.FlipkartUpstreamError("private upstream diagnostic")

    monkeypatch.setattr(api, "fetch_flipkart_data", fake_fetch)

    response = client.get(
        "/get-phone-details",
        params={"phone_name": "Samsung Galaxy S24"},
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Product data is temporarily unavailable"
    }
    assert "private upstream diagnostic" not in response.text


def test_flipkart_product_shape_is_parsed_safely() -> None:
    product = api._parse_product(
        {
            "productBaseInfoV1": {
                "title": "Redmi Note 13 Pro",
                "flipkartSellingPrice": {
                    "amount": 24999,
                    "currency": "INR",
                },
                "imageUrls": {
                    "400x400": "https://example.com/redmi.jpg",
                },
                "productUrl": "/redmi-note-13-pro/p/abc",
            }
        },
        "redmi note 13 pro",
    )

    assert product == {
        "title": "Redmi Note 13 Pro",
        "price": "₹24999",
        "image_url": "https://example.com/redmi.jpg",
        "buy_url": "https://www.flipkart.com/redmi-note-13-pro/p/abc",
    }