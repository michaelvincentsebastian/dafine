"""
Dafine — FastAPI Backend
Pipeline:
  Upload → Parse → Profile → Prompt Build → AI (OpenRouter)
  → Parse SQL → Execute DuckDB → Convert Output → Return file + reasoning
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
app = FastAPI(title="Dafine API", version="0.2.0")

# 1. Daftarkan Middleware secara terpisah
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. Daftarkan Router setelah middleware
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
# STEP 2 — PROFILING
# ══════════════════════════════════════════════════════════════════
def profile_data(con: duckdb.DuckDBPyConnection) -> dict:
    rel        = con.sql(f"SELECT * FROM {VIEW_NAME} LIMIT 0")
    col_names  = rel.columns
    dtypes     = rel.dtypes
    total_rows = con.sql(f"SELECT COUNT(*) FROM {VIEW_NAME}").fetchone()[0]
    profiles   = []

    for col, dtype in zip(col_names, dtypes):
        dtype_str  = str(dtype).lower()
        is_numeric = any(t in dtype_str for t in ["int", "float", "double", "decimal", "numeric", "bigint"])

        null_count   = con.sql(f'SELECT COUNT(*) FROM {VIEW_NAME} WHERE "{col}" IS NULL').fetchone()[0]
        null_pct     = round((null_count / total_rows) * 100, 2) if total_rows > 0 else 0
        unique_count = con.sql(f'SELECT COUNT(DISTINCT "{col}") FROM {VIEW_NAME}').fetchone()[0]

        head   = [r[0] for r in con.sql(f'SELECT "{col}" FROM {VIEW_NAME} LIMIT 5').fetchall()]
        # rowid tidak tersedia di DuckDB view — gunakan OFFSET untuk tail
        offset = max(0, total_rows - 5)
        tail   = [r[0] for r in con.sql(f'SELECT "{col}" FROM {VIEW_NAME} LIMIT 5 OFFSET {offset}').fetchall()]
        sample = [r[0] for r in con.sql(f'SELECT "{col}" FROM {VIEW_NAME} USING SAMPLE 5').fetchall()]

        def _fmt(v, max_len=80):
            """Truncate long values agar tidak overflow prompt AI."""
            s = str(v) if v is not None else "null"
            return s[:max_len] + "…" if len(s) > max_len else s

        profile: dict = {
            "name": col, "type": "numeric" if is_numeric else "string",
            "dtype": str(dtype), "total_rows": total_rows,
            "null_count": null_count, "null_pct": null_pct,
            "unique_count": unique_count,
            "head":   [_fmt(v) for v in head],
            "tail":   [_fmt(v) for v in tail],
            "sample": [_fmt(v) for v in sample],
        }

        if is_numeric:
            stats = con.sql(f'SELECT MIN("{col}"), MAX("{col}"), AVG("{col}") FROM {VIEW_NAME}').fetchone()
            profile.update({"min": stats[0], "max": stats[1], "avg": round(float(stats[2]), 2) if stats[2] else None})

        profiles.append(profile)

    return {"total_rows": total_rows, "total_columns": len(col_names), "columns": profiles}


# ══════════════════════════════════════════════════════════════════
# STEP 3 — DYNAMIC PROMPT BUILDER
# ══════════════════════════════════════════════════════════════════
def build_prompt(profile: dict, contexts: dict = None) -> str:
    # Antisipasi jika contexts bernilai None
    if contexts is None:
        contexts = {}

    col_names = [c["name"] for c in profile["columns"]]

    p = f"""You are a DuckDB SQL data cleaning expert.

STRICT OUTPUT RULES (violating these will break the pipeline):
1. Output ONLY a single raw SQL statement — no markdown fences, no -- comments, no explanation text
2. The query MUST start with: CREATE TABLE cleaned_table AS
3. Source table: `{VIEW_NAME}` (NEVER use any other name)
4. Output table: `cleaned_table` (MUST exist after execution)
5. DuckDB syntax only — no stored procedures, no TEMP TABLE

DUCKDB RULES (avoid these common mistakes):
- If using CTEs, ALL columns used in the final SELECT must be explicitly available in scope
- If you aggregate in one CTE and need it in another, use CROSS JOIN or subquery
- COALESCE works normally: COALESCE(col, default_value)
- Use TRY_CAST(col AS DOUBLE) for safe numeric conversion
- ROW_NUMBER() OVER (...) is valid in DuckDB

CLEANING STRATEGY:
- Fill NULLs with sensible defaults (0 for numeric, 'Unknown' for string)
- Deduplicate if needed using ROW_NUMBER() in a CTE, then filter WHERE rn = 1
- Normalize string casing with LOWER() or UPPER() if inconsistent
- Do NOT drop columns unless clearly irrelevant
- IMPORTANT: Pay close attention to the 'User Column Context' provided for each column to understand its business rules or meaning.

EXACT SQL PATTERN TO FOLLOW (single CTE example):
CREATE TABLE cleaned_table AS
SELECT
  col1,
  COALESCE(col2, 0) AS col2,
  LOWER(TRIM(col3)) AS col3
FROM {VIEW_NAME};

EXACT SQL PATTERN if deduplication needed:
CREATE TABLE cleaned_table AS
WITH deduped AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY id_col ORDER BY id_col) AS rn
  FROM {VIEW_NAME}
)
SELECT {", ".join([f'"{c}"' for c in col_names])}
FROM deduped
WHERE rn = 1;

Dataset Summary:
- Rows: {profile["total_rows"]} | Columns: {profile["total_columns"]}
- Columns: {", ".join(col_names)}

Column Profiles:
"""
    for i, col in enumerate(profile["columns"], 1):
        col_name = col["name"]
        # Ambil konteks dari user berdasarkan nama kolom
        user_context = contexts.get(col_name, "")
        context_line = f"\n   User Column Context: {user_context}" if user_context else ""

        p += f"""
{i}. `{col_name}` — {col["type"]} ({col["dtype"]}){context_line}
   Null: {col["null_count"]} ({col["null_pct"]}%) | Unique: {col["unique_count"]}
   Head:   {", ".join(col["head"])}
   Sample: {", ".join(col["sample"])}"""
        if col["type"] == "numeric":
            p += f"\n   Min: {col['min']} | Max: {col['max']} | Avg: {col['avg']}"

    p += """

NOW OUTPUT THE SQL QUERY ONLY. Start immediately with CREATE TABLE cleaned_table AS
"""
    return p


# ══════════════════════════════════════════════════════════════════
# STEP 4 — CALL AI
# ══════════════════════════════════════════════════════════════════
async def call_ai(prompt: str, api_key: str = None) -> dict:
    # Gunakan api_key dari user, jika tidak ada baru gunakan global fallback
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
            {"role": "system", "content": f"DuckDB SQL expert. Table=`{VIEW_NAME}`. Output table=`cleaned_table`. content field = raw SQL ONLY, no fences no comments. reasoning field = explanation."},
            {"role": "user",   "content": prompt},
        ],
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(OPENROUTER_URL, headers=headers, json=body)

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"AI API error: {resp.text}")

    message = resp.json()["choices"][0]["message"]

    # Bersihkan content dari markdown fence
    raw_sql = message.get("content", "")
    sql = re.sub(r"```(?:sql|SQL)?", "", raw_sql).replace("```", "").strip()

    # Bersihkan baris komentar (-- dan // keduanya tidak valid di DuckDB)
    cleaned_lines = []
    for ln in sql.splitlines():
        stripped = ln.strip()
        # Buang baris yang pure comment
        if stripped.startswith("--") or stripped.startswith("//"):
            continue
        # Buang inline // comment di akhir baris
        if "//" in ln:
            ln = ln[:ln.index("//")].rstrip()
        if ln.strip():  # skip baris kosong hasil strip
            cleaned_lines.append(ln)
    sql = "\n".join(cleaned_lines).strip()

    # Pastikan SQL dimulai dari CREATE TABLE (buang teks sebelumnya jika ada)
    match = re.search(r"(CREATE\s+TABLE)", sql, re.IGNORECASE)
    if match:
        sql = sql[match.start():]

    # Potong setelah titik koma pertama (buang teks sampah setelah query)
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

def compute_outlier_report(con: duckdb.DuckDBPyConnection, profile: dict) -> dict:
    """
    Menghitung pencilan (outliers) menggunakan metode statistik IQR (Interquartile Range)
    atau Tukey's Fences langsung menggunakan mesin query DuckDB.
    
    Metode ini bekerja pada kolom bertipe numerik dengan rumus:
      - Interquartile Range (IQR) = Q3 - Q1
      - Batas Bawah (Lower Bound) = Q1 - (1.5 * IQR)
      - Batas Atas (Upper Bound)  = Q3 + (1.5 * IQR)
      - Data dianggap outlier jika nilai < Lower Bound ATAU nilai > Upper Bound
    """
    report = {}
    total_rows = profile.get("total_rows", 0)
    if total_rows == 0:
        return report

    # Lakukan iterasi untuk setiap kolom yang ada pada profil data
    for col in profile.get("columns", []):
        # Deteksi pencilan hanya dilakukan pada kolom bertipe numerik
        if col["type"] != "numeric":
            continue

        col_name = col["name"]
        try:
            # Langkah 1: Ambil nilai Q1 (25%), Median (50%), dan Q3 (75%) menggunakan quantile_cont DuckDB
            stats_query = f"""
                SELECT 
                    quantile_cont("{col_name}", 0.25) AS q1,
                    quantile_cont("{col_name}", 0.50) AS median,
                    quantile_cont("{col_name}", 0.75) AS q3
                FROM {VIEW_NAME}
                WHERE "{col_name}" IS NOT NULL
            """
            res = con.execute(stats_query).fetchone()
            if not res or res[0] is None or res[2] is None:
                continue

            q1, median, q3 = res[0], res[1], res[2]
            iqr = q3 - q1
            lower_bound = q1 - 1.5 * iqr
            upper_bound = q3 + 1.5 * iqr

            # Langkah 2: Hitung total baris data yang nilainya menembus batas (outliers)
            count_query = f"""
                SELECT COUNT(*) 
                FROM {VIEW_NAME} 
                WHERE "{col_name}" < {lower_bound} OR "{col_name}" > {upper_bound}
            """
            outlier_count = con.execute(count_query).fetchone()[0] or 0
            outlier_pct = round((outlier_count / total_rows) * 100, 2)

            # Langkah 3: Ambil maksimal 5 contoh data ekstrem yang terdeteksi sebagai outlier
            sample_query = f"""
                SELECT "{col_name}" 
                FROM {VIEW_NAME} 
                WHERE "{col_name}" < {lower_bound} OR "{col_name}" > {upper_bound}
                LIMIT 5
            """
            samples_raw = con.execute(sample_query).fetchall()
            samples = [str(r[0]) for r in samples_raw]

            # Simpan hasil kalkulasi statistik ke dalam payload report berdasarkan nama kolom
            report[col_name] = {
                "q1": round(float(q1), 2),
                "median": round(float(median), 2),
                "q3": round(float(q3), 2),
                "iqr": round(float(iqr), 2),
                "lower_bound": round(float(lower_bound), 2),
                "upper_bound": round(float(upper_bound), 2),
                "outlier_count": outlier_count,
                "outlier_percentage": outlier_pct,
                "samples": samples
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

        logger.info("STEP 2 — profile_data")
        profile = profile_data(con)
        logger.info(f"  {profile['total_rows']} rows, {profile['total_columns']} cols")

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
            val = val.replace("\r\n"," ").replace("\n"," ").replace("\r"," ")
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

        # 3. Upload file to Supabase Storage (needs history_id first — use ai_out_id as temp key)
        # We insert clean_file with placeholder location, then update after we have history_id
        file_row = sb.table("clean_file").insert({
            "user_id":       user_id,
            "original_name": original_name,
            "name":          clean_name,
            "type":          ext.lstrip("."),
            "location":      "pending",  # updated below
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

    # Fetch history + clean_file joined
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

    # Hapus file dari Supabase Storage
    location = hist.data.get("clean_file", {}).get("location")
    if location and location != "pending":
        try:
            delete_clean_file(location)
        except Exception as e:
            logger.warning(f"Storage delete failed: {e}")

    # Hapus dari DB (cascade akan hapus clean_file, ai_output, column_context)
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
    return {"status": "ok", "service": "Dafine API", "version": "0.2.0"}