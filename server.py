"""
Patient Handover Agent v4
==========================
- Auto-detect Individual vs Ward Round handover
- Auto emoji keys
- PLEX section when mentioned
- Google Sheets memory
- Multi-message per patient
- Timestamps on everything
- Neurology / Psychiatry / General
"""

import os, json, logging, requests, tempfile
from datetime import datetime
from flask import Flask, request, Response

import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler("audit.log"), logging.StreamHandler()]
)
log = logging.getLogger("bot")

OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_API   = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
SHEET_ID       = "1Ys68GsrZpt8Sk-hgYXh8BKqJX-xAWedjLHG5MP1aCJ0"
TODAY          = lambda: datetime.utcnow().strftime("%Y-%m-%d")
NOW            = lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

# ── Google Sheets ──────────────────────────────────────────────────────────────
def get_sheet(name="Patients"):
    creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
    creds = Credentials.from_service_account_info(creds_dict, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ])
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    try:
        return sh.worksheet(name)
    except:
        ws = sh.add_worksheet(name, rows=1000, cols=30)
        if name == "Patients":
            ws.append_row([
                "Name", "Age", "Specialty", "Diagnosis",
                "Past Hx", "C/O", "Vitals", "Examination",
                "Staff Plan + Justification", "New Labs", "Investigations",
                "Pending Inv", "Inv To Be Done", "Consultations",
                "Next Plan", "Medications", "PLEX", "MSE",
                "Extra Fields", "History Log", "Last Updated", "Added By"
            ])
        elif name == "WardRounds":
            ws.append_row([
                "Date", "Ward", "Patients Summary",
                "Key Updates", "Tasks", "Added By"
            ])
        return ws


def find_row(ws, value, col=1):
    try:
        return ws.find(value, in_column=col).row
    except:
        return None


def save_patient(data: dict, sender: str):
    ws   = get_sheet("Patients")
    name = data.get("name", "Unknown")
    now  = NOW()
    row  = find_row(ws, name)

    def j(v): return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else (v or "")

    meds_list = data.get("medications") or []
    meds_str  = "\n".join([
        f"• {m.get('name','')} {m.get('dose','')} | Start: {m.get('start_date','?')} | Duration: {m.get('duration','?')} | Next change: {m.get('next_change','?')}"
        for m in meds_list
    ]) if meds_list and isinstance(meds_list[0], dict) else "\n".join([f"• {m}" for m in meds_list])

    history_entry = f"[{now}] @{sender} updated"
    row_data = [
        name,
        data.get("age", ""),
        data.get("specialty", "General"),
        data.get("diagnosis", ""),
        data.get("past_hx", ""),
        data.get("chief_complaint", ""),
        data.get("vitals", ""),
        data.get("examination", ""),
        data.get("staff_plan", ""),
        data.get("new_labs", ""),
        data.get("investigations", ""),
        data.get("pending_inv", ""),
        data.get("inv_to_be_done", ""),
        data.get("consultations", ""),
        data.get("next_plan", ""),
        meds_str,
        j(data.get("plex")),
        j(data.get("mse")),
        j(data.get("extra_fields")),
        "",
        now,
        sender
    ]

    if row:
        old_history = ws.cell(row, 20).value or ""
        row_data[19] = (old_history + "\n" + history_entry).strip()
        # Merge non-empty fields with existing
        existing = ws.row_values(row)
        for i, val in enumerate(row_data):
            if not val and i < len(existing):
                row_data[i] = existing[i]
        row_data[20] = now
        ws.update(f"A{row}:V{row}", [row_data])
        return "updated"
    else:
        row_data[19] = history_entry
        ws.append_row(row_data)
        return "created"


def get_patient(name: str):
    ws  = get_sheet("Patients")
    row = find_row(ws, name)
    if not row:
        return None
    v = ws.row_values(row)
    def s(i): return v[i] if i < len(v) else ""
    return {
        "name": s(0), "age": s(1), "specialty": s(2), "diagnosis": s(3),
        "past_hx": s(4), "chief_complaint": s(5), "vitals": s(6),
        "examination": s(7), "staff_plan": s(8), "new_labs": s(9),
        "investigations": s(10), "pending_inv": s(11), "inv_to_be_done": s(12),
        "consultations": s(13), "next_plan": s(14), "medications": s(15),
        "plex": s(16), "mse": s(17), "extra_fields": s(18),
        "history": s(19), "last_updated": s(20)
    }


