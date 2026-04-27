"""
Patient Handover Bot v5 - Clean (No Groq)
OpenRouter + Telegram + Google Sheets
"""
import os, json, logging, requests, tempfile, threading, base64, time
from datetime import datetime
from flask import Flask, request, Response
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("audit.log"), logging.StreamHandler()])
log = logging.getLogger("bot")

OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_API   = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
SHEET_ID       = "1Ys68GsrZpt8Sk-hgYXh8BKqJX-xAWedjLHG5MP1aCJ0"
NOW            = lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
sessions       = {}
msg_buffer     = {}
BUFFER_WAIT    = 8

MODELS = [
    "google/gemini-2.5-pro-exp-03-25:free",
    "google/gemini-2.0-flash-exp:free",
    "deepseek/deepseek-chat-v3-0324:free",
    "deepseek/deepseek-r1:free",
    "qwen/qwen-2.5-72b-instruct:free",
    "mistralai/mistral-7b-instruct:free",
]

# ── OpenRouter ─────────────────────────────────────────────────────────────────
def ai(system_prompt, user_msg, max_tok=2000):
    last_error = None
    for model in MODELS:
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_msg}
                    ],
                    "max_tokens": max_tok,
                    "temperature": 0.1
                },
                timeout=60
            )
            result = resp.json()
            if "choices" not in result:
                last_error = result.get("error", result)
                continue
            raw = result["choices"][0]["message"]["content"].strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"): raw = raw[4:]
            return json.loads(raw.strip())
        except Exception as e:
            last_error = str(e)
            continue
    raise Exception(f"All models failed: {last_error}")

def ai_ocr(image_path):
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    for model in ["google/gemini-2.0-flash-exp:free", "google/gemini-2.5-pro-exp-03-25:free"]:
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                        {"type": "text", "text": "Extract ALL text from this medical image including all values, dates, units, findings. Be thorough."}
                    ]}],
                    "max_tokens": 1000
                },
                timeout=30
            )
            result = resp.json()
            if "choices" in result:
                return result["choices"][0]["message"]["content"]
        except:
            continue
    return "Could not read image."

