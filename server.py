"""
Patient Handover Agent — Gemini Version (Fixed)
================================================
- Uses new google-genai library
- Runs on port 8000 (Koyeb default)
"""

import os
import json
import logging
from datetime import datetime
from flask import Flask, request, Response
import requests
from google import genai
from dotenv import load_dotenv

load_dotenv()

# ── App & Logging ──────────────────────────────────────────────────────────────
app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("patient_handover_audit.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("server")

# ── Gemini Setup (new library) ─────────────────────────────────────────────────
gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# ── Twilio Setup ───────────────────────────────────────────────────────────────
ULTRAMSG_INSTANCE = "instance171769"
ULTRAMSG_TOKEN = "dpfekvfc3yf608yq"
# ── System Prompt ──────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """
You are a Patient Handover Agent in a clinical/healthcare setting.
Extract structured patient handover data from the message below.
Return ONLY a valid JSON object — no preamble, no explanation, no markdown fences.

JSON schema to follow exactly:
{
  "patient_name": "string",
  "patient_id": "string or null",
  "dob": "YYYY-MM-DD or null",
  "ward": "string or null",
  "diagnosis": "string",
  "current_medications": ["list of strings"],
  "allergies": ["list of strings"],
  "pending_tasks": ["list of strings"],
  "handover_notes": "string",
  "priority": "low | medium | high | critical",
  "handover_from": "string or null",
  "handover_to": "string or null",
  "timestamp": "ISO 8601 datetime"
}

Rules:
- Extract ONLY what is mentioned. Use null for missing fields.
- Infer priority from urgency language (urgent=high, critical=critical, stable=low).
- Use current UTC time for timestamp if not provided.
- Return ONLY the JSON object. Nothing else.
"""

# ── Agent: Process Handover ────────────────────────────────────────────────────
def process_handover(message: str, sender: str) -> dict:
    log.info(f"INCOMING | from={sender} | msg={message[:80]}...")

    full_prompt = f"{SYSTEM_PROMPT}\n\nMessage:\n{message}"

    response = gemini_client.models.generate_content(
        model="gemini-2.0-flash",
        contents=full_prompt
    )

    raw = response.text.strip()

    # Strip markdown fences if Gemini adds them
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    data = json.loads(raw)
    data["_meta"] = {
        "received_from_phone": sender,
        "processed_at": datetime.utcnow().isoformat(),
        "agent": "PatientHandoverAgent/Gemini/v2"
    }

    log.info(f"PROCESSED | patient={data.get('patient_name')} | priority={data.get('priority')}")
    return data


# ── Format WhatsApp Reply ──────────────────────────────────────────────────────
def format_reply(data: dict) -> str:
    p = data.get
    meds      = "\n  • ".join(p("current_medications", []) or ["None listed"])
    allergies = ", ".join(p("allergies", []) or ["None listed"])
    tasks     = "\n  ☐ ".join(p("pending_tasks", []) or ["None"])
    emoji     = {"low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}.get(p("priority", ""), "⚪")

    return f"""✅ *Patient Handover Received*
{emoji} Priority: *{(p('priority') or 'unknown').upper()}*

👤 *Patient:* {p('patient_name') or 'Unknown'}
🆔 ID: {p('patient_id') or 'N/A'}
🏥 Ward: {p('ward') or 'N/A'}
🩺 Diagnosis: {p('diagnosis') or 'N/A'}

💊 *Medications:*
  • {meds}

⚠️ *Allergies:* {allergies}

📋 *Pending Tasks:*
  ☐ {tasks}

📝 *Notes:*
{p('handover_notes') or 'No additional notes.'}

🔁 {p('handover_from') or '?'} → {p('handover_to') or '?'}
🕐 {p('timestamp') or datetime.utcnow().isoformat()}

_Logged & confirmed by Patient Handover Agent_""".strip()


# ── Send WhatsApp Reply via Twilio ─────────────────────────────────────────────
def send_reply(to: str, body: str):
    requests.post(
        f"https://api.ultramsg.com/{ULTRAMSG_INSTANCE}/messages/chat",
        data={
            "token": ULTRAMSG_TOKEN,
            "to": to,
            "body": body
        }
    )
    log.info(f"REPLY SENT | to={to}")


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "agent": "PatientHandoverAgent/Gemini/v2"}, 200


@app.route("/webhook", methods=["POST"])
def webhook():
    sender = request.form.get("From", "").replace("whatsapp:", "")
    body   = request.form.get("Body", "").strip()

    log.info(f"WEBHOOK | from={sender} | body={body[:60]}")

    if not body:
        return Response("No body", status=200)

    try:
        data  = process_handover(body, sender)
        reply = format_reply(data)
        send_reply(sender, reply)
    except json.JSONDecodeError as e:
        log.error(f"JSON ERROR | {e}")
        send_reply(sender, "⚠️ Could not parse handover. Please re-send with clearer formatting.")
    except Exception as e:
        log.error(f"AGENT ERROR | {e}")
        send_reply(sender, "⚠️ Server error. Please try again shortly.")

    return Response("", status=200)


@app.route("/test", methods=["POST"])
def test_endpoint():
    body   = request.json or {}
    sender = body.get("from", "+0000000000")
    msg    = body.get("message", "")

    if not msg:
        return {"error": "No message provided"}, 400

    try:
        data  = process_handover(msg, sender)
        reply = format_reply(data)
        return {"structured": data, "whatsapp_reply": reply}, 200
    except Exception as e:
        return {"error": str(e)}, 500


# ── Entry Point — port 8000 for Koyeb ─────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    log.info(f"🚀 Patient Handover Agent (Gemini) starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