def list_patients():
    ws   = get_sheet("Patients")
    rows = ws.get_all_values()[1:]
    return [(r[0], r[2], r[3], r[20]) for r in rows if r and r[0]]


# ── AI Prompts ─────────────────────────────────────────────────────────────────
DETECT_PROMPT = """
Analyze this medical message and determine if it is:
1. "individual" - a handover for a single specific patient
2. "ward" - a ward round summary or multiple patients overview

Reply with ONLY one word: individual OR ward
"""

INDIVIDUAL_PROMPT = """
You are a Medical Handover Agent. Extract ALL information from this handover message.
Return ONLY valid JSON — no preamble, no markdown.

Auto-detect specialty: Neurology | Psychiatry | Nephrology | General

Auto-add emoji keys:
- ✴️ for nursing tasks
- ✳️ for intern/resident tasks
- 🌟 for upcoming deadlines or appointments
- ⚠️ for critical labs, dangerous vitals, or serious alerts
- 📌 for paperwork or morning round tasks
- 💥 for night shift or evening round tasks

JSON schema:
{
  "name": "patient name in Arabic",
  "age": "age",
  "specialty": "detected specialty",
  "diagnosis": "main diagnosis with date if mentioned",
  "past_hx": "past medical and surgical history",
  "chief_complaint": "chief complaint",
  "vitals": "all vitals with ⚠️ if critical",
  "examination": {
    "general": null,
    "gcs": null,
    "pupils": null,
    "eom": null,
    "facial_nerve": null,
    "gag": null,
    "power": null,
    "tone": null,
    "reflexes": null,
    "coordination": null,
    "gait": null,
    "sensory": null,
    "vibration": null,
    "chest": null,
    "heart": null,
    "abdomen": null,
    "other": null
  },
  "mse": {
    "mood": null,
    "affect": null,
    "speech": null,
    "appearance": null,
    "perception": null,
    "thought": null,
    "insight": null,
    "cognition": null
  },
  "staff_plan": "staff rounding plan + justification",
  "new_labs": "new or changed lab results with dates and ⚠️ if critical",
  "investigations": "all investigations with dates — keep last 2-3 results for trending",
  "pending_inv": "pending investigations awaiting results",
  "inv_to_be_done": "investigations that need to be ordered or done",
  "consultations": "other specialties consultations",
  "next_plan": "our team plan (not staff recommendations)",
  "medications": [
    {
      "name": "drug name",
      "dose": "dose + route + frequency",
      "start_date": "start date if mentioned",
      "duration": "course duration if decided",
      "next_change": "date of next change if mentioned"
    }
  ],
  "plex": {
    "papers_status": "papers location — with us / filing / pending",
    "total_sessions": "total number of PLEX sessions",
    "last_session": "date of last session",
    "next_session": "date of next session",
    "plasma_available": "yes/no/unknown"
  },
  "nursing_tasks": ["list of ✴️ nursing tasks"],
  "intern_tasks": ["list of ✳️ intern tasks"],
  "deadlines": ["list of 🌟 upcoming deadlines"],
  "extra_fields": {}
}

IMPORTANT:
- Name ONLY in Arabic. Everything else in English.
- Add ⚠️ automatically to critical values.
- Put PLEX section ONLY if PLEX is mentioned.
- Put MSE ONLY if psychiatry content detected.
- Any info that doesn't fit → put in extra_fields with smart title.
- NB: Add date to EVERY lab and investigation result.
- Return ONLY JSON.
"""

WARD_PROMPT = """
You are a Medical Ward Round Summary Agent.
Extract the ward round summary from this message.
Return ONLY valid JSON — no preamble, no markdown.

JSON schema:
{
  "date": "date of ward round",
  "ward": "ward name or number",
  "patients": [
    {
      "name": "patient name in Arabic",
      "diagnosis": "diagnosis",
      "key_update": "main update today with ⚠️ if critical",
      "plan": "today's plan",
      "tasks": ["tasks with emoji keys"]
    }
  ],
  "general_notes": "any general ward notes",
  "upcoming": ["🌟 upcoming deadlines for the ward"]
}

Auto-add emoji keys:
- ✴️ nursing tasks
- ✳️ intern tasks
- 🌟 deadlines
- ⚠️ critical alerts
- 📌 morning round tasks
- 💥 night shift tasks

Return ONLY JSON.
"""