# ── Google Sheets ──────────────────────────────────────────────────────────────
def get_sheet(name="Patients"):
    creds_dict = json.loads(os.environ.get("GOOGLE_CREDENTIALS", "{}"))
    creds = Credentials.from_service_account_info(creds_dict, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"])
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    try:
        return sh.worksheet(name)
    except:
        ws = sh.add_worksheet(name, rows=1000, cols=25)
        ws.append_row(["Name","Age","Specialty","Diagnosis","Past Hx","C/O",
            "Vitals","Examination","Staff Plan","New Labs","Investigations",
            "Pending Inv","Inv To Do","Consultations","Next Plan","Medications",
            "PLEX","MSE","Extra","History","Last Updated","Added By"])
        return ws

def find_row(ws, name):
    try: return ws.find(name, in_column=1).row
    except: return None

def to_str(v):
    if v is None: return ""
    if isinstance(v, (dict, list)): return json.dumps(v, ensure_ascii=False)
    return str(v)

def save_patient(data, sender):
    ws   = get_sheet()
    name = data.get("name", "Unknown")
    now  = NOW()
    row  = find_row(ws, name)
    meds = data.get("medications") or []
    if meds and isinstance(meds, list) and len(meds) > 0 and isinstance(meds[0], dict):
        meds_str = "\n".join([f"• {m.get('name','')} {m.get('dose','')} | Start:{m.get('start_date','?')} | Dur:{m.get('duration','?')} | Next:{m.get('next_change','?')}" for m in meds])
    elif meds and isinstance(meds, list):
        meds_str = "\n".join([f"• {m}" for m in meds])
    else:
        meds_str = to_str(meds)
    entry = f"[{now}] @{sender}"
    row_data = [
        to_str(name), to_str(data.get("age","")), to_str(data.get("specialty","General")),
        to_str(data.get("diagnosis","")), to_str(data.get("past_hx","")), to_str(data.get("chief_complaint","")),
        to_str(data.get("vitals","")), to_str(data.get("examination","")), to_str(data.get("staff_plan","")),
        to_str(data.get("new_labs","")), to_str(data.get("investigations","")), to_str(data.get("pending_inv","")),
        to_str(data.get("inv_to_be_done","")), to_str(data.get("consultations","")), to_str(data.get("next_plan","")),
        meds_str, to_str(data.get("plex","")), to_str(data.get("mse","")), to_str(data.get("extra_fields","")),
        "", now, sender
    ]
    if row:
        old = ws.cell(row, 20).value or ""
        row_data[19] = (old + "\n" + entry).strip()
        existing = ws.row_values(row)
        for i, v in enumerate(row_data):
            if not v and i < len(existing): row_data[i] = existing[i]
        row_data[20] = now
        ws.update(f"A{row}:V{row}", [row_data])
        return "updated"
    else:
        row_data[19] = entry
        ws.append_row(row_data)
        return "created"

def get_patient(name):
    ws  = get_sheet()
    row = find_row(ws, name)
    if not row: return None
    v = ws.row_values(row)
    s = lambda i: v[i] if i < len(v) else ""
    return {"name":s(0),"age":s(1),"specialty":s(2),"diagnosis":s(3),
        "past_hx":s(4),"chief_complaint":s(5),"vitals":s(6),
        "examination":s(7),"staff_plan":s(8),"new_labs":s(9),
        "investigations":s(10),"pending_inv":s(11),"inv_to_be_done":s(12),
        "consultations":s(13),"next_plan":s(14),"medications":s(15),
        "plex":s(16),"mse":s(17),"extra_fields":s(18),
        "history":s(19),"last_updated":s(20)}

def list_patients():
    ws = get_sheet()
    rows = ws.get_all_values()[1:]
    return [(r[0], r[2], r[20]) for r in rows if r and r[0]]

# ── Prompts ────────────────────────────────────────────────────────────────────
IND_PROMPT = """You are a Medical Handover Agent. Extract ALL info from this handover message.
Return ONLY valid JSON — no preamble, no markdown fences.

Auto-add emojis: ✴️ nursing tasks | ✳️ intern tasks | 🌟 deadlines | ⚠️ critical values | 📌 morning tasks | 💥 night tasks

{
  "name": "patient name in Arabic exactly as written",
  "age": "age",
  "specialty": "Neurology|Psychiatry|Nephrology|General",
  "diagnosis": "main diagnosis with date",
  "past_hx": "past medical and surgical history",
  "chief_complaint": "chief complaint",
  "vitals": "all vitals as string — add ⚠️ if critical",
  "examination": "full examination as formatted string",
  "mse": "MSE findings as string — only if psychiatry",
  "staff_plan": "staff rounding plan and justification",
  "new_labs": "new or changed results with dates — add ⚠️ if critical",
  "investigations": "all investigations with dates — keep last 2-3 for trending",
  "pending_inv": "pending investigations",
  "inv_to_be_done": "investigations to be ordered",
  "consultations": "other specialties consultations",
  "next_plan": "our team plan only",
  "medications": [{"name":"","dose":"","start_date":"","duration":"","next_change":""}],
  "plex": "PLEX details as string — only if mentioned",
  "nursing_tasks": ["list with ✴️"],
  "intern_tasks": ["list with ✳️"],
  "deadlines": ["list with 🌟"],
  "extra_fields": {}
}

RULES:
- Name ONLY in Arabic. Everything else in English.
- null for missing fields.
- Add date to EVERY lab and investigation.
- Any info not fitting schema goes in extra_fields.
- Return ONLY JSON."""

WARD_PROMPT = """Extract ward round summary. Return ONLY valid JSON.
{
  "date": "", "ward": "",
  "patients": [{"name":"Arabic name","diagnosis":"","key_update":"","plan":"","tasks":[]}],
  "general_notes": "",
  "upcoming": []
}
Auto-add: ✴️✳️🌟⚠️📌💥. Return ONLY JSON."""

# ── Format ─────────────────────────────────────────────────────────────────────
def fmt(d):
    V = lambda v: str(v).strip() if v and str(v).strip() not in ["null","None","{}","[]",""] else "—"
    nursing   = "\n".join([f"  ✴️ {t}" for t in (d.get("nursing_tasks") or [])]) or "  —"
    intern    = "\n".join([f"  ✳️ {t}" for t in (d.get("intern_tasks") or [])]) or "  —"
    deadlines = "\n".join([f"  🌟 {t}" for t in (d.get("deadlines") or [])]) or "  —"
    history   = f"\n━━━━━━━━━━━━━━━━\n🕐 *Updates:*\n{d['history']}" if d.get("history") else ""
    plex_sec  = f"\n━━━━━━━━━━━━━━━━\n🔄 *PLEX:*\n{V(d.get('plex'))}" if d.get("plex") and V(d.get("plex")) != "—" else ""
    mse_sec   = f"\n━━━━━━━━━━━━━━━━\n🧩 *MSE:*\n{V(d.get('mse'))}" if d.get("mse") and V(d.get("mse")) != "—" else ""

    return f"""🏥 *Handover — {d.get('specialty','General')}*
_Updated: {d.get('last_updated', NOW())}_
━━━━━━━━━━━━━━━━
👤 *{V(d.get('name'))}* | 🎂 {V(d.get('age'))}
🔬 *Dx:* {V(d.get('diagnosis'))}
📋 *Past Hx:* {V(d.get('past_hx'))}
🩺 *C/O:* {V(d.get('chief_complaint'))}
━━━━━━━━━━━━━━━━
📊 *Vitals:*
{V(d.get('vitals'))}
━━━━━━━━━━━━━━━━
🔍 *O/E:*
{V(d.get('examination'))}{mse_sec}
━━━━━━━━━━━━━━━━
👨‍⚕️ *Staff Plan:*
{V(d.get('staff_plan'))}
━━━━━━━━━━━━━━━━
🧪 *New Labs:*
{V(d.get('new_labs'))}
📁 *Investigations:*
{V(d.get('investigations'))}
⏳ *Pending:* {V(d.get('pending_inv'))}
📝 *To Do:* {V(d.get('inv_to_be_done'))}
━━━━━━━━━━━━━━━━
🤝 *Consults:* {V(d.get('consultations'))}
━━━━━━━━━━━━━━━━
🎯 *Our Plan:*
{V(d.get('next_plan'))}
━━━━━━━━━━━━━━━━
💊 *Meds:*
{V(d.get('medications'))}{plex_sec}
━━━━━━━━━━━━━━━━
✅ *Tasks:*
Nursing: {nursing}
Intern: {intern}
Deadlines: {deadlines}{history}"""

def fmt_ward(d):
    pts = ""
    for p in (d.get("patients") or []):
        tasks = "\n    ".join(p.get("tasks") or ["—"])
        pts += f"\n👤 *{p.get('name','?')}* | {p.get('diagnosis','')}\n  📌 {p.get('key_update','—')}\n  🎯 {p.get('plan','—')}\n  {tasks}\n━━━━━━━━━━━━━━━━"
    upcoming = "\n".join([f"  🌟 {u}" for u in (d.get("upcoming") or [])]) or "  —"
    return f"🏥 *Ward Round*\n📅 {d.get('date','')}\n🏢 {d.get('ward','')}\n━━━━━━━━━━━━━━━━{pts}\n📝 {d.get('general_notes','—')}\n🌟 Upcoming:\n{upcoming}"

# ── Telegram ───────────────────────────────────────────────────────────────────
def send(chat_id, text):
    for i in range(0, len(text), 4000):
        requests.post(f"{TELEGRAM_API}/sendMessage", json={
            "chat_id": chat_id, "text": text[i:i+4000], "parse_mode": "Markdown"})

def dl(file_id):
    info = requests.get(f"{TELEGRAM_API}/getFile?file_id={file_id}").json()
    path = info["result"]["file_path"]
    url  = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{path}"
    ext  = path.split(".")[-1] if "." in path else "ogg"
    tmp  = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}")
    tmp.write(requests.get(url).content)
    tmp.close()
    return tmp.name

