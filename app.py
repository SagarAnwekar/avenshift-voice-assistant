import hmac, hashlib
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

SHEET_URL = os.environ.get("SHEET_WEBHOOK_URL", ""); SHEET_SECRET = os.environ.get("SHEET_SECRET", "")
async def sheet(payload):
    if not SHEET_URL: return None
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            r = await c.post(SHEET_URL, json={"secret": SHEET_SECRET, **payload})
        return r.json()
    except Exception: return None

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

ORCH_KEY = os.environ.get("ORCH_KEY", "")
_SEC = (ORCH_KEY or os.environ.get("SHEET_SECRET", "") or "dev").encode()
def code_for(w): return f"{int(hmac.new(_SEC, ('code%d' % w).encode(), hashlib.sha256).hexdigest()[:8], 16) % 1000000:06d}"
def make_token():
    ts = str(int(time.time())); return ts + "." + hmac.new(_SEC, ("tok" + ts).encode(), hashlib.sha256).hexdigest()[:32]
def valid_token(t):
    try:
        ts, sig = t.split(".", 1)
        return time.time() - int(ts) < 30 * 86400 and secrets.compare_digest(sig, hmac.new(_SEC, ("tok" + ts).encode(), hashlib.sha256).hexdigest()[:32])
    except Exception: return False
def auth(tok):
    if tok and ORCH_KEY and secrets.compare_digest(tok, ORCH_KEY): return
    if tok and valid_token(tok): return
    raise HTTPException(401, "login first")

