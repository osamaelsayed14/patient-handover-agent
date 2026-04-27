"""
Patient Handover Agent v5 - OpenRouter
"""
import os, json, logging, requests, tempfile, threading, base64
from datetime import datetime
from flask import Flask, request, Response
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("audit.log"), logging.StreamHandler()])
log = logging.getLogger("bot")

OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_API   = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
SHEET_ID       = "1Ys68GsrZpt8Sk-hgYXh8BKqJX-xAWedjLHG5MP1aCJ0"
NOW            = lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
sessions       = {}
MODEL          = "google/gemini-2.5-pro-exp-03-25:free"

# ── OpenRouter AI ──────────────────────────────────────────────────────────────
def ai(system_prompt, user_msg, max_tok=2000):
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_msg}
            ],
            "max_tokens": max_tok,
            "temperature": 0.1
        },
        timeout=60
    )
    raw = resp.json()["choices"][0]["message"]["content"].strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
    return json.loads(raw.strip())

def ai_ocr(image_path):
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
        json={
            "model": "google/gemini-2.0-flash-exp:free",
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": "Extract ALL text from this medical image. Include all values, dates, units, and findings."}
            ]}],
            "max_tokens": 1000
        },
        timeout=30
    )
    return resp.json()["choices"][0]["message"]["content"]

# ── Google Sheets ──────────────────────────────────────────────────────────────
def get_sheet(name="Patients"):
    creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
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

def save_patient(data, sender):
    ws   = get_sheet()
    name = data.get("name","Unknown")
    now  = NOW()
    row  = find_row(ws, name)
    def s(v):
        if v is None: return ""
        if isinstance(v,(dict,list)): return json.dumps(v, ensure_ascii=False)
        return str(v)
    meds = data.get("medications") or []
    if meds and isinstance(meds,list) and len(meds)>0 and isinstance(meds[0],dict):
        meds_str = "\n".join([f"• {m.get('name','')} {m.get('dose','')} | Start:{m.get('start_date','?')} | Dur:{m.get('duration','?')} | Next:{m.get('next_change','?')}" for m in meds])
    elif meds and isinstance(meds,list):
        meds_str = "\n".join([f"• {m}" for m in meds])
    else:
        meds_str = s(meds)
    entry = f"[{now}] @{sender}"
    row_data = [s(name),s(data.get("age","")),s(data.get("specialty","General")),
        s(data.get("diagnosis","")),s(data.get("past_hx","")),s(data.get("chief_complaint","")),
        s(data.get("vitals","")),s(data.get("examination","")),s(data.get("staff_plan","")),
        s(data.get("new_labs","")),s(data.get("investigations","")),s(data.get("pending_inv","")),
        s(data.get("inv_to_be_done","")),s(data.get("consultations","")),s(data.get("next_plan","")),
        meds_str,s(data.get("plex","")),s(data.get("mse","")),s(data.get("extra_fields","")),
        "", now, sender]
    if row:
        old = ws.cell(row,20).value or ""
        row_data[19] = (old+"\n"+entry).strip()
        existing = ws.row_values(row)
        for i,v in enumerate(row_data):
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
    return [(r[0],r[2],r[3],r[20]) for r in rows if r and r[0]]