# ── Background processor ───────────────────────────────────────────────────────
def process(chat_id, sender, text, voice=None, photo=None, doc=None):
    try:
        if text == "/start":
            send(chat_id, "👋 *Patient Handover Bot*\n\n/new /show /list /open [name]\n\nSend text, 🎙️ voice, or 📷 photo"); return
        if text == "/list":
            pts = list_patients()
            send(chat_id, "📋 *Patients:*\n\n" + ("\n".join([f"• *{p[0]}* | {p[1]}" for p in pts]) if pts else "None")); return
        if text == "/show":
            name = sessions.get(chat_id)
            if name:
                p = get_patient(name)
                if p: send(chat_id, fmt(p))
            else: send(chat_id, "No active patient.")
            return
        if text == "/new":
            sessions.pop(chat_id, None); send(chat_id, "✅ Ready!"); return
        if text.startswith("/open "):
            name = text[6:].strip()
            p = get_patient(name)
            if p: sessions[chat_id] = name; send(chat_id, f"✅ Opened: *{name}*")
            else: send(chat_id, "❌ Not found. Use /list")
            return

        if voice:
            send(chat_id, "🎙️ Reading voice note...")
            text = ai_ocr(dl(voice["file_id"]))
            send(chat_id, f"📝 _{text[:300]}_")
        elif photo or doc:
            send(chat_id, "🔍 Reading image...")
            fid  = photo[-1]["file_id"] if photo else doc["file_id"]
            text = ai_ocr(dl(fid))
            send(chat_id, f"📄 _{text[:300]}_")

        if not text: return

        send(chat_id, "⏳ Processing...")

        ward_kw = ["ward round", "morning round", "المرور", "نباطشية", "جميع المرضى"]
        if any(k in text.lower() for k in ward_kw):
            d = ai(WARD_PROMPT, text, 1500)
            send(chat_id, fmt_ward(d))
        else:
            d = ai(IND_PROMPT, text, 2000)
            if not d.get("name") and sessions.get(chat_id):
                d["name"] = sessions[chat_id]
            if not d.get("name"):
                send(chat_id, "⚠️ Name not found. Please include patient name."); return
            sessions[chat_id] = d["name"]
            status  = save_patient(d, sender)
            patient = get_patient(d["name"])
            send(chat_id, "✅ New patient!" if status == "created" else "🔄 Updated!")
            send(chat_id, fmt(patient))

    except Exception as e:
        log.error(f"ERROR | {e}")
        send(chat_id, f"⚠️ Error: {str(e)[:150]}")

