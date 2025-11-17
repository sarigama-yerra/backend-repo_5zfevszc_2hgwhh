import os
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import requests

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------------
# Models
# -----------------------------
class Address(BaseModel):
    country: str = Field(..., description="Country name")
    city: str = Field(..., description="City name")
    postal_code: str = Field(..., description="Postal/ZIP code")
    street: str = Field(..., description="Street address")


class PricingRequest(BaseModel):
    quantity: int = Field(..., ge=1, le=20, description="Number of bottles")
    address: Address


class PricingResponse(BaseModel):
    product_price: float
    quantity: int
    subtotal: float
    shipping_cost: float
    total: float
    shipping_rule: str


class CreateOrderRequest(PricingRequest):
    pass


class CreateOrderResponse(BaseModel):
    order_id: str
    total: float


class CaptureOrderRequest(BaseModel):
    order_id: str


# -----------------------------
# Constants
# -----------------------------
PRODUCT_NAME = "Testosterone Booster"
UNIT_PRICE = 24.99
CURRENCY = "EUR"


# -----------------------------
# Utils
# -----------------------------

def is_germany(country: str) -> bool:
    return country.strip().lower() in {"germany", "de", "deutschland"}


def is_eu_country(country: str) -> bool:
    eu_countries = {
        "austria", "belgium", "bulgaria", "croatia", "cyprus", "czechia", "czech republic",
        "denmark", "estonia", "finland", "france", "germany", "greece", "hungary",
        "ireland", "italy", "latvia", "lithuania", "luxembourg", "malta", "netherlands",
        "poland", "portugal", "romania", "slovakia", "slovenia", "spain", "sweden"
    }
    return country.strip().lower() in eu_countries


def calculate_shipping(quantity: int, address: Address) -> tuple[float, str]:
    # Domestic Germany
    if is_germany(address.country):
        if address.city.strip().lower() == "berlin":
            return 0.0, "Berlin: Free same-day delivery"
        # Other Germany cities
        if quantity == 2:
            return 0.0, "Germany (non-Berlin): Free shipping for exactly 2 bottles"
        return 7.99, "Germany (non-Berlin): €7.99 shipping"

    # EU (outside Germany)
    if is_eu_country(address.country):
        if not is_germany(address.country):
            if quantity == 3:
                return 0.0, "EU (non-DE): Free shipping for exactly 3 bottles"
            return 14.99, "EU (non-DE): €14.99 shipping"

    # Outside EU
    if quantity == 5:
        return 0.0, "International: Free shipping for exactly 5 bottles"
    return 19.99, "International: €19.99 shipping"


def round2(x: float) -> float:
    return float(f"{x:.2f}")


# -----------------------------
# Core Endpoints
# -----------------------------
@app.get("/")
def read_root():
    return {"message": "Ecommerce Backend Running"}


@app.post("/api/calculate-pricing", response_model=PricingResponse)
def api_calculate_pricing(payload: PricingRequest):
    shipping_cost, rule = calculate_shipping(payload.quantity, payload.address)
    subtotal = UNIT_PRICE * payload.quantity
    total = subtotal + shipping_cost
    return PricingResponse(
        product_price=round2(UNIT_PRICE),
        quantity=payload.quantity,
        subtotal=round2(subtotal),
        shipping_cost=round2(shipping_cost),
        total=round2(total),
        shipping_rule=rule,
    )


# -----------------------------
# PayPal Integration (Server-side create/capture)
# -----------------------------
PAYPAL_BASE = os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com")
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID")
PAYPAL_SECRET = os.getenv("PAYPAL_SECRET")


def paypal_get_access_token() -> str:
    if not PAYPAL_CLIENT_ID or not PAYPAL_SECRET:
        raise HTTPException(status_code=500, detail="PayPal credentials not configured on server")
    resp = requests.post(
        f"{PAYPAL_BASE}/v1/oauth2/token",
        auth=(PAYPAL_CLIENT_ID, PAYPAL_SECRET),
        data={"grant_type": "client_credentials"},
        headers={"Accept": "application/json", "Accept-Language": "en_US"},
        timeout=15,
    )
    if resp.status_code >= 400:
        raise HTTPException(status_code=500, detail=f"PayPal auth failed: {resp.text[:200]}")
    return resp.json()["access_token"]


@app.post("/api/checkout/create-order", response_model=CreateOrderResponse)
def create_order(payload: CreateOrderRequest):
    # Calculate totals using the same logic
    pricing = api_calculate_pricing(payload)

    access_token = paypal_get_access_token()

    order_body = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "reference_id": "TESTOSTERONE-BOOSTER",
                "description": PRODUCT_NAME,
                "amount": {
                    "currency_code": CURRENCY,
                    "value": f"{pricing.total:.2f}",
                    "breakdown": {
                        "item_total": {
                            "currency_code": CURRENCY,
                            "value": f"{pricing.subtotal:.2f}",
                        },
                        "shipping": {
                            "currency_code": CURRENCY,
                            "value": f"{pricing.shipping_cost:.2f}",
                        },
                    },
                },
                "items": [
                    {
                        "name": PRODUCT_NAME,
                        "quantity": str(pricing.quantity),
                        "unit_amount": {
                            "currency_code": CURRENCY,
                            "value": f"{UNIT_PRICE:.2f}",
                        },
                        "category": "PHYSICAL_GOODS",
                    }
                ],
                "shipping": {
                    "address": {
                        "address_line_1": payload.address.street,
                        "admin_area_2": payload.address.city,
                        "postal_code": payload.address.postal_code,
                        "country_code": payload.address.country[:2].upper(),
                    }
                },
            }
        ],
        "application_context": {
            "shipping_preference": "SET_PROVIDED_ADDRESS",
            "user_action": "PAY_NOW",
        },
    }

    resp = requests.post(
        f"{PAYPAL_BASE}/v2/checkout/orders",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {access_token}"},
        json=order_body,
        timeout=20,
    )

    if resp.status_code >= 400:
        raise HTTPException(status_code=500, detail=f"PayPal order creation failed: {resp.text[:300]}")

    data = resp.json()
    return CreateOrderResponse(order_id=data.get("id"), total=pricing.total)


@app.post("/api/checkout/capture-order")
def capture_order(payload: CaptureOrderRequest):
    access_token = paypal_get_access_token()
    resp = requests.post(
        f"{PAYPAL_BASE}/v2/checkout/orders/{payload.order_id}/capture",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    if resp.status_code >= 400:
        raise HTTPException(status_code=500, detail=f"PayPal capture failed: {resp.text[:300]}")
    return resp.json()


@app.get("/test")
def test_database():
    """Basic health + env check"""
    response = {
        "backend": "✅ Running",
        "paypal_client": "✅ Set" if bool(PAYPAL_CLIENT_ID) else "❌ Not Set",
        "paypal_secret": "✅ Set" if bool(PAYPAL_SECRET) else "❌ Not Set",
    }
    return response


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
