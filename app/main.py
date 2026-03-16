# app/main.py
import json
import logging
import os
import random
import socket
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Literal, Optional
from uuid import UUID

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from jose import JWTError, jwt
from openai import OpenAI
from passlib.context import CryptContext
from pydantic import BaseModel
from sqlalchemy import create_engine, text

from app.schemas import AttemptIn, AttemptOut, ProgressOut
from app.services.progress import save_attempt_and_update_progress, SELECT_PROGRESS

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "app.log"

logger = logging.getLogger("edugamificada")
logger.setLevel(logging.INFO)
if not logger.handlers:
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
    )
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
logger.propagate = False

# Carga variables de entorno antes de leer DATABASE_URL (carga explícita del .env en el backend)
load_dotenv(BASE_DIR / ".env")
logger.info("Loaded .env from %s", BASE_DIR / ".env")

# ---------------------------
# Configuración / Conexión DB
# ---------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
logger.info("DATABASE_URL = %s", DATABASE_URL)

# Comprobar DNS del host (opcional, para debug):
try:
    host = DATABASE_URL.split("@")[1].split(":")[0]  # db.<ref>.supabase.co
    logger.info("DB HOST = %s -> %s", host, socket.gethostbyname(host))
except Exception as e:
    logger.warning("DNS lookup failed: %s", e)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

CREATE_CUSTOM_ITEMS_TABLE = text(
    """
    create table if not exists custom_items (
        id bigserial primary key,
        subject text not null check (subject in ('matematica','castellano')),
        prompt text not null,
        options jsonb not null default '[]'::jsonb,
        answer text not null,
        created_by uuid not null references users(id) on delete cascade,
        is_active boolean not null default true,
        created_at timestamptz not null default now()
    )
    """
)
CREATE_CUSTOM_ITEMS_INDEX = text(
    """
    create index if not exists idx_custom_items_subject
        on custom_items (subject)
    """
)

CREATE_ACHIEVEMENTS_TABLE = text(
    """
    create table if not exists achievements (
        id bigserial primary key,
        student_id uuid not null references users(id) on delete cascade,
        code text not null,
        title text not null,
        description text not null,
        earned_at timestamptz not null default now(),
        unique (student_id, code)
    )
    """
)

CREATE_AI_HISTORY_TABLE = text(
    """
    create table if not exists ai_history (
        id bigserial primary key,
        student_id uuid not null references users(id) on delete cascade,
        subject text not null,
        prompt text not null,
        correct boolean not null,
        created_at timestamptz not null default now()
    )
    """
)


def ensure_custom_tables():
    try:
        with engine.begin() as conn:
            conn.execute(CREATE_CUSTOM_ITEMS_TABLE)
            conn.execute(CREATE_CUSTOM_ITEMS_INDEX)
            conn.execute(CREATE_ACHIEVEMENTS_TABLE)
            conn.execute(CREATE_AI_HISTORY_TABLE)
        logger.info("custom_items table ready")
    except Exception as exc:
        logger.error("Failed to ensure custom_items table: %s", exc)


ensure_custom_tables()

# Parámetros BKT-lite (puedes ajustarlos en .env)
SLIP  = float(os.getenv("SLIP", 0.10))
GUESS = float(os.getenv("GUESS", 0.20))
LEARN = float(os.getenv("LEARN", 0.15))
JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "4320"))
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")
SubjectLiteral = Literal["castellano", "matematica"]
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
logger.info("OpenAI client configured: %s", bool(openai_client))

# Cache simple en memoria para evitar llamar a la IA repetidamente con los mismos parámetros
_AI_ITEMS_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL_SECONDS = 90
_CACHE_MAX_ENTRIES = 50

DAILY_GOAL = int(os.getenv("DAILY_GOAL", "10"))

