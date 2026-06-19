"""
Dafine — FastAPI Backend
Pipeline:
  Upload → Parse → Deep Profile (skewness/IQR/mode/labels) → Prompt Build (per-column instructions)
  → AI (OpenRouter) → Parse SQL → Execute DuckDB → Convert Output → Outlier Report → Save History → Return
"""

import io
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dafine")

import duckdb, httpx
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel

from auth_routes import router as auth_router, get_current_user, get_user_api_key
from db import get_supabase
from storage_helper import upload_clean_file, download_as_csv, delete_clean_file

# ─── CONFIG ───────────────────────────────────────────────────────
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")  # fallback jika user tidak set di akun
OPENROUTER_MODEL   = os.getenv("OPENROUTER_MODEL", "openai/gpt-oss-120b:free")
OPENROUTER_URL     = "https://openrouter.ai/api/v1/chat/completions"

VIEW_NAME = "source_table"  # Nama view tetap di DuckDB, diberitahu ke AI

ALLOWED_EXTENSIONS = {".csv", ".parquet", ".xlsx", ".xls", ".db", ".sqlite"}

# ─── APP ──────────────────────────────────────────────────────────
app = FastAPI(title="Dafine API", version="0.3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)


# ─── MODELS ───────────────────────────────────────────────────────
class ColumnInfo(BaseModel):
    name: str
    dtype: str

class PreviewResponse(BaseModel):
    file_type: str
    total_rows: int
    total_columns: int
    total_nulls: int
    columns: list[ColumnInfo]
    rows: list[dict[str, Any]]


# ══════════════════════════════════════════════════════════════════
# HELPER — Baca file → DuckDB view "source_table"
# ══════════════════════════════════════════════════════════════════
def load_to_duckdb(filepath: str, ext: str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()

    if ext == ".csv":
        con.execute(f"CREATE VIEW {VIEW_NAME} AS SELECT * FROM read_csv_auto('{filepath}')")
    elif ext == ".parquet":
        con.execute(f"CREATE VIEW {VIEW_NAME} AS SELECT * FROM read_parquet('{filepath}')")
    elif ext in (".xlsx", ".xls"):
        con.execute("INSTALL spatial; LOAD spatial;")
        con.execute(f"CREATE VIEW {VIEW_NAME} AS SELECT * FROM st_read('{filepath}')")
    elif ext in (".db", ".sqlite"):
        con.execute(f"ATTACH '{filepath}' AS src (TYPE sqlite);")
        tables = con.sql("SELECT table_name FROM information_schema.tables WHERE table_schema = 'src'").fetchall()
        if not tables:
            raise HTTPException(status_code=422, detail="SQLite database has no tables.")
        con.execute(f'CREATE VIEW {VIEW_NAME} AS SELECT * FROM src."{tables[0][0]}"')
    else:
        raise HTTPException(status_code=422, detail=f"Unsupported file type: {ext}")

    return con


def _fmt(v, max_len=80) -> str:
    """Truncate long values agar tidak overflow prompt AI."""
    s = str(v) if v is not None else "null"
    return (s[:max_len] + "…") if len(s) > max_len else s


def _col_labels(col_name: str, dtype_str: str, null_pct: float,
                unique_count: int, total_rows: int) -> tuple[list[str], bool]:
    """Compute labels for a column. Returns (labels, is_categorical)."""
    labels = []
    TIME_KW = ['date', 'time', 'year', 'month', 'day', 'timestamp', 'created', 'updated', 'period']

    if null_pct > 40:
        labels.append("HIGH_NULL")
    if any(k in col_name.lower() for k in TIME_KW) or any(t in dtype_str for t in ['date', 'timestamp']):
        labels.append("TIME_SERIES")

    is_cat = 0 < unique_count <= 20 and unique_count < 0.05 * total_rows
    if is_cat:
        labels.append("LIKELY_CATEGORICAL")

    return labels, is_cat


# ══════════════════════════════════════════════════════════════════
# STEP 1 — /preview
# ══════════════════════════════════════════════════════════════════
@app.post("/preview", response_model=PreviewResponse)
async def preview(file: UploadFile = File(...)):
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail=f"File type '{ext}' not supported.")

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        con = load_to_duckdb(tmp_path, ext)
        total_rows    = con.sql(f"SELECT COUNT(*) FROM {VIEW_NAME}").fetchone()[0]
        rel           = con.sql(f"SELECT * FROM {VIEW_NAME} LIMIT 0")
        col_names     = rel.columns
        dtypes        = rel.dtypes
        total_columns = len(col_names)
        columns       = [ColumnInfo(name=n, dtype=str(d)) for n, d in zip(col_names, dtypes)]

        null_expr   = " + ".join([f'COUNT(*) FILTER (WHERE "{c}" IS NULL)' for c in col_names])
        total_nulls = con.sql(f"SELECT {null_expr} FROM {VIEW_NAME}").fetchone()[0] or 0

        rows_raw = con.sql(f"SELECT * FROM {VIEW_NAME} LIMIT 10").fetchall()
        rows     = [dict(zip(col_names, row)) for row in rows_raw]

        return PreviewResponse(
            file_type=ext.lstrip("."),
            total_rows=total_rows,
            total_columns=total_columns,
            total_nulls=total_nulls,
            columns=columns,
            rows=rows,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DuckDB error: {str(e)}")
    finally:
        os.unlink(tmp_path)


# ══════════════════════════════════════════════════════════════════
# STEP 2 — DEEP PROFILING
# Skewness, IQR, median, mode, label system → dipakai untuk
# menentukan strategi imputasi NULL per kolom secara otomatis.
# ══════════════════════════════════════════════════════════════════
def profile_data(con: duckdb.DuckDBPyConnection) -> dict:
    rel        = con.sql(f"SELECT * FROM {VIEW_NAME} LIMIT 0")
    col_names  = rel.columns
    dtypes     = rel.dtypes
    total_rows = con.sql(f"SELECT COUNT(*) FROM {VIEW_NAME}").fetchone()[0]

    # Exact duplicate count (seluruh kolom identik)
    dup_count = 0
    try:
        all_cols_expr = ", ".join([f'"{c}"' for c in col_names])
        total_unique  = con.sql(f"SELECT COUNT(*) FROM (SELECT DISTINCT {all_cols_expr} FROM {VIEW_NAME})").fetchone()[0]
        dup_count     = total_rows - total_unique
    except Exception:
        pass

    profiles = []
    FIN_KW = ['revenue', 'cost', 'profit', 'income', 'expense', 'price', 'amount', 'total', 'balance', 'sales', 'margin']

    for col, dtype in zip(col_names, dtypes):
        dtype_str  = str(dtype).lower()
        is_numeric = any(t in dtype_str for t in ["int", "float", "double", "decimal", "numeric", "bigint"])
        is_date    = any(t in dtype_str for t in ["date", "timestamp"])

        null_count   = con.sql(f'SELECT COUNT(*) FROM {VIEW_NAME} WHERE "{col}" IS NULL').fetchone()[0]
        null_pct     = round((null_count / total_rows) * 100, 2) if total_rows > 0 else 0
        unique_count = con.sql(f'SELECT COUNT(DISTINCT "{col}") FROM {VIEW_NAME}').fetchone()[0]

        labels, is_cat = _col_labels(col, dtype_str, null_pct, unique_count, total_rows)

        head   = [r[0] for r in con.sql(f'SELECT "{col}" FROM {VIEW_NAME} LIMIT 5').fetchall()]
        offset = max(0, total_rows - 5)
        tail   = [r[0] for r in con.sql(f'SELECT "{col}" FROM {VIEW_NAME} LIMIT 5 OFFSET {offset}').fetchall()]
        try:
            sample = [r[0] for r in con.sql(f'SELECT "{col}" FROM {VIEW_NAME} USING SAMPLE 5').fetchall()]
        except Exception:
            sample = head

        top_raw = con.sql(f'''
            SELECT CAST("{col}" AS VARCHAR), COUNT(*) AS cnt
            FROM {VIEW_NAME} WHERE "{col}" IS NOT NULL
            GROUP BY "{col}" ORDER BY cnt DESC LIMIT 5
        ''').fetchall()
        top_values = [{"value": _fmt(r[0], 50), "count": r[1], "pct": round(r[1] / total_rows * 100, 1)} for r in top_raw]

        profile: dict = {
            "name": col,
            "type": "numeric" if is_numeric else ("date" if is_date else "string"),
            "dtype": str(dtype),
            "total_rows": total_rows,
            "null_count": null_count, "null_pct": null_pct,
            "unique_count": unique_count,
            "labels": labels,
            "top_values": top_values,
            "head":   [_fmt(v) for v in head],
            "tail":   [_fmt(v) for v in tail],
            "sample": [_fmt(v) for v in sample],
            "null_strategy": None,
            "fill_value": None,
            "outlier_count": 0,
            "lower_bound": None,
            "upper_bound": None,
        }

        # ── Numeric deep stats: median, std, skewness, IQR ─────────
        if is_numeric and null_count < total_rows:
            try:
                stats = con.sql(f'''
                    SELECT
                        MIN("{col}"), MAX("{col}"),
                        AVG("{col}"), STDDEV("{col}"), SKEWNESS("{col}"),
                        PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY "{col}"),
                        PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY "{col}"),
                        PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY "{col}")
                    FROM {VIEW_NAME} WHERE "{col}" IS NOT NULL
                ''').fetchone()

                mn, mx, avg, std, skew, q1, median, q3 = stats
                iqr = (q3 - q1) if (q1 is not None and q3 is not None) else None
                lb  = (q1 - 1.5 * iqr) if iqr is not None else None
                ub  = (q3 + 1.5 * iqr) if iqr is not None else None

                outlier_count = 0
                if lb is not None:
                    outlier_count = con.sql(f'''
                        SELECT COUNT(*) FROM {VIEW_NAME}
                        WHERE "{col}" IS NOT NULL AND ("{col}" < {lb} OR "{col}" > {ub})
                    ''').fetchone()[0]
                    if outlier_count > 0:
                        labels.append("POTENTIAL_OUTLIER")

                profile.update({
                    "min": mn, "max": mx,
                    "avg":      round(float(avg), 4)    if avg    is not None else None,
                    "std":      round(float(std), 4)    if std    is not None else None,
                    "skewness": round(float(skew), 4)   if skew   is not None else None,
                    "median":   round(float(median), 4) if median is not None else None,
                    "q1":       round(float(q1), 4)     if q1     is not None else None,
                    "q3":       round(float(q3), 4)     if q3     is not None else None,
                    "lower_bound":   round(float(lb), 4) if lb is not None else None,
                    "upper_bound":   round(float(ub), 4) if ub is not None else None,
                    "outlier_count": outlier_count,
                })

                # ── NULL strategy untuk kolom numerik ──────────────
                if null_count > 0:
                    if unique_count <= 10 and unique_count < 0.02 * total_rows:
                        # Numerik tapi sebenarnya kategorikal (misal kode gender 0/1/2)
                        labels.append("NUMERIC_AS_CATEGORICAL")
                        mode_r = con.sql(f'SELECT "{col}" FROM {VIEW_NAME} WHERE "{col}" IS NOT NULL GROUP BY "{col}" ORDER BY COUNT(*) DESC LIMIT 1').fetchone()
                        profile["null_strategy"] = "mode"
                        profile["fill_value"]    = mode_r[0] if mode_r else 0
                    elif skew is not None and abs(float(skew)) > 0.5:
                        # Distribusi skewed → median lebih robust dari outlier
                        profile["null_strategy"] = "median"
                        profile["fill_value"]    = round(float(median), 4) if median is not None else 0
                    else:
                        # Distribusi normal-ish → mean
                        profile["null_strategy"] = "mean"
                        profile["fill_value"]    = round(float(avg), 4) if avg is not None else 0

            except Exception as e:
                logger.warning(f"Numeric stats failed for '{col}': {e}")

        # ── String / Date stats ─────────────────────────────────────
        else:
            # Long text detection (kolom seperti synopsis/deskripsi)
            try:
                avg_len = con.sql(f'SELECT AVG(LENGTH(CAST("{col}" AS VARCHAR))) FROM {VIEW_NAME} WHERE "{col}" IS NOT NULL').fetchone()[0]
                if avg_len and float(avg_len) > 100:
                    labels.append("LONG_TEXT")
            except Exception:
                pass

            if is_cat:
                # Mixed casing check
                try:
                    lower_u = con.sql(f'SELECT COUNT(DISTINCT LOWER(CAST("{col}" AS VARCHAR))) FROM {VIEW_NAME}').fetchone()[0]
                    if lower_u < unique_count:
                        labels.append("MIXED_CASING")
                except Exception:
                    pass

                if null_count > 0:
                    mode_r = con.sql(f'SELECT CAST("{col}" AS VARCHAR) FROM {VIEW_NAME} WHERE "{col}" IS NOT NULL GROUP BY "{col}" ORDER BY COUNT(*) DESC LIMIT 1').fetchone()
                    profile["null_strategy"] = "mode"
                    profile["fill_value"]    = mode_r[0] if mode_r else "Unknown"
            else:
                # Whitespace issue check
                try:
                    ws = con.sql(f'SELECT COUNT(*) FROM {VIEW_NAME} WHERE "{col}" IS NOT NULL AND CAST("{col}" AS VARCHAR) != TRIM(CAST("{col}" AS VARCHAR))').fetchone()[0]
                    if ws > 0:
                        labels.append("WHITESPACE_ISSUE")
                except Exception:
                    pass

                if null_count > 0:
                    if "TIME_SERIES" in labels:
                        profile["null_strategy"] = "forward_fill"
                    else:
                        profile["null_strategy"] = "constant"
                        profile["fill_value"]    = "Unknown"

        profile["labels"] = labels
        profiles.append(profile)

    fin_cols = [c for c in col_names if any(k in c.lower() for k in FIN_KW)]

    return {
        "total_rows": total_rows,
        "total_columns": len(col_names),
        "col_names": list(col_names),
        "columns": profiles,
        "dup_count": dup_count,
        "correlated_groups": {"financial": fin_cols} if len(fin_cols) >= 2 else {},
    }


# ══════════════════════════════════════════════════════════════════
# STEP 3 — DYNAMIC PROMPT BUILDER
# Instruksi cleaning per-kolom berdasarkan profiling + user context.
# ══════════════════════════════════════════════════════════════════
def build_prompt(profile: dict, contexts: dict = None) -> str:
    if contexts is None:
        contexts = {}

    col_names   = profile["col_names"]
    select_cols = ", ".join([f'"{c}"' for c in col_names])
    dup_count   = profile.get("dup_count", 0)

    dedup_note = (
        f"STEP 1 — REMOVE EXACT DUPLICATES: {dup_count} duplicate rows detected. Use SELECT DISTINCT."
        if dup_count > 0 else "No exact duplicates detected."
    )

    p = f"""You are a DuckDB SQL data cleaning expert.

STRICT OUTPUT RULES:
1. Output ONLY a single raw SQL statement — no markdown fences, no comments, no explanation text
2. MUST start with: CREATE TABLE cleaned_table AS
3. Source: `{VIEW_NAME}` — NEVER use any other table name
4. Output: `cleaned_table` — MUST exist after execution
5. DuckDB syntax ONLY — no rowid, no stored procedures, no TEMP TABLE

DUCKDB REMINDERS:
- Combine ops in one SELECT: LOWER(TRIM(COALESCE(col,'Unknown')))
- ROW_NUMBER() OVER (...) is valid
- SELECT DISTINCT removes exact duplicate rows
- COALESCE(col, default_value) for NULL fill
- TRY_CAST(col AS DOUBLE) for safe numeric conversion

{dedup_note}

EXACT PATTERN:
CREATE TABLE cleaned_table AS
WITH base AS (
  SELECT DISTINCT {select_cols} FROM {VIEW_NAME}
)
SELECT
  col1,
  COALESCE(col2, <fill_value>) AS col2,
  LOWER(TRIM(COALESCE(col3, 'Unknown'))) AS col3
FROM base;

Dataset: {profile['total_rows']} rows × {profile['total_columns']} columns

COLUMN-BY-COLUMN CLEANING INSTRUCTIONS (follow exactly for each column):
"""

    for col in profile["columns"]:
        name     = col["name"]
        labels   = col.get("labels", [])
        strategy = col.get("null_strategy")
        fill_val = col.get("fill_value")
        null_cnt = col["null_count"]
        col_type = col["type"]
        user_ctx = contexts.get(name, "")
        user_ctx = user_ctx.strip() if isinstance(user_ctx, str) else ""

        parts = []

        # User context — highest priority
        if user_ctx:
            parts.append(f"[USER CONTEXT: {user_ctx}]")

        # NULL strategy dengan nilai yang sudah dihitung
        if null_cnt > 0 and strategy:
            if strategy == "mean":
                parts.append(f"NULL fill → mean={fill_val} (distribution normal, skewness≈0): COALESCE(\"{name}\", {fill_val})")
            elif strategy == "median":
                parts.append(f"NULL fill → median={fill_val} (skewed distribution, use median not mean): COALESCE(\"{name}\", {fill_val})")
            elif strategy == "mode":
                fv = f"'{fill_val}'" if isinstance(fill_val, str) else str(fill_val)
                parts.append(f"NULL fill → mode={fv} (categorical): COALESCE(\"{name}\", {fv})")
            elif strategy == "forward_fill":
                mode_fallback = col.get("top_values", [{}])[0].get("value", "Unknown") if col.get("top_values") else "Unknown"
                parts.append(f"TIME_SERIES — fill NULL with most frequent value ('{mode_fallback}') as approximation: COALESCE(\"{name}\", '{mode_fallback}')")
            elif strategy == "constant":
                parts.append(f"NULL fill → 'Unknown': COALESCE(\"{name}\", 'Unknown')")
        elif null_cnt == 0:
            parts.append("no NULL")

        # String transformations
        if "LONG_TEXT" in labels:
            parts.append("LONG_TEXT — select as-is, NO transformation")
        else:
            transforms = []
            if "WHITESPACE_ISSUE" in labels:
                transforms.append("TRIM()")
            if "MIXED_CASING" in labels:
                transforms.append("LOWER()")
            if transforms:
                parts.append(f"apply: {', '.join(transforms)}")

        # Value distribution context for categorical columns
        if "LIKELY_CATEGORICAL" in labels or "NUMERIC_AS_CATEGORICAL" in labels:
            tv = col.get("top_values", [])
            if tv:
                vals_str = " | ".join([f"{t['value']}={t['pct']}%" for t in tv[:5]])
                parts.append(f"top values: [{vals_str}]")

        # Numeric stats
        if col_type == "numeric":
            stat_parts = []
            if col.get("skewness") is not None:
                stat_parts.append(f"skewness={col['skewness']}")
            if col.get("std") is not None:
                stat_parts.append(f"std={col['std']}")
            if col.get("outlier_count", 0) > 0:
                stat_parts.append(f"{col['outlier_count']} outliers outside [{col.get('lower_bound')}–{col.get('upper_bound')}]")
            if stat_parts:
                parts.append(" | ".join(stat_parts))

        p += f"  · \"{name}\" ({col_type}/{col['dtype']}): {' | '.join(parts) if parts else 'pass through as-is'}\n"

    # Correlated financial columns
    fin = profile.get("correlated_groups", {}).get("financial", [])
    if fin:
        p += f"\nCORRELATED FINANCIAL COLUMNS: {', '.join(fin)}\n"
        p += "  → Derive missing value from others (e.g., profit = revenue - cost) ONLY if all required columns available.\n"

    p += f"""
FINAL SELECT must include ALL columns in this exact order: {select_cols}
OUTPUT THE SQL ONLY. Start with: CREATE TABLE cleaned_table AS
"""
    return p


# ══════════════════════════════════════════════════════════════════
# STEP 4 — CALL AI
# ══════════════════════════════════════════════════════════════════
async def call_ai(prompt: str, api_key: str = None) -> dict:
    token = api_key or OPENROUTER_API_KEY
    if not token:
        raise HTTPException(status_code=400, detail="OpenRouter API Key tidak ditemukan.")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }
    body = {
        "model": OPENROUTER_MODEL,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": f"DuckDB SQL expert. Source=`{VIEW_NAME}`. Output=`cleaned_table`. Return raw SQL ONLY — no fences, no comments. reasoning field = explanation."},
            {"role": "user",   "content": prompt},
        ],
    }

    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(OPENROUTER_URL, headers=headers, json=body)

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"AI API error: {resp.text}")

    message = resp.json()["choices"][0]["message"]

    raw_sql = message.get("content", "")
    sql = re.sub(r"```(?:sql|SQL)?", "", raw_sql).replace("```", "").strip()

    cleaned_lines = []
    for ln in sql.splitlines():
        stripped = ln.strip()
        if stripped.startswith("--") or stripped.startswith("//"):
            continue
        if "//" in ln:
            ln = ln[:ln.index("//")].rstrip()
        if ln.strip():
            cleaned_lines.append(ln)
    sql = "\n".join(cleaned_lines).strip()

    match = re.search(r"(CREATE\s+TABLE)", sql, re.IGNORECASE)
    if match:
        sql = sql[match.start():]

    if ";" in sql:
        sql = sql[:sql.index(";") + 1]

    reasoning = message.get("reasoning", "") or "No reasoning provided."
    return {"sql": sql, "reasoning": reasoning}


# ══════════════════════════════════════════════════════════════════
# STEP 5 — EXECUTE + EXPORT ke format asal
# ══════════════════════════════════════════════════════════════════
def execute_and_export(con: duckdb.DuckDBPyConnection, sql: str, ext: str):
    try:
        con.execute(sql)
    except Exception as e:
        logger.error(f"DuckDB execution error: {str(e)}")
        logger.error(f"SQL that failed:\n{sql}")
        raise HTTPException(status_code=422, detail=f"SQL execution error: {str(e)}\n\nSQL:\n{sql}")

    tables = [r[0] for r in con.sql("SHOW TABLES").fetchall()]
    if "cleaned_table" not in tables:
        raise HTTPException(status_code=422, detail="AI SQL did not produce `cleaned_table`.")

    buf = io.BytesIO()

    if ext == ".csv":
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            out = f.name
        con.execute(f"COPY cleaned_table TO '{out}' (HEADER, DELIMITER ',')")
        buf.write(open(out, "rb").read()); os.unlink(out)

    elif ext == ".parquet":
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
            out = f.name
        con.execute(f"COPY cleaned_table TO '{out}' (FORMAT PARQUET)")
        buf.write(open(out, "rb").read()); os.unlink(out)

    elif ext in (".xlsx", ".xls"):
        try:
            import pandas as pd
            df = con.sql("SELECT * FROM cleaned_table").df()
            df.to_excel(buf, index=False, engine="openpyxl")
        except ImportError:
            ext = ".csv"
            with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
                out = f.name
            con.execute(f"COPY cleaned_table TO '{out}' (HEADER, DELIMITER ',')")
            buf.write(open(out, "rb").read()); os.unlink(out)

    else:  # sqlite / db → fallback CSV
        ext = ".csv"
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            out = f.name
        con.execute(f"COPY cleaned_table TO '{out}' (HEADER, DELIMITER ',')")
        buf.write(open(out, "rb").read()); os.unlink(out)

    buf.seek(0)
    return buf, ext


# ══════════════════════════════════════════════════════════════════
# STEP 6 — OUTLIER REPORT (post-cleaning, IQR method via DuckDB)
# ══════════════════════════════════════════════════════════════════
def compute_outlier_report(con: duckdb.DuckDBPyConnection, profile: dict) -> dict:
    """
    Menghitung pencilan (outliers) menggunakan metode IQR / Tukey's Fences
    pada cleaned_table (data hasil pembersihan), bukan source_table.
    """
    report = {}
    try:
        total_rows = con.sql("SELECT COUNT(*) FROM cleaned_table").fetchone()[0]
    except Exception:
        return report

    if total_rows == 0:
        return report

    for col in profile.get("columns", []):
        if col["type"] != "numeric":
            continue
        col_name = col["name"]
        try:
            stats_query = f'''
                SELECT
                    quantile_cont("{col_name}", 0.25) AS q1,
                    quantile_cont("{col_name}", 0.50) AS median,
                    quantile_cont("{col_name}", 0.75) AS q3
                FROM cleaned_table
                WHERE "{col_name}" IS NOT NULL
            '''
            res = con.execute(stats_query).fetchone()
            if not res or res[0] is None or res[2] is None:
                continue

            q1, median, q3 = res[0], res[1], res[2]
            iqr = q3 - q1
            lower_bound = q1 - 1.5 * iqr
            upper_bound = q3 + 1.5 * iqr

            outlier_count = con.execute(f'''
                SELECT COUNT(*) FROM cleaned_table
                WHERE "{col_name}" < {lower_bound} OR "{col_name}" > {upper_bound}
            ''').fetchone()[0] or 0
            outlier_pct = round((outlier_count / total_rows) * 100, 2)

            samples_raw = con.execute(f'''
                SELECT "{col_name}" FROM cleaned_table
                WHERE "{col_name}" < {lower_bound} OR "{col_name}" > {upper_bound}
                LIMIT 5
            ''').fetchall()
            samples = [str(r[0]) for r in samples_raw]

            report[col_name] = {
                "q1": round(float(q1), 2),
                "median": round(float(median), 2),
                "q3": round(float(q3), 2),
                "iqr": round(float(iqr), 2),
                "lower_bound": round(float(lower_bound), 2),
                "upper_bound": round(float(upper_bound), 2),
                "outlier_count": outlier_count,
                "outlier_percentage": outlier_pct,
                "samples": samples,
            }
        except Exception as e:
            logger.warning(f"Gagal memproses outlier untuk kolom '{col_name}': {e}")
            continue

    return report


# ══════════════════════════════════════════════════════════════════
# MAIN ENDPOINT — /clean
# ══════════════════════════════════════════════════════════════════
@app.post("/clean")
async def clean(
    file: UploadFile = File(...),
    column_contexts: str = Form(default="{}"),
    title: str = Form(default=""),
    current_user: dict = Depends(get_current_user),
):
    ext  = Path(file.filename).suffix.lower()
    stem = Path(file.filename).stem

    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail=f"File type '{ext}' not supported.")

    try:
        contexts = json.loads(column_contexts)
    except Exception:
        contexts = {}

    file_bytes = await file.read()

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    try:
        logger.info("STEP 1 — load_to_duckdb")
        con = load_to_duckdb(tmp_path, ext)

        logger.info("STEP 2 — profile_data (deep)")
        profile = profile_data(con)
        logger.info(f"  {profile['total_rows']} rows, {profile['total_columns']} cols, {profile['dup_count']} dups")

        logger.info("STEP 3 — build_prompt")
        prompt = build_prompt(profile, contexts)

        logger.info("STEP 4 — call_ai")
        user_api_key = get_user_api_key(current_user)
        ai_result    = await call_ai(prompt, api_key=user_api_key)
        sql          = ai_result["sql"]
        reasoning    = ai_result["reasoning"]
        logger.info(f"  SQL preview: {sql[:200]}")

        logger.info("STEP 5 — execute_and_export")
        output_buf, output_ext = execute_and_export(con, sql, ext)
        logger.info("STEP 5 done")

        logger.info("STEP 6 — outlier_report")
        outlier_report = compute_outlier_report(con, profile)

        logger.info("STEP 7 — save to database")
        history_id = await _save_history(
            con=con, user=current_user, original_name=file.filename,
            ext=ext, title=title, contexts=contexts,
            sql=sql, reasoning=reasoning, profile=profile,
        )
        logger.info(f"  saved history_id={history_id}")

        mime_map = {
            ".csv":     "text/csv",
            ".parquet": "application/octet-stream",
            ".xlsx":    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }

        def _hdr(val: str, limit: int = 2000) -> str:
            val = val.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
            return val.encode("ascii", errors="replace").decode("ascii")[:limit]

        return StreamingResponse(
            output_buf,
            media_type=mime_map.get(output_ext, "application/octet-stream"),
            headers={
                "Content-Disposition":           f'attachment; filename="{stem}_cleaned{output_ext}"',
                "X-AI-Reasoning":                _hdr(reasoning),
                "X-AI-SQL":                      _hdr(sql),
                "X-Outlier-Report":              _hdr(json.dumps(outlier_report)),
                "X-History-ID":                  str(history_id) if history_id else "",
                "Access-Control-Expose-Headers": "X-AI-Reasoning,X-AI-SQL,X-Outlier-Report,X-History-ID,Content-Disposition",
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        os.unlink(tmp_path)


# ══════════════════════════════════════════════════════════════════
# HELPER — Save cleaning result to Supabase
# ══════════════════════════════════════════════════════════════════
async def _save_history(
    con, user: dict, original_name: str, ext: str, title: str,
    contexts: dict, sql: str, reasoning: str, profile: dict,
) -> int | None:
    """Insert records into column_context, ai_output, clean_file, cleaning_history."""
    try:
        sb         = get_supabase()
        user_id    = user["id"]
        clean_name = f"{Path(original_name).stem}_cleaned.parquet"
        rows_after = con.sql("SELECT COUNT(*) FROM cleaned_table").fetchone()[0]

        # 1. column_context (nullable - only if contexts provided)
        ctx_id = None
        if contexts:
            ctx_row = sb.table("column_context").insert({"context": contexts}).execute()
            ctx_id  = ctx_row.data[0]["id"]

        # 2. ai_output
        ai_row    = sb.table("ai_output").insert({"sql_query": sql, "explanation": reasoning}).execute()
        ai_out_id = ai_row.data[0]["id"]

        # 3. clean_file (placeholder location, diisi setelah upload)
        file_row = sb.table("clean_file").insert({
            "user_id":       user_id,
            "original_name": original_name,
            "name":          clean_name,
            "type":          ext.lstrip("."),
            "location":      "pending",
        }).execute()
        file_id = file_row.data[0]["id"]

        # 4. cleaning_history
        hist_row = sb.table("cleaning_history").insert({
            "user_id":            user_id,
            "title":              title or Path(original_name).stem,
            "original_file_name": original_name,
            "original_file_type": ext.lstrip("."),
            "rows_before":        profile["total_rows"],
            "rows_after":         rows_after,
            "status":             "completed",
            "column_context_id":  ctx_id,
            "ai_output_id":       ai_out_id,
            "clean_file_id":      file_id,
        }).execute()
        history_id = hist_row.data[0]["id"]

        # 5. Upload parquet to storage, update clean_file.location
        storage_path = upload_clean_file(user_id, history_id, con)
        sb.table("clean_file").update({"location": storage_path, "size_bytes": len(storage_path)}).eq("id", file_id).execute()

        return history_id

    except Exception as e:
        logger.error(f"Failed to save history: {e}")
        return None


# ══════════════════════════════════════════════════════════════════
# GET /history/{history_id}/download — download cleaned file
# ══════════════════════════════════════════════════════════════════
@app.get("/history/{history_id}/download")
async def download_history_file(
    history_id: int,
    current_user: dict = Depends(get_current_user),
):
    sb = get_supabase()

    hist = sb.table("cleaning_history").select("*, clean_file(*)").eq("id", history_id).eq("user_id", current_user["id"]).maybe_single().execute()
    if not hist or not hist.data:
        raise HTTPException(status_code=404, detail="History not found.")

    clean_file = hist.data.get("clean_file", {})
    location   = clean_file.get("location")
    if not location or location == "pending":
        raise HTTPException(status_code=404, detail="File not available.")

    try:
        csv_bytes = download_as_csv(location, clean_file.get("original_name", "cleaned.csv"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")

    filename = Path(clean_file.get("original_name", "cleaned.csv")).stem + "_cleaned.csv"
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ══════════════════════════════════════════════════════════════════
# DELETE /history/{history_id} — hapus history + file
# ══════════════════════════════════════════════════════════════════
@app.delete("/history/{history_id}")
async def delete_history(
    history_id: int,
    current_user: dict = Depends(get_current_user),
):
    sb = get_supabase()

    hist = sb.table("cleaning_history").select("*, clean_file(location)").eq("id", history_id).eq("user_id", current_user["id"]).maybe_single().execute()
    if not hist or not hist.data:
        raise HTTPException(status_code=404, detail="History not found.")

    location = hist.data.get("clean_file", {}).get("location")
    if location and location != "pending":
        try:
            delete_clean_file(location)
        except Exception as e:
            logger.warning(f"Storage delete failed: {e}")

    sb.table("cleaning_history").delete().eq("id", history_id).execute()
    return {"message": "History deleted successfully."}


# ══════════════════════════════════════════════════════════════════
# GET /history — list semua history milik user
# ══════════════════════════════════════════════════════════════════
@app.get("/history")
async def get_history(current_user: dict = Depends(get_current_user)):
    sb = get_supabase()
    result = sb.table("cleaning_history").select(
        "id, title, original_file_name, original_file_type, original_file_size, rows_before, rows_after, status, created_at, ai_output(sql_query, explanation), column_context(context)"
    ).eq("user_id", current_user["id"]).order("created_at", desc=True).execute()
    return {"history": result.data}


@app.get("/")
def root():
    return {"status": "ok", "service": "Dafine API", "version": "0.3.0"}


@app.get("/health")
def health():
    return {"status": "healthy"}