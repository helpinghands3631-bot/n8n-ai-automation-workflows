"""webhook_receiver.py - Central n8n Webhook Receiver & Router
Receives payloads from Stripe, Notion, Telegram and routes to correct handler.
Acts as the glue layer between external services and n8n workflows."""

import os
import hmac
import hashlib
import logging
import json
from functools import wraps
from typing import Callable

import requests
from flask import Flask, request, jsonify, abort

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
N8N_BASE = os.environ.get("N8N_BASE_URL", "https://niorlusx.app.n8n.cloud")
N8N_ROUTES = {
    "payment_intent.succeeded":   f"{N8N_BASE}/webhook/stripe-payment-success",
    "payment_intent.failed":      f"{N8N_BASE}/webhook/stripe-payment-failed",
    "customer.subscription.created": f"{N8N_BASE}/webhook/stripe-sub-created",
    "customer.subscription.deleted": f"{N8N_BASE}/webhook/stripe-sub-cancelled",
    "checkout.session.completed": f"{N8N_BASE}/webhook/stripe-checkout-complete",
    "lead.inbound":               f"{N8N_BASE}/webhook/lead-inbound",
    "support.ticket":             f"{N8N_BASE}/webhook/support-ticket",
    "notion.crm_update":          f"{N8N_BASE}/webhook/notion-crm-update",
}

# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------
def verify_stripe_signature(payload: bytes, sig_header: str, secret: str) -> bool:
    """Verify Stripe webhook signature (HMAC-SHA256)."""
    if not secret:
        logger.warning("Stripe secret not configured, skipping verification")
        return True
    try:
        parts = {k: v for part in sig_header.split(",") for k, v in [part.split("=", 1)]}
        ts = parts.get("t", "")
        sigs = [v for k, v in parts.items() if k == "v1"]
        signed = f"{ts}.{payload.decode('utf-8')}"
        expected = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
        return any(hmac.compare_digest(expected, sig) for sig in sigs)
    except Exception as exc:
        logger.error("Signature verification error: %s", exc)
        return False

# ---------------------------------------------------------------------------
# Event router
# ---------------------------------------------------------------------------
def route_event(event_type: str, payload: dict) -> dict:
    """Forward event to the correct n8n webhook URL."""
    url = N8N_ROUTES.get(event_type)
    if not url:
        logger.warning("No route for event type: %s", event_type)
        return {"status": "ignored", "event_type": event_type}
    try:
        resp = requests.post(url, json={"event_type": event_type, "data": payload}, timeout=15)
        resp.raise_for_status()
        logger.info("Routed %s -> %s [%s]", event_type, url, resp.status_code)
        return {"status": "forwarded", "event_type": event_type, "n8n_status": resp.status_code}
    except requests.exceptions.Timeout:
        logger.error("Timeout routing %s to n8n", event_type)
        return {"status": "timeout", "event_type": event_type}
    except Exception as exc:
        logger.error("Route error for %s: %s", event_type, exc)
        return {"status": "error", "event_type": event_type, "error": str(exc)}

# ---------------------------------------------------------------------------
# Stripe webhook
# ---------------------------------------------------------------------------
@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")
    if not verify_stripe_signature(payload, sig, STRIPE_WEBHOOK_SECRET):
        logger.warning("Invalid Stripe signature")
        abort(400, "Invalid signature")
    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        abort(400, "Invalid JSON")
    event_type = event.get("type", "unknown")
    result = route_event(event_type, event.get("data", {}).get("object", {}))
    return jsonify(result), 200

# ---------------------------------------------------------------------------
# Generic inbound webhook (from keyforagents.com contact/lead forms)
# ---------------------------------------------------------------------------
@app.route("/webhook/lead", methods=["POST"])
def lead_webhook():
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "Empty payload"}), 400
    # Minimal validation
    if not data.get("email"):
        return jsonify({"error": "email required"}), 422
    result = route_event("lead.inbound", data)
    return jsonify(result), 200

# ---------------------------------------------------------------------------
# Support ticket webhook (Helping Hands NDIS)
# ---------------------------------------------------------------------------
@app.route("/webhook/support", methods=["POST"])
def support_webhook():
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "Empty payload"}), 400
    required = ["name", "issue"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing: {missing}"}), 422
    result = route_event("support.ticket", data)
    return jsonify(result), 200

# ---------------------------------------------------------------------------
# Notion CRM update hook
# ---------------------------------------------------------------------------
@app.route("/webhook/notion", methods=["POST"])
def notion_webhook():
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "Empty payload"}), 400
    result = route_event("notion.crm_update", data)
    return jsonify(result), 200

# ---------------------------------------------------------------------------
# Health & routing table
# ---------------------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "webhook-receiver", "routes": list(N8N_ROUTES.keys())}), 200

@app.route("/routes", methods=["GET"])
def routes_list():
    return jsonify({"routes": N8N_ROUTES}), 200

# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------
@app.errorhandler(400)
def bad_request(e):
    return jsonify({"error": str(e)}), 400

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "endpoint not found"}), 404

@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "internal server error"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5002)), debug=False)