def flush_buffer(chat_id, sender):
    time.sleep(BUFFER_WAIT)
    buf = msg_buffer.pop(chat_id, None)
    if not buf: return
    combined = buf.get("text", "").strip()
    if combined:
        process(chat_id, sender, combined)

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/health")
def health(): return {"status": "ok", "version": "v5-clean"}, 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    try:
        msg     = data.get("message", {})
        chat_id = str(msg["chat"]["id"])
        sender  = msg["from"].get("username", chat_id)
        text    = msg.get("text", "").strip()
        voice   = msg.get("voice") or msg.get("audio")
        photo   = msg.get("photo")
        doc     = msg.get("document")

        # Commands and media — process immediately
        if text.startswith("/") or voice or photo or doc:
            threading.Thread(target=process, args=(chat_id, sender, text, voice, photo, doc)).start()
            return Response("OK", status=200)

        # Text — buffer to combine split messages
        if text:
            if chat_id in msg_buffer:
                existing = msg_buffer[chat_id].get("text", "")
                msg_buffer[chat_id]["text"] = (existing + "\n" + text).strip()
                t = msg_buffer[chat_id].get("timer")
                if t: t.cancel()
            else:
                msg_buffer[chat_id] = {"text": text}

            t = threading.Timer(BUFFER_WAIT, flush_buffer, args=(chat_id, sender))
            msg_buffer[chat_id]["timer"] = t
            t.start()

    except Exception as e:
        log.error(f"WEBHOOK ERROR | {e}")

    return Response("OK", status=200)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    log.info(f"🚀 Handover Bot v5 Clean — port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