# ── Prompts ────────────────────────────────────────────────────────────────────
IND_PROMPT = """You are a Medical Handover Agent. Extract ALL info from this handover message.
Return ONLY valid JSON — no preamble, no markdown fences.

Auto-add emojis:
✴️ nursing tasks | ✳️ intern tasks | 🌟 deadlines | ⚠️ critical values | 📌 morning tasks | 💥 night tasks

JSON schema:
{
  "name": "patient name in Arabic exactly as written",
  "age": "age",
  "specialty": "Neurology|Psychiatry|Nephrology|General",
  "diagnosis": "main diagnosis with date",
  "past_hx": "past medical and surgical history",
  "chief_complaint": "chief complaint",
  "vitals": "all vitals — add ⚠️ if critical",
  "examination": {
    "general": null, "gcs": null, "pupils": null, "eom": null,
    "facial_nerve": null, "gag": null, "power": null, "tone": null,
    "reflexes": null, "coordination": null, "gait": null,
    "sensory": null, "vibration": null, "chest": null, "heart": null, "abdomen": null
  },
  "mse": {
    "mood": null, "affect": null, "speech": null, "appearance": null,
    "perception": null, "thought": null, "insight": null, "cognition": null
  },
  "staff_plan": "staff rounding plan + justification",
  "new_labs": "new/changed results with dates — add ⚠️ if critical",
  "investigations": "all investigations with dates — keep last 2-3 for trending",
  "pending_inv": "pending investigations",
  "inv_to_be_done": "investigations to be ordered",
  "consultations": "other specialties consultations",
  "next_plan": "our team plan only",
  "medications": [{"name":"","dose":"","start_date":"","duration":"","next_change":""}],
  "plex": {"papers_status":null,"total_sessions":null,"last_session":null,"next_session":null,"plasma_available":null},
  "nursing_tasks": [],
  "intern_tasks": [],
  "deadlines": [],
  "extra_fields": {}
}

RULES:
- Name ONLY in Arabic. Everything else in English.
- null for missing. PLEX only if mentioned. MSE only if psychiatry.
- Add date to EVERY lab and investigation.
- Any info not fitting schema → extra_fields with smart title.
- Return ONLY JSON."""

WARD_PROMPT = """Extract ward round summary. Return ONLY valid JSON.
{
  "date": "", "ward": "",
  "patients": [{"name":"Arabic name","diagnosis":"","key_update":"⚠️ if critical","plan":"","tasks":[]}],
  "general_notes": "",
  "upcoming": []
}
Auto-add: ✴️✳️🌟⚠️📌💥. Return ONLY JSON."""