def detect_type(message: str) -> str:
    resp = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": DETECT_PROMPT},
            {"role": "user",   "content": message[:500]}
        ],
        temperature=0, max_tokens=10
    )
    result = resp.choices[0].message.content.strip().lower()
    return "ward" if "ward" in result else "individual"


def extract_individual(message: str) -> dict:
    resp = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": INDIVIDUAL_PROMPT},
            {"role": "user",   "content": message}
        ],
        temperature=0.1, max_tokens=2500
    )
    raw = resp.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
    return json.loads(raw.strip())


def extract_ward(message: str) -> dict:
    resp = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": WARD_PROMPT},
            {"role": "user",   "content": message}
        ],
        temperature=0.1, max_tokens=2500
    )
    raw = resp.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
    return json.loads(raw.strip())


def transcribe_voice(file_path: str) -> str:
    with open(file_path, "rb") as f:
        resp = groq_client.audio.transcriptions.create(
            model="whisper-large-v3", file=f, language="ar"
        )
    return resp.text


def ocr_image(file_path: str) -> str:
    import base64
    with open(file_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    resp = groq_client.chat.completions.create(
        model="llama-3.2-11b-vision-preview",
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": "Extract ALL text from this medical image. Include all values, dates, units, and findings. Be thorough and accurate."}
            ]
        }],
        max_tokens=1000
    )
    return resp.choices[0].message.content


# ── Format Individual Report ───────────────────────────────────────────────────
def format_individual(d: dict) -> str:
    def val(v): return str(v).strip() if v and str(v).strip() not in ["null","None","{}","[]",""] else "—"

    ex   = d.get("examination") or {}
    mse  = d.get("mse") or {}
    plex = d.get("plex")
    spec = d.get("specialty", "General")
    now  = d.get("last_updated", NOW())

    # Exam section
    exam_lines = []
    exam_map = {
        "general":"General", "gcs":"GCS", "pupils":"Pupils", "eom":"EOM",
        "facial_nerve":"FN", "gag":"Gag", "power":"Ms Power", "tone":"Ms Tone",
        "reflexes":"Reflexes", "coordination":"Coordination", "gait":"Gait",
        "sensory":"Sensory", "vibration":"Vibration",
        "chest":"Chest", "heart":"Heart", "abdomen":"Abdomen", "other":"Other"
    }
    for key, label in exam_map.items():
        v = ex.get(key)
        if v and val(v) != "—":
            exam_lines.append(f"  • {label}: {val(v)}")
    exam_section = "\n".join(exam_lines) if exam_lines else "  • —"

    # MSE section
    mse_section = ""
    if spec == "Psychiatry" and any(mse.values()):
        mse_lines = [f"  • {k.title()}: {val(v)}" for k,v in mse.items() if val(v) != "—"]
        mse_section = f"""
━━━━━━━━━━━━━━━━
🧩 *MSE:*
{chr(10).join(mse_lines)}"""

    # PLEX section
    plex_section = ""
    if plex and isinstance(plex, dict) and any(plex.values()):
        plex_section = f"""
━━━━━━━━━━━━━━━━
🔄 *PLEX:*
  • Papers: {val(plex.get('papers_status'))}
  • Total sessions: {val(plex.get('total_sessions'))}
  • Last session: {val(plex.get('last_session'))}
  • Next session: {val(plex.get('next_session'))}
  • Plasma available: {val(plex.get('plasma_available'))}"""

    # Tasks
    nursing  = "\n".join([f"  ✴️ {t}" for t in (d.get("nursing_tasks") or [])]) or "  —"
    intern   = "\n".join([f"  ✳️ {t}" for t in (d.get("intern_tasks") or [])]) or "  —"
    deadlines= "\n".join([f"  🌟 {t}" for t in (d.get("deadlines") or [])]) or "  —"

    # Extra fields
    extra = d.get("extra_fields") or {}
    extra_section = ""
    if extra and isinstance(extra, dict):
        lines = "\n".join([f"  • *{k}:* {v}" for k,v in extra.items() if v])
        if lines:
            extra_section = f"\n━━━━━━━━━━━━━━━━\n📌 *Additional:*\n{lines}"

    return f"""🏥 *Individual Handover — {spec}*
_Updated: {now}_

━━━━━━━━━━━━━━━━
👤 *Name:* {val(d.get('name'))}
🎂 *Age:* {val(d.get('age'))}
🔬 *Diagnosis:* {val(d.get('diagnosis'))}

📋 *Past Hx:* {val(d.get('past_hx'))}
🩺 *C/O:* {val(d.get('chief_complaint'))}

━━━━━━━━━━━━━━━━
📊 *Vitals:*
{val(d.get('vitals'))}

━━━━━━━━━━━━━━━━
🔍 *Examination:*
{exam_section}
{mse_section}
━━━━━━━━━━━━━━━━
👨‍⚕️ *Staff Plan + Justification:*
{val(d.get('staff_plan'))}

━━━━━━━━━━━━━━━━
🧪 *New Labs/Results:*
{val(d.get('new_labs'))}

📁 *All Investigations:*
{val(d.get('investigations'))}

⏳ *Pending Inv:*
{val(d.get('pending_inv'))}

📝 *Inv To Be Done:*
{val(d.get('inv_to_be_done'))}

━━━━━━━━━━━━━━━━
🤝 *Consultations:*
{val(d.get('consultations'))}

━━━━━━━━━━━━━━━━
🎯 *Next Plan (Our Plan):*
{val(d.get('next_plan'))}

━━━━━━━━━━━━━━━━
💊 *Medications:*
{val(d.get('medications'))}
{plex_section}
━━━━━━━━━━━━━━━━
✅ *Tasks:*
Nursing ✴️:
{nursing}

Intern ✳️:
{intern}

Deadlines 🌟:
{deadlines}
{extra_section}"""


