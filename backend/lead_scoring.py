"""lead_scoring.py - AI Lead Scoring Engine for KeyForAgents.com
Scores real estate agent leads based on engagement, budget, and intent signals.
Integrates with n8n webhooks and Notion CRM."""

import os
import json
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime

import requests
from flask import Flask, request, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
N8N_WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL", "https://niorlusx.app.n8n.cloud/webhook/lead-inbound")
NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
NOTION_DB_ID = os.environ.get("NOTION_DB_ID", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
# Lead dataclass
# ---------------------------------------------------------------------------
@dataclass
class Lead:
    name: str
    email: str
    phone: Optional[str] = None
    suburb: Optional[str] = None
    budget: Optional[float] = None
    source: str = "website"
    utm_campaign: Optional[str] = None
    page_views: int = 0
    time_on_site: int = 0  # seconds
    form_fills: int = 0
    score: int = field(default=0, init=False)
    tier: str = field(default="cold", init=False)
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

# ---------------------------------------------------------------------------
# Scoring engine
# ---------------------------------------------------------------------------
SCORE_RULES = [
    # (field, condition_fn, points, label)
    ("budget",       lambda v: v and v >= 800_000,     30, "high budget"),
    ("budget",       lambda v: v and 400_000 <= v < 800_000, 15, "mid budget"),
    ("page_views",   lambda v: v >= 10,                20, "high engagement"),
    ("page_views",   lambda v: 5 <= v < 10,            10, "mid engagement"),
    ("time_on_site", lambda v: v >= 180,               15, "long session"),
    ("form_fills",   lambda v: v >= 2,                 20, "multiple forms"),
    ("form_fills",   lambda v: v == 1,                 10, "one form"),
    ("phone",        lambda v: bool(v),                10, "phone provided"),
    ("utm_campaign", lambda v: v and "paid" in v,      10, "paid traffic"),
    ("suburb",       lambda v: bool(v),                 5, "suburb known"),
]

def score_lead(lead: Lead) -> Lead:
    """Apply scoring rules and assign tier."""
    total = 0
    reasons = []
    for field_name, condition, points, label in SCORE_RULES:
        value = getattr(lead, field_name)
        if condition(value):
            total += points
            reasons.append(label)
    lead.score = total
    if total >= 60:
        lead.tier = "hot"
    elif total >= 30:
        lead.tier = "warm"
    else:
        lead.tier = "cold"
    logger.info("Scored %s: %d (%s) — %s", lead.email, total, lead.tier, ", ".join(reasons))
    return lead

# ---------------------------------------------------------------------------
# Notion CRM
# ---------------------------------------------------------------------------
def push_to_notion(lead: Lead) -> dict:
    """Create or update a Notion page for the lead."""
    if not NOTION_API_KEY or not NOTION_DB_ID:
        logger.warning("Notion credentials not set, skipping.")
        return {}
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    payload = {
        "parent": {"database_id": NOTION_DB_ID},
        "properties": {
            "Name":   {"title": [{"text": {"content": lead.name}}]},
            "Email":  {"email": lead.email},
            "Phone":  {"phone_number": lead.phone or ""},
            "Suburb": {"rich_text": [{"text": {"content": lead.suburb or ""}}]},
            "Budget": {"number": lead.budget or 0},
            "Score":  {"number": lead.score},
            "Tier":   {"select": {"name": lead.tier.capitalize()}},
            "Source": {"rich_text": [{"text": {"content": lead.source}}]},
            "Created At": {"date": {"start": lead.created_at}},
        },
    }
    resp = requests.post("https://api.notion.com/v1/pages", headers=headers, json=payload, timeout=10)
    resp.raise_for_status()
    logger.info("Notion page created for %s", lead.email)
    return resp.json()

# ---------------------------------------------------------------------------
# Telegram alert
# ---------------------------------------------------------------------------
def send_telegram_alert(lead: Lead) -> None:
    """Send hot/warm lead alert to Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    if lead.tier not in ("hot", "warm"):
        return
    emoji = "🔥" if lead.tier == "hot" else "♨️"
    msg = (
        f"{emoji} *New {lead.tier.upper()} Lead*\n"
        f"Name: {lead.name}\n"
        f"Email: {lead.email}\n"
        f"Phone: {lead.phone or 'N/A'}\n"
        f"Suburb: {lead.suburb or 'N/A'}\n"
        f"Budget: ${lead.budget:,.0f}\n" if lead.budget else f"Budget: Unknown\n"
        f"Score: {lead.score}\n"
        f"Source: {lead.source}"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    logger.info("Telegram alert sent for %s", lead.email)

# ---------------------------------------------------------------------------
# Forward to n8n
# ---------------------------------------------------------------------------
def forward_to_n8n(lead: Lead) -> None:
    """POST scored lead data to n8n webhook for further automation."""
    try:
        requests.post(N8N_WEBHOOK_URL, json=asdict(lead), timeout=10)
        logger.info("Forwarded to n8n: %s", lead.email)
    except Exception as exc:
        logger.error("n8n forward failed: %s", exc)

# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "lead-scoring"}), 200

@app.route("/score", methods=["POST"])
def score_endpoint():
    """Accept lead JSON, score it, push to Notion, alert Telegram, forward to n8n."""
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "No JSON body"}), 400
    required = ["name", "email"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {missing}"}), 422
    lead = Lead(
        name=data["name"],
        email=data["email"],
        phone=data.get("phone"),
        suburb=data.get("suburb"),
        budget=data.get("budget"),
        source=data.get("source", "website"),
        utm_campaign=data.get("utm_campaign"),
        page_views=int(data.get("page_views", 0)),
        time_on_site=int(data.get("time_on_site", 0)),
        form_fills=int(data.get("form_fills", 0)),
    )
    lead = score_lead(lead)
    push_to_notion(lead)
    send_telegram_alert(lead)
    forward_to_n8n(lead)
    return jsonify(asdict(lead)), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
