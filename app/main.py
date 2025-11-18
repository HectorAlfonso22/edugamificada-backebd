# app/main.py
import json
import os
import socket
from dotenv import load_dotenv
load_dotenv()  # <<< CARGA EL .env ANTES DE leer DATABASE_URL

import random
from datetime import date
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy import text
from fastapi import Query
from fastapi import FastAPI, Request
from sqlalchemy import create_engine, text
from pydantic import BaseModel
from uuid import UUID

from app.schemas import AttemptIn, AttemptOut, ProgressOut
from app.services.progress import save_attempt_and_update_progress, SELECT_PROGRESS

# ---------------------------
# Configuración / Conexión DB
# ---------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
print("DEBUG DATABASE_URL =", DATABASE_URL)

# Comprobar DNS del host (opcional, para debug):
try:
    host = DATABASE_URL.split("@")[1].split(":")[0]  # db.<ref>.supabase.co
    print("DEBUG DB HOST =", host, "->", socket.gethostbyname(host))
except Exception as e:
    print("DEBUG DNS ERROR:", e)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# Parámetros BKT-lite (puedes ajustarlos en .env)
SLIP  = float(os.getenv("SLIP", 0.10))
GUESS = float(os.getenv("GUESS", 0.20))
LEARN = float(os.getenv("LEARN", 0.15))

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
# POST /attempts
# Guarda intento, valida (opcional) y actualiza dominio, SRS y XP
# ---------------------------

# Consulta para resolver la materia a partir del item_id
RESOLVE_SUBJECT_SQL = text("SELECT subject FROM items WHERE id = :item_id")

@app.post("/attempts")
async def post_attempt(request: Request):
    # 1) Leemos el JSON crudo que llega del front
    body = await request.json()
    print("DEBUG /attempts payload:", body)

    # 2) Normalizamos nombres de campos:
    #    - is_correct <- is_correct o correct
    #    - response   <- response o answer_text
    attempt_dict = {
        "student_id": body.get("student_id"),
        "item_id": body.get("item_id"),
        "is_correct": body.get("is_correct", body.get("correct")),
        "response": body.get("response", body.get("answer_text")),
        "time_ms": body.get("time_ms", 0),
        "hints_used": body.get("hints_used", 0),
        "subject": body.get("subject"),  # puede venir vacío
    }

    # Validación mínima
    if not attempt_dict["student_id"] or attempt_dict["item_id"] is None:
        raise HTTPException(status_code=400, detail="student_id e item_id son obligatorios")

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
    student_id: str = Query(..., min_length=8),
    subject: str = Query(...),   # sin regex para no bloquear
    k: int = Query(5, ge=1, le=20),
):
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
    sql = text(
        """
        select 
            i.id,
            i.type,
            i.prompt,
            i.options,
            i.answer,
            i.skill_id
        from items i
        join skills s on s.id = i.skill_id
        where lower(trim(
            replace(replace(replace(replace(replace(s.subject,'á','a'),'é','e'),'í','i'),'ó','o'),'ú','u')
        )) = :subject
        order by random()
        limit :k
        """
    )

    try:
        with engine.begin() as conn:
            rows = conn.execute(sql, {"subject": subj, "k": k}).mappings().all()

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

            items.append(
                {
                    "id": int(r["id"]),
                    "type": r["type"],
                    "prompt": r["prompt"],
                    "options": opts,
                    "skill_id": int(r["skill_id"]),
                    # 👇 clave importante para el front
                    "answer": r.get("answer"),
                    # opcional, por si lo quieres usar en el front
                    "subject": subj,
                }
            )

        return {"items": items, "count": len(items), "subject_used": subj}
    except Exception as e:
        print("ERROR /next-items:", repr(e))
        return {"items": [], "count": 0, "error": "fallback"}

# ---------------------------
# GET /reports/student/{sid}
# KPIs simples + habilidades más débiles
# ---------------------------
@app.get("/reports/student/{sid}")
def report_student(sid: str):
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

