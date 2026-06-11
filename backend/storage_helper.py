"""
Dafine — Storage helper
Bucket  : dafine-files (private)
Format  : Parquet (ringan, efisien untuk penyimpanan)
Path    : {user_id}/{cleaning_history_id}.parquet
Download: parquet → CSV (konversi saat user request)
"""

import io
import os
import tempfile

import duckdb

from db import get_supabase

BUCKET = "dafine-files"


def upload_clean_file(
    user_id: int,
    history_id: int,
    con: duckdb.DuckDBPyConnection,
) -> str:
    """
    Export cleaned_table → parquet → upload ke Supabase Storage.
    Returns storage path string.
    """
    sb   = get_supabase()
    path = f"{user_id}/{history_id}.parquet"

    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
        tmp = f.name

    try:
        con.execute(f"COPY cleaned_table TO '{tmp}' (FORMAT PARQUET)")
        with open(tmp, "rb") as f:
            sb.storage.from_(BUCKET).upload(
                path, f.read(),
                {"content-type": "application/octet-stream", "upsert": "true"},
            )
    finally:
        os.unlink(tmp)

    return path


def download_as_csv(storage_path: str, original_filename: str) -> bytes:
    """
    Download parquet dari Supabase Storage → convert ke CSV bytes.
    Returns raw CSV bytes yang bisa langsung dikirim ke user.
    """
    sb            = get_supabase()
    parquet_bytes = sb.storage.from_(BUCKET).download(storage_path)

    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
        f.write(parquet_bytes)
        tmp_path = f.name

    try:
        con     = duckdb.connect()
        csv_str = con.sql(f"SELECT * FROM read_parquet('{tmp_path}')").df().to_csv(index=False)
        return csv_str.encode("utf-8")
    finally:
        os.unlink(tmp_path)


def delete_clean_file(storage_path: str) -> None:
    """Hapus file dari Supabase Storage saat history didelete."""
    sb = get_supabase()
    sb.storage.from_(BUCKET).remove([storage_path])