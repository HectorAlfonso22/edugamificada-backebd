import json
from sqlalchemy import text

# --- SQL ya definido antes ---
INSERT_ATTEMPT = text("""
INSERT INTO attempts (
    student_id,
    item_id,
    correct,
    answer_text,
    time_ms,
    hints_used
)
VALUES (
    :student_id,
    :item_id,
    :correct,
    :answer_text,
    :time_ms,
    :hints_used
)
RETURNING id, created_at
""")

SELECT_PROGRESS = text("""
SELECT
    student_id,
    subject,
    total_attempts,
    correct_attempts,
    accuracy,
    xp,
    level,
    current_streak,
    best_streak,
    last_answer_at
FROM student_progress
WHERE student_id = :student_id
  AND subject     = :subject
""")

UPSERT_PROGRESS = text("""
INSERT INTO student_progress (
    student_id,
    subject,
    total_attempts,
    correct_attempts,
    accuracy,
    xp,
    level,
    current_streak,
    best_streak,
    last_answer_at
)
VALUES (
    :student_id,
    :subject,
    :total_attempts,
    :correct_attempts,
    :accuracy,
    :xp,
    :level,
    :current_streak,
    :best_streak,
    :last_answer_at
)
ON CONFLICT (student_id, subject) DO UPDATE
SET
    total_attempts = EXCLUDED.total_attempts,
    correct_attempts = EXCLUDED.correct_attempts,
    accuracy = EXCLUDED.accuracy,
    xp = EXCLUDED.xp,
    level = EXCLUDED.level,
    current_streak = EXCLUDED.current_streak,
    best_streak = EXCLUDED.best_streak,
    last_answer_at = EXCLUDED.last_answer_at
RETURNING
    student_id,
    subject,
    total_attempts,
    correct_attempts,
    accuracy,
    xp,
    level,
    current_streak,
    best_streak,
    last_answer_at
""")


def _apply_rules(base: dict, is_correct: bool) -> dict:
    """Aplica las reglas de progreso y gamificación"""
    total_attempts = (base.get("total_attempts") or 0) + 1
    correct_attempts = (base.get("correct_attempts") or 0) + (1 if is_correct else 0)
    accuracy = correct_attempts / total_attempts if total_attempts else 0.0

    xp = (base.get("xp") or 0) + (10 if is_correct else 2)
    level = max(1, int(xp / 50))
    current_streak = (base.get("current_streak") or 0) + 1 if is_correct else 0
    best_streak = max(base.get("best_streak") or 0, current_streak)

    return {
        "total_attempts": total_attempts,
        "correct_attempts": correct_attempts,
        "accuracy": accuracy,
        "xp": xp,
        "level": level,
        "current_streak": current_streak,
        "best_streak": best_streak,
    }


def save_attempt_and_update_progress(conn, attempt: dict):
    """
    Guarda el intento del estudiante y actualiza su progreso acumulado.
    attempt debe tener:
      - student_id (str)
      - item_id (int)
      - subject (str)
      - is_correct (bool)
      - response (str o dict)
      - time_ms (int)
      - hints_used (int)
    """

    # --- Manejo seguro del campo response ---
    resp = attempt.get("response")

    # Si llega como dict, lo convertimos a JSON string
    if isinstance(resp, dict):
        resp = json.dumps(resp, ensure_ascii=False)

    # --- Insertamos el intento individual ---
    r = conn.execute(
        INSERT_ATTEMPT,
        {
            "student_id": str(attempt["student_id"]),
            "item_id": attempt["item_id"],
            "correct": attempt["is_correct"],
            "answer_text": resp,
            "time_ms": attempt.get("time_ms") or 0,
            "hints_used": attempt.get("hints_used") or 0,
        },
    ).mappings().first()

    attempt_id = r["id"]
    last_answer_at = r["created_at"]

    # --- Leemos progreso acumulado ---
    row = conn.execute(
        SELECT_PROGRESS,
        {
            "student_id": str(attempt["student_id"]),
            "subject": attempt["subject"],
        },
    ).mappings().first()

    base = row or {
        "total_attempts": 0,
        "correct_attempts": 0,
        "accuracy": 0.0,
        "xp": 0,
        "level": 1,
        "current_streak": 0,
        "best_streak": 0,
    }

    # --- Calculamos nuevos valores ---
    newvals = _apply_rules(base, attempt["is_correct"])

    # --- Hacemos UPSERT en student_progress ---
    p = conn.execute(
        UPSERT_PROGRESS,
        {
            "student_id": str(attempt["student_id"]),
            "subject": attempt["subject"],
            "total_attempts": newvals["total_attempts"],
            "correct_attempts": newvals["correct_attempts"],
            "accuracy": newvals["accuracy"],
            "xp": newvals["xp"],
            "level": newvals["level"],
            "current_streak": newvals["current_streak"],
            "best_streak": newvals["best_streak"],
            "last_answer_at": last_answer_at,
        },
    ).mappings().first()

    return attempt_id, p
