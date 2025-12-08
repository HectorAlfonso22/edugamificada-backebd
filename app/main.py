# app/main.py
import json
import logging
import os
import random
import socket
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

load_dotenv()  # Carga variables de entorno antes de leer DATABASE_URL

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


def ensure_custom_tables():
    try:
        with engine.begin() as conn:
            conn.execute(CREATE_CUSTOM_ITEMS_TABLE)
            conn.execute(CREATE_CUSTOM_ITEMS_INDEX)
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


def generate_ai_summary(context: dict) -> Optional[dict]:
    if not openai_client:
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
            logger.error("AI summary JSON parse failed: %s | raw=%r", exc, raw[:500])
            return None

    def _extract_content(choice) -> str:
        """
        Normaliza el contenido devuelto por la API de OpenAI (string o lista de partes).
        """
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

    prompt = f"""
Eres un tutor educativo. Resume el progreso del alumno usando estos datos:
Intentos: {context['attempts']}
Precisión: {context['accuracy_percent']}%
Tiempo promedio por pregunta (s): {context['time_avg_s']}
Habilidades más débiles: {context['weak_list']}

Responde en JSON con las llaves: overview (string breve), strengths (lista de strings), focus (lista de strings).
Sé concreto y amable.
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
            coalesce(m.p_mastery, 0.3) as mastery_score
        from items i
        join skills s on s.id = i.skill_id
        left join mastery m on m.skill_id = i.skill_id and m.student_id = :student_id
        where lower(trim(
            replace(replace(replace(replace(replace(s.subject,'á','a'),'é','e'),'í','i'),'ó','o'),'ú','u')
        )) = :subject
    """

    if mode == "adaptive":
        sql = text(
            base_sql
            + """
            order by mastery_score asc, random()
            limit :k
            """
        )
    else:
        sql = text(
            base_sql
            + """
            order by random()
            limit :k
            """
        )

    try:
        with engine.begin() as conn:
            rows = conn.execute(sql, {"subject": subj, "k": k, "student_id": token_student_id}).mappings().all()

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

            custom_rows = []
            try:
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
                    "reason": "refuerzo" if mode == "adaptive" else "aleatorio",
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

        return {"items": items, "count": len(items), "subject_used": subj}
    except Exception as e:
        logger.error("ERROR /next-items: %s", repr(e))
        return {"items": [], "count": 0, "error": "fallback"}

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
        return {
            **ai_summary,
            "attempts": attempts,
            "accuracy": accuracy,
            "time_avg_s": time_avg_s,
            "weak_skills": weak_skills,
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

