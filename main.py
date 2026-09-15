import os
import requests
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
from supabase import create_client, Client

app = FastAPI(title="Jivanya Titan ERP Engine", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Supabase Credentials
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://kslgapyssopepcieujgq.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "sb_publishable_-_Lcmap3PWsl9XPjMq1Otg_XdjrOucW")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ----------------- DATA SCHEMAS -----------------

class PouchSpec(BaseModel):
    sku: str
    dead_weight_g: float
    length_cm: float
    width_cm: float
    height_cm: float

class ThermalLabelOCR(BaseModel):
    order_id: str
    channel: str
    customer_name: str
    phone: str
    address: str
    city: str
    pincode: str
    charged_weight_g: int
    actual_weight_g: int

class ReconciliationEntry(BaseModel):
    order_id: str
    channel: str
    expected_payout: float
    settled_payout: float
    settlement_utr: Optional[str] = None

class CompetitorItem(BaseModel):
    marketplace: str
    product_name: str
    competitor_price: float
    jivanya_price: float

# ----------------- TELEGRAM BOT DISPATCHER -----------------

def send_telegram_alert(message: str):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        try:
            requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=8)
        except Exception as e:
            print("Telegram alert error:", e)

# ----------------- 8 PRODUCTION ERP MODULES -----------------

# Module 1: Master Packaging & ₹45 Courier Slab Gatekeeper
@app.post("/api/v1/packaging/validate-slab")
def validate_shipping_slab(spec: PouchSpec):
    # Volumetric Weight = (L x W x H) / 5000 * 1000 with 15% safety buffer
    volumetric = ((spec.length_cm * spec.width_cm * spec.height_cm) / 5000.0) * 1000.0
    buffered_weight = max(spec.dead_weight_g, volumetric) * 1.15
    is_safe = buffered_weight <= 500.0

    return {
        "sku": spec.sku,
        "dead_weight_g": spec.dead_weight_g,
        "volumetric_weight_g": round(volumetric, 2),
        "buffered_weight_g": round(buffered_weight, 2),
        "eligible_for_45_slab": is_safe,
        "status": "APPROVED: ₹45 Slab Locked" if is_safe else "REJECTED: Exceeds 500g slab. Trim pouch height."
    }

# Module 2 & 5: Thermal OCR Scraper & Customer Vault CRM Sync
@app.post("/api/v1/ocr/ingest-label")
def ingest_thermal_label(label: ThermalLabelOCR, background_tasks: BackgroundTasks):
    weight_gap = label.charged_weight_g - label.actual_weight_g
    is_dispute = weight_gap > 50

    try:
        vault = supabase.table("customer_vault").select("*").eq("phone", label.phone).execute()
        if vault.data:
            cust = vault.data[0]
            supabase.table("customer_vault").update({
                "total_orders": cust.get("total_orders", 1) + 1,
                "complete_address": label.address,
                "city": label.city,
                "pincode": label.pincode,
                "last_order_date": "now()"
            }).eq("phone", label.phone).execute()
        else:
            supabase.table("customer_vault").insert({
                "phone": label.phone,
                "full_name": label.customer_name,
                "city": label.city,
                "pincode": label.pincode,
                "complete_address": label.address,
                "total_orders": 1
            }).execute()
    except Exception as err:
        print("Vault CRM update warning:", err)

    try:
        supabase.table("financial_reconciliation").insert({
            "order_id": label.order_id,
            "channel": label.channel,
            "charged_weight_g": label.charged_weight_g,
            "actual_weight_g": label.actual_weight_g,
            "weight_discrepancy_flag": is_dispute,
            "is_dispute_filed": False
        }).execute()
    except Exception as err:
        print("Reconciliation record note:", err)

    if is_dispute:
        alert = (
            f"🚨 *WEIGHT DISCREPANCY DETECTED*\n"
            f"Order ID: `{label.order_id}` ({label.channel})\n"
            f"Actual: `{label.actual_weight_g}g` | Charged: `{label.charged_weight_g}g`\n"
            f"Excess Overcharge: `{weight_gap}g`\n"
            f"Action: Flagged for Carrier Refund Claim."
        )
        background_tasks.add_task(send_telegram_alert, alert)

    return {"status": "SUCCESS", "dispute_flag": is_dispute, "excess_grams": weight_gap}