@app.post("/auth/request")
async def auth_request():
    code = code_for(int(time.time() // 600))
    ok = await tg_send(f"Avenshift Voice login code: {code} (10 min)")
    if not ok: raise HTTPException(400, "Pehle Telegram pe @avenshift_bot ko 'hi' bhej, phir dubara try kar.")
    return {"ok": True}

@app.post("/auth/verify")
async def auth_verify(code: str = Form(...)):
    w = int(time.time() // 600)
    if not any(secrets.compare_digest(code_for(x), code.strip()) for x in (w, w - 1)):
        raise HTTPException(403, "galat code")
    return {"token": make_token()}

@app.get("/variants")
def variants(): return {k: {"name": v[0], "ready": (v[1] != "deepgram" or bool(DEEPGRAM)) and (v[2] == "gemini" and bool(GEMINI) or any(f[0] == v[2] and os.environ.get(f[2]) for f in FALLBACKS))} for k, v in VARIANTS.items()}

@app.get("/health")
def health(): return {"ok": True, "sarvam": bool(SARVAM), "gemini": bool(GEMINI), "telegram": bool(TG_TOKEN), "fallbacks": [n for n,_,k,_ in FALLBACKS if os.environ.get(k)]}

@app.post("/contacts")
async def contacts(file: UploadFile = File(...), x_token: str = Header(None)):
    auth(x_token)
    rows = list(csv.DictReader(io.StringIO((await file.read()).decode("utf-8-sig"))))
    out = []
    for r in rows:
        k = {a.strip().lower(): (b or "").strip() for a, b in r.items() if a}
        if k.get("name") and k.get("phone"): out.append({"name": k["name"], "phone": k["phone"], "registered": k.get("registered", "").lower() in ("yes","y","true","1"), "status": "pending"})
    s = load(); s["contacts"] = out; save(s)
    return {"count": len(out)}

@app.post("/contacts/sync")
async def contacts_sync(x_token: str = Header(None)):
    auth(x_token)
    r = await sheet({"action": "contacts"})
    if not r or not r.get("ok"): raise HTTPException(502, "sheet not connected")
    out = [{"name": c["name"], "phone": c["phone"], "registered": c["registered"].lower() in ("yes","y","true","1"), "status": "pending" if c["status"] in ("", "pending") else "done"} for c in r["contacts"]]
    s = load(); s["contacts"] = out; save(s)
    return {"count": len(out)}

@app.get("/next")
def nxt(x_token: str = Header(None)):
    auth(x_token)
    s = load()
    for c in s["contacts"]:
        if c["status"] == "pending": return {"contact": c, "intro": FACTS["caller_intro"], "intro_en": FACTS["caller_intro_en"], "rem": FACTS["caller_intro_reminder"], "rem_en": FACTS["caller_intro_reminder_en"], "left": sum(1 for z in s["contacts"] if z["status"] == "pending")}
    return {"contact": None, "left": 0}

SYS = ("You are a polite, human-sounding phone assistant for " + FACTS["company"] + ". Speak short (1-2 sentences), in the same language the person uses "
       "(Hindi/Marathi/English, Roman or native script both fine). Use ONLY these approved facts: " + json.dumps(FACTS["approved_facts"], ensure_ascii=False) +
       ". " + FACTS["rule"] + " Understand what the person needs, answer clearly, and when they want webinar/course details say you will send the join link. "
       "If the answer is not in the approved facts, say you do not know and that the team will call back or share details; never guess. If the person interrupts or changes topic, drop what you were saying and answer their latest words first. "
       "STYLE (phone call, mobile audio): you are the AI voice assistant of Sagar. In a demo call you may speak as Sagar, but if anyone asks whether you are a real person or AI, answer honestly that you are an AI voice assistant. Never claim to be human. Speak natural Indian-accent Hinglish (Hindi with simple English words); if the person speaks English, Kannada or Marathi, switch to that language and stay in it. Before ending the call always ask once if they need any other help (Hindi: aur kuch madad chahiye?). SALES-CALL MANNER like a polished real-estate agent: speak very properly, confident, warm and courteous (use 'aap', 'ji', 'please'). Flow: 1) clear opening already given, 2) state the purpose in one line (free AI developer program by NextLearn), 3) ask short questions, then LISTEN, 4) handle objections politely: acknowledge first ('bilkul samajh sakta hoon'), then one approved fact, never push twice, 5) give a clear next step (session join / share details on email or chat), 6) courteous close ('aapka samay dene ke liye dhanyavaad'). No slang, no filler like 'umm'. CALL FLOW: the opening already greeted and asked if it is a good time. If yes, introduce NextLearn's AI program naturally in 1-2 short sentences (approved facts only: free AI developer program, certificate, free tools, hands-on learning), then ask ONE question. If they say no or busy: acknowledge, give ONE short line of value (free AI developer program, certificate, free tools - approved facts only), then offer to send details by message or call back later. If they decline again or say stop or do not call, close politely at once, no pressure, and respect it. Never jump straight into a hard pitch. HUMAN FEEL: short sentences with natural pauses (commas and ...), start with a brief acknowledgement (haan ji, achha, I see), vary tone, mirror the person's language and politeness, occasional light filler (ek minute, dekhiye), never repeat the same phrase twice, stop talking when interrupted, ask ONE question at a time, max ~25 words per turn. Approved facts only, no promises of jobs, salary or guarantees. Max 1-2 short sentences (under ~25 words), ONE idea or ONE question per turn. "
       "Each turn: acknowledge what they said in a few words, give the useful answer, then move forward with one small question. "
       "Natural Indian conversational style: acknowledgements like Achha, Haan ji, Theek hai, Bilkul, Samajh gaya, used sparingly and varied, in the caller's language (Marathi: Ho, Barobar; Kannada: Sari, Houdu), not a generic English-assistant tone. "
       "A short filler like Achha or Haan ji is already played before your reply, so do not start your reply with the same word; go straight to the answer. "
       "No markdown, no lists, no brackets, no emojis. Say dates in words (Thursday, fifteenth October, shaam saat baje). Mirror their language and Hinglish mix. "
       "If you did not catch something, politely ask them to repeat; after 3 failed tries offer a callback and end kindly. If they say not interested, thank them and stop, no pressure. "
       "Never say 'let me check' or 'one moment'. Do not claim to be human if asked.")

DEMO_NOTE = (" DEMO TEST CALL: for this call only, the placeholder webinar is " + FACTS["demo_only"]["session_date_time"] + "; the join link is TBD (will be shared in chat). "
              "Say clearly that this date is a demo/placeholder and not final.")
import re
def clean_reply(t):
    t = re.sub(r"[*_`#>\[\]]+", "", t); t = re.sub(r"\s+", " ", t).strip()
    s = re.split(r"(?<=[.!?।])\s+", t)
    return " ".join(s[:3])[:400]

def sys_for(phone): return SYS + (DEMO_NOTE if phone in ALLOWED else "")

FALLBACKS = [
    ("sarvam", "https://api.sarvam.ai/v1/chat/completions", "SARVAM_API_KEY", os.environ.get("SARVAM_CHAT_MODEL", "sarvam-105b-conversations")),
    ("groq", "https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY", os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")),
    ("nvidia", "https://integrate.api.nvidia.com/v1/chat/completions", "NVIDIA_API_KEY", os.environ.get("NVIDIA_MODEL", "meta/llama-3.3-70b-instruct")),
    ("openrouter", "https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_API_KEY", os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")),
]

async def gemini(contents, system=SYS, json_out=False, brain=""):
    """Brain with fallback chain: Gemini -> Groq -> NVIDIA -> OpenRouter (only those with keys set)."""
    errs = []
    if GEMINI and brain in ("", "gemini"):
        body = {"system_instruction": {"parts": [{"text": system}]}, "contents": contents}
        if json_out: body["generationConfig"] = {"responseMimeType": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=40) as c:
                r = await c.post(f"https://generativelanguage.googleapis.com/v1beta/models/{GMODEL}:generateContent", headers={"x-goog-api-key": GEMINI}, json=body)
            if r.status_code == 200: return r.json()["candidates"][0]["content"]["parts"][0]["text"]
            errs.append(f"gemini {r.status_code}")
        except Exception as e: errs.append(f"gemini {type(e).__name__}")
    msgs = [{"role": "system", "content": system + (" Respond with JSON only." if json_out else "")}] + [
        {"role": "assistant" if c["role"] == "model" else "user", "content": c["parts"][0]["text"]} for c in contents]
    order = sorted(FALLBACKS, key=lambda f: 0 if f[0] == brain else 1)
    for name, url, envk, model in order:
        key = os.environ.get(envk, "")
        if not key: continue
        try:
            async with httpx.AsyncClient(timeout=40) as c:
                r = await c.post(url, headers={"Authorization": "Bearer " + key}, json={"model": model, "messages": msgs, "temperature": 0.4})
            if r.status_code == 200: return r.json()["choices"][0]["message"]["content"]
            errs.append(f"{name} {r.status_code}")
        except Exception as e: errs.append(f"{name} {type(e).__name__}")
    if GEMINI and brain not in ("", "gemini"):
        return await gemini(contents, system, json_out, "gemini")
    raise HTTPException(502, "brain failed: " + ", ".join(errs))

def notes_for(phone):
    s = load(); n = s.get("notes", {}).get(phone)
    return n

DEEPGRAM = os.environ.get("DEEPGRAM_API_KEY", "")
async def stt_deepgram(data):
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post("https://api.deepgram.com/v1/listen?model=nova-3&detect_language=true&smart_format=true", headers={"Authorization": "Token " + DEEPGRAM, "Content-Type": "application/octet-stream"}, content=data)
    if r.status_code != 200: raise HTTPException(502, f"deepgram {r.status_code}: {r.text[:200]}")
    ch = r.json()["results"]["channels"][0]
    return ch["alternatives"][0].get("transcript", ""), {"hi": "hi-IN", "en": "en-IN", "mr": "mr-IN", "kn": "kn-IN"}.get((ch.get("detected_language") or "hi")[:2], "hi-IN")

VARIANTS = {"A": ("Sarvam STT + Gemini + Sarvam voice", "sarvam", "gemini"), "B": ("Sarvam STT + Groq + Sarvam voice", "sarvam", "groq"),
            "C": ("Sarvam STT + NVIDIA + Sarvam voice", "sarvam", "nvidia"), "D": ("Deepgram STT + Gemini + Sarvam voice", "deepgram", "gemini"),
            "F": ("All-Sarvam: STT + sarvam-105b-conversations + voice", "sarvam", "sarvam"),
            "E": ("Sarvam STT + OpenRouter + Sarvam voice", "sarvam", "openrouter")}

async def stt(data, name, engine="sarvam"):
    if engine == "deepgram" and DEEPGRAM: return await stt_deepgram(data)
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post("https://api.sarvam.ai/speech-to-text", headers={"api-subscription-key": SARVAM},
                         files={"file": (name, data, "application/octet-stream")}, data={"model": "saaras:v3", "mode": "codemix", "language_code": "unknown"})
    if r.status_code != 200: raise HTTPException(502, f"stt {r.status_code}: {r.text[:200]}")
    j = r.json(); return j.get("transcript", ""), j.get("language_code") or "hi-IN"

def pick_voice(voice, lang):
    v = (voice or os.environ.get("SARVAM_SPEAKER", "shubh")).lower()
    if v == "ashutosh" and lang != "hi-IN": v = "ratan"
    return v if v in ("shubh", "ratan", "ashutosh", "priya", "ritu", "ishita") else "shubh"

async def tts(text, lang, voice=""):
    lang = lang if lang in ("hi-IN","mr-IN","en-IN","kn-IN","ta-IN","te-IN","bn-IN","gu-IN","ml-IN","pa-IN","od-IN") else "hi-IN"
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post("https://api.sarvam.ai/text-to-speech", headers={"api-subscription-key": SARVAM},
                         json={"text": text[:2400], "language_code": lang, "model": "bulbul:v3", "speaker": pick_voice(voice, lang), "pace": 1.0, "temperature": 0.6, "output_audio_codec": "mp3"})
    if r.status_code != 200: raise HTTPException(502, f"tts {r.status_code}: {r.text[:200]}")
    return r.json()["audios"][0]

@app.post("/turn")
async def turn(audio: UploadFile = File(...), history: str = Form("[]"), phone: str = Form(""), variant: str = Form("A"), voice: str = Form(""), x_token: str = Header(None)):
    auth(x_token)
    _, engine, brain = VARIANTS.get(variant, VARIANTS["A"])
    t0 = time.time()
    text, lang = await stt(await audio.read(), audio.filename or "a.webm", engine)
    t_stt = time.time() - t0
    hist = json.loads(history)[-24:]
    memo = notes_for(phone)
    sysm = sys_for(phone) + ((" Notes from earlier calls with this person (use naturally, do not recite): " + json.dumps(memo, ensure_ascii=False)) if memo else "")
    if not text.strip(): return {"heard": "", "reply": "", "audio": ""}
    contents = [{"role": "user" if h["who"] == "person" else "model", "parts": [{"text": h["text"]}]} for h in hist]
    contents.append({"role": "user", "parts": [{"text": text}]})
    raw = await gemini(contents, system=sysm + ' Return JSON only: {"reply":"<your spoken reply>","lang":"<BCP-47 of the reply: hi-IN, en-IN, mr-IN or kn-IN>"}', json_out=True, brain=brain)
    t_brain = time.time() - t0 - t_stt
    try:
        j = json.loads(raw); reply = clean_reply(str(j["reply"])); lang = j.get("lang") or lang
    except Exception:
        reply = raw.strip()
    au = await tts(reply, lang, voice)
    return {"heard": text, "reply": reply, "audio": au, "lang": lang, "variant": variant, "secs": {"stt": round(t_stt,2), "brain": round(t_brain,2), "total": round(time.time()-t0,2)}}

@app.post("/speak")
async def speak(text: str = Form(...), lang: str = Form("hi-IN"), voice: str = Form(""), x_token: str = Header(None)):
    auth(x_token); return {"audio": await tts(text, lang, voice)}

ANALYSE = ('Read this call transcript. Return JSON: {"interest":"interested|callback|not_interested|unclear","summary":"2 lines",'
           '"needs":"what they asked","email":"email if they gave one else empty","language":"language used","questions":"their questions",'
           '"callback_time":"if said else empty","whatsapp_message":"short message with webinar details using ONLY approved facts and placeholder [JOIN LINK]"}')

def build_email(name, phone, a, transcript):
    # PLACEHOLDER format. Replace with Sagar's sample format once he sends 4-5 emails.
    return ("Subject: Call report - %s (%s)\n\nResult: %s\nSummary: %s\nQuestions asked: %s\nLanguage: %s\nEmail given: %s\nCallback: %s\n\nTranscript:\n%s"
            % (name, phone, a.get("interest"), a.get("summary"), a.get("questions", ""), a.get("language", ""), a.get("email", "") or "none (share details via chat)", a.get("callback_time", ""), transcript))

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
    s.setdefault("notes", {})[phone] = {"last_summary": a.get("summary"), "language": a.get("language"), "email": a.get("email"), "interest": a.get("interest"), "callback_time": a.get("callback_time"), "questions": a.get("questions")}; save(s)
    a["email_draft"] = build_email(name, phone, a, transcript)
    await sheet({"action": "call", "name": name, "phone": phone, **{k: a.get(k, "") for k in ("interest","summary","needs","questions","language","email","callback_time","whatsapp_message","email_draft")}})
    s["calls"][-1]["email_draft"] = a["email_draft"]; save(s)
    await tg_send(f"EMAIL DRAFT (format pending Sagar's samples, not sent):\n{a['email_draft']}")
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


# ---------------- Twilio phone test (own number only) ----------------
import hmac, hashlib, uuid
from fastapi import Request
from fastapi.responses import Response
TW_SID = os.environ.get("TWILIO_ACCOUNT_SID", ""); TW_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", ""); TW_FROM = os.environ.get("TWILIO_FROM", "")
PUBLIC = os.environ.get("PUBLIC_URL", "https://avenshift-voice-assistant.onrender.com")
ALLOWED = [x.strip() for x in os.environ.get("ALLOWED_CALL_NUMBERS", "+919370727222").split(",") if x.strip()]
AUDIO, CALLS = {}, {}

def intro_for(name, mode, lang, phone):
    en = lang == "en-IN"
    demo = phone in ALLOWED and not os.environ.get("LEAD_MODE_ALWAYS")   # demo_mode: own number; lead_mode: anyone else
    who = "Hi, I am Sagar from NextLearn." if demo else "Hi, I am from NextLearn's AI voice team."
    if mode == "rem":
        t = who + " " + ((FACTS["caller_intro_reminder_en"].split("NextLearn. ",1)[-1]).replace("{name}", name) if en else (FACTS["caller_intro_reminder"].split("baare me. ",1)[-1]).replace("{name}", name))
    else:
        t = who + (" Do you have one minute?")
    if not demo: t += " This call may be recorded." if en else " Call record ho sakti hai."
    return t

def twiml(body): return Response('<?xml version="1.0" encoding="UTF-8"?><Response>' + body + "</Response>", media_type="application/xml")

async def tw_check(request: Request, form):
    sig = request.headers.get("X-Twilio-Signature", "")
    url = PUBLIC + request.url.path + (("?" + request.url.query) if request.url.query else "")
    data = url + "".join(k + form[k] for k in sorted(form.keys()))
    mac = base64.b64encode(hmac.new(TW_TOKEN.encode(), data.encode(), hashlib.sha1).digest()).decode()
    if TW_TOKEN and hmac.compare_digest(mac, sig): return
    cid = request.query_params.get("cid", "")
    if cid in CALLS:  # unguessable per-call id created by our own authenticated /twilio/call
        print("tw_check: signature mismatch, allowed by cid", cid, flush=True); return
    raise HTTPException(403, "bad signature")

@app.post("/twilio/call")
async def tw_call(to: str = Form(...), name: str = Form(...), mode: str = Form("enq"), lang: str = Form("hi-IN"), variant: str = Form("A"), voice: str = Form(""), src: str = Form(""), x_token: str = Header(None)):
    auth(x_token)
    if to not in ALLOWED: raise HTTPException(403, "number not allowed (test numbers only)")
    if not (TW_SID and TW_TOKEN and TW_FROM): raise HTTPException(400, "Twilio not configured")
    cid = uuid.uuid4().hex[:10]
    CALLS[cid] = {"name": name, "phone": to, "hist": [], "lang": lang, "mode": mode, "turns": 0, "variant": variant, "voice": voice, "src": src}
    await prep_fillers(voice)
    try:
        en0 = lang == "en-IN"; c0 = CALLS[cid]
        t0 = intro_for(name, mode, lang, to)
        c0["intro_text"] = t0; c0["intro_url"] = await say_play(t0, lang, voice)
    except Exception as e: print("intro pregen failed", e, flush=True)
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"https://api.twilio.com/2010-04-01/Accounts/{TW_SID}/Calls.json", auth=(TW_SID, TW_TOKEN),
            data={"To": to, "From": TW_FROM, "Url": f"{PUBLIC}/twilio/voice?cid={cid}", "StatusCallback": f"{PUBLIC}/twilio/status?cid={cid}", "StatusCallbackEvent": "completed"})
    if r.status_code >= 300: raise HTTPException(502, "twilio: " + r.text[:200])
    return {"ok": True, "cid": cid}

@app.get("/audio/{aid}.mp3")
def get_audio(aid: str):
    b = AUDIO.get(aid)
    if not b: raise HTTPException(404)
    return Response(base64.b64decode(b), media_type="audio/mpeg")

async def say_play(text, lang, voice=""):
    aid = uuid.uuid4().hex[:12]; AUDIO[aid] = await tts(text, lang, voice)
    return f"{PUBLIC}/audio/{aid}.mp3"

def gather(cid, lang, play=""):
    inner = f"<Play>{play}</Play>" if play else ""
    return f'<Gather input="speech" bargeIn="true" language="{lang}" speechTimeout="auto" hints="yes,haan,ha,haan ji,ji,theek hai,nahi,no,okay,sure" speechModel="phone_call" enhanced="true" timeout="10" action="{PUBLIC}/twilio/turn?cid={cid}">{inner}</Gather><Redirect method="POST">{PUBLIC}/twilio/turn?cid={cid}</Redirect>'

@app.post("/twilio/voice")
async def tw_voice(request: Request, cid: str):
    form = dict(await request.form()); await tw_check(request, form)
    c = CALLS.get(cid)
    if not c: return twiml("<Hangup/>")
    en = c["lang"] == "en-IN"
    t = intro_for(c["name"], c["mode"], c["lang"], c["phone"])
    c["hist"].append({"who": "agent", "text": t})
    url = c.get("intro_url") if c.get("intro_text") == t and c.get("intro_url") else await say_play(t, c["lang"], c.get("voice",""))
    return twiml(gather(cid, "en-IN" if en else "hi-IN", url))

FILLERS = ["Achha...", "Haan ji...", "Theek hai...", "Bilkul..."]
FILLER_AUDIO = {}  # (voice, i) -> aid

async def prep_fillers(voice):
    for i, t in enumerate(FILLERS):
        k = (pick_voice(voice, "hi-IN"), i)
        if k in FILLER_AUDIO: continue
        try:
            aid = "f" + uuid.uuid4().hex[:10]; AUDIO[aid] = await tts(t, "hi-IN", voice); FILLER_AUDIO[k] = aid
        except Exception: pass

def pick_filler(voice):
    ks = [k for k in FILLER_AUDIO if k[0] == pick_voice(voice, "hi-IN")]
    return f"{PUBLIC}/audio/{FILLER_AUDIO[random.choice(ks)]}.mp3" if ks else ""

async def compute_reply(c):
    memo = notes_for(c["phone"])
    sysm = sys_for(c["phone"]) + ((" Earlier notes: " + json.dumps(memo, ensure_ascii=False)) if memo else "") + ' Return JSON only: {"reply":"...","lang":"hi-IN|en-IN|mr-IN|kn-IN"}'
    contents = [{"role": "user" if h["who"] == "person" else "model", "parts": [{"text": h["text"]}]} for h in c["hist"][-24:]]
    _t = time.time()
    raw = await gemini(contents, system=sysm, json_out=True, brain=VARIANTS.get(c.get("variant","A"), VARIANTS["A"])[2])
    print("brain_secs", round(time.time() - _t, 2), flush=True)
    try: j = json.loads(raw); reply, lang = clean_reply(str(j["reply"])), j.get("lang") or "hi-IN"
    except Exception: reply, lang = clean_reply(raw), "hi-IN"
    c["hist"].append({"who": "agent", "text": reply})
    gl = {"hi-IN": "hi-IN", "en-IN": "en-IN", "mr-IN": "mr-IN", "kn-IN": "kn-IN"}.get(lang, "hi-IN")
    return gl, await say_play(reply, lang, c.get("voice", ""))

@app.post("/twilio/turn")
async def tw_turn(request: Request, cid: str):
    form = dict(await request.form()); await tw_check(request, form)
    c = CALLS.get(cid)
    said = (form.get("SpeechResult") or "").strip()
    if not c or c["turns"] >= 8: return twiml("<Say>Thank you. Goodbye.</Say><Hangup/>")
    if not said:
        c["empty"] = c.get("empty", 0) + 1
        if c["empty"] >= 4: return twiml("<Say>Thank you. Goodbye.</Say><Hangup/>")
        c["turns"] += 1
        c["hist"].append({"who": "person", "sys": True, "text": "(silence - the person has not spoken. Do NOT pitch more. In ONE short sentence, politely check if they can hear you or if now is a bad time, and offer to call back. Never mention this note.)" if c["empty"] < 3 else "(silence again - say a very short polite goodbye and that you will message details later. Never mention this note.)"})
        gl, url = await compute_reply(c); return twiml(gather(cid, gl, url))
    c["empty"] = 0
    c["turns"] += 1; c["hist"].append({"who": "person", "text": said})
    try:
        if c.get("task") and not c["task"].done(): c["task"].cancel()
    except Exception: pass
    c["task"] = asyncio.create_task(compute_reply(c))   # brain + voice run while the filler plays
    f = pick_filler(c.get("voice", ""))
    if f: return twiml(f'<Gather input="speech" bargeIn="true" language="{"en-IN" if c["lang"] == "en-IN" else "hi-IN"}" speechTimeout="1" speechModel="phone_call" enhanced="true" timeout="1" action="{PUBLIC}/twilio/turn?cid={cid}"><Play>{f}</Play></Gather><Redirect method="POST">{PUBLIC}/twilio/reply?cid={cid}</Redirect>')
    gl, url = await c["task"]; return twiml(gather(cid, gl, url))

@app.post("/twilio/reply")
async def tw_reply(request: Request, cid: str):
    form = dict(await request.form()); await tw_check(request, form)
    c = CALLS.get(cid)
    if not c or not c.get("task"): return twiml("<Hangup/>")
    try: gl, url = await c["task"]
    except (Exception, asyncio.CancelledError): return twiml("<Say>Sorry, network issue. We will call you back. Goodbye.</Say><Hangup/>")
    return twiml(gather(cid, gl, url))

@app.post("/twilio/status")
async def tw_status(request: Request, cid: str):
    form = dict(await request.form()); await tw_check(request, form)
    c = CALLS.pop(cid, None)
    if c and c["hist"]:
        tr = "\n".join(("AGENT: " if h["who"] == "agent" else "PERSON: ") + ("(silence)" if h.get("sys") else h["text"]) for h in c["hist"])
        raw = await gemini([{"role": "user", "parts": [{"text": tr}]}], system=ANALYSE + " Approved facts: " + json.dumps(FACTS["approved_facts"]), json_out=True)
        try: a = json.loads(raw)
        except Exception: a = {"interest": "unclear", "summary": raw[:200]}
        s = load(); s["calls"].append({"name": c["name"], "phone": c["phone"], "t": time.time(), "transcript": tr, "via": "twilio", **a})
        s.setdefault("notes", {})[c["phone"]] = {"last_summary": a.get("summary"), "language": a.get("language"), "email": a.get("email"), "interest": a.get("interest")}; save(s)
        if c.get("src") != "n8n":  # n8n workflow does its own Sheet log + Telegram summary
            await sheet({"action": "call", "name": c["name"], "phone": c["phone"], "email_draft": build_email(c["name"], c["phone"], a, tr), **{k: a.get(k, "") for k in ("interest","summary","needs","questions","language","email","callback_time","whatsapp_message")}})
            await tg_send("PHONE TEST CALL done: " + c["name"] + "\nResult: " + str(a.get("interest")) + "\n" + str(a.get("summary")) + "\n\n" + build_email(c["name"], c["phone"], a, tr))
    return Response("ok")

@app.get("/calls")
def calls(since: float = 0, x_token: str = Header(None)):
    auth(x_token)
    return {"calls": [{k: c.get(k) for k in ("name","phone","t","transcript","interest","summary","needs","questions","language","email","callback_time","whatsapp_message","email_draft")} for c in load()["calls"] if c["t"] > since]}

@app.get("/")
def home(): return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


# ---- n8n helper endpoints (all need x-token) ----
@app.get("/n8n/contacts")
async def n8n_contacts(x_token: str = Header(None)):
    auth(x_token)
    r = await sheet({"action": "contacts"})
    if not r or not r.get("ok"): raise HTTPException(502, "sheet not connected")
    return r
@app.get("/n8n/facts")
def n8n_facts(x_token: str = Header(None)):
    auth(x_token); return {"approved_facts": FACTS["approved_facts"]}
@app.post("/n8n/log")
async def n8n_log(request: Request, x_token: str = Header(None)):
    auth(x_token); d = await request.json()
    r = await sheet({"action": "call", **{k: d.get(k, "") for k in ("name","phone","interest","summary","needs","questions","language","email","callback_time","whatsapp_message","email_draft")}})
    return {"ok": bool(r and r.get("ok", True))}
@app.post("/n8n/retry")
async def n8n_retry(request: Request, x_token: str = Header(None)):
    auth(x_token); d = await request.json()
    st = str(d.get("status", "no_answer")).lower()
    r = await sheet({"action": "retry", "name": d.get("name", ""), "phone": d.get("phone", ""), "status": st})
    return {"ok": bool(r and r.get("ok")), "result": r}
@app.post("/n8n/notify")
async def n8n_notify(request: Request, x_token: str = Header(None)):
    auth(x_token); d = await request.json(); await tg_send(str(d.get("text", ""))); return {"ok": True}

# ---- temporary voice comparison (fixed texts, whitelisted voices only) ----
SAMPLE_OK = {"v3:shubh","v3:ratan","v3:ashutosh","v4:shubh_enhi_companion","v4:shubh_enhi_ads","v4:sunny_enhi_customer","v4:amit_hi_conversational","v4:ashutosh_hi_conversational","v4:kabir_hi_conversational","v4:aayan_hi_conversational","v4:manan_hi_conversational","v4:chetan_kn_conversation","v4:nilesh_mr_conversation"}
SAMPLE_TXT = {"hi-IN": "Hi, I am Sagar from NextLearn. Do you have one minute? Hum ek free AI developer program chala rahe hain, jisme aapko certificate bhi milta hai.",
              "kn-IN": "ನಮಸ್ಕಾರ, ನಾನು NextLearn ನಿಂದ ಸಾಗರ್ ಮಾತನಾಡುತ್ತಿದ್ದೇನೆ. ಈಗ ಒಂದು ನಿಮಿಷ ಮಾತನಾಡಬಹುದಾ? ನಾವು ಉಚಿತ AI ಡೆವಲಪರ್ ಕಾರ್ಯಕ್ರಮ ನಡೆಸುತ್ತಿದ್ದೇವೆ.",
              "mr-IN": "नमस्कार, मी NextLearn कडून सागर बोलतोय. तुमच्याकडे एक मिनिट वेळ आहे का? आम्ही एक मोफत AI डेव्हलपर कार्यक्रम चालवत आहोत."}
@app.get("/sample")
async def sample(v: str, l: str = "hi-IN"):
    if v not in SAMPLE_OK or l not in SAMPLE_TXT: raise HTTPException(400, "no")
    mdl, spk = v.split(":"); t0 = time.time()
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post("https://api.sarvam.ai/text-to-speech", headers={"api-subscription-key": SARVAM},
            json={"text": SAMPLE_TXT[l], "language_code": l, "model": "bulbul:v3" if mdl == "v3" else "bulbul:v4-flash", "speaker": spk, "pace": 1.0, "temperature": 0.6, "output_audio_codec": "mp3"})
    if r.status_code != 200: raise HTTPException(502, f"{r.status_code}: {r.text[:300]}")
    return Response(base64.b64decode(r.json()["audios"][0]), media_type="audio/mpeg", headers={"x-secs": f"{time.time()-t0:.2f}"})

STT_TESTS = [("hi-IN", "नमस्ते, मैं नेक्स्टलर्न से सागर बोल रहा हूँ। क्या आपके पास एक मिनट है?"),
             ("hi-IN", "हम एक फ्री एआई डेवलपर प्रोग्राम चला रहे हैं, जिसमें आपको सर्टिफिकेट भी मिलता है।"),
             ("hi-IN", "ठीक है, मैं आपको व्हाट्सएप पर पूरी डिटेल्स भेज देता हूँ, आप अपने समय पर देख लीजिए।"),
             ("hi-IN", "आज शाम छह बजे वेबिनार है, क्या मैं आपका रजिस्ट्रेशन कन्फर्म कर दूँ?")]
def _wer(ref, hyp):
    import re
    n = lambda s: re.sub(r"[^\w\s]", " ", s.lower()).split()
    r, h = n(ref), n(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        p, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            t = d[j]; d[j] = min(d[j] + 1, d[j - 1] + 1, p + (r[i - 1] != h[j - 1])); p = t
    return round(d[len(h)] / max(1, len(r)), 3)
@app.get("/stttest")
async def stttest():
    out = []
    async with httpx.AsyncClient(timeout=60) as c:
        for lang, text in STT_TESTS:
            t0 = time.time()
            r = await c.post("https://api.sarvam.ai/text-to-speech", headers={"api-subscription-key": SARVAM},
                json={"text": text, "language_code": lang, "model": "bulbul:v3", "speaker": "shubh", "output_audio_codec": "mp3"})
            if r.status_code != 200: out.append({"err": f"tts {r.status_code}"}); continue
            audio = base64.b64decode(r.json()["audios"][0]); tt = round(time.time() - t0, 2)
            row = {"ref": text, "tts_s": tt}
            for eng in ("deepgram", "sarvam"):
                t1 = time.time()
                try:
                    tr, lg = await stt(audio, "t.mp3", eng)
                    row[eng] = {"text": tr, "wer": _wer(text, tr), "secs": round(time.time() - t1, 2)}
                except Exception as e: row[eng] = {"err": str(e)[:120]}
            out.append(row)
    return JSONResponse(out)
