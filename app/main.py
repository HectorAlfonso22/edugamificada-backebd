# app/main.py
import json
import logging
import os
import random
import socket
import threading
import time
import unicodedata
import uuid
from datetime import date, datetime, timedelta, timezone
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
        unit_key text,
        difficulty text,
        evaluation_mode text not null default 'exact',
        accepted_answers jsonb not null default '[]'::jsonb,
        rubric text,
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
ALTER_CUSTOM_ITEMS_ADD_UNIT_KEY = text(
    "alter table custom_items add column if not exists unit_key text"
)
ALTER_CUSTOM_ITEMS_ADD_DIFFICULTY = text(
    "alter table custom_items add column if not exists difficulty text"
)
ALTER_CUSTOM_ITEMS_ADD_EVAL_MODE = text(
    "alter table custom_items add column if not exists evaluation_mode text not null default 'exact'"
)
ALTER_CUSTOM_ITEMS_ADD_ACCEPTED = text(
    "alter table custom_items add column if not exists accepted_answers jsonb not null default '[]'::jsonb"
)
ALTER_CUSTOM_ITEMS_ADD_RUBRIC = text(
    "alter table custom_items add column if not exists rubric text"
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

CREATE_STUDENT_UNIT_PROGRESS_TABLE = text(
    """
    create table if not exists student_unit_progress (
        student_id uuid not null references users(id) on delete cascade,
        subject text not null check (subject in ('matematica','castellano')),
        unit_key text not null,
        unit_name text not null,
        total_attempts int not null default 0,
        correct_attempts int not null default 0,
        accuracy numeric not null default 0,
        p_mastery numeric not null default 0.30,
        last_answer_at timestamptz,
        updated_at timestamptz not null default now(),
        primary key (student_id, subject, unit_key)
    )
    """
)
CREATE_STUDENT_UNIT_PROGRESS_INDEX = text(
    """
    create index if not exists idx_student_unit_progress_subject
        on student_unit_progress (student_id, subject, p_mastery)
    """
)
CREATE_TEACHER_ASSIGNMENTS_TABLE = text(
    """
    create table if not exists teacher_assignments (
        id bigserial primary key,
        teacher_id uuid not null references users(id) on delete cascade,
        student_id uuid not null references users(id) on delete cascade,
        subject text not null check (subject in ('matematica','castellano')),
        unit_key text not null,
        title text not null,
        target_attempts int not null default 5,
        difficulty text,
        notes text,
        status text not null default 'active',
        completed_attempts int not null default 0,
        last_progress_at timestamptz,
        created_at timestamptz not null default now()
    )
    """
)
CREATE_TEACHER_ASSIGNMENTS_INDEX = text(
    """
    create index if not exists idx_teacher_assignments_student_subject
        on teacher_assignments (student_id, subject, status, created_at desc)
    """
)
CREATE_TEACHER_NOTES_TABLE = text(
    """
    create table if not exists teacher_notes (
        id bigserial primary key,
        teacher_id uuid not null references users(id) on delete cascade,
        student_id uuid not null references users(id) on delete cascade,
        note text not null,
        created_at timestamptz not null default now(),
        updated_at timestamptz not null default now()
    )
    """
)
CREATE_TEACHER_NOTES_INDEX = text(
    """
    create index if not exists idx_teacher_notes_student
        on teacher_notes (student_id, updated_at desc)
    """
)

CURRICULUM_UNITS = {
    "matematica": [
        {
            "key": "operaciones_naturales",
            "name": "Operaciones Naturales",
            "contents": [
                "Lectura y escritura de numeros naturales hasta 100000",
                "Valor posicional, absoluto y relativo",
                "Adicion y sustraccion con y sin reagrupacion",
                "Multiplicacion por una y dos cifras y tablas del 2 al 9",
                "Division por una y dos cifras",
                "Sistema monetario en guaranies",
            ],
        },
        {
            "key": "geometria_y_medida",
            "name": "Geometria y Medida",
            "contents": [
                "Triangulos y cuadrilateros",
                "Perimetro de figuras regulares",
                "Longitud: metro, decimetro y centimetro",
                "Masa y capacidad: 1, 1/2, 1/4 y 3/4 de kilogramo y litro",
                "Tiempo: hora, minuto y segundo",
            ],
        },
        {
            "key": "estadistica",
            "name": "Estadistica",
            "contents": [
                "Recoleccion sencilla de datos",
                "Tablas de frecuencias absolutas",
                "Graficos de barras horizontales",
            ],
        },
    ],
    "castellano": [
        {
            "key": "comprension_oral",
            "name": "Comprension de Textos Orales",
            "contents": [
                "Interpretacion de ordenes, instrucciones y dialogos",
                "Significado de palabras en contexto",
                "Personajes, acciones, lugares, temas e ideas principales",
                "Prediccion e inferencia",
                "Hechos posibles e imaginarios",
            ],
        },
        {
            "key": "expresion_oral",
            "name": "Expresion Oral",
            "contents": [
                "Saludos, presentaciones y despedidas",
                "Rimas, canciones y descripciones",
                "Opiniones fundamentadas",
                "Dramatizacion de fabulas, leyendas y cuentos",
            ],
        },
        {
            "key": "comprension_escrita",
            "name": "Comprension de Textos Escritos",
            "contents": [
                "Prediccion de contenido",
                "Secuencia de hechos e inicio, desarrollo y final",
                "Abreviaturas, simbolos y siglas",
                "Figuras literarias basicas",
                "Lectura oral con fluidez",
            ],
        },
        {
            "key": "expresion_escrita",
            "name": "Expresion Escrita",
            "contents": [
                "Esquelas, cartas familiares, historietas, avisos y narraciones breves",
                "Mayusculas, puntuacion y acentuacion",
                "Palabras agudas, llanas y esdrujulas",
                "Concordancia de genero y numero",
                "Tiempos verbales",
            ],
        },
    ],
}


def ensure_custom_tables():
    try:
        with engine.begin() as conn:
            conn.execute(CREATE_CUSTOM_ITEMS_TABLE)
            conn.execute(ALTER_CUSTOM_ITEMS_ADD_UNIT_KEY)
            conn.execute(ALTER_CUSTOM_ITEMS_ADD_DIFFICULTY)
            conn.execute(ALTER_CUSTOM_ITEMS_ADD_EVAL_MODE)
            conn.execute(ALTER_CUSTOM_ITEMS_ADD_ACCEPTED)
            conn.execute(ALTER_CUSTOM_ITEMS_ADD_RUBRIC)
            conn.execute(CREATE_CUSTOM_ITEMS_INDEX)
            conn.execute(CREATE_ACHIEVEMENTS_TABLE)
            conn.execute(CREATE_AI_HISTORY_TABLE)
            conn.execute(CREATE_STUDENT_UNIT_PROGRESS_TABLE)
            conn.execute(CREATE_STUDENT_UNIT_PROGRESS_INDEX)
            conn.execute(CREATE_TEACHER_ASSIGNMENTS_TABLE)
            conn.execute(CREATE_TEACHER_ASSIGNMENTS_INDEX)
            conn.execute(CREATE_TEACHER_NOTES_TABLE)
            conn.execute(CREATE_TEACHER_NOTES_INDEX)
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
    unit_key: Optional[str] = None
    difficulty: Optional[str] = None
    evaluation_mode: Optional[str] = "exact"
    accepted_answers: List[str] = []
    rubric: Optional[str] = None


class TeacherAssignmentPayload(BaseModel):
    student_id: str
    subject: SubjectLiteral
    unit_key: str
    title: str
    target_attempts: int = 5
    difficulty: Optional[str] = None
    notes: Optional[str] = None


class TeacherNotePayload(BaseModel):
    student_id: str
    note: str


class TeacherQuestionSuggestPayload(BaseModel):
    subject: SubjectLiteral
    unit_key: str
    difficulty: Optional[str] = None
    objective: Optional[str] = None
    count: int = 3


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


def get_curriculum_units(subject: str) -> List[dict]:
    return CURRICULUM_UNITS.get(subject, [])


def curriculum_units_prompt(subject: str) -> str:
    units = get_curriculum_units(subject)
    lines: List[str] = []
    for unit in units:
        contents = ", ".join(unit.get("contents") or [])
        lines.append(f"- {unit['key']}: {unit['name']} ({contents})")
    return "\n".join(lines)


def valid_unit_keys(subject: str) -> set[str]:
    return {str(unit["key"]) for unit in get_curriculum_units(subject)}


def find_unit_name(subject: str, unit_key: Optional[str]) -> Optional[str]:
    if not unit_key:
        return None
    for unit in get_curriculum_units(subject):
        if unit["key"] == unit_key:
            return str(unit["name"])
    return None


def normalize_text_value(value: Optional[str]) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    cleaned: List[str] = []
    prev_space = False
    for ch in text:
        if ch.isalnum():
            cleaned.append(ch)
            prev_space = False
        else:
            if not prev_space:
                cleaned.append(" ")
                prev_space = True
    return " ".join("".join(cleaned).split())


def parse_string_list(value) -> List[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v or "").strip()]
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v or "").strip()]
        except Exception:
            return [raw]
    return []