# Module 4: 3-Way P&L & Settlement Reconciliation
@app.post("/api/v1/finance/reconcile-payout")
def reconcile_payout(data: ReconciliationEntry, background_tasks: BackgroundTasks):
    diff = round(data.expected_payout - data.settled_payout, 2)
    has_leakage = diff > 5.00

    try:
        supabase.table("financial_reconciliation").update({
            "marketplace_settlement_utr": data.settlement_utr,
            "expected_payout": data.expected_payout,
            "settled_payout": data.settled_payout,
            "payout_difference": diff,
            "is_dispute_filed": has_leakage
        }).eq("order_id", data.order_id).execute()
    except Exception as err:
        print("Payout reconciliation note:", err)

    if has_leakage:
        msg = (
            f"⚠️ *PAYOUT LEAKAGE DETECTED*\n"
            f"Order: `{data.order_id}` ({data.channel})\n"
            f"Expected: `₹{data.expected_payout}` | Settled: `₹{data.settled_payout}`\n"
            f"Underpaid Amount: `₹{diff}`\n"
            f"UTR: `{data.settlement_utr or 'N/A'}`"
        )
        background_tasks.add_task(send_telegram_alert, msg)

    return {"status": "RECONCILED", "leakage": has_leakage, "underpaid_amount": diff}

# Module 6: Competitor & Price Hijacker Sentinel
@app.post("/api/v1/sentinel/price-check")
def check_competitor_price(item: CompetitorItem, background_tasks: BackgroundTasks):
    is_undercut = item.competitor_price < item.jivanya_price
    price_gap = round(item.jivanya_price - item.competitor_price, 2)

    if is_undercut:
        alert = (
            f"⚔️ *COMPETITOR UNDERCUT ALERT*\n"
            f"Marketplace: `{item.marketplace}`\n"
            f"Product: `{item.product_name}`\n"
            f"Their Price: `₹{item.competitor_price}` | Our Price: `₹{item.jivanya_price}`\n"
            f"Price Gap: `₹{price_gap}` lower"
        )
        background_tasks.add_task(send_telegram_alert, alert)

    return {"status": "CHECKED", "undercut": is_undercut, "gap": price_gap}

# Module 7: Daily Google SEO & IndexNow Ping Hook
@app.get("/api/v1/seo/ping-indexnow")
def ping_search_engines():
    search_engines = [
        "https://www.bing.com/indexnow",
        "https://api.indexnow.org/indexnow"
    ]
    payload = {
        "host": "jivanyafoods-store.github.io",
        "key": "jivanya2026seosecurekey",
        "keyLocation": "https://jivanyafoods-store.github.io/jivanya2026seosecurekey.txt",
        "urlList": [
            "https://jivanyafoods-store.github.io/jivanyafoods/",
            "https://jivanyafoods-store.github.io/jivanyafoods/#bestsellers",
            "https://jivanyafoods-store.github.io/jivanyafoods/#recommended"
        ]
    }
    responses = {}
    for endpoint in search_engines:
        try:
            r = requests.post(endpoint, json=payload, timeout=6)
            responses[endpoint] = r.status_code
        except Exception as e:
            responses[endpoint] = str(e)

    return {"status": "PING_COMPLETED", "engines": responses}

# Module 8: Telegram Founder Cockpit & Daily GMV Dispatcher
@app.get("/api/v1/founder/daily-briefing")
def founder_daily_briefing():
    try:
        orders_res = supabase.table("orders").select("total_amount, order_status").execute()
        orders = orders_res.data or []
        total_gmv = sum([float(o.get("total_amount", 0)) for o in orders])
        order_count = len(orders)

        report = (
            f"🏛 *JIVANYA FOODS - FOUNDER COCKPIT*\n\n"
            f"📦 Total Orders Ingested: `{order_count}`\n"
            f"💰 Gross GMV Tracked: `₹{total_gmv:,.2f}`\n"
            f"🚚 Central Dispatch Hub: Online\n"
            f"🛡️ Profit Guard & ₹45 Packaging Slab: Active\n"
            f"⚡ System Status: Fully Autonomous"
        )
        send_telegram_alert(report)
        return {"status": "DISPATCHED", "summary": report}
    except Exception as err:
        raise HTTPException(status_code=500, detail=str(err))

@app.get("/")
def root():
    return {"service": "Jivanya Titan ERP Backend", "status": "ONLINE", "mode": "AUTONOMOUS"}