# ---------------------------
# FastAPI + CORS
# ---------------------------
app = FastAPI(title="EduGamificada API", version="0.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------
# Modelos Pydantic
# ---------------------------
class AttemptIn(BaseModel):
    student_id: str
    item_id: int
    # Puedes enviar directamente la corrección...
    correct: Optional[bool] = None
    # ...o enviar la respuesta para validarla server-side:
    answer_text: Optional[str] = None
    time_ms: int
    hints_used: int = 0


class RegisterPayload(BaseModel):
    email: str
    password: str
    role: Literal["student", "teacher"]
    display_name: Optional[str] = None
    age: Optional[int] = None


class LoginPayload(BaseModel):
    email: str
    password: str


class AgePayload(BaseModel):
    age: int


class CustomQuestionPayload(BaseModel):
    subject: SubjectLiteral
    prompt: str
    options: List[str]
    answer: str


# ---------------------------
# Utilidades
# ---------------------------
def bkt_update(p_old: float, correct: bool) -> float:
    """Actualiza la probabilidad de dominio (BKT-lite)."""
    if correct:
        num = p_old * (1 - SLIP)
        den = p_old * (1 - SLIP) + (1 - p_old) * GUESS
    else:
        num = p_old * SLIP
        den = p_old * SLIP + (1 - p_old) * (1 - GUESS)
    p_obs = num / den
    p_new = p_obs + (1 - p_obs) * LEARN
    return max(0.0, min(1.0, p_new))


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return pwd_context.verify(password, hashed)


def create_access_token(user_id: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": user_id, "exp": expire}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def sanitize_user(row: dict) -> dict:
    if not row:
        return {}
    created_at = row.get("created_at")
    if isinstance(created_at, (datetime, date)):
        created_at = created_at.isoformat()
    return {
        "id": str(row.get("id")),
        "email": row.get("email"),
        "role": row.get("role"),
        "display_name": row.get("display_name"),
        "age": row.get("age"),
        "created_at": created_at,
    }


def build_strengths_fallback(accuracy: float, attempts: int, time_avg_s: float) -> List[str]:
    strengths: List[str] = []
    acc_pct = int(accuracy * 100)
    if accuracy >= 0.8:
        strengths.append(f"Precisión sólida ({acc_pct}%).")
    elif accuracy >= 0.6:
        strengths.append(f"Precisión aceptable ({acc_pct}%), sigue reforzando.")
    if attempts >= 10:
        strengths.append(f"Constancia: {attempts} intentos acumulados.")
    if time_avg_s and time_avg_s <= 8:
        strengths.append(f"Buen ritmo de respuesta ({time_avg_s}s).")
    if not strengths:
        strengths.append("Continúas practicando, sigue así.")
    return strengths


def band_from_age(age: Optional[int]) -> str:
    if age is None:
        return "B"
    if age <= 7:
        return "A"
    if age <= 10:
        return "B"
    return "C"


def clamp_band(band: str) -> str:
    order = ["A", "B", "C"]
    if band not in order:
        return "B"
    return band


def adjust_band(base: str, acc_recent: Optional[float]) -> str:
    if acc_recent is None:
        return clamp_band(base)
    order = ["A", "B", "C"]
    idx = order.index(clamp_band(base))
    if acc_recent < 0.4 and idx > 0:
        idx -= 1
    elif acc_recent > 0.8 and idx < len(order) - 1:
        idx += 1
    return order[idx]


def recent_accuracy_by_subject(conn, student_id: str, subject: str) -> Optional[float]:
    try:
        acc = conn.execute(
            text(
                """
                select avg(correct_val) as acc
                from (
                    select a.correct::int as correct_val
                    from attempts a
                    join items i on i.id = a.item_id
                    where a.student_id = :sid
                      and lower(trim(
                          replace(replace(replace(replace(replace(i.subject,'á','a'),'é','e'),'í','i'),'ó','o'),'ú','u')
                      )) = :subject
                    order by a.created_at desc
                    limit 10
                ) as sub
                """
            ),
            {"sid": student_id, "subject": subject},
        ).scalar()
        return float(acc) if acc is not None else None
    except Exception as exc:
        logger.warning("recent_accuracy query failed: %s", exc)
        return None


def _parse_json_safely(raw: str) -> Optional[dict]:
    try:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
        return json.loads(cleaned)
    except Exception as exc:
        logger.error("AI JSON parse failed: %s | raw=%r", exc, raw[:500])
        return None


def _extract_content(choice) -> str:
    """Normaliza el contenido devuelto por la API de OpenAI (string o lista de partes)."""
    msg = choice.message
    content = getattr(msg, "content", "")
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(part.get("text") or "")
            else:
                txt = getattr(part, "text", None)
                if txt:
                    parts.append(txt)
        return "\n".join([p for p in parts if p])
    return content or ""


def generate_ai_summary(context: dict) -> Optional[dict]:
    if not openai_client:
        return None

    prompt = f"""
Eres un tutor educativo. Usa estos datos:
- Intentos: {context['attempts']}
- Precisión: {context['accuracy_percent']}%
- Tiempo promedio (s): {context['time_avg_s']}
- Habilidades más débiles: {context['weak_list']}

Devuelve SIEMPRE un JSON con:
- overview: frase breve (1-2 líneas)
- strengths: lista con al menos 2 fortalezas concretas (ej: "Precisión sólida", "Buen ritmo", "Constancia en intentos")
- focus: lista con al menos 2 próximos pasos accionables (ej: "Practicar Sumas 0-20", "Repasar Restas 0-10")

Sé conciso, positivo y específico.
"""
    models_to_try = [OPENAI_MODEL] if OPENAI_MODEL else []
    if "gpt-4o-mini" not in models_to_try:
        models_to_try.append("gpt-4o-mini")

    for mdl in models_to_try:
        try:
            resp = openai_client.chat.completions.create(
                model=mdl,
                messages=[{"role": "user", "content": prompt}],
                max_completion_tokens=180,
                response_format={"type": "json_object"},
            )
            raw = _extract_content(resp.choices[0])
            if not raw.strip():
                logger.warning("AI summary returned empty content | model=%s | message=%r", mdl, resp.choices[0].message)
                continue
            parsed = _parse_json_safely(raw)
            if parsed:
                return parsed
        except Exception as exc:
            logger.error("AI summary failed with model %s: %s", mdl, exc)

    return None


def _cache_key_for_items(subject: str, k: int, age: Optional[int], band: str, weak_skills: Optional[List[str]]) -> str:
    weak_norm = ",".join(sorted([w.strip().lower() for w in (weak_skills or []) if w]))
    return f"{subject}|{k}|{age or 'unk'}|{band}|{weak_norm}"


def _get_cached_items(key: str) -> Optional[List[dict]]:
    now = time.time()
    with _CACHE_LOCK:
        entry = _AI_ITEMS_CACHE.get(key)
        if not entry:
            return None
        if now - entry["ts"] > _CACHE_TTL_SECONDS:
            del _AI_ITEMS_CACHE[key]
            return None
        return entry["items"]


def _set_cached_items(key: str, items: List[dict]) -> None:
    with _CACHE_LOCK:
        if len(_AI_ITEMS_CACHE) >= _CACHE_MAX_ENTRIES:
            # borrar el cache más viejo
            oldest = min(_AI_ITEMS_CACHE.items(), key=lambda kv: kv[1]["ts"])[0]
            del _AI_ITEMS_CACHE[oldest]
        _AI_ITEMS_CACHE[key] = {"ts": time.time(), "items": items}


def generate_ai_items(
    subject: str,
    k: int,
    age: Optional[int],
    band: str,
    student_context: Optional[dict] = None,
    already_answered_prompts: Optional[List[str]] = None,
) -> List[dict]:
    """Genera k ejercicios usando OpenAI y devuelve items listos para el frontend."""
    if not openai_client:
        logger.warning("OpenAI client not configured - skipping AI item generation")
        return []

    weak_skills = (student_context or {}).get("weak_skills") or []
    cache_key = _cache_key_for_items(subject, k, age, band, weak_skills)
    cached = _get_cached_items(cache_key)
    if cached:
        logger.info("Returning cached AI items for key=%s", cache_key)
        return cached

    ctx_lines = []
    if student_context:
        acc = student_context.get("accuracy")
        attempts = student_context.get("attempts")
        weak = student_context.get("weak_skills") or []
        ctx_lines.append(f"Precision actual: {acc * 100:.0f}%" if acc is not None else "")
        ctx_lines.append(f"Intentos totales: {attempts}" if attempts is not None else "")
        if weak:
            ctx_lines.append(f"Habilidades débiles: {', '.join(weak)}")

    extra_instructions = ""
    if already_answered_prompts:
        short_list = [p for p in already_answered_prompts if p]
        if short_list:
            summary = "; ".join(short_list[:10])
            extra_instructions = (
                "\n\nNO repitas preguntas ya respondidas correctamente. "
                "Evita preguntas similares a: "
                f"{summary}."
            )

    prompt = f"""
Eres un tutor educativo de primaria.
Genera exactamente un JSON válido con un arreglo llamado "items".
Cada item debe tener:
  - prompt: texto de la pregunta
  - options: lista de opciones (puede ser [] para respuesta libre)
  - answer: texto con la respuesta correcta

Contexto del estudiante:
- Edad: {age or 'desconocida'}
- Materia: {subject}
- Banda de dificultad sugerida: {band}
{''.join([f'- {l}\n' for l in ctx_lines if l])}
{extra_instructions}

IMPORTANTE:
- Si la materia es "castellano", SOLO genera ejercicios de lengua (gramática, ortografía, lectura, vocabulario, comprensión). NO incluyas operaciones numéricas ni problemas matemáticos.
- Si la materia es "matematica", SOLO genera ejercicios de matemáticas (aritmética, lógica, problemas numéricos). NO incluyas preguntas de idioma.

Haz {k} ejercicios.
- Incluye al menos 1 ejercicio de opción múltiple y al menos 1 ejercicio de respuesta libre.
- Prioriza fortalecer las habilidades débiles listadas, y ajusta la dificultad si el estudiante ya tiene buena precisión.

Devuelve SOLO el JSON, sin texto adicional.
Ejemplo de salida:
{{"items":[{{"prompt":"...","options":["...","..."],"answer":"..."}},{{"prompt":"...","options":[],"answer":"..."}}]}}
"""

    # Intentamos varias veces / con fallback de modelo para mayor resiliencia.
    models_to_try = [OPENAI_MODEL or "gpt-4o-mini", "gpt-4o-mini"]
    last_exception = None

    for model in models_to_try:
        try:
            logger.info(
                "Generating AI items (model=%s subject=%s age=%s band=%s k=%s)",
                model,
                subject,
                age,
                band,
                k,
            )

            resp = openai_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_completion_tokens=450,
                response_format={"type": "json_object"},
                timeout=30,
            )

            raw = _extract_content(resp.choices[0])
            if raw and len(raw) > 1000:
                logger.info("AI raw response (truncated): %s", raw[:1000])
            else:
                logger.info("AI raw response: %s", raw)

            parsed = _parse_json_safely(raw)
            if not parsed or not isinstance(parsed, dict):
                logger.warning("AI items parse failed or returned non-dict: %r", parsed)
                continue

            items_raw = parsed.get("items") or []
            if not isinstance(items_raw, list):
                continue

            results: List[dict] = []
            base_id = -int(datetime.utcnow().timestamp() * 1000)
            for idx, it in enumerate(items_raw):
                if not isinstance(it, dict):
                    continue
                prompt_text = str(it.get("prompt") or "").strip()
                answer = str(it.get("answer") or "").strip()
                opts = it.get("options") or []
                if isinstance(opts, str):
                    try:
                        opts = json.loads(opts)
                    except Exception:
                        opts = []
                if not isinstance(opts, list):
                    opts = []

                results.append(
                    {
                        "id": base_id - idx,
                        "type": "ai",
                        "prompt": prompt_text,
                        "options": [str(o) for o in opts],
                        "answer": answer,
                        "subject": subject,
                        "reason": "ia",
                    }
                )

            if results:
                _set_cached_items(cache_key, results)
                return results

            logger.warning(
                "AI items generation returned 0 results (parsed items length=%d) for model=%s",
                len(items_raw),
                model,
            )
        except Exception as exc:
            last_exception = exc
            logger.warning("AI items generation failed (model=%s): %s", model, exc)

    if last_exception:
        logger.error("AI generation failed after retries: %s", last_exception)
    return []


def generate_default_items(subject: str, k: int) -> List[dict]:
    """Fallback muy básico cuando no hay datos en DB ni IA disponible."""
    base_id = -int(datetime.utcnow().timestamp() * 1000)
    items: List[dict] = []

    if subject == "matematica":
        items = [
            {
                "id": base_id,
                "type": "fallback",
                "prompt": "¿Cuánto es 2 + 3?",
                "options": ["3", "4", "5", "6"],
                "answer": "5",
                "subject": subject,
                "reason": "fallback",
            },
            {
                "id": base_id - 1,
                "type": "fallback",
                "prompt": "Escribe el resultado de 7 + 4:",
                "options": [],
                "answer": "11",
                "subject": subject,
                "reason": "fallback",
            },
        ]
    else:
        items = [
            {
                "id": base_id,
                "type": "fallback",
                "prompt": "¿Cuál es la palabra correcta: 'casa' o 'cassa'?",
                "options": ["casa", "cassa"],
                "answer": "casa",
                "subject": subject,
                "reason": "fallback",
            },
            {
                "id": base_id - 1,
                "type": "fallback",
                "prompt": "Escribe la palabra que falta: 'El gato es ____.'",
                "options": [],
                "answer": "negro",
                "subject": subject,
                "reason": "fallback",
            },
        ]

    # Asegurar que devolvemos hasta k items.
    return items[:k]


def get_token_from_request(request: Request) -> str:
    auth_header = request.headers.get("Authorization") or ""
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Token de autenticación requerido")
    return token


def require_user(request: Request, allowed_roles: Optional[List[str]] = None) -> dict:
    token = get_token_from_request(request)
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token sin usuario")

    with engine.begin() as conn:
        row = conn.execute(SELECT_USER_PUBLIC, {"user_id": user_id}).mappings().first()

    if not row:
        raise HTTPException(status_code=401, detail="Usuario no encontrado")

    if allowed_roles and row["role"] not in allowed_roles:
        raise HTTPException(status_code=403, detail="No autorizado")

    return row


def award_achievement(conn, student_id: str, code: str, title: str, desc: str) -> Optional[dict]:
    try:
        row = conn.execute(
            text(
                """
                insert into achievements (student_id, code, title, description)
                values (:sid, :code, :title, :desc)
                on conflict (student_id, code) do nothing
                returning id, code, title, description, earned_at
                """
            ),
            {"sid": student_id, "code": code, "title": title, "desc": desc},
        ).mappings().first()
        if row:
            return {
                "code": row["code"],
                "title": row["title"],
                "description": row["description"],
                "earned_at": row["earned_at"].isoformat() if row.get("earned_at") else None,
            }
    except Exception as exc:
        logger.warning("award_achievement failed: %s", exc)
    return None

# ---------------------------
# Endpoints básicos de salud
# ---------------------------
@app.get("/health")
def health():
    return {"ok": True}

@app.get("/db-check")
def db_check():
    with engine.begin() as conn:
        v = conn.execute(text("select 1")).scalar()
    return {"db": "ok", "result": int(v)}


# ---------------------------
# Autenticación y perfiles
# ---------------------------
@app.post("/auth/register")
def register_user(payload: RegisterPayload):
    role = payload.role.lower()
    if role not in {"student", "teacher"}:
        raise HTTPException(status_code=400, detail="Rol inválido")

    if payload.age is not None and (payload.age < 3 or payload.age > 120):
        raise HTTPException(status_code=400, detail="Edad fuera de rango")

    user_id = str(uuid.uuid4())
    hashed = hash_password(payload.password)

    with engine.begin() as conn:
        existing = conn.execute(SELECT_USER_BY_EMAIL, {"email": payload.email}).mappings().first()
        if existing:
            raise HTTPException(status_code=400, detail="Ese email ya está registrado")

        row = conn.execute(
            INSERT_USER_SQL,
            {
                "id": user_id,
                "email": payload.email,
                "password_hash": hashed,
                "role": role,
                "display_name": payload.display_name,
                "age": payload.age if role == "student" else None,
            },
        ).mappings().first()

    token = create_access_token(user_id)
    return {"token": token, "user": sanitize_user(row)}


@app.post("/auth/login")
def login_user(payload: LoginPayload):
    with engine.begin() as conn:
        row = conn.execute(SELECT_USER_BY_EMAIL, {"email": payload.email}).mappings().first()

    if not row or not verify_password(payload.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Credenciales inválidas")

    token = create_access_token(str(row["id"]))
    return {"token": token, "user": sanitize_user(row)}


@app.get("/auth/me")
def get_current_user(request: Request):
    user = require_user(request)
    return {"user": sanitize_user(user)}


@app.put("/students/me/age")
def update_student_age(payload: AgePayload, request: Request):
    if payload.age < 3 or payload.age > 120:
        raise HTTPException(status_code=400, detail="Edad fuera de rango")

    user = require_user(request, allowed_roles=["student"])
    with engine.begin() as conn:
        updated = conn.execute(
            UPDATE_STUDENT_AGE_SQL, {"age": payload.age, "user_id": user["id"]}
        ).mappings().first()

    if not updated:
        raise HTTPException(status_code=400, detail="No se pudo actualizar la edad")

    return {"user": sanitize_user(updated)}


@app.post("/teacher/questions")
def create_custom_question(payload: CustomQuestionPayload, request: Request):
    teacher = require_user(request, allowed_roles=["teacher"])
    if len(payload.options) < 2:
        raise HTTPException(status_code=400, detail="Debes proveer al menos dos opciones")

    norm_options = [str(o).strip() for o in payload.options if str(o).strip()]
    if not norm_options:
        raise HTTPException(status_code=400, detail="Opciones inválidas")

    options_json = json.dumps(norm_options, ensure_ascii=False)
    with engine.begin() as conn:
        row = conn.execute(
            INSERT_CUSTOM_ITEM_SQL,
            {
                "subject": payload.subject,
                "prompt": payload.prompt.strip(),
                "options": options_json,
                "answer": payload.answer.strip(),
                "created_by": str(teacher["id"]),
            },
        ).mappings().first()

    return {"question": {**row, "options": norm_options}}


@app.put("/teacher/questions/{question_id}")
def update_custom_question(question_id: int, payload: CustomQuestionPayload, request: Request):
    if question_id <= 0:
        raise HTTPException(status_code=400, detail="ID inválido")

    teacher = require_user(request, allowed_roles=["teacher"])
    if len(payload.options) < 2:
        raise HTTPException(status_code=400, detail="Debes proveer al menos dos opciones")

    norm_options = [str(o).strip() for o in payload.options if str(o).strip()]
    if not norm_options:
        raise HTTPException(status_code=400, detail="Opciones inválidas")

    with engine.begin() as conn:
        existing = conn.execute(
            SELECT_CUSTOM_ITEM_SQL, {"id": question_id}
        ).mappings().first()
        if not existing or str(existing["created_by"]) != str(teacher["id"]):
            raise HTTPException(status_code=404, detail="Pregunta no encontrada")

        options_json = json.dumps(norm_options, ensure_ascii=False)
        row = conn.execute(
            UPDATE_CUSTOM_ITEM_SQL,
            {
                "id": question_id,
                "teacher_id": str(teacher["id"]),
                "subject": payload.subject,
                "prompt": payload.prompt.strip(),
                "options": options_json,
                "answer": payload.answer.strip(),
            },
        ).mappings().first()

    if not row:
        raise HTTPException(status_code=400, detail="No se pudo actualizar la pregunta")

    opts = row.get("options")
    if isinstance(opts, str):
        try:
            opts = json.loads(opts)
        except json.JSONDecodeError:
            opts = norm_options

    return {"question": {**row, "options": opts}}


@app.delete("/teacher/questions/{question_id}")
def delete_custom_question(question_id: int, request: Request):
    teacher = require_user(request, allowed_roles=["teacher"])
    with engine.begin() as conn:
        existing = conn.execute(
            SELECT_CUSTOM_ITEM_SQL, {"id": question_id}
        ).mappings().first()
        if not existing or str(existing["created_by"]) != str(teacher["id"]):
            raise HTTPException(status_code=404, detail="Pregunta no encontrada")

        conn.execute(
            DELETE_CUSTOM_ITEM_SQL,
            {"id": question_id, "teacher_id": str(teacher["id"])},
        )
    return {"deleted": True, "id": question_id}


@app.get("/teacher/questions")
def list_custom_questions(request: Request):
    teacher = require_user(request, allowed_roles=["teacher"])
    with engine.begin() as conn:
        rows = conn.execute(
            LIST_CUSTOM_BY_CREATOR_SQL, {"user_id": str(teacher["id"])}
        ).mappings().all()

    questions = []
    for r in rows:
        opts = r.get("options") or []
        if isinstance(opts, str):
            try:
                opts = json.loads(opts)
            except json.JSONDecodeError:
                opts = []
        questions.append(
            {
                "id": r["id"],
                "subject": r["subject"],
                "prompt": r["prompt"],
                "options": opts,
                "answer": r["answer"],
                "created_at": r.get("created_at"),
            }
        )
    return {"questions": questions}


@app.get("/teacher/students")
def list_students(request: Request):
    teacher = require_user(request, allowed_roles=["teacher"])
    _ = teacher  # lint
    with engine.begin() as conn:
        rows = conn.execute(LIST_STUDENTS_SQL).mappings().all()
    students = []
    for r in rows:
        created_at = r.get("created_at")
        if isinstance(created_at, (datetime, date)):
            created_at = created_at.isoformat()
        students.append(
            {
                "id": str(r["id"]),
                "email": r["email"],
                "display_name": r.get("display_name"),
                "age": r.get("age"),
                "created_at": created_at,
            }
        )
    return {"students": students}


@app.get("/progress/daily")
def daily_progress(request: Request, sid: str, subject: Optional[str] = None):
    user = require_user(request, allowed_roles=["student", "teacher"])
    # Si es alumno, solo puede ver su propio progreso
    if user["role"] == "student" and str(user["id"]) != sid:
        raise HTTPException(status_code=403, detail="No autorizado")

    subj_norm = None
    if subject:
        subj_norm = (
            subject.strip()
            .lower()
            .replace("á", "a")
            .replace("é", "e")
            .replace("í", "i")
            .replace("ó", "o")
            .replace("ú", "u")
        )

    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                select date(a.created_at) as d, count(*) as n
                from attempts a
                join items i on i.id = a.item_id
                where a.student_id = :sid
                  and (:subject is null or lower(trim(
                        replace(replace(replace(replace(replace(i.subject,'á','a'),'é','e'),'í','i'),'ó','o'),'ú','u')
                  )) = :subject)
                group by 1
                order by d desc
                limit 7
                """
            ),
            {"sid": sid, "subject": subj_norm},
        ).mappings().all()

    today = date.today()
    attempts_today = 0
    streak_days = 0
    dates_counts = {r["d"]: int(r["n"]) for r in rows}
    if today in dates_counts:
        attempts_today = dates_counts[today]
    # calcular racha desde hoy hacia atrás
    cursor = today
    while cursor in dates_counts and dates_counts[cursor] > 0:
        streak_days += 1
        cursor = cursor - timedelta(days=1)

    history = [
        {"date": str(d), "attempts": dates_counts[d]} for d in sorted(dates_counts.keys(), reverse=True)
    ]

    return {
        "attempts_today": attempts_today,
        "goal": DAILY_GOAL,
        "streak_days": streak_days,
        "history": history,
    }


# ---------------------------
# POST /attempts
# Guarda intento, valida (opcional) y actualiza dominio, SRS y XP
# ---------------------------

# Consulta para resolver la materia a partir del item_id
RESOLVE_SUBJECT_SQL = text("SELECT subject FROM items WHERE id = :item_id")
SELECT_USER_BY_EMAIL = text(
    """
    select id, email, role, display_name, age, created_at, password_hash
    from users
    where lower(email) = lower(:email)
    limit 1
    """
)
SELECT_USER_PUBLIC = text(
    """
    select id, email, role, display_name, age, created_at
    from users
    where id = :user_id
    """
)
INSERT_USER_SQL = text(
    """
    insert into users (id, email, password_hash, role, display_name, age)
    values (:id, lower(:email), :password_hash, :role, :display_name, :age)
    returning id, email, role, display_name, age, created_at
    """
)
UPDATE_STUDENT_AGE_SQL = text(
    """
    update users
    set age = :age
    where id = :user_id and role = 'student'
    returning id, email, role, display_name, age, created_at
    """
)
INSERT_CUSTOM_ITEM_SQL = text(
    """
    insert into custom_items (subject, prompt, options, answer, created_by)
    values (lower(:subject), :prompt, CAST(:options AS jsonb), :answer, :created_by)
    returning id, subject, prompt, options, answer, created_at
    """
)
UPDATE_CUSTOM_ITEM_SQL = text(
    """
    update custom_items
    set subject = lower(:subject),
        prompt = :prompt,
        options = CAST(:options AS jsonb),
        answer = :answer
    where id = :id and created_by = :teacher_id
    returning id, subject, prompt, options, answer, created_at
    """
)
SELECT_CUSTOM_ITEM_SQL = text(
    """
    select id, subject, prompt, options, answer, created_by
    from custom_items
    where id = :id
    """
)
DELETE_CUSTOM_ITEM_SQL = text(
    """
    delete from custom_items
    where id = :id and created_by = :teacher_id
    """
)
LIST_STUDENTS_SQL = text(
    """
    select id, email, display_name, age, created_at
    from users
    where role = 'student'
    order by created_at asc
    """
)
LIST_ACHIEVEMENTS_SQL = text(
    """
    select code, title, description, earned_at
    from achievements
    where student_id = :sid
    order by earned_at desc
    """
)
LIST_CUSTOM_BY_CREATOR_SQL = text(
    """
    select id, subject, prompt, options, answer, created_at
    from custom_items
    where created_by = :user_id
    order by created_at desc
    """
)
RANDOM_CUSTOM_ITEMS_SQL = text(
    """
    select id, subject, prompt, options, answer
    from custom_items
    where lower(subject) = :subject and is_active = true
    order by random()
    limit :k
    """
)

@app.post("/attempts")
async def post_attempt(request: Request):
    student = require_user(request, allowed_roles=["student"])

    # 1) Leemos el JSON crudo que llega del front
    body = await request.json()
    logger.info("POST /attempts payload: %s", body)

    # 2) Normalizamos nombres de campos:
    #    - is_correct <- is_correct o correct
    #    - response   <- response o answer_text
    attempt_dict = {
        "student_id": str(student["id"]),
        "item_id": body.get("item_id"),
        "is_correct": body.get("is_correct", body.get("correct")),
        "response": body.get("response", body.get("answer_text")),
        "time_ms": body.get("time_ms", 0),
        "hints_used": body.get("hints_used", 0),
        "subject": body.get("subject"),  # puede venir vacío
    }

    # Validación mínima
    if attempt_dict["item_id"] is None:
        raise HTTPException(status_code=400, detail="item_id es obligatorio")

    if attempt_dict["is_correct"] is None:
        raise HTTPException(status_code=400, detail="is_correct/correct es obligatorio")

    # 3) Si no vino subject en el body, lo resolvemos desde la tabla items
    with engine.begin() as conn:
        if not attempt_dict["subject"]:
            row = conn.execute(
                RESOLVE_SUBJECT_SQL,
                {"item_id": attempt_dict["item_id"]},
            ).first()
            if not row or not row[0]:
                raise HTTPException(
                    status_code=400,
                    detail="No se pudo resolver subject para ese item_id",
                )
            attempt_dict["subject"] = row[0]

        # 4) Guardamos intento + actualizamos progreso
        attempt_id, p = save_attempt_and_update_progress(conn, attempt_dict)
        unlocked = []
        # Reglas de logros básicos
        if p["current_streak"] >= 5:
            ach = award_achievement(
                conn,
                attempt_dict["student_id"],
                "streak_5",
                "Racha 5",
                "Alcanzaste una racha de 5 respuestas correctas.",
            )
            if ach:
                unlocked.append(ach)
        acc_pct = float(p.get("accuracy") or 0) * 100
        if acc_pct >= 80 and p.get("total_attempts", 0) >= 10:
            ach = award_achievement(
                conn,
                attempt_dict["student_id"],
                "accuracy_80",
                "Precisión 80%",
                "Mantienes una precisión de 80% o más.",
            )
            if ach:
                unlocked.append(ach)
        # Meta diaria
        try:
            today_attempts = conn.execute(
                text(
                    """
                    select count(*) from attempts
                    where student_id = :sid and date(created_at) = current_date
                    """
                ),
                {"sid": attempt_dict["student_id"]},
            ).scalar() or 0
            if today_attempts >= DAILY_GOAL:
                ach = award_achievement(
                    conn,
                    attempt_dict["student_id"],
                    "daily_goal",
                    "Meta diaria",
                    f"Completaste tu meta de {DAILY_GOAL} ejercicios hoy.",
                )
                if ach:
                    unlocked.append(ach)
        except Exception as exc:
            logger.warning("daily goal check failed: %s", exc)

    # 5) Devolvemos el mismo formato que ya usabas para el progreso
    return {
        "id": attempt_id,
        "saved": True,
        "progress": {
            "student_id": p["student_id"],
            "subject": p["subject"],
            "total_attempts": p["total_attempts"],
            "correct_attempts": p["correct_attempts"],
            "accuracy": float(p["accuracy"]),
            "xp": p["xp"],
            "level": p["level"],
            "current_streak": p["current_streak"],
            "best_streak": p["best_streak"],
            "last_answer_at": p["last_answer_at"].isoformat() if p["last_answer_at"] else None,
        },
        "achievements_unlocked": unlocked,
    }

@app.get("/progress", response_model=ProgressOut)
def get_progress(student_id: UUID, subject: str):
    with engine.begin() as conn:
        row = conn.execute(SELECT_PROGRESS, {
            "student_id": str(student_id),
            "subject": subject
        }).mappings().first()
        if not row:
            return {
                "student_id": str(student_id),
                "subject": subject,
                "total_attempts": 0,
                "correct_attempts": 0,
                "accuracy": 0.0,
                "xp": 0,
                "level": 1,
                "current_streak": 0,
                "best_streak": 0,
                "last_answer_at": None
            }
        return {
            "student_id": row["student_id"],
            "subject": row["subject"],
            "total_attempts": row["total_attempts"],
            "correct_attempts": row["correct_attempts"],
            "accuracy": float(row["accuracy"]),
            "xp": row["xp"],
            "level": row["level"],
            "current_streak": row["current_streak"],
            "best_streak": row["best_streak"],
            "last_answer_at": row["last_answer_at"].isoformat() if row["last_answer_at"] else None
        }


# ---------------------------
# GET /next-items
# Recomendador simple: prioriza habilidades con menor dominio,
# incluye revisión por SRS y hace ε-greedy (explora un poco).
# ---------------------------
@app.get("/next-items")
def next_items(
    request: Request,
    mode: str = Query("adaptive"),
    subject: str = Query(...),   # sin regex para no bloquear
    k: int = Query(5, ge=1, le=20),
    student_id: Optional[str] = Query(None, min_length=8),
):
    current = require_user(request, allowed_roles=["student"])
    token_student_id = str(current["id"])
    if student_id and student_id != token_student_id:
        logger.warning(
            "next-items mismatch token %s vs query %s", token_student_id, student_id
        )
    logger.info("next-items requested by %s for %s (k=%s)", token_student_id, subject, k)

    # normalizar subject recibido
    subj = (
        subject.strip()
        .lower()
        .replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
    )

    # 1) Consulta principal: AHORA incluye i.answer
    base_sql = """
        select 
            i.id,
            i.type,
            i.prompt,
            i.options,
            i.answer,
            i.skill_id,
            coalesce(m.p_mastery, 0.3) as mastery_score,
            i.difficulty
        from items i
        join skills s on s.id = i.skill_id
        left join mastery m on m.skill_id = i.skill_id and m.student_id = :student_id
        where lower(trim(
            replace(replace(replace(replace(replace(s.subject,'á','a'),'é','e'),'í','i'),'ó','o'),'ú','u')
        )) = :subject
    """

    def get_student_context(conn, student_id: str, subject: str) -> dict:
        kpis = conn.execute(
            text(
                """
                select
                  count(*) as n,
                  avg(case when correct then 1.0 else 0.0 end) as acc
                from attempts
                where student_id=:sid
            """
            ),
            dict(sid=student_id),
        ).mappings().one()

        weak = conn.execute(
            text(
                """
                select s.name, coalesce(m.p_mastery,0.2) as p
                from skills s
                left join mastery m on m.skill_id=s.id and m.student_id=:sid
                order by p asc
                limit 3
                """
            ),
            dict(sid=student_id),
        ).mappings().all()

        return {
            "attempts": int(kpis["n"] or 0),
            "accuracy": float(kpis["acc"] or 0),
            "weak_skills": [w["name"] for w in weak],
        }

    # Calcular los bands si estamos en modo adaptativo
    bands = None
    if mode == "adaptive":
        base_band = band_from_age(current.get("age"))
        acc_recent = None
        with engine.begin() as conn:
            acc_recent = recent_accuracy_by_subject(conn, token_student_id, subj)
        current_band = adjust_band(base_band, acc_recent)
        bands = [current_band]
        if current_band == "A":
            bands.append("B")
        elif current_band == "B":
            bands.extend(["A", "C"])
        else:
            bands.append("B")

    # Obtener contexto del alumno para que la IA ajuste la dificultad
    student_ctx = {}
    already_answered_prompts: List[str] = []
    try:
        with engine.begin() as conn:
            student_ctx = get_student_context(conn, token_student_id, subj)

            # Obtener las últimas preguntas contestadas correctamente por IA para evitar repetirlas
            rows_prompts = conn.execute(
                text(
                    """
                    select prompt
                    from ai_history
                    where student_id = :sid
                      and subject = :subject
                      and correct = true
                    order by created_at desc
                    limit 20
                    """
                ),
                {"sid": token_student_id, "subject": subj},
            ).scalars().all()
            already_answered_prompts = [str(r) for r in rows_prompts if r]
    except Exception as exc:
        logger.warning("Failed to get student context: %s", exc)

    # 2) Generar ejercicios con OpenAI
    rows = generate_ai_items(
        subject=subj,
        k=k,
        age=current.get("age"),
        band=bands[0] if bands else "B",
        student_context=student_ctx,
        already_answered_prompts=already_answered_prompts,
    )

    # 3) Fallback mínimo: si OpenAI falla, usamos la consulta antigua
    custom_rows = []
    if not rows:
        if mode == "adaptive":
            sql = text(
                base_sql
                + """
                {difficulty_filter}
                order by mastery_score asc, random()
                limit :k
                """.format(
                    difficulty_filter="and (i.difficulty = ANY(:bands))" if bands else ""
                )
            )
        else:
            sql = text(
                base_sql
                + """
                order by random()
                limit :k
                """
            )

        with engine.begin() as conn:
            params = {"subject": subj, "k": k, "student_id": token_student_id}
            if bands:
                params["bands"] = bands
            try:
                rows = conn.execute(sql, params).mappings().all()
            except Exception as exc:
                logger.warning(
                    "Difficulty filter failed or missing column: %s. Falling back to random.",
                    exc,
                )
                rows = conn.execute(
                    text(
                        base_sql
                        + """
                        order by random()
                        limit :k
                        """
                    ),
                    {"subject": subj, "k": k, "student_id": token_student_id},
                ).mappings().all()

            # Fallback: si no encuentra para ese subject, trae cualquiera
            if not rows:
                fallback_sql = text(
                    """
                    select 
                        i.id,
                        i.type,
                        i.prompt,
                        i.options,
                        i.answer,
                        i.skill_id
                    from items i
                    order by random()
                    limit :k
                    """
                )
                rows = conn.execute(fallback_sql, {"k": k}).mappings().all()

            try:
                custom_rows = conn.execute(
                    RANDOM_CUSTOM_ITEMS_SQL, {"subject": subj, "k": k}
                ).mappings().all()
            except Exception as custom_exc:
                logger.error("custom_items query failed: %s", custom_exc)
    else:
        try:
            with engine.begin() as conn:
                custom_rows = conn.execute(
                    RANDOM_CUSTOM_ITEMS_SQL, {"subject": subj, "k": k}
                ).mappings().all()
        except Exception as custom_exc:
            logger.error("custom_items query failed: %s", custom_exc)

    items = []
    for r in rows:
        opts = r.get("options")
        if isinstance(opts, str):
            try:
                opts = json.loads(opts)
            except Exception:
                opts = []
        elif opts is None:
            opts = []

        skill_id = r.get("skill_id")
        skill_val = int(skill_id) if skill_id is not None else None

        items.append(
            {
                "id": int(r["id"]),
                "type": r.get("type"),
                "prompt": r.get("prompt"),
                "options": opts,
                "skill_id": skill_val,
                "answer": r.get("answer"),
                "subject": subj,
                "reason": r.get("reason") or ("refuerzo" if mode == "adaptive" else "aleatorio"),
            }
        )

    for r in custom_rows:
        opts = r.get("options")
        if isinstance(opts, str):
            try:
                opts = json.loads(opts)
            except Exception:
                opts = []
        elif opts is None:
            opts = []

        items.append(
            {
                "id": -int(r["id"]),
                "type": "custom",
                "prompt": r.get("prompt"),
                "options": opts,
                "skill_id": None,
                "answer": r.get("answer"),
                "subject": subj,
                "reason": "custom",
            }
        )

    if len(items) > k:
        random.shuffle(items)
        items = items[:k]

    if not items:
        # Como último recurso, proveemos un conjunto mínimo de ejercicios básicos
        # para evitar que la app quede sin contenido si falta DB o la IA falla.
        items = generate_default_items(subj, k)
        if items:
            logger.info(
                "/next-items falling back to default items (subject=%s, mode=%s)",
                subj,
                mode,
            )
        else:
            logger.warning("/next-items returned 0 items (subject=%s, mode=%s)", subj, mode)
            return {"items": [], "count": 0, "subject_used": subj, "error": "no_items"}

    return {"items": items, "count": len(items), "subject_used": subj}

# ---------------------------
# GET /reports/student/{sid}
# KPIs simples + habilidades más débiles
# ---------------------------
@app.get("/reports/student/{sid}")
def report_student(sid: str, request: Request):
    requester = require_user(request, allowed_roles=["student", "teacher"])
    if requester["role"] == "student" and str(requester["id"]) != sid:
        raise HTTPException(status_code=403, detail="No puedes ver reportes de otros estudiantes")

    with engine.begin() as conn:
        kpis = conn.execute(
            text("""
                select
                  count(*) as n,
                  avg(case when correct then 1.0 else 0.0 end) as acc,
                  avg(time_ms) as t_avg
                from attempts
                where student_id=:sid
            """),
            dict(sid=sid)
        ).mappings().one()

        weak = conn.execute(
            text("""
                select s.name, coalesce(m.p_mastery,0.2) as p
                from skills s
                left join mastery m on m.skill_id=s.id and m.student_id=:sid
                order by p asc
                limit 3
            """),
            dict(sid=sid)
        ).mappings().all()

        return {
            "attempts": int(kpis["n"] or 0),
            "accuracy": round(float(kpis["acc"] or 0), 2),
            "time_avg_ms": int(kpis["t_avg"] or 0),
            "weak_skills": [{"name": w["name"], "p": float(w["p"])} for w in weak],
        }


@app.get("/reports/summary")
def report_summary(request: Request, sid: str):
    requester = require_user(request, allowed_roles=["student", "teacher"])
    if requester["role"] == "student" and str(requester["id"]) != sid:
        raise HTTPException(status_code=403, detail="No puedes ver reportes de otros estudiantes")

    with engine.begin() as conn:
        kpis = conn.execute(
            text(
                """
                select
                  count(*) as n,
                  avg(case when correct then 1.0 else 0.0 end) as acc,
                  avg(time_ms) as t_avg
                from attempts
                where student_id=:sid
                """
            ),
            dict(sid=sid),
        ).mappings().one()

        weak = conn.execute(
            text(
                """
                select s.name, coalesce(m.p_mastery,0.2) as p
                from skills s
                left join mastery m on m.skill_id=s.id and m.student_id=:sid
                order by p asc
                limit 3
                """
            ),
            dict(sid=sid),
        ).mappings().all()

    attempts = int(kpis["n"] or 0)
    accuracy = round(float(kpis["acc"] or 0), 2)
    time_avg_s = round((kpis["t_avg"] or 0) / 1000, 1)
    weak_skills = [{"name": w["name"], "p": float(w["p"])} for w in weak]

    context = {
        "attempts": attempts,
        "accuracy_percent": int(accuracy * 100),
        "time_avg_s": time_avg_s,
        "weak_list": ", ".join([w["name"] for w in weak_skills]) or "ninguna",
    }

    ai_summary = generate_ai_summary(context)
    if ai_summary:
        strengths = ai_summary.get("strengths") or []
        if not strengths:
            strengths = build_strengths_fallback(accuracy, attempts, time_avg_s)
        focus = ai_summary.get("focus") or [w["name"] for w in weak_skills]
        return {
            **ai_summary,
            "attempts": attempts,
            "accuracy": accuracy,
            "time_avg_s": time_avg_s,
            "weak_skills": weak_skills,
            "strengths": strengths,
            "focus": focus,
        }

    strengths = []
    focus = []
    if accuracy >= 0.8:
        strengths.append("Muy buena precisión general, sigue así.")
    elif accuracy >= 0.6:
        strengths.append("Estás progresando; la precisión va en buen camino.")
    else:
        focus.append("Necesitas subir la precisión general con repasos cortos.")

    if time_avg_s <= 6:
        strengths.append("Respondes rápido, buen ritmo.")
    else:
        focus.append("Toma más tiempo para leer enunciados y evitar errores.")

    for w in weak_skills:
        focus.append(f"Refuerza {w['name']} (dominio {int(w['p']*100)}%).")

    overview = (
        f"Llevas {attempts} intentos, con {int(accuracy*100)}% de precisión "
        f"y un tiempo medio de {time_avg_s}s. "
        "Aquí tienes tus siguientes pasos."
    )

    return {
        "overview": overview,
        "strengths": strengths,
        "focus": focus,
        "attempts": attempts,
        "accuracy": accuracy,
        "time_avg_s": time_avg_s,
        "weak_skills": weak_skills,
    }


@app.get("/students/{sid}/achievements")
def list_achievements(sid: str, request: Request):
    user = require_user(request, allowed_roles=["student", "teacher"])
    if user["role"] == "student" and str(user["id"]) != sid:
        raise HTTPException(status_code=403, detail="No autorizado")
    with engine.begin() as conn:
        rows = conn.execute(LIST_ACHIEVEMENTS_SQL, {"sid": sid}).mappings().all()
    achievements = []
    for r in rows:
        earned_at = r.get("earned_at")
        if isinstance(earned_at, (datetime, date)):
            earned_at = earned_at.isoformat()
        achievements.append(
            {
                "code": r["code"],
                "title": r["title"],
                "description": r["description"],
                "earned_at": earned_at,
            }
        )
    return {"achievements": achievements}

@app.get("/db-check")
def db_check():
    with engine.begin() as c:
        total = c.execute(text("select count(*) from items")).scalar()
        por_materia = c.execute(text("""
            select lower(trim(s.subject)) as subj, count(*) as n
            from skills s
            join items i on i.skill_id = s.id
            group by 1 order by 1
        """)).mappings().all()
    return {
        "items_total": int(total or 0),
        "by_subject": [{ "subject": r["subj"], "count": int(r["n"]) } for r in por_materia],
    }


@app.get("/seed-check")
def seed_check():
    with engine.begin() as conn:
        rows = conn.execute(text("""
            select lower(trim(subject)) as subj, count(*) as n
            from skills s
            join items i on i.skill_id = s.id
            group by 1
            order by 1
        """)).mappings().all()
    return {"by_subject": [{ "subject": r["subj"], "count": int(r["n"]) } for r in rows]}

