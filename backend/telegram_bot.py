"""telegram_bot.py - Telegram Bot for KeyForAgents.com & Helping Hands
Handles inbound messages, commands, and forwards to n8n automation.
Supports /start, /status, /newlead, /help commands."""

import os
import logging
import json
from typing import Optional

import requests
from flask import Flask, request, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
N8N_LEAD_WEBHOOK = os.environ.get("N8N_LEAD_WEBHOOK", "https://niorlusx.app.n8n.cloud/webhook/lead-inbound")
N8N_SUPPORT_WEBHOOK = os.environ.get("N8N_SUPPORT_WEBHOOK", "https://niorlusx.app.n8n.cloud/webhook/support-ticket")
ALLOWED_ADMIN_IDS = [int(x) for x in os.environ.get("TELEGRAM_ADMIN_IDS", "0").split(",") if x.strip().isdigit()]

# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------
def send_message(chat_id: int, text: str, parse_mode: str = "Markdown") -> dict:
    resp = requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
        timeout=10,
    )
    return resp.json()

def send_reply_keyboard(chat_id: int, text: str, buttons: list) -> dict:
    keyboard = {"keyboard": [[{"text": b}] for b in buttons], "resize_keyboard": True, "one_time_keyboard": True}
    resp = requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={"chat_id": chat_id, "text": text, "reply_markup": keyboard},
        timeout=10,
    )
    return resp.json()

def set_webhook(webhook_url: str) -> dict:
    resp = requests.post(f"{TELEGRAM_API}/setWebhook", json={"url": webhook_url}, timeout=10)
    return resp.json()

# ---------------------------------------------------------------------------
# Session state (in-memory; use Redis for production multi-instance)
# ---------------------------------------------------------------------------
_sessions: dict = {}

def get_session(chat_id: int) -> dict:
    return _sessions.setdefault(chat_id, {"state": "idle", "data": {}})

def clear_session(chat_id: int) -> None:
    _sessions[chat_id] = {"state": "idle", "data": {}}

# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
def handle_start(chat_id: int, user: dict) -> None:
    name = user.get("first_name", "there")
    send_message(
        chat_id,
        f"*Welcome, {name}!* 🏠\n\n"
        "I'm the KeyForAgents assistant bot. I can help you:\n"
        "\u2022 Submit a new lead: /newlead\n"
        "\u2022 Check system status: /status\n"
        "\u2022 Get help: /help\n\n"
        "_Powered by KeyForAgents.com_",
    )

def handle_status(chat_id: int) -> None:
    # Check if n8n webhook is reachable
    try:
        r = requests.get(N8N_LEAD_WEBHOOK.replace("/webhook/", "/healthz"), timeout=5)
        n8n_status = "\u2705 Online" if r.status_code < 500 else "\u274c Degraded"
    except Exception:
        n8n_status = "\u26a0\ufe0f Unknown"
    send_message(
        chat_id,
        f"*System Status*\n\nn8n Automation: {n8n_status}\nTelegram Bot: \u2705 Online\nLead Scoring: \u2705 Active",
    )

def handle_help(chat_id: int) -> None:
    send_message(
        chat_id,
        "*Available Commands*\n\n"
        "/start \u2014 Welcome message\n"
        "/newlead \u2014 Submit a new lead\n"
        "/status \u2014 Check system status\n"
        "/help \u2014 Show this message\n\n"
        "For support: dean@helpinghands.com.au",
    )

def handle_newlead_flow(chat_id: int, text: str) -> None:
    session = get_session(chat_id)
    state = session["state"]
    data = session["data"]

    if state == "idle":
        session["state"] = "await_name"
        send_message(chat_id, "*New Lead Submission*\n\nPlease enter the lead's *full name*:")
        return

    if state == "await_name":
        data["name"] = text
        session["state"] = "await_email"
        send_message(chat_id, f"Got it! Now enter the lead's *email address*:")
        return

    if state == "await_email":
        data["email"] = text
        session["state"] = "await_phone"
        send_message(chat_id, "Enter their *phone number* (or type 'skip'):")
        return

    if state == "await_phone":
        data["phone"] = None if text.lower() == "skip" else text
        session["state"] = "await_suburb"
        send_message(chat_id, "Enter their *suburb* (or type 'skip'):")
        return

    if state == "await_suburb":
        data["suburb"] = None if text.lower() == "skip" else text
        session["state"] = "await_budget"
        send_reply_keyboard(chat_id, "Select approximate *budget*:", [
            "Under $400k", "$400k-$800k", "$800k-$1.2M", "Over $1.2M", "Unknown"
        ])
        return

    if state == "await_budget":
        budget_map = {
            "under $400k": 350_000,
            "$400k-$800k": 600_000,
            "$800k-$1.2m": 1_000_000,
            "over $1.2m": 1_400_000,
            "unknown": None,
        }
        data["budget"] = budget_map.get(text.lower())
        data["source"] = "telegram"
        # Submit to n8n
        try:
            resp = requests.post(N8N_LEAD_WEBHOOK, json=data, timeout=10)
            resp.raise_for_status()
            send_message(
                chat_id,
                f"\u2705 *Lead submitted!*\n\n"
                f"Name: {data['name']}\nEmail: {data['email']}\n"
                f"Phone: {data.get('phone') or 'N/A'}\nSuburb: {data.get('suburb') or 'N/A'}\n"
                "\nThe lead has been scored and added to the CRM.",
            )
        except Exception as exc:
            logger.error("Lead submit failed: %s", exc)
            send_message(chat_id, "\u274c Submission failed. Please try again or contact support.")
        clear_session(chat_id)
        return

# ---------------------------------------------------------------------------
# Main update handler
# ---------------------------------------------------------------------------
def handle_update(update: dict) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    chat_id: int = message["chat"]["id"]
    text: str = message.get("text", "").strip()
    user: dict = message.get("from", {})
    session = get_session(chat_id)

    # Commands
    if text.startswith("/start"):
        clear_session(chat_id)
        handle_start(chat_id, user)
    elif text.startswith("/status"):
        handle_status(chat_id)
    elif text.startswith("/help"):
        handle_help(chat_id)
    elif text.startswith("/newlead") or session["state"] != "idle":
        handle_newlead_flow(chat_id, text.replace("/newlead", "").strip() or text)
    else:
        send_message(chat_id, "I didn't understand that. Type /help to see available commands.")

# ---------------------------------------------------------------------------
# Flask webhook endpoint
# ---------------------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "telegram-bot"}), 200

@app.route("/webhook/telegram", methods=["POST"])
def telegram_webhook():
    update = request.get_json(force=True)
    if not update:
        return jsonify({"error": "No data"}), 400
    try:
        handle_update(update)
    except Exception as exc:
        logger.exception("Unhandled error: %s", exc)
    return jsonify({"ok": True}), 200

@app.route("/register-webhook", methods=["POST"])
def register():
    """Admin endpoint to register this server as the Telegram webhook."""
    webhook_url = request.get_json(force=True).get("url")
    if not webhook_url:
        return jsonify({"error": "url required"}), 400
    result = set_webhook(webhook_url)
    return jsonify(result), 200

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "register":
        url = sys.argv[2]
        print(set_webhook(url))
    else:
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)), debug=False)
