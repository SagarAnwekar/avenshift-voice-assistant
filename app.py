import os, io, csv, json, time, base64, random, secrets, asyncio
import httpx
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Header
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()
SARVAM = os.environ.get("SARVAM_API_KEY", "")
GEMINI = os.environ.get("GEMINI_API_KEY", "")
GMODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
STATE = "/tmp/state.json"
FACTS = json.load(open(os.path.join(os.path.dirname(__file__), "facts.json")))
sessions, codes = {}, {}

def load():
    try: return json.load(open(STATE))
    except Exception: return {"contacts": [], "calls": []}
def save(s): json.dump(s, open(STATE, "w"), ensure_ascii=False)

async def tg_chat():
    global TG_CHAT
    if TG_CHAT: return TG_CHAT
    async with httpx.AsyncClient(timeout=20) as c:
        r = (await c.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates")).json()
    for u in reversed(r.get("result", [])):
        m = u.get("message")
        if m and m.get("chat", {}).get("type") == "private":
            TG_CHAT = str(m["chat"]["id"]); return TG_CHAT
    return ""

async def tg_send(text):
    chat = await tg_chat()
    if not chat: return False
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": chat, "text": text[:4000]})
    return r.status_code == 200

def auth(tok):
    if not tok or tok not in sessions: raise HTTPException(401, "login first")

@app.post("/auth/request")
async def auth_request():
    code = f"{random.randint(0, 999999):06d}"
    codes["c"] = (code, time.time() + 600)
    ok = await tg_send(f"Avenshift Voice login code: {code} (10 min)")
    if not ok: raise HTTPException(400, "Pehle Telegram pe @avenshift_bot ko 'hi' bhej, phir dubara try kar.")
    return {"ok": True}

@app.post("/auth/verify")
async def auth_verify(code: str = Form(...)):
    c = codes.get("c")
    if not c or time.time() > c[1] or not secrets.compare_digest(c[0], code.strip()):
        raise HTTPException(403, "galat code")
    t = secrets.token_hex(16); sessions[t] = time.time(); codes.pop("c", None)
    return {"token": t}

@app.get("/health")
def health(): return {"ok": True, "sarvam": bool(SARVAM), "gemini": bool(GEMINI), "telegram": bool(TG_TOKEN)}

@app.post("/contacts")
async def contacts(file: UploadFile = File(...), x_token: str = Header(None)):
    auth(x_token)
    rows = list(csv.DictReader(io.StringIO((await file.read()).decode("utf-8-sig"))))
    out = []
    for r in rows:
        k = {a.strip().lower(): (b or "").strip() for a, b in r.items() if a}
        if k.get("name") and k.get("phone"): out.append({"name": k["name"], "phone": k["phone"], "status": "pending"})
    s = load(); s["contacts"] = out; save(s)
    return {"count": len(out)}

@app.get("/next")
def nxt(x_token: str = Header(None)):
    auth(x_token)
    s = load()
    for c in s["contacts"]:
        if c["status"] == "pending": return {"contact": c, "intro": FACTS["caller_intro"], "left": sum(1 for z in s["contacts"] if z["status"] == "pending")}
    return {"contact": None, "left": 0}

SYS = ("You are a polite, human-sounding phone assistant for " + FACTS["company"] + ". Speak short (1-2 sentences), in the same language the person uses "
       "(Hindi/Marathi/English, Roman or native script both fine). Use ONLY these approved facts: " + json.dumps(FACTS["approved_facts"], ensure_ascii=False) +
       ". " + FACTS["rule"] + " Understand what the person needs, answer clearly, and when they want webinar/course details say you will send the join link. "
       "Do not claim to be human if asked.")

async def gemini(contents, system=SYS, json_out=False):
    body = {"system_instruction": {"parts": [{"text": system}]}, "contents": contents}
    if json_out: body["generationConfig"] = {"responseMimeType": "application/json"}
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"https://generativelanguage.googleapis.com/v1beta/models/{GMODEL}:generateContent", headers={"x-goog-api-key": GEMINI}, json=body)
    if r.status_code != 200: raise HTTPException(502, f"gemini {r.status_code}: {r.text[:200]}")
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]