# ── Format Ward Round Report ───────────────────────────────────────────────────
def format_ward(d: dict) -> str:
    patients_text = ""
    for p in (d.get("patients") or []):
        tasks = "\n    ".join(p.get("tasks") or ["—"])
        patients_text += f"""
👤 *{p.get('name','?')}* | {p.get('diagnosis','?')}
  📌 Update: {p.get('key_update','—')}
  🎯 Plan: {p.get('plan','—')}
  Tasks:
    {tasks}
━━━━━━━━━━━━━━━━"""

    upcoming = "\n".join([f"  🌟 {u}" for u in (d.get("upcoming") or [])]) or "  —"

    return f"""🏥 *Ward Round Summary*
📅 Date: {d.get('date', TODAY())}
🏢 Ward: {d.get('ward','—')}

━━━━━━━━━━━━━━━━
{patients_text}

📝 *General Notes:*
{d.get('general_notes','—')}

━━━━━━━━━━━━━━━━
🌟 *Upcoming:*
{upcoming}

_Generated: {NOW()}_"""


# ── Telegram ───────────────────────────────────────────────────────────────────
def send(chat_id, text):
    # Split long messages
    if len(text) > 4000:
        parts = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for part in parts:
            requests.post(f"{TELEGRAM_API}/sendMessage", json={
                "chat_id": chat_id, "text": part, "parse_mode": "Markdown"
            })
    else:
        requests.post(f"{TELEGRAM_API}/sendMessage", json={
            "chat_id": chat_id, "text": text, "parse_mode": "Markdown"
        })


def download_file(file_id: str) -> str:
    info = requests.get(f"{TELEGRAM_API}/getFile?file_id={file_id}").json()
    path = info["result"]["file_path"]
    url  = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{path}"
    ext  = path.split(".")[-1] if "." in path else "ogg"
    tmp  = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}")
    tmp.write(requests.get(url).content)
    tmp.close()
    return tmp.name


sessions = {}  # chat_id -> patient_name


# ── Webhook ────────────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "agent": "HandoverBot/v4"}, 200