# ── Format ─────────────────────────────────────────────────────────────────────
def fmt(d):
    V = lambda v: str(v).strip() if v and str(v).strip() not in ["null","None","{}","[]",""] else "—"
    ex   = d.get("examination") or {}
    mse  = d.get("mse") or {}
    plex = d.get("plex")
    spec = d.get("specialty","General")

    exam_lines = ""
    for k,l in [("general","General"),("gcs","GCS"),("pupils","Pupils"),("eom","EOM"),
        ("facial_nerve","FN"),("gag","Gag"),("power","Power"),("tone","Tone"),
        ("reflexes","Reflexes"),("coordination","Coord"),("gait","Gait"),
        ("sensory","Sensory"),("vibration","Vibration"),("chest","Chest"),
        ("heart","Heart"),("abdomen","Abdomen")]:
        v = ex.get(k) if isinstance(ex,dict) else None
        if v and V(v) != "—": exam_lines += f"\n  • {l}: {V(v)}"

    mse_sec = ""
    if spec == "Psychiatry" and isinstance(mse,dict) and any(mse.values()):
        lines = "\n".join([f"  • {k.title()}: {V(v)}" for k,v in mse.items() if V(v)!="—"])
        mse_sec = f"\n━━━━━━━━━━━━━━━━\n🧩 *MSE:*\n{lines}"

    plex_sec = ""
    if plex and isinstance(plex,dict) and any(v for v in plex.values() if v and v!="null"):
        plex_sec = f"""\n━━━━━━━━━━━━━━━━\n🔄 *PLEX:*
  • Papers: {V(plex.get('papers_status'))}
  • Sessions: {V(plex.get('total_sessions'))}
  • Last: {V(plex.get('last_session'))}
  • Next: {V(plex.get('next_session'))}
  • Plasma: {V(plex.get('plasma_available'))}"""

    nursing   = "\n".join([f"  ✴️ {t}" for t in (d.get("nursing_tasks") or [])]) or "  —"
    intern    = "\n".join([f"  ✳️ {t}" for t in (d.get("intern_tasks") or [])]) or "  —"
    deadlines = "\n".join([f"  🌟 {t}" for t in (d.get("deadlines") or [])]) or "  —"

    history = ""
    if d.get("history"):
        history = f"\n━━━━━━━━━━━━━━━━\n🕐 *Updates:*\n{d['history']}"

    return f"""🏥 *Handover — {spec}*
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
🔍 *O/E:*{exam_lines if exam_lines else chr(10)+"  • —"}{mse_sec}
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
            "chat_id":chat_id, "text":text[i:i+4000], "parse_mode":"Markdown"})

def dl(file_id):
    info = requests.get(f"{TELEGRAM_API}/getFile?file_id={file_id}").json()
    path = info["result"]["file_path"]
    url  = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{path}"
    ext  = path.split(".")[-1] if "." in path else "ogg"
    tmp  = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}")
    tmp.write(requests.get(url).content)
    tmp.close()
    return tmp.name

def transcribe(path):
    # Use OpenRouter Whisper via Groq as fallback
    try:
        from groq import Groq
        gc = Groq(api_key=os.getenv("GROQ_API_KEY",""))
        with open(path,"rb") as f:
            return gc.audio.transcriptions.create(model="whisper-large-v3",file=f,language="ar").text
    except:
        return "Could not transcribe audio."

# ── Background ─────────────────────────────────────────────────────────────────
def bg(msg_data):
    chat_id = None
    try:
        msg     = msg_data.get("message",{})
        chat_id = str(msg["chat"]["id"])
        sender  = msg["from"].get("username", chat_id)
        text    = msg.get("text","").strip()
        voice   = msg.get("voice") or msg.get("audio")
        photo   = msg.get("photo")
        doc     = msg.get("document")

        if text == "/start":
            send(chat_id, "👋 *Patient Handover Bot v5*\n\n/new /show /list /open [name]\n\nSend text, 🎙️ voice, or 📷 photo\n\n*Powered by Gemini 2.5 Pro* 🧠")
            return
        if text == "/list":
            pts = list_patients()
            send(chat_id, "📋 *Patients:*\n\n" + ("\n".join([f"• *{p[0]}* | {p[1]}" for p in pts]) if pts else "No patients yet."))
            return
        if text == "/show":
            name = sessions.get(chat_id)
            if name:
                p = get_patient(name)
                if p: send(chat_id, fmt(p))
            else: send(chat_id, "No active patient. Use /open [name]")
            return
        if text == "/new":
            sessions.pop(chat_id, None)
            send(chat_id, "✅ Ready for new patient!")
            return
        if text.startswith("/open "):
            name = text[6:].strip()
            p = get_patient(name)
            if p: sessions[chat_id]=name; send(chat_id, f"✅ Opened: *{name}*\nSend more data to update.")
            else: send(chat_id, "❌ Not found. Use /list")
            return

        if voice:
            send(chat_id, "🎙️ Transcribing...")
            text = transcribe(dl(voice["file_id"]))
            send(chat_id, f"📝 _{text[:300]}_")
        elif photo or doc:
            send(chat_id, "🔍 Reading image...")
            fid  = photo[-1]["file_id"] if photo else doc["file_id"]
            text = ai_ocr(dl(fid))
            send(chat_id, f"📄 _{text[:300]}_")

        if not text: return

        send(chat_id, "⏳ Processing with Gemini 2.5 Pro...")

        ward_kw = ["ward round","morning round","المرور","نباطشية","جميع المرضى","ward summary"]
        if any(k in text.lower() for k in ward_kw):
            d = ai(WARD_PROMPT, text, 1500)
            send(chat_id, "🏥 *Ward Round Detected*")
            send(chat_id, fmt_ward(d))
        else:
            d = ai(IND_PROMPT, text, 2000)
            if not d.get("name") and sessions.get(chat_id):
                d["name"] = sessions[chat_id]
            if not d.get("name"):
                send(chat_id, "⚠️ Patient name not found. Please include name.")
                return
            sessions[chat_id] = d["name"]
            status  = save_patient(d, sender)
            patient = get_patient(d["name"])
            send(chat_id, "✅ New patient created!" if status=="created" else "🔄 Patient updated!")
            send(chat_id, fmt(patient))

    except Exception as e:
        log.error(f"ERROR | {e}")
        if chat_id: send(chat_id, f"⚠️ Error: {str(e)[:200]}")

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/health")
def health(): return {"status":"ok","model":"gemini-2.5-pro"}, 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    threading.Thread(target=bg, args=(data,)).start()
    return Response("OK", status=200)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    log.info(f"🚀 Handover Bot v5 (Gemini 2.5 Pro) port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