def resolve_unit_name(subject: str, unit_key: Optional[str]) -> str:
    return find_unit_name(subject, unit_key) or (unit_key or "Sin unidad")


def mastery_label(value: float) -> str:
    if value >= 0.8:
        return "domina"
    if value >= 0.55:
        return "en progreso"
    return "reforzar"


def update_student_unit_progress(
    conn,
    student_id: str,
    subject: str,
    unit_key: Optional[str],
    is_correct: bool,
    last_answer_at,
):
    if not unit_key:
        return None

    unit_name = find_unit_name(subject, unit_key)
    if not unit_name:
        return None

    row = conn.execute(
        text(
            """
            select total_attempts, correct_attempts, accuracy, p_mastery
            from student_unit_progress
            where student_id = :sid and subject = :subject and unit_key = :unit_key
            """
        ),
        {"sid": student_id, "subject": subject, "unit_key": unit_key},
    ).mappings().first()

    base_total = int((row or {}).get("total_attempts") or 0)
    base_correct = int((row or {}).get("correct_attempts") or 0)
    base_mastery = float((row or {}).get("p_mastery") or 0.30)
    base_accuracy = float((row or {}).get("accuracy") or 0.0)

    total_attempts = base_total + 1
    correct_attempts = base_correct + (1 if is_correct else 0)
    accuracy = correct_attempts / total_attempts if total_attempts else 0.0
    p_mastery = bkt_update(base_mastery, is_correct)

    conn.execute(
        text(
            """
            insert into student_unit_progress (
                student_id,
                subject,
                unit_key,
                unit_name,
                total_attempts,
                correct_attempts,
                accuracy,
                p_mastery,
                last_answer_at,
                updated_at
            )
            values (
                :sid,
                :subject,
                :unit_key,
                :unit_name,
                :total_attempts,
                :correct_attempts,
                :accuracy,
                :p_mastery,
                :last_answer_at,
                now()
            )
            on conflict (student_id, subject, unit_key) do update
            set
                unit_name = excluded.unit_name,
                total_attempts = excluded.total_attempts,
                correct_attempts = excluded.correct_attempts,
                accuracy = excluded.accuracy,
                p_mastery = excluded.p_mastery,
                last_answer_at = excluded.last_answer_at,
                updated_at = now()
            """
        ),
        {
            "sid": student_id,
            "subject": subject,
            "unit_key": unit_key,
            "unit_name": unit_name,
            "total_attempts": total_attempts,
            "correct_attempts": correct_attempts,
            "accuracy": accuracy,
            "p_mastery": p_mastery,
            "last_answer_at": last_answer_at,
        },
    )
    return {
        "subject": subject,
        "unit_key": unit_key,
        "unit_name": unit_name,
        "previous_total_attempts": base_total,
        "previous_correct_attempts": base_correct,
        "previous_accuracy": base_accuracy,
        "previous_p_mastery": base_mastery,
        "total_attempts": total_attempts,
        "correct_attempts": correct_attempts,
        "accuracy": accuracy,
        "p_mastery": p_mastery,
        "status": mastery_label(p_mastery),
        "last_answer_at": last_answer_at,
    }


def serialize_unit_progress_rows(rows) -> List[dict]:
    items = []
    for r in rows:
        updated_at = r.get("updated_at")
        last_answer_at = r.get("last_answer_at")
        items.append(
            {
                "subject": r["subject"],
                "unit_key": r["unit_key"],
                "unit_name": r["unit_name"],
                "total_attempts": int(r["total_attempts"] or 0),
                "correct_attempts": int(r["correct_attempts"] or 0),
                "accuracy": float(r["accuracy"] or 0.0),
                "p_mastery": float(r["p_mastery"] or 0.0),
                "status": mastery_label(float(r["p_mastery"] or 0.0)),
                "updated_at": updated_at.isoformat() if isinstance(updated_at, (datetime, date)) else updated_at,
                "last_answer_at": last_answer_at.isoformat() if isinstance(last_answer_at, (datetime, date)) else last_answer_at,
            }
        )
    return items


def build_student_missions(conn, student_id: str, subject: str) -> List[dict]:
    rows = conn.execute(
        text(
            """
            select subject, unit_key, unit_name, total_attempts, correct_attempts, accuracy, p_mastery, updated_at, last_answer_at
            from student_unit_progress
            where student_id = :sid and subject = :subject
            order by p_mastery asc, total_attempts asc, updated_at asc nulls first
            """
        ),
        {"sid": student_id, "subject": subject},
    ).mappings().all()

    known_units = {r["unit_key"] for r in rows}
    subject_units = get_curriculum_units(subject)
    for unit in subject_units:
        if unit["key"] not in known_units:
            rows.append(
                {
                    "subject": subject,
                    "unit_key": unit["key"],
                    "unit_name": unit["name"],
                    "total_attempts": 0,
                    "correct_attempts": 0,
                    "accuracy": 0.0,
                    "p_mastery": 0.30,
                    "updated_at": None,
                    "last_answer_at": None,
                }
            )

    rows = sorted(rows, key=lambda r: (float(r.get("p_mastery") or 0.0), int(r.get("total_attempts") or 0)))
    weakest = rows[0] if rows else None
    strongest = max(rows, key=lambda r: float(r.get("p_mastery") or 0.0)) if rows else None
    missions = []

    if weakest:
        attempts_now = int(weakest.get("total_attempts") or 0)
        practice_target = max(5, attempts_now if attempts_now else 5)
        missions.append(
            {
                "code": f"practice_{subject}_{weakest['unit_key']}",
                "title": f"Practica {weakest['unit_name']}",
                "description": f"Completa {practice_target} intentos acumulados en esta unidad para ganar más base.",
                "unit_key": weakest["unit_key"],
                "unit_name": weakest["unit_name"],
                "subject": subject,
                "progress": min(attempts_now, practice_target),
                "target": practice_target,
                "metric": "attempts",
                "status": "completed" if attempts_now >= practice_target else "active",
            }
        )
        mastery_now = float(weakest.get("p_mastery") or 0.0)
        missions.append(
            {
                "code": f"mastery_{subject}_{weakest['unit_key']}",
                "title": f"Sube el dominio en {weakest['unit_name']}",
                "description": "Lleva esta unidad al nivel 'en progreso' para consolidar la base.",
                "unit_key": weakest["unit_key"],
                "unit_name": weakest["unit_name"],
                "subject": subject,
                "progress": min(mastery_now, 0.55),
                "target": 0.55,
                "metric": "mastery",
                "status": "completed" if mastery_now >= 0.55 else "active",
            }
        )

    if strongest:
        mastery_now = float(strongest.get("p_mastery") or 0.0)
        missions.append(
            {
                "code": f"dominate_{subject}_{strongest['unit_key']}",
                "title": f"Domina {strongest['unit_name']}",
                "description": "Lleva esta unidad a nivel de dominio alto.",
                "unit_key": strongest["unit_key"],
                "unit_name": strongest["unit_name"],
                "subject": subject,
                "progress": min(mastery_now, 0.80),
                "target": 0.80,
                "metric": "mastery",
                "status": "completed" if mastery_now >= 0.80 else "active",
            }
        )

    return missions[:3]


def update_assignment_progress(
    conn,
    student_id: str,
    subject: str,
    unit_key: Optional[str],
    attempt_created_at,
):
    if not unit_key:
        return

    assignments = conn.execute(
        text(
            """
            select id, target_attempts, completed_attempts
            from teacher_assignments
            where student_id = :sid
              and subject = :subject
              and unit_key = :unit_key
              and status = 'active'
            order by created_at asc
            """
        ),
        {"sid": student_id, "subject": subject, "unit_key": unit_key},
    ).mappings().all()

    for assignment in assignments:
        new_completed = int(assignment["completed_attempts"] or 0) + 1
        new_status = "completed" if new_completed >= int(assignment["target_attempts"] or 0) else "active"
        conn.execute(
            text(
                """
                update teacher_assignments
                set completed_attempts = :completed,
                    status = :status,
                    last_progress_at = :progress_at
                where id = :id
                """
            ),
            {
                "completed": new_completed,
                "status": new_status,
                "progress_at": attempt_created_at,
                "id": assignment["id"],
            },
        )


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


def generate_ai_feedback(question: str, user_answer: str, correct_answer: str, subject: str) -> Optional[str]:
    """Genera una retroalimentación breve y motivadora cuando la respuesta es incorrecta."""
    if not openai_client:
        return None

    prompt = f"""
Eres un docente amable y motivador. El alumno respondió mal a una pregunta.

Pregunta: {question}
Respuesta del alumno: {user_answer}
Respuesta correcta: {correct_answer}
Materia: {subject}

Da una retroalimentación corta (1-2 oraciones) explicando:
- por qué la opción del alumno no es correcta
- cómo llegar a la respuesta correcta

Sé positivo, evita sonar crítico y no incluyas la palabra 'incorrecto' al inicio.

Responde solo con el texto de retroalimentación, sin encabezados ni formato extra.
"""

    models_to_try = [OPENAI_MODEL] if OPENAI_MODEL else []
    if "gpt-4o-mini" not in models_to_try:
        models_to_try.append("gpt-4o-mini")

    for mdl in models_to_try:
        try:
            resp = openai_client.chat.completions.create(
                model=mdl,
                messages=[{"role": "user", "content": prompt}],
                max_completion_tokens=120,
                timeout=20,
            )
            raw = _extract_content(resp.choices[0]).strip()
            if raw:
                return raw
        except Exception as exc:
            logger.error("AI feedback failed with model %s: %s", mdl, exc)

    return None


def judge_castellano_response_with_ai(
    question: str,
    user_answer: str,
    correct_answer: str,
    accepted_answers: Optional[List[str]] = None,
    rubric: Optional[str] = None,
) -> Optional[dict]:
    if not openai_client:
        return None

    accepted_text = ", ".join([a for a in (accepted_answers or []) if a]) or "ninguna"
    prompt = f"""
Evalua si la respuesta de un alumno de primaria en Castellano debe considerarse valida.

Pregunta: {question}
Respuesta del alumno: {user_answer}
Respuesta ejemplo: {correct_answer}
Otras respuestas aceptables: {accepted_text}
Criterio adicional: {rubric or 'ninguno'}

Reglas:
- Acepta sinonimos, variantes correctas y respuestas semanticamente validas.
- No aceptes respuestas sin sentido, con categoria gramatical incorrecta o que no respondan a la consigna.
- Si la respuesta es valida aunque no coincida exactamente con la respuesta ejemplo, marca true.
- Se estricto pero razonable para tercer grado.

Devuelve SOLO JSON con:
- is_correct: boolean
- reason: explicacion breve
"""

    models_to_try = [OPENAI_MODEL] if OPENAI_MODEL else []
    if "gpt-4o-mini" not in models_to_try:
        models_to_try.append("gpt-4o-mini")

    for mdl in models_to_try:
        try:
            resp = openai_client.chat.completions.create(
                model=mdl,
                messages=[{"role": "user", "content": prompt}],
                max_completion_tokens=140,
                response_format={"type": "json_object"},
                timeout=20,
            )
            parsed = _parse_json_safely(_extract_content(resp.choices[0]))
            if isinstance(parsed, dict) and isinstance(parsed.get("is_correct"), bool):
                return {
                    "is_correct": bool(parsed["is_correct"]),
                    "reason": str(parsed.get("reason") or "").strip(),
                }
        except Exception as exc:
            logger.error("AI castellano judge failed with model %s: %s", mdl, exc)

    return None


def evaluate_student_response(
    subject: str,
    prompt: str,
    response: Optional[str],
    correct_answer: Optional[str],
    options: Optional[List[str]] = None,
    evaluation_mode: Optional[str] = None,
    accepted_answers: Optional[List[str]] = None,
    rubric: Optional[str] = None,
) -> dict:
    response_text = str(response or "").strip()
    correct_text = str(correct_answer or "").strip()
    response_norm = normalize_text_value(response_text)
    correct_norm = normalize_text_value(correct_text)

    if not response_norm:
        return {"is_correct": False, "reason": "Respuesta vacia."}

    if response_norm and correct_norm:
        response_num = response_norm.replace(" ", "")
        correct_num = correct_norm.replace(" ", "")
        if response_num.isdigit() and correct_num.isdigit():
            return {"is_correct": response_num == correct_num, "reason": ""}

    option_values = [normalize_text_value(opt) for opt in (options or []) if str(opt or "").strip()]
    accepted = accepted_answers or []
    accepted_norm = {
        normalize_text_value(val)
        for val in ([correct_text] + accepted)
        if normalize_text_value(val)
    }
    mode = (evaluation_mode or "").strip().lower()
    if not mode:
        mode = "exact" if option_values else ("accepted_answers" if subject == "castellano" else "exact")

    if response_norm == correct_norm:
        return {"is_correct": True, "reason": ""}
    if response_norm in accepted_norm:
        return {"is_correct": True, "reason": ""}

    if option_values:
        return {"is_correct": False, "reason": ""}

    if mode == "exact":
        return {"is_correct": False, "reason": ""}

    if mode in {"accepted_answers", "ai_judge"} and subject == "castellano":
        judged = judge_castellano_response_with_ai(
            question=prompt,
            user_answer=response_text,
            correct_answer=correct_text,
            accepted_answers=accepted,
            rubric=rubric,
        )
        if judged:
            return judged

    return {"is_correct": False, "reason": ""}


def generate_teacher_question_suggestions(
    subject: str,
    unit_key: str,
    difficulty: Optional[str],
    objective: Optional[str],
    count: int,
) -> List[dict]:
    if not openai_client:
        return []

    unit_name = resolve_unit_name(subject, unit_key)
    prompt = f"""
Eres un asistente para docentes de primaria.
Genera exactamente un JSON con una lista "items" para que el profesor pueda revisar y guardar.

Materia: {subject}
Unidad: {unit_name} ({unit_key})
Dificultad sugerida: {difficulty or 'media'}
Objetivo pedagógico: {objective or 'reforzar contenidos clave del tercer grado'}
Cantidad: {count}

Cada item debe tener:
- prompt
- options
- answer
- evaluation_mode
- accepted_answers
- rubric

Reglas:
- Si es opción múltiple usa evaluation_mode="exact".
- Si es respuesta abierta de Castellano con varias respuestas válidas, usa evaluation_mode="accepted_answers" o "ai_judge" cuando haga falta criterio.
- Mantén el nivel adecuado para tercer grado.

Devuelve solo JSON.
"""
    try:
        resp = openai_client.chat.completions.create(
            model=OPENAI_MODEL or "gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=500,
            response_format={"type": "json_object"},
            timeout=30,
        )
        parsed = _parse_json_safely(_extract_content(resp.choices[0]))
        items = parsed.get("items") if isinstance(parsed, dict) else []
        if not isinstance(items, list):
            return []
        results = []
        for item in items[:count]:
            if not isinstance(item, dict):
                continue
            results.append(
                {
                    "prompt": str(item.get("prompt") or "").strip(),
                    "options": parse_string_list(item.get("options")),
                    "answer": str(item.get("answer") or "").strip(),
                    "evaluation_mode": str(item.get("evaluation_mode") or "exact").strip().lower(),
                    "accepted_answers": parse_string_list(item.get("accepted_answers")),
                    "rubric": str(item.get("rubric") or "").strip(),
                    "subject": subject,
                    "unit_key": unit_key,
                    "unit_name": unit_name,
                    "difficulty": difficulty or "",
                }
            )
        return [r for r in results if r["prompt"] and r["answer"]]
    except Exception as exc:
        logger.warning("teacher suggestion generation failed: %s", exc)
        return []


def _cache_key_for_items(
    subject: str,
    k: int,
    age: Optional[int],
    band: str,
    weak_skills: Optional[List[str]],
    weak_units: Optional[List[str]] = None,
) -> str:
    weak_norm = ",".join(sorted([w.strip().lower() for w in (weak_skills or []) if w]))
    units_norm = ",".join(sorted([u.strip().lower() for u in (weak_units or []) if u]))
    return f"{subject}|{k}|{age or 'unk'}|{band}|{weak_norm}|{units_norm}"


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
    target_unit_key: Optional[str] = None,
    target_difficulty: Optional[str] = None,
) -> List[dict]:
    """Genera k ejercicios usando OpenAI y devuelve items listos para el frontend."""
    if not openai_client:
        logger.warning("OpenAI client not configured - skipping AI item generation")
        return []

    weak_skills = (student_context or {}).get("weak_skills") or []
    weak_units = [
        str(u.get("unit_key"))
        for u in ((student_context or {}).get("weak_units") or [])
        if isinstance(u, dict) and u.get("unit_key")
    ]
    cache_key = _cache_key_for_items(
        subject,
        k,
        age,
        band,
        weak_skills,
        weak_units + ([target_unit_key] if target_unit_key else []),
    ) + f"|{target_difficulty or ''}"
    cached = _get_cached_items(cache_key)
    if cached:
        logger.info("Returning cached AI items for key=%s", cache_key)
        return cached

    ctx_lines = []
    if student_context:
        acc = student_context.get("accuracy")
        attempts = student_context.get("attempts")
        weak = student_context.get("weak_skills") or []
        weak_units_ctx = student_context.get("weak_units") or []
        strong_units_ctx = student_context.get("strong_units") or []
        ctx_lines.append(f"Precision actual: {acc * 100:.0f}%" if acc is not None else "")
        ctx_lines.append(f"Intentos totales: {attempts}" if attempts is not None else "")
        if weak:
            ctx_lines.append(f"Habilidades débiles: {', '.join(weak)}")
        if weak_units_ctx:
            ctx_lines.append(
                "Unidades a fortalecer: "
                + ", ".join(
                    [
                        f"{u.get('unit_name')} ({int(float(u.get('accuracy') or 0) * 100)}%)"
                        for u in weak_units_ctx
                        if u.get("unit_name")
                    ]
                )
            )
        if strong_units_ctx:
            ctx_lines.append(
                "Unidades dominadas: "
                + ", ".join(
                    [
                        f"{u.get('unit_name')} ({int(float(u.get('accuracy') or 0) * 100)}%)"
                        for u in strong_units_ctx
                        if u.get("unit_name")
                    ]
                )
            )

    curriculum_prompt = curriculum_units_prompt(subject)
    valid_keys = sorted(valid_unit_keys(subject))
    valid_keys_text = ", ".join(valid_keys)
    target_unit_name = resolve_unit_name(subject, target_unit_key) if target_unit_key else None

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
  - unit_key: una de las claves curriculares permitidas para la materia
  - evaluation_mode: "exact", "accepted_answers" o "ai_judge"
  - accepted_answers: lista opcional de otras respuestas validas (usa [] si no aplica)
  - rubric: criterio breve para evaluar la respuesta si no basta una sola solucion

Contexto del estudiante:
- Edad: {age or 'desconocida'}
- Materia: {subject}
- Banda de dificultad sugerida: {band}
- Unidad prioritaria: {target_unit_name or 'ninguna'}
- Dificultad prioritaria: {target_difficulty or 'ninguna'}
{''.join([f'- {l}\n' for l in ctx_lines if l])}
{extra_instructions}

Unidades curriculares permitidas para {subject}:
{curriculum_prompt}

IMPORTANTE:
- Si la materia es "castellano", SOLO genera ejercicios de lengua (gramática, ortografía, lectura, vocabulario, comprensión). NO incluyas operaciones numéricas ni problemas matemáticos.
- Si la materia es "matematica", SOLO genera ejercicios de matemáticas (aritmética, lógica, problemas numéricos). NO incluyas preguntas de idioma.
- Para cada ejercicio, asigna el unit_key correcto según la unidad curricular trabajada.
- unit_key debe ser exactamente uno de estos valores: {valid_keys_text}
- Si se indica una unidad prioritaria, concentra la mayoría de los ejercicios en esa unidad.
- Si la materia es "matematica" y NO hay unidad prioritaria, distribuye el lote entre distintas unidades curriculares y evita concentrar todo en "operaciones_naturales".
- Si la materia es "matematica" y generas 3 o más ejercicios sin unidad prioritaria, incluye al menos 2 unit_key distintos.
- Si se indica una dificultad prioritaria, respétala al redactar los ejercicios.
- Si la pregunta tiene una unica respuesta objetiva o es de opcion multiple, usa evaluation_mode="exact".
- Si la pregunta de Castellano admite varias palabras correctas razonables, usa evaluation_mode="accepted_answers" y llena accepted_answers con 3 a 6 variantes validas.
- Si la pregunta de Castellano es abierta y requiere criterio semantico o gramatical mas amplio, usa evaluation_mode="ai_judge" y explica el criterio en rubric.
- No uses "ai_judge" para matematica.

Haz {k} ejercicios.
- Incluye al menos 1 ejercicio de opción múltiple y al menos 1 ejercicio de respuesta libre.
- Prioriza fortalecer las habilidades débiles y las unidades a reforzar listadas, y ajusta la dificultad si el estudiante ya tiene buena precisión.

Devuelve SOLO el JSON, sin texto adicional.
Ejemplo de salida:
{{"items":[{{"prompt":"...","options":["...","..."],"answer":"...","unit_key":"{valid_keys[0] if valid_keys else 'unidad'}","evaluation_mode":"exact","accepted_answers":[],"rubric":""}},{{"prompt":"...","options":[],"answer":"...","unit_key":"{valid_keys[0] if valid_keys else 'unidad'}","evaluation_mode":"accepted_answers","accepted_answers":["...","..."],"rubric":"acepta adjetivos validos y coherentes con la oracion"}}]}}
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
                unit_key = str(it.get("unit_key") or "").strip().lower()
                evaluation_mode = str(it.get("evaluation_mode") or "").strip().lower()
                accepted_answers = parse_string_list(it.get("accepted_answers"))
                rubric = str(it.get("rubric") or "").strip()
                opts = it.get("options") or []
                if isinstance(opts, str):
                    try:
                        opts = json.loads(opts)
                    except Exception:
                        opts = []
                if not isinstance(opts, list):
                    opts = []
                if unit_key not in valid_unit_keys(subject):
                    unit_key = valid_keys[0] if valid_keys else ""
                unit_name = find_unit_name(subject, unit_key)
                if evaluation_mode not in {"exact", "accepted_answers", "ai_judge"}:
                    evaluation_mode = "exact" if opts else ("accepted_answers" if subject == "castellano" else "exact")
                if subject != "castellano" and evaluation_mode == "ai_judge":
                    evaluation_mode = "exact"

                results.append(
                    {
                        "id": base_id - idx,
                        "type": "ai",
                        "prompt": prompt_text,
                        "options": [str(o) for o in opts],
                        "answer": answer,
                        "subject": subject,
                        "unit_key": unit_key or None,
                        "unit_name": unit_name,
                        "evaluation_mode": evaluation_mode,
                        "accepted_answers": accepted_answers,
                        "rubric": rubric,
                        "reason": "ia",
                    }
                )

            if (
                subject == "matematica"
                and not target_unit_key
                and k >= 3
                and len({str(item.get("unit_key") or "") for item in results if item.get("unit_key")}) < 2
            ):
                logger.warning("AI items lacked unit diversity for matematica; retrying generation")
                continue

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
                "unit_key": "operaciones_naturales",
                "unit_name": "Operaciones Naturales",
                "evaluation_mode": "exact",
                "accepted_answers": [],
                "rubric": "",
                "reason": "fallback",
            },
            {
                "id": base_id - 1,
                "type": "fallback",
                "prompt": "Escribe el resultado de 7 + 4:",
                "options": [],
                "answer": "11",
                "subject": subject,
                "unit_key": "operaciones_naturales",
                "unit_name": "Operaciones Naturales",
                "evaluation_mode": "exact",
                "accepted_answers": [],
                "rubric": "",
                "reason": "fallback",
            },
            {
                "id": base_id - 2,
                "type": "fallback",
                "prompt": "¿Cuál figura tiene 4 lados?",
                "options": ["Triángulo", "Cuadrado", "Círculo", "Óvalo"],
                "answer": "Cuadrado",
                "subject": subject,
                "unit_key": "geometria_y_medida",
                "unit_name": "Geometria y Medida",
                "evaluation_mode": "exact",
                "accepted_answers": [],
                "rubric": "",
                "reason": "fallback",
            },
            {
                "id": base_id - 3,
                "type": "fallback",
                "prompt": "En una tabla de datos, ¿qué usamos para organizar cuántas veces aparece cada valor?",
                "options": ["Frecuencia", "Perímetro", "Multiplicación", "Resta"],
                "answer": "Frecuencia",
                "subject": subject,
                "unit_key": "estadistica",
                "unit_name": "Estadistica",
                "evaluation_mode": "exact",
                "accepted_answers": [],
                "rubric": "",
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
                "evaluation_mode": "exact",
                "accepted_answers": [],
                "rubric": "",
                "reason": "fallback",
            },
            {
                "id": base_id - 1,
                "type": "fallback",
                "prompt": "Escribe la palabra que falta: 'El gato es ____.'",
                "options": [],
                "answer": "negro",
                "subject": subject,
                "evaluation_mode": "accepted_answers",
                "accepted_answers": ["negra", "bonito", "bonita", "feliz", "pequeno", "pequeño"],
                "rubric": "Acepta adjetivos validos y coherentes para describir al gato.",
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
    norm_options = [str(o).strip() for o in payload.options if str(o).strip()]
    accepted_answers = [str(v).strip() for v in payload.accepted_answers if str(v).strip()]
    if norm_options and len(norm_options) < 2:
        raise HTTPException(status_code=400, detail="Debes proveer al menos dos opciones")
    if not payload.prompt.strip() or not payload.answer.strip():
        raise HTTPException(status_code=400, detail="Completa enunciado y respuesta")
    if payload.unit_key and payload.unit_key not in valid_unit_keys(payload.subject):
        raise HTTPException(status_code=400, detail="Unidad inválida")
    evaluation_mode = (payload.evaluation_mode or "exact").strip().lower()
    if evaluation_mode not in {"exact", "accepted_answers", "ai_judge"}:
        raise HTTPException(status_code=400, detail="Modo de evaluación inválido")
    if payload.subject != "castellano" and evaluation_mode == "ai_judge":
        raise HTTPException(status_code=400, detail="ai_judge solo aplica a Castellano")

    with engine.begin() as conn:
        row = conn.execute(
            INSERT_CUSTOM_ITEM_SQL,
            {
                "subject": payload.subject,
                "prompt": payload.prompt.strip(),
                "options": json.dumps(norm_options, ensure_ascii=False),
                "answer": payload.answer.strip(),
                "unit_key": payload.unit_key,
                "difficulty": payload.difficulty,
                "evaluation_mode": evaluation_mode,
                "accepted_answers": json.dumps(accepted_answers, ensure_ascii=False),
                "rubric": (payload.rubric or "").strip() or None,
                "created_by": str(teacher["id"]),
            },
        ).mappings().first()

    return {"question": {**row, "options": norm_options, "accepted_answers": accepted_answers}}


@app.put("/teacher/questions/{question_id}")
def update_custom_question(question_id: int, payload: CustomQuestionPayload, request: Request):
    if question_id <= 0:
        raise HTTPException(status_code=400, detail="ID inválido")

    teacher = require_user(request, allowed_roles=["teacher"])
    norm_options = [str(o).strip() for o in payload.options if str(o).strip()]
    accepted_answers = [str(v).strip() for v in payload.accepted_answers if str(v).strip()]
    if norm_options and len(norm_options) < 2:
        raise HTTPException(status_code=400, detail="Debes proveer al menos dos opciones")

    with engine.begin() as conn:
        existing = conn.execute(
            SELECT_CUSTOM_ITEM_SQL, {"id": question_id}
        ).mappings().first()
        if not existing or str(existing["created_by"]) != str(teacher["id"]):
            raise HTTPException(status_code=404, detail="Pregunta no encontrada")

        row = conn.execute(
            UPDATE_CUSTOM_ITEM_SQL,
            {
                "id": question_id,
                "teacher_id": str(teacher["id"]),
                "subject": payload.subject,
                "prompt": payload.prompt.strip(),
                "options": json.dumps(norm_options, ensure_ascii=False),
                "answer": payload.answer.strip(),
                "unit_key": payload.unit_key,
                "difficulty": payload.difficulty,
                "evaluation_mode": (payload.evaluation_mode or "exact").strip().lower(),
                "accepted_answers": json.dumps(accepted_answers, ensure_ascii=False),
                "rubric": (payload.rubric or "").strip() or None,
            },
        ).mappings().first()

    if not row:
        raise HTTPException(status_code=400, detail="No se pudo actualizar la pregunta")

    return {
        "question": {
            **row,
            "options": parse_string_list(row.get("options")),
            "accepted_answers": parse_string_list(row.get("accepted_answers")),
        }
    }


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
                "unit_key": r.get("unit_key"),
                "difficulty": r.get("difficulty"),
                "evaluation_mode": r.get("evaluation_mode"),
                "accepted_answers": parse_string_list(r.get("accepted_answers")),
                "rubric": r.get("rubric"),
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


@app.get("/teacher/students/{sid}/unit-progress")
def teacher_student_unit_progress(sid: str, request: Request):
    require_user(request, allowed_roles=["teacher"])
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                select subject, unit_key, unit_name, total_attempts, correct_attempts, accuracy, p_mastery, updated_at
                from student_unit_progress
                where student_id = :sid
                order by subject, unit_name
                """
            ),
            {"sid": sid},
        ).mappings().all()
    return {"units": serialize_unit_progress_rows(rows)}


@app.get("/students/{sid}/unit-progress")
def student_unit_progress(sid: str, request: Request, subject: Optional[str] = None):
    requester = require_user(request, allowed_roles=["student", "teacher"])
    if requester["role"] == "student" and str(requester["id"]) != sid:
        raise HTTPException(status_code=403, detail="No autorizado")

    sql = """
        select subject, unit_key, unit_name, total_attempts, correct_attempts, accuracy, p_mastery, updated_at, last_answer_at
        from student_unit_progress
        where student_id = :sid
    """
    params = {"sid": sid}
    if subject:
        sql += " and subject = :subject"
        params["subject"] = subject.strip().lower()
    sql += " order by subject, p_mastery asc, updated_at desc nulls last"

    with engine.begin() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    return {"units": serialize_unit_progress_rows(rows)}


@app.get("/students/{sid}/missions")
def student_missions(sid: str, request: Request, subject: str = Query(...)):
    requester = require_user(request, allowed_roles=["student", "teacher"])
    if requester["role"] == "student" and str(requester["id"]) != sid:
        raise HTTPException(status_code=403, detail="No autorizado")
    with engine.begin() as conn:
        missions = build_student_missions(conn, sid, subject.strip().lower())
    return {"missions": missions}


@app.get("/teacher/students/{sid}/errors")
def teacher_student_errors(sid: str, request: Request, limit: int = Query(10, ge=1, le=50)):
    require_user(request, allowed_roles=["teacher"])
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                select
                  a.created_at,
                  a.answer_text,
                  a.item_id,
                  coalesce(i.subject, c.subject, h.subject) as subject,
                  coalesce(i.prompt, c.prompt, h.prompt) as prompt
                from attempts a
                left join items i
                  on a.item_id > 0 and i.id = a.item_id
                left join custom_items c
                  on a.item_id < 0 and c.id = abs(a.item_id)
                left join lateral (
                    select ah.subject, ah.prompt
                    from ai_history ah
                    where ah.student_id = a.student_id
                      and ah.correct = false
                      and abs(extract(epoch from (ah.created_at - a.created_at))) <= 120
                    order by abs(extract(epoch from (ah.created_at - a.created_at))) asc
                    limit 1
                ) h on a.item_id < 0
                where a.student_id = :sid
                  and a.correct = false
                order by a.created_at desc
                limit :limit
                """
            ),
            {"sid": sid, "limit": limit},
        ).mappings().all()
    return {
        "errors": [
            {
                "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
                "answer_text": r.get("answer_text"),
                "item_id": int(r["item_id"] or 0),
                "subject": r.get("subject"),
                "prompt": r.get("prompt"),
            }
            for r in rows
        ]
    }


@app.get("/teacher/notes")
def list_teacher_notes(request: Request, student_id: Optional[str] = None):
    teacher = require_user(request, allowed_roles=["teacher"])
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                select n.id, n.student_id, n.note, n.created_at, n.updated_at, u.display_name, u.email
                from teacher_notes n
                join users u on u.id = n.student_id
                where n.teacher_id = :teacher_id
                  and (:student_id is null or n.student_id = cast(:student_id as uuid))
                order by n.updated_at desc
                """
            ),
            {"teacher_id": str(teacher["id"]), "student_id": student_id},
        ).mappings().all()
    return {
        "notes": [
            {
                "id": int(r["id"]),
                "student_id": str(r["student_id"]),
                "student_name": r.get("display_name") or r.get("email"),
                "note": r["note"],
                "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
                "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else None,
            }
            for r in rows
        ]
    }


@app.post("/teacher/notes")
def create_teacher_note(payload: TeacherNotePayload, request: Request):
    teacher = require_user(request, allowed_roles=["teacher"])
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                insert into teacher_notes (teacher_id, student_id, note)
                values (:teacher_id, cast(:student_id as uuid), :note)
                returning id, student_id, note, created_at, updated_at
                """
            ),
            {
                "teacher_id": str(teacher["id"]),
                "student_id": payload.student_id,
                "note": payload.note.strip(),
            },
        ).mappings().first()
    return {
        "note": {
            "id": int(row["id"]),
            "student_id": str(row["student_id"]),
            "note": row["note"],
            "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
            "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
        }
    }


@app.get("/teacher/assignments")
def list_teacher_assignments(request: Request, student_id: Optional[str] = None):
    teacher = require_user(request, allowed_roles=["teacher"])
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                select a.*, u.display_name, u.email
                from teacher_assignments a
                join users u on u.id = a.student_id
                where a.teacher_id = :teacher_id
                  and (:student_id is null or a.student_id = cast(:student_id as uuid))
                order by a.created_at desc
                """
            ),
            {"teacher_id": str(teacher["id"]), "student_id": student_id},
        ).mappings().all()
    return {
        "assignments": [
            {
                "id": int(r["id"]),
                "student_id": str(r["student_id"]),
                "student_name": r.get("display_name") or r.get("email"),
                "subject": r["subject"],
                "unit_key": r["unit_key"],
                "unit_name": resolve_unit_name(r["subject"], r["unit_key"]),
                "title": r["title"],
                "target_attempts": int(r["target_attempts"] or 0),
                "completed_attempts": int(r["completed_attempts"] or 0),
                "difficulty": r.get("difficulty"),
                "notes": r.get("notes"),
                "status": r["status"],
                "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
                "last_progress_at": r["last_progress_at"].isoformat() if r.get("last_progress_at") else None,
            }
            for r in rows
        ]
    }


@app.post("/teacher/assignments")
def create_teacher_assignment(payload: TeacherAssignmentPayload, request: Request):
    teacher = require_user(request, allowed_roles=["teacher"])
    if payload.unit_key not in valid_unit_keys(payload.subject):
        raise HTTPException(status_code=400, detail="Unidad inválida")
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                insert into teacher_assignments (
                    teacher_id, student_id, subject, unit_key, title, target_attempts, difficulty, notes
                )
                values (
                    :teacher_id, cast(:student_id as uuid), :subject, :unit_key, :title, :target_attempts, :difficulty, :notes
                )
                returning id, student_id, subject, unit_key, title, target_attempts, completed_attempts, difficulty, notes, status, created_at
                """
            ),
            {
                "teacher_id": str(teacher["id"]),
                "student_id": payload.student_id,
                "subject": payload.subject,
                "unit_key": payload.unit_key,
                "title": payload.title.strip(),
                "target_attempts": max(1, int(payload.target_attempts or 1)),
                "difficulty": payload.difficulty,
                "notes": (payload.notes or "").strip() or None,
            },
        ).mappings().first()
    return {
        "assignment": {
            "id": int(row["id"]),
            "student_id": str(row["student_id"]),
            "subject": row["subject"],
            "unit_key": row["unit_key"],
            "unit_name": resolve_unit_name(row["subject"], row["unit_key"]),
            "title": row["title"],
            "target_attempts": int(row["target_attempts"] or 0),
            "completed_attempts": int(row["completed_attempts"] or 0),
            "difficulty": row.get("difficulty"),
            "notes": row.get("notes"),
            "status": row["status"],
            "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        }
    }


@app.post("/teacher/questions/suggest")
def suggest_teacher_questions(payload: TeacherQuestionSuggestPayload, request: Request):
    require_user(request, allowed_roles=["teacher"])
    suggestions = generate_teacher_question_suggestions(
        subject=payload.subject,
        unit_key=payload.unit_key,
        difficulty=payload.difficulty,
        objective=payload.objective,
        count=max(1, min(int(payload.count or 3), 8)),
    )
    return {"suggestions": suggestions}


@app.get("/teacher/dashboard")
def teacher_dashboard(request: Request):
    require_user(request, allowed_roles=["teacher"])
    with engine.begin() as conn:
        students = conn.execute(LIST_STUDENTS_SQL).mappings().all()
        total_students = len(students)
        activity_rows = conn.execute(
            text(
                """
                select date(created_at) as d, count(*) as n
                from attempts
                where created_at >= now() - interval '7 days'
                group by 1
                order by d asc
                """
            )
        ).mappings().all()
        progress_rows = conn.execute(
            text(
                """
                select sp.student_id, u.display_name, u.email, sp.subject, sp.accuracy, sp.total_attempts, sp.last_answer_at
                from student_progress sp
                join users u on u.id = sp.student_id
                where lower(u.email) not like '%test%'
                order by sp.last_answer_at desc nulls last
                """
            )
        ).mappings().all()
        unit_rows = conn.execute(
            text(
                """
                select sup.student_id, u.display_name, u.email, sup.subject, sup.unit_key, sup.unit_name, sup.accuracy, sup.p_mastery, sup.total_attempts
                from student_unit_progress sup
                join users u on u.id = sup.student_id
                where lower(u.email) not like '%test%'
                order by sup.subject, sup.unit_name
                """
            )
        ).mappings().all()
        assignment_rows = conn.execute(
            text(
                """
                select count(*) filter (where status = 'active') as active_count,
                       count(*) filter (where status = 'completed') as completed_count
                from teacher_assignments
                """
            )
        ).mappings().first() or {"active_count": 0, "completed_count": 0}
        notes_count = conn.execute(text("select count(*) from teacher_notes")).scalar() or 0

    grouped_students: dict[str, dict] = {}
    for row in progress_rows:
        sid = str(row["student_id"])
        grouped_students.setdefault(
            sid,
            {
                "student_id": sid,
                "student_name": row.get("display_name") or row.get("email"),
                "subjects": {},
                "last_answer_at": row.get("last_answer_at"),
            },
        )
        grouped_students[sid]["subjects"][row["subject"]] = {
            "accuracy": float(row["accuracy"] or 0),
            "total_attempts": int(row["total_attempts"] or 0),
        }
        if row.get("last_answer_at") and (
            not grouped_students[sid]["last_answer_at"]
            or row["last_answer_at"] > grouped_students[sid]["last_answer_at"]
        ):
            grouped_students[sid]["last_answer_at"] = row["last_answer_at"]

    at_risk = []
    inactive = []
    now_dt = datetime.now(timezone.utc)
    for student in students:
        sid = str(student["id"])
        info = grouped_students.get(sid)
        if not info or not info.get("last_answer_at"):
            inactive.append(
                {"student_id": sid, "student_name": student.get("display_name") or student.get("email"), "reason": "Sin actividad registrada"}
            )
            continue
        last_answer = info["last_answer_at"]
        if isinstance(last_answer, datetime) and (now_dt - last_answer).days >= 5:
            inactive.append(
                {
                    "student_id": sid,
                    "student_name": info["student_name"],
                    "reason": f"Sin actividad desde hace {(now_dt - last_answer).days} días",
                }
            )
        low_subjects = [
            subj for subj, metrics in info["subjects"].items()
            if metrics["total_attempts"] >= 3 and metrics["accuracy"] < 0.55
        ]
        if low_subjects:
            at_risk.append(
                {
                    "student_id": sid,
                    "student_name": info["student_name"],
                    "subjects": low_subjects,
                }
            )

    unit_summary: dict[tuple[str, str], dict] = {}
    heatmap: List[dict] = []
    for row in unit_rows:
        key = (row["subject"], row["unit_key"])
        bucket = unit_summary.setdefault(
            key,
            {
                "subject": row["subject"],
                "unit_key": row["unit_key"],
                "unit_name": row["unit_name"],
                "students": 0,
                "accuracy_total": 0.0,
                "mastery_total": 0.0,
            },
        )
        bucket["students"] += 1
        bucket["accuracy_total"] += float(row["accuracy"] or 0)
        bucket["mastery_total"] += float(row["p_mastery"] or 0)
        heatmap.append(
            {
                "student_id": str(row["student_id"]),
                "student_name": row.get("display_name") or row.get("email"),
                "subject": row["subject"],
                "unit_key": row["unit_key"],
                "unit_name": row["unit_name"],
                "accuracy": float(row["accuracy"] or 0),
                "p_mastery": float(row["p_mastery"] or 0),
                "status": mastery_label(float(row["p_mastery"] or 0)),
            }
        )

    units_ranked = []
    for item in unit_summary.values():
        students_count = max(1, int(item["students"]))
        units_ranked.append(
            {
                "subject": item["subject"],
                "unit_key": item["unit_key"],
                "unit_name": item["unit_name"],
                "students": int(item["students"]),
                "accuracy_avg": item["accuracy_total"] / students_count,
                "mastery_avg": item["mastery_total"] / students_count,
            }
        )
    units_ranked.sort(key=lambda x: (x["mastery_avg"], x["accuracy_avg"]))

    recommendations = []
    if units_ranked:
        first = units_ranked[0]
        recommendations.append(
            f"Conviene reforzar {first['unit_name']} en {first['subject']}: dominio medio {int(first['mastery_avg']*100)}%."
        )
    if inactive:
        recommendations.append(f"Hay {len(inactive)} alumnos con baja actividad reciente.")
    if at_risk:
        recommendations.append(f"{len(at_risk)} alumnos presentan riesgo académico en al menos una materia.")

    return {
        "overview": {
            "total_students": total_students,
            "active_assignments": int(assignment_rows["active_count"] or 0),
            "completed_assignments": int(assignment_rows["completed_count"] or 0),
            "teacher_notes": int(notes_count or 0),
        },
        "activity_last_7_days": [
            {"date": str(r["d"]), "attempts": int(r["n"] or 0)}
            for r in activity_rows
        ],
        "students_overview": [
            {
                "student_id": item["student_id"],
                "student_name": item["student_name"],
                "last_answer_at": item["last_answer_at"].isoformat() if item.get("last_answer_at") else None,
                "subjects": item["subjects"],
            }
            for item in grouped_students.values()
        ],
        "alerts": {
            "at_risk": at_risk[:8],
            "inactive": inactive[:8],
        },
        "units_summary": units_ranked,
        "heatmap": heatmap,
        "recommendations": recommendations,
    }


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
    insert into custom_items (
        subject, prompt, options, answer, unit_key, difficulty,
        evaluation_mode, accepted_answers, rubric, created_by
    )
    values (
        lower(:subject), :prompt, CAST(:options AS jsonb), :answer, :unit_key, :difficulty,
        :evaluation_mode, CAST(:accepted_answers AS jsonb), :rubric, :created_by
    )
    returning id, subject, prompt, options, answer, unit_key, difficulty, evaluation_mode, accepted_answers, rubric, created_at
    """
)
UPDATE_CUSTOM_ITEM_SQL = text(
    """
    update custom_items
    set subject = lower(:subject),
        prompt = :prompt,
        options = CAST(:options AS jsonb),
        answer = :answer,
        unit_key = :unit_key,
        difficulty = :difficulty,
        evaluation_mode = :evaluation_mode,
        accepted_answers = CAST(:accepted_answers AS jsonb),
        rubric = :rubric
    where id = :id and created_by = :teacher_id
    returning id, subject, prompt, options, answer, unit_key, difficulty, evaluation_mode, accepted_answers, rubric, created_at
    """
)
SELECT_CUSTOM_ITEM_SQL = text(
    """
    select id, subject, prompt, options, answer, unit_key, difficulty, evaluation_mode, accepted_answers, rubric, created_by
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
      and lower(email) not like '%test%'
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
    select id, subject, prompt, options, answer, unit_key, difficulty, evaluation_mode, accepted_answers, rubric, created_at
    from custom_items
    where created_by = :user_id
    order by created_at desc
    """
)
RANDOM_CUSTOM_ITEMS_SQL = text(
    """
    select id, subject, prompt, options, answer, unit_key, difficulty, evaluation_mode, accepted_answers, rubric
    from custom_items
    where lower(subject) = :subject and is_active = true
      and (:unit_key is null or unit_key = :unit_key)
      and (:difficulty is null or difficulty is null or difficulty = :difficulty)
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
        "prompt": body.get("prompt"),
        "unit_key": body.get("unit_key"),
        "correct_answer": body.get("correct_answer") or body.get("answer"),
        "options": body.get("options"),
        "evaluation_mode": body.get("evaluation_mode"),
        "accepted_answers": body.get("accepted_answers"),
        "rubric": body.get("rubric"),
    }

    # Validación mínima
    if attempt_dict["item_id"] is None:
        raise HTTPException(status_code=400, detail="item_id es obligatorio")

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

        evaluation = evaluate_student_response(
            subject=str(attempt_dict.get("subject") or ""),
            prompt=str(attempt_dict.get("prompt") or ""),
            response=str(attempt_dict.get("response") or ""),
            correct_answer=str(attempt_dict.get("correct_answer") or ""),
            options=parse_string_list(attempt_dict.get("options")),
            evaluation_mode=str(attempt_dict.get("evaluation_mode") or ""),
            accepted_answers=parse_string_list(attempt_dict.get("accepted_answers")),
            rubric=str(attempt_dict.get("rubric") or ""),
        )
        attempt_dict["is_correct"] = bool(evaluation["is_correct"])

        # 4) Guardamos intento + actualizamos progreso
        attempt_id, p = save_attempt_and_update_progress(conn, attempt_dict)
        unit_progress = update_student_unit_progress(
            conn,
            student_id=attempt_dict["student_id"],
            subject=str(attempt_dict["subject"]),
            unit_key=str(attempt_dict.get("unit_key") or "").strip().lower() or None,
            is_correct=bool(attempt_dict["is_correct"]),
            last_answer_at=p.get("last_answer_at"),
        )
        update_assignment_progress(
            conn,
            student_id=attempt_dict["student_id"],
            subject=str(attempt_dict["subject"]),
            unit_key=str(attempt_dict.get("unit_key") or "").strip().lower() or None,
            attempt_created_at=p.get("last_answer_at"),
        )
        unlocked = []
        if unit_progress:
            previous_mastery = float(unit_progress.get("previous_p_mastery") or 0.0)
            current_mastery = float(unit_progress.get("p_mastery") or 0.0)
            unit_name = str(unit_progress.get("unit_name") or "Unidad")
            unit_key = str(unit_progress.get("unit_key") or "")
            subject_key = str(unit_progress.get("subject") or "")
            if previous_mastery < 0.55 <= current_mastery:
                ach = award_achievement(
                    conn,
                    attempt_dict["student_id"],
                    f"unit_progress_{subject_key}_{unit_key}",
                    f"Avance en {unit_name}",
                    f"Llevaste {unit_name} al nivel en progreso.",
                )
                if ach:
                    unlocked.append(ach)
            if previous_mastery < 0.80 <= current_mastery:
                ach = award_achievement(
                    conn,
                    attempt_dict["student_id"],
                    f"unit_mastery_{subject_key}_{unit_key}",
                    f"Dominio de {unit_name}",
                    f"Alcanzaste dominio alto en la unidad {unit_name}.",
                )
                if ach:
                    unlocked.append(ach)
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

    feedback_msg = None
    if not attempt_dict.get("is_correct"):
        # Generar retroalimentación personalizada usando AI (si está configurado)
        try:
            feedback_msg = generate_ai_feedback(
                question=str(attempt_dict.get("prompt") or ""),
                user_answer=str(attempt_dict.get("response") or ""),
                correct_answer=str(body.get("correct_answer") or body.get("answer") or ""),
                subject=str(attempt_dict.get("subject") or ""),
            )
        except Exception as exc:
            logger.warning("AI feedback generation failed: %s", exc)

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
        "feedback": feedback_msg,
        "is_correct": bool(attempt_dict["is_correct"]),
        "unit_progress": unit_progress,
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
                  total_attempts as n,
                  accuracy as acc
                from student_progress
                where student_id=:sid
                  and subject=:subject
            """
            ),
            dict(sid=student_id, subject=subject),
        ).mappings().first() or {"n": 0, "acc": 0}

        weak = conn.execute(
            text(
                """
                select s.name, coalesce(m.p_mastery,0.2) as p
                from skills s
                left join mastery m on m.skill_id=s.id and m.student_id=:sid
                where lower(trim(
                    replace(replace(replace(replace(replace(s.subject,'á','a'),'é','e'),'í','i'),'ó','o'),'ú','u')
                )) = :subject
                order by p asc
                limit 3
                """
            ),
            dict(sid=student_id, subject=subject),
        ).mappings().all()

        weak_units = conn.execute(
            text(
                """
                select unit_key, unit_name, accuracy, p_mastery, total_attempts
                from student_unit_progress
                where student_id = :sid and subject = :subject
                order by p_mastery asc, total_attempts desc, updated_at desc
                limit 3
                """
            ),
            dict(sid=student_id, subject=subject),
        ).mappings().all()

        strong_units = conn.execute(
            text(
                """
                select unit_key, unit_name, accuracy, p_mastery, total_attempts
                from student_unit_progress
                where student_id = :sid and subject = :subject
                order by p_mastery desc, total_attempts desc, updated_at desc
                limit 2
                """
            ),
            dict(sid=student_id, subject=subject),
        ).mappings().all()

        return {
            "attempts": int(kpis["n"] or 0),
            "accuracy": float(kpis["acc"] or 0),
            "weak_skills": [w["name"] for w in weak],
            "weak_units": [
                {
                    "unit_key": str(w["unit_key"]),
                    "unit_name": str(w["unit_name"]),
                    "accuracy": float(w["accuracy"] or 0),
                    "p_mastery": float(w["p_mastery"] or 0),
                    "total_attempts": int(w["total_attempts"] or 0),
                }
                for w in weak_units
            ],
            "strong_units": [
                {
                    "unit_key": str(w["unit_key"]),
                    "unit_name": str(w["unit_name"]),
                    "accuracy": float(w["accuracy"] or 0),
                    "p_mastery": float(w["p_mastery"] or 0),
                    "total_attempts": int(w["total_attempts"] or 0),
                }
                for w in strong_units
            ],
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
    active_assignment = None
    try:
        with engine.begin() as conn:
            student_ctx = get_student_context(conn, token_student_id, subj)
            active_assignment = conn.execute(
                text(
                    """
                    select id, unit_key, difficulty, title, target_attempts, completed_attempts
                    from teacher_assignments
                    where student_id = :sid and subject = :subject and status = 'active'
                    order by created_at asc
                    limit 1
                    """
                ),
                {"sid": token_student_id, "subject": subj},
            ).mappings().first()

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
        target_unit_key=str((active_assignment or {}).get("unit_key") or "") or None,
        target_difficulty=str((active_assignment or {}).get("difficulty") or "") or None,
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
                    RANDOM_CUSTOM_ITEMS_SQL,
                    {
                        "subject": subj,
                        "k": k,
                        "unit_key": (active_assignment or {}).get("unit_key"),
                        "difficulty": (active_assignment or {}).get("difficulty"),
                    },
                ).mappings().all()
            except Exception as custom_exc:
                logger.error("custom_items query failed: %s", custom_exc)
    else:
        try:
            with engine.begin() as conn:
                custom_rows = conn.execute(
                    RANDOM_CUSTOM_ITEMS_SQL,
                    {
                        "subject": subj,
                        "k": k,
                        "unit_key": (active_assignment or {}).get("unit_key"),
                        "difficulty": (active_assignment or {}).get("difficulty"),
                    },
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
                "unit_key": r.get("unit_key"),
                "unit_name": r.get("unit_name"),
                "evaluation_mode": r.get("evaluation_mode") or ("exact" if opts else ("accepted_answers" if subj == "castellano" else "exact")),
                "accepted_answers": parse_string_list(r.get("accepted_answers")),
                "rubric": r.get("rubric") or "",
                "reason": (
                    "asignacion"
                    if active_assignment and r.get("unit_key") == active_assignment.get("unit_key")
                    else r.get("reason") or ("refuerzo" if mode == "adaptive" else "aleatorio")
                ),
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
                "unit_key": r.get("unit_key"),
                "unit_name": r.get("unit_name"),
                "evaluation_mode": r.get("evaluation_mode") or ("exact" if opts else ("accepted_answers" if subj == "castellano" else "exact")),
                "accepted_answers": parse_string_list(r.get("accepted_answers")),
                "rubric": r.get("rubric") or "",
                "reason": "asignacion" if active_assignment else "custom",
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