@app.route("/webhook", methods=["POST"])
def webhook():
    data    = request.json
    chat_id = None
    try:
        msg     = data.get("message", {})
        chat_id = str(msg["chat"]["id"])
        sender  = msg["from"].get("username", chat_id)
        text    = msg.get("text", "").strip()
        voice   = msg.get("voice") or msg.get("audio")
        photo   = msg.get("photo")
        doc     = msg.get("document")

        # ── Commands ──────────────────────────────────────────────────────────
        if text.startswith("/start"):
            send(chat_id, """👋 *Patient Handover Bot v4*

*Commands:*
/new — new patient session
/show — show current patient
/list — list all patients
/open [name] — open patient record

*Just send:*
• Text handover → auto-detected (individual or ward)
• 🎙️ Voice note → transcribed automatically
• 📷 Photo (labs/CT) → OCR + extracted

*Emoji Keys (auto-added):*
✴️ Nursing task
✳️ Intern task
🌟 Upcoming deadline
⚠️ Critical alert
📌 Morning round task
💥 Night shift task""")
            return Response("OK", status=200)

        if text.startswith("/list"):
            patients = list_patients()
            if not patients:
                send(chat_id, "No patients found.")
            else:
                lines = "\n".join([f"• *{p[0]}* | {p[1]} | {p[2]} | {p[3]}" for p in patients])
                send(chat_id, f"📋 *All Patients:*\n\n{lines}")
            return Response("OK", status=200)

        if text.startswith("/show"):
            name = sessions.get(chat_id)
            if not name:
                send(chat_id, "No active patient. Send data or use /open [name]")
                return Response("OK", status=200)
            patient = get_patient(name)
            if patient:
                send(chat_id, format_individual(patient))
            else:
                send(chat_id, "Patient not found.")
            return Response("OK", status=200)

        if text.startswith("/new"):
            sessions.pop(chat_id, None)
            send(chat_id, "✅ Ready for new patient or ward round. Send the data!")
            return Response("OK", status=200)

        if text.startswith("/open"):
            parts = text.split(" ", 1)
            if len(parts) < 2:
                send(chat_id, "Usage: /open [patient name]")
                return Response("OK", status=200)
            name = parts[1].strip()
            patient = get_patient(name)
            if patient:
                sessions[chat_id] = name
                send(chat_id, f"✅ Opened: *{name}*\nSend more data to update.")
            else:
                send(chat_id, f"❌ '{name}' not found. Use /list")
            return Response("OK", status=200)

        # ── Voice ─────────────────────────────────────────────────────────────
        if voice:
            send(chat_id, "🎙️ Transcribing...")
            file_path = download_file(voice["file_id"])
            text = transcribe_voice(file_path)
            send(chat_id, f"📝 Transcribed:\n_{text[:500]}_")

        # ── Image OCR ─────────────────────────────────────────────────────────
        elif photo or doc:
            send(chat_id, "🔍 Reading image...")
            file_id   = photo[-1]["file_id"] if photo else doc["file_id"]
            file_path = download_file(file_id)
            text = ocr_image(file_path)
            send(chat_id, f"📄 Extracted:\n_{text[:400]}_")

        if not text:
            return Response("OK", status=200)

        # ── Detect type ───────────────────────────────────────────────────────
        send(chat_id, "⏳ Processing...")
        htype = detect_type(text)

        if htype == "ward":
            data_extracted = extract_ward(text)
            report = format_ward(data_extracted)
            send(chat_id, "🏥 *Ward Round Detected*")
            send(chat_id, report)

        else:
            data_extracted = extract_individual(text)

            if not data_extracted.get("name") and sessions.get(chat_id):
                data_extracted["name"] = sessions[chat_id]

            if not data_extracted.get("name"):
                send(chat_id, "⚠️ Patient name not detected. Please include the name.")
                return Response("OK", status=200)

            sessions[chat_id] = data_extracted["name"]
            status  = save_patient(data_extracted, sender)
            patient = get_patient(data_extracted["name"])

            action = "✅ New patient created!" if status == "created" else "🔄 Patient updated!"
            send(chat_id, action)
            send(chat_id, format_individual(patient))

    except Exception as e:
        log.error(f"ERROR | {e}")
        if chat_id:
            send(chat_id, f"⚠️ Error: {str(e)[:200]}")

    return Response("OK", status=200)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    log.info(f"🚀 Handover Bot v4 starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
