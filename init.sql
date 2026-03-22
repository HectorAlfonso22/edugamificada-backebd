-- init.sql
-- Script para crear las tablas necesarias y datos mínimos de arranque.

-- 1) Usuarios
CREATE TABLE IF NOT EXISTS users (
  id UUID PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('student','teacher')),
  display_name TEXT,
  age INT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2) Habilidades / temas
CREATE TABLE IF NOT EXISTS skills (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  subject TEXT NOT NULL CHECK (subject IN ('matematica','castellano'))
);

-- 3) Items (ejercicios)
CREATE TABLE IF NOT EXISTS items (
  id BIGSERIAL PRIMARY KEY,
  type TEXT NOT NULL DEFAULT 'ai',
  subject TEXT NOT NULL CHECK (subject IN ('matematica','castellano')),
  prompt TEXT NOT NULL,
  options JSONB NOT NULL DEFAULT '[]'::jsonb,
  answer TEXT NOT NULL,
  skill_id BIGINT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
  difficulty TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 4) Mastery (dominio por alumno)
CREATE TABLE IF NOT EXISTS mastery (
  student_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  skill_id BIGINT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
  p_mastery NUMERIC NOT NULL DEFAULT 0.30,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (student_id, skill_id)
);

-- 5) Intentos
CREATE TABLE IF NOT EXISTS attempts (
  id BIGSERIAL PRIMARY KEY,
  student_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  item_id BIGINT NOT NULL,
  correct BOOLEAN NOT NULL,
  answer_text TEXT,
  time_ms INT NOT NULL DEFAULT 0,
  hints_used INT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 6) Progreso acumulado
CREATE TABLE IF NOT EXISTS student_progress (
  student_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  subject TEXT NOT NULL,
  total_attempts INT NOT NULL DEFAULT 0,
  correct_attempts INT NOT NULL DEFAULT 0,
  accuracy NUMERIC NOT NULL DEFAULT 0,
  xp INT NOT NULL DEFAULT 0,
  level INT NOT NULL DEFAULT 1,
  current_streak INT NOT NULL DEFAULT 0,
  best_streak INT NOT NULL DEFAULT 0,
  last_answer_at TIMESTAMPTZ,
  PRIMARY KEY (student_id, subject)
);

-- 7) Historial de preguntas generadas por IA (para evitar repeticiones)
CREATE TABLE IF NOT EXISTS ai_history (
  id BIGSERIAL PRIMARY KEY,
  student_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  subject TEXT NOT NULL,
  prompt TEXT NOT NULL,
  correct BOOLEAN NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ai_history_student_subject ON ai_history (student_id, subject);

-- 8) Progreso por unidad curricular
CREATE TABLE IF NOT EXISTS student_unit_progress (
  student_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  subject TEXT NOT NULL CHECK (subject IN ('matematica','castellano')),
  unit_key TEXT NOT NULL,
  unit_name TEXT NOT NULL,
  total_attempts INT NOT NULL DEFAULT 0,
  correct_attempts INT NOT NULL DEFAULT 0,
  accuracy NUMERIC NOT NULL DEFAULT 0,
  p_mastery NUMERIC NOT NULL DEFAULT 0.30,
  last_answer_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (student_id, subject, unit_key)
);

CREATE INDEX IF NOT EXISTS idx_student_unit_progress_subject
ON student_unit_progress (student_id, subject, p_mastery);

-- 9) Logros
CREATE TABLE IF NOT EXISTS achievements (
  id BIGSERIAL PRIMARY KEY,
  student_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  code TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  earned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (student_id, code)
);

-- 10) Ejercicios custom de docentes
CREATE TABLE IF NOT EXISTS custom_items (
  id BIGSERIAL PRIMARY KEY,
  subject TEXT NOT NULL CHECK (subject IN ('matematica','castellano')),
  prompt TEXT NOT NULL,
  options JSONB NOT NULL DEFAULT '[]'::jsonb,
  answer TEXT NOT NULL,
  created_by UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  is_active BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_custom_items_subject ON custom_items (subject);

-- Datos de ejemplo (puedes cambiarlos según tus necesidades)
-- Nota: reemplaza <STUDENT_UUID> por el UUID real del alumno creado.

-- Habilidades base
INSERT INTO skills (name, subject) VALUES
  ('Sumas básicas', 'matematica'),
  ('Restas básicas', 'matematica'),
  ('Ortografía', 'castellano')
ON CONFLICT DO NOTHING;

-- Items de ejemplo (no es obligatorio si usás solo IA)
INSERT INTO items (subject, prompt, options, answer, skill_id, difficulty) VALUES
  ('matematica', '¿Cuánto es 3 + 2?', '["4","5","6","7"]', '5', 1, 'A'),
  ('matematica', '¿Cuánto es 10 - 4?', '["4","5","6","7"]', '6', 2, 'A'),
  ('castellano', 'Selecciona la palabra correctamente escrita.', '["valla","vaya","baya","vaza"]', 'vaya', 3, 'A')
ON CONFLICT DO NOTHING;