async def stt(data, name):
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post("https://api.sarvam.ai/speech-to-text", headers={"api-subscription-key": SARVAM},
                         files={"file": (name, data, "application/octet-stream")}, data={"model": "saaras:v3", "mode": "codemix", "language_code": "unknown"})
    if r.status_code != 200: raise HTTPException(502, f"stt {r.status_code}: {r.text[:200]}")
    j = r.json(); return j.get("transcript", ""), j.get("language_code") or "hi-IN"

async def tts(text, lang):
    lang = lang if lang in ("hi-IN","mr-IN","en-IN","kn-IN","ta-IN","te-IN","bn-IN","gu-IN","ml-IN","pa-IN","od-IN") else "hi-IN"
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post("https://api.sarvam.ai/text-to-speech", headers={"api-subscription-key": SARVAM},
                         json={"text": text[:2400], "language_code": lang, "model": "bulbul:v3", "speaker": "ritu", "output_audio_codec": "mp3"})
    if r.status_code != 200: raise HTTPException(502, f"tts {r.status_code}: {r.text[:200]}")
    return r.json()["audios"][0]

@app.post("/turn")
async def turn(audio: UploadFile = File(...), history: str = Form("[]"), x_token: str = Header(None)):
    auth(x_token)
    text, lang = await stt(await audio.read(), audio.filename or "a.webm")
    hist = json.loads(history)
    if not text.strip(): return {"heard": "", "reply": "", "audio": ""}
    contents = [{"role": "user" if h["who"] == "person" else "model", "parts": [{"text": h["text"]}]} for h in hist]
    contents.append({"role": "user", "parts": [{"text": text}]})
    reply = (await gemini(contents)).strip()
    return {"heard": text, "reply": reply, "audio": await tts(reply, lang), "lang": lang}

@app.post("/speak")
async def speak(text: str = Form(...), lang: str = Form("hi-IN"), x_token: str = Header(None)):
    auth(x_token); return {"audio": await tts(text, lang)}

ANALYSE = ('Read this call transcript. Return JSON: {"interest":"interested|callback|not_interested|unclear","summary":"2 lines",'
           '"needs":"what they asked","callback_time":"if said else empty","whatsapp_message":"short message with webinar details using ONLY approved facts and placeholder [JOIN LINK]"}')

@app.post("/end")
async def end(phone: str = Form(...), name: str = Form(...), transcript: str = Form(...), x_token: str = Header(None)):
    auth(x_token)
    raw = await gemini([{"role": "user", "parts": [{"text": transcript}]}], system=ANALYSE + " Approved facts: " + json.dumps(FACTS["approved_facts"]), json_out=True)
    try: a = json.loads(raw)
    except Exception: a = {"interest": "unclear", "summary": raw[:200]}
    s = load()
    for c in s["contacts"]:
        if c["phone"] == phone and c["status"] == "pending": c["status"] = "done"
    s["calls"].append({"name": name, "phone": phone, "t": time.time(), "transcript": transcript, **a}); save(s)
    await tg_send(f"Call done: {name} ({phone})\nResult: {a.get('interest')}\n{a.get('summary')}\nNeeds: {a.get('needs','')}\nCallback: {a.get('callback_time','')}\n\nWhatsApp draft (NOT sent):\n{a.get('whatsapp_message','')}")
    return a

@app.post("/report")
async def report(hours: float = Form(4), x_token: str = Header(None)):
    auth(x_token)
    s = load(); cut = time.time() - hours * 3600
    rows = [c for c in s["calls"] if c["t"] >= cut]
    lines = [f"Report (last {hours:g}h): {len(rows)} calls"]
    for k, label in (("interested", "Interested"), ("callback", "Call again"), ("not_interested", "Not interested"), ("unclear", "Unclear")):
        m = [c for c in rows if c.get("interest") == k]
        if m: lines.append(f"{label}: " + ", ".join(f"{c['name']} ({c['phone']})" for c in m))
    txt = "\n".join(lines); await tg_send(txt); return {"report": txt}

@app.get("/")
def home(): return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))
