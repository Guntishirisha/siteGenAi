"""SiteGen AI backend (v0.1). Local-first: SQLite storage, Claude called from the server only."""
import hmac
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from typing import Optional
 
import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Path
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
 
load_dotenv()
 
DB_PATH = os.getenv("DB_PATH", "sitegen.db")
APP_TOKEN = os.getenv("APP_TOKEN", "")
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")  # change in Render settings if Google retires it
ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:5500,http://127.0.0.1:5500").split(",") if o.strip()]
 
app = FastAPI(title="SiteGen AI Backend", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=["*"], allow_headers=["*"])
 
 
@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()
 
 
def init_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS sites (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, spec TEXT NOT NULL,
                score INTEGER, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS marketing (
                site_id TEXT PRIMARY KEY REFERENCES sites(id) ON DELETE CASCADE,
                plan TEXT, posts TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS ai_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, model TEXT,
                input_tokens INTEGER, output_tokens INTEGER);
            """
        )
 
 
init_db()
 
 
def check_token(x_app_token: str = Header(default="")):
    """If APP_TOKEN is set, every /api call must send it as the X-App-Token header."""
    if APP_TOKEN and not hmac.compare_digest(x_app_token, APP_TOKEN):
        raise HTTPException(401, "Invalid or missing X-App-Token")
 
 
SiteId = Path(pattern=r"^[a-z0-9]{4,32}$")
 
 
class SiteIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    spec: dict
    score: Optional[int] = Field(default=None, ge=0, le=100)
 
 
class MarketingIn(BaseModel):
    plan: Optional[dict] = None
    posts: list[dict] = Field(default_factory=list, max_length=100)
 
 
class AIIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=30000)
 
 
@app.get("/health")
def health():
    p = provider()
    return {"status": "ok", "ai_configured": p is not None, "ai_provider": p}
 
 
@app.get("/api/sites", dependencies=[Depends(check_token)])
def list_sites():
    with db() as c:
        rows = c.execute("SELECT id, name, score, updated FROM sites ORDER BY updated DESC").fetchall()
    return [dict(r) for r in rows]
 
 
@app.put("/api/sites/{site_id}", dependencies=[Depends(check_token)])
def save_site(body: SiteIn, site_id: str = SiteId):
    with db() as c:
        c.execute(
            "INSERT INTO sites (id, name, spec, score, updated) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, spec=excluded.spec, "
            "score=excluded.score, updated=excluded.updated",
            (site_id, body.name, json.dumps(body.spec), body.score, time.time()),
        )
    return {"id": site_id, "saved": True}
 
 
@app.get("/api/sites/{site_id}", dependencies=[Depends(check_token)])
def get_site(site_id: str = SiteId):
    with db() as c:
        r = c.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Site not found")
    return {"id": r["id"], "name": r["name"], "score": r["score"], "updated": r["updated"], "spec": json.loads(r["spec"])}
 
 
@app.delete("/api/sites/{site_id}", dependencies=[Depends(check_token)])
def delete_site(site_id: str = SiteId):
    with db() as c:
        n = c.execute("DELETE FROM sites WHERE id=?", (site_id,)).rowcount
    if not n:
        raise HTTPException(404, "Site not found")
    return {"id": site_id, "deleted": True}
 
 
@app.put("/api/marketing/{site_id}", dependencies=[Depends(check_token)])
def save_marketing(body: MarketingIn, site_id: str = SiteId):
    with db() as c:
        if not c.execute("SELECT 1 FROM sites WHERE id=?", (site_id,)).fetchone():
            raise HTTPException(404, "Save the site first, then its marketing plan")
        c.execute(
            "INSERT INTO marketing (site_id, plan, posts, updated) VALUES (?,?,?,?) "
            "ON CONFLICT(site_id) DO UPDATE SET plan=excluded.plan, posts=excluded.posts, updated=excluded.updated",
            (site_id, json.dumps(body.plan), json.dumps(body.posts), time.time()),
        )
    return {"site_id": site_id, "saved": True}
 
 
@app.get("/api/marketing/{site_id}", dependencies=[Depends(check_token)])
def get_marketing(site_id: str = SiteId):
    with db() as c:
        r = c.execute("SELECT plan, posts, updated FROM marketing WHERE site_id=?", (site_id,)).fetchone()
    if not r:
        return {"plan": None, "posts": [], "updated": None}
    return {"plan": json.loads(r["plan"]), "posts": json.loads(r["posts"]), "updated": r["updated"]}
 
 
def extract_json(text: str):
    """Claude sometimes wraps JSON in markdown fences or adds a sentence around it."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError("no JSON object found")
        return json.loads(m.group(0))
 
 
def provider():
    """Pick the AI provider. AI_PROVIDER=gemini|claude forces one; otherwise use whichever key is set."""
    want = os.getenv("AI_PROVIDER", "").lower()
    has = {"claude": bool(os.getenv("ANTHROPIC_API_KEY")), "gemini": bool(os.getenv("GEMINI_API_KEY"))}
    if want in has:
        return want if has[want] else None
    return "claude" if has["claude"] else "gemini" if has["gemini"] else None
 
 
def log_usage(model, tin, tout):
    with db() as c:
        c.execute("INSERT INTO ai_usage (ts, model, input_tokens, output_tokens) VALUES (?,?,?,?)", (time.time(), model, tin, tout))
 
 
def call_claude(prompt: str) -> str:
    import anthropic
 
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], timeout=90)
    try:
        r = client.messages.create(model=MODEL, max_tokens=8000, messages=[{"role": "user", "content": prompt}])
    except anthropic.RateLimitError:
        raise HTTPException(429, "The AI provider is rate limiting requests. Try again shortly.")
    except anthropic.APIError:
        raise HTTPException(502, "The AI provider returned an error")
    log_usage(MODEL, r.usage.input_tokens, r.usage.output_tokens)
    return "".join(b.text for b in r.content if b.type == "text")
 
 
def call_gemini(prompt: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 8000},
    }
    try:
        r = httpx.post(url, json=body, headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]}, timeout=90)
    except httpx.HTTPError:
        raise HTTPException(502, "Could not reach the Gemini API")
    if r.status_code == 429:
        raise HTTPException(429, "Gemini is rate limiting requests (free tier limits). Try again shortly.")
    if r.status_code in (400, 401, 403):
        raise HTTPException(502, "Gemini rejected the request. Check GEMINI_API_KEY and its restrictions.")
    if r.status_code == 404:
        raise HTTPException(502, f"Gemini model '{GEMINI_MODEL}' was not found. Set GEMINI_MODEL to a current model name.")
    if r.status_code != 200:
        raise HTTPException(502, "The AI provider returned an error")
    data = r.json()
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError):
        raise HTTPException(502, "Gemini returned no usable answer (it may have been blocked). Try rephrasing.")
    u = data.get("usageMetadata", {})
    log_usage(GEMINI_MODEL, u.get("promptTokenCount"), u.get("candidatesTokenCount"))
    return text
 
 
@app.post("/api/ai/json", dependencies=[Depends(check_token)])
def ai_json(body: AIIn):
    p = provider()
    if p is None:
        raise HTTPException(503, "No AI key is set on the server (set GEMINI_API_KEY or ANTHROPIC_API_KEY)")
    text = call_gemini(body.prompt) if p == "gemini" else call_claude(body.prompt)
    try:
        return {"result": extract_json(text)}
    except ValueError:
        raise HTTPException(502, "The AI returned invalid JSON. Please try again.")
 
