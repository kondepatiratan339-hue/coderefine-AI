from fastapi import FastAPI
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sqlite3, json, time, os, jwt, bcrypt
from dotenv import load_dotenv
from pathlib import Path

# Load environment variables from .env file
load_dotenv()

# AI imports
import google.genai as genai
from mistralai.client import Mistral
from fastapi.staticfiles import StaticFiles

# ---------------- CONFIG ----------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")
JWT_SECRET = os.getenv("JWT_SECRET", "secret123")

mistral = Mistral(api_key=MISTRAL_API_KEY)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- DB ----------------
conn = sqlite3.connect("db.sqlite", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""CREATE TABLE IF NOT EXISTS users(
id INTEGER PRIMARY KEY, email TEXT, password TEXT)""")

cursor.execute("""CREATE TABLE IF NOT EXISTS sessions(
id INTEGER PRIMARY KEY, title TEXT, language TEXT, updatedAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""");

cursor.execute("""CREATE TABLE IF NOT EXISTS refinements(
id INTEGER PRIMARY KEY,
session_id INTEGER,
original_code TEXT,
refined_code TEXT,
explanation TEXT,
language TEXT,
score INTEGER,
created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")

conn.commit()

# ---------------- MODELS ----------------
class AuthBody(BaseModel):
    email: str
    password: str

class SessionBody(BaseModel):
    title: str
    language: str

class RefineBody(BaseModel):
    code: str
    language: str

# ---------------- AUTH ----------------
@app.post("/api/auth/register")
def register(data: AuthBody):
    hashed = bcrypt.hashpw(data.password.encode(), bcrypt.gensalt())
    cursor.execute("INSERT INTO users(email,password) VALUES (?,?)",
                   (data.email, hashed))
    conn.commit()
    return {"msg": "registered"}

@app.post("/api/auth/login")
def login(data: AuthBody):
    cursor.execute("SELECT * FROM users WHERE email=?", (data.email,))
    user = cursor.fetchone()
    if not user:
        return JSONResponse(status_code=401, content={"error": "User not found"})

    if not bcrypt.checkpw(data.password.encode(), user[2]):
        return JSONResponse(status_code=401, content={"error": "Wrong password"})

    token = jwt.encode({"userId": user[0]}, JWT_SECRET, algorithm="HS256")
    return {"token": token}

# ---------------- SCORE ----------------
def calc_score(code: str):
    score = 100
    if "any" in code: score -= 10
    if len(code) < 20: score -= 20
    return max(score, 0)

# ---------------- AI ----------------
def ai_refine(code: str, language: str):
    prompt = f"""
You are an expert code reviewer.
Return ONLY JSON with this exact structure:
{{
 "refinedCode": "...",
 "explanation": "...",
 "errorChanges": [
   {{"error": "error name", "before": "bad code snippet", "after": "fixed code snippet", "reason": "why it was wrong"}},
   ...
 ]
}}

Code to review:
{code}
"""

    # ---- GEMINI ----
    try:
        if not GEMINI_API_KEY:
            raise Exception("GEMINI_API_KEY not set")
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        text = response.text

        start = text.find("{")
        end = text.rfind("}") + 1
        return json.loads(text[start:end]), "gemini"

    except Exception as e:
        print(f"❌ Gemini failed: {type(e).__name__}: {str(e)[:200]}")

    # ---- MISTRAL FALLBACK ----
    try:
        if not MISTRAL_API_KEY:
            raise Exception("MISTRAL_API_KEY not set")
        res = mistral.chat.complete(
            model="mistral-large-latest",
            messages=[{"role": "user", "content": prompt}]
        )

        text = res.choices[0].message.content
        start = text.find("{")
        end = text.rfind("}") + 1

        return json.loads(text[start:end]), "mistral"

    except Exception as e:
        print(f"❌ Mistral failed: {type(e).__name__}: {str(e)[:200]}")

    return {
        "refinedCode": code,
        "explanation": "AI failed"
    }, "fallback"

# ---------------- SESSION APIs ----------------
@app.get("/api/refine/sessions")
def get_sessions():
    cursor.execute("SELECT * FROM sessions ORDER BY updatedAt DESC")
    rows = cursor.fetchall()
    return [{"id": r[0], "title": r[1], "language": r[2], "updatedAt": r[3]} for r in rows]

@app.post("/api/refine/sessions")
def create_session(data: SessionBody):
    cursor.execute("INSERT INTO sessions(title,language,updatedAt) VALUES (?,?,?)",
                   (data.title, data.language, None))
    conn.commit()
    return {"id": cursor.lastrowid, "title": data.title, "language": data.language, "updatedAt": None}

@app.get("/api/refine/sessions/{sid}")
def get_session(sid: int):
    cursor.execute("SELECT * FROM sessions WHERE id=?", (sid,))
    s = cursor.fetchone()

    cursor.execute("SELECT * FROM refinements WHERE session_id=? ORDER BY created_at DESC", (sid,))
    refs = cursor.fetchall()

    return {
        "id": s[0],
        "title": s[1],
        "language": s[2],
        "updatedAt": s[3],
        "refinements": [
            {
                "id": r[0],
                "refinedCode": r[3],
                "explanation": r[4],
                "language": r[5],
                "score": r[6],
                "createdAt": r[7]
            } for r in refs
        ]
    }

@app.delete("/api/refine/sessions/{sid}")
def delete_session(sid: int):
    cursor.execute("DELETE FROM sessions WHERE id=?", (sid,))
    cursor.execute("DELETE FROM refinements WHERE session_id=?", (sid,))
    conn.commit()
    return {"ok": True}

# ---------------- STREAM API ----------------
@app.post("/api/refine/sessions/{sid}/refine")
def refine(sid: int, body: RefineBody):

    def generator():
        loading = "Analyzing...\nFixing...\nOptimizing...\n"
        for ch in loading:
            chunk_data = {'chunk': ch}
            yield f"data: {json.dumps(chunk_data)}\n\n"
            time.sleep(0.003)

        result, model_used = ai_refine(body.code, body.language)

        refined = result.get("refinedCode", body.code)
        explanation = result.get("explanation", "")
        score = calc_score(refined)

        cursor.execute("""
        INSERT INTO refinements
        (session_id,original_code,refined_code,explanation,language,score)
        VALUES (?,?,?,?,?,?)
        """, (sid, body.code, refined, explanation, body.language, score))
        conn.commit()

        # Update session's updatedAt timestamp
        cursor.execute("UPDATE sessions SET updatedAt=CURRENT_TIMESTAMP WHERE id=?", (sid,))
        conn.commit()

        data = {
            'done': True,
            'refinedCode': refined,
            'explanation': explanation,
            'errorChanges': result.get("errorChanges", []),
            'score': score,
            'model': model_used
        }
        yield f"data: {json.dumps(data)}\n\n"

    return StreamingResponse(generator(), media_type="text/event-stream")

# -------- STATIC FILES --------
# Serve frontend from the parent directory
frontend_path = Path(__file__).parent.parent / "frontend"
if frontend_path.exists():
    app.mount("/static", StaticFiles(directory=frontend_path), name="static")

@app.get("/")
async def root():
    frontend_html = frontend_path / "index.html"
    if frontend_html.exists():
        from fastapi.responses import FileResponse
        return FileResponse(frontend_html)
    return {"message": "Frontend not found"}

# -------- RUN SERVER --------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )