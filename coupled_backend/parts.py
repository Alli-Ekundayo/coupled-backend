"""Component store: SQL hard-filter + vector cosine rank.

Uses Neon Lakebase Postgres (via psycopg 3 pooled connection) when DATABASE_URL is configured,
and falls back to local SQLite + numpy cosine for offline/unit-test runs.
"""
from __future__ import annotations

import json
import re
import sqlite3
import zlib
from typing import Any, Mapping, Protocol

import numpy as np
from psycopg.types.json import Jsonb

from . import config, db as pgdb
from .models import Part, Pin

DIM = 512

_PART_COLS = (
    "mpn, lcsc_id, category, package, description, manufacturer, "
    "value, price, stock, voltage_max, footprint_ref, pins"
)


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> np.ndarray: ...


class HashEmbedder:
    """Deterministic hashed bag-of-words (+bigrams). No model download; good enough for seed-scale demos."""

    def embed(self, texts):
        out = np.zeros((len(texts), DIM), dtype=np.float32)
        for i, t in enumerate(texts):
            toks = re.findall(r"[a-z0-9.]+", t.lower())
            for a, b in zip(toks, toks[1:] + [""]):
                out[i, zlib.crc32(a.encode()) % DIM] += 1.0
                if b:
                    out[i, zlib.crc32((a + "_" + b).encode()) % DIM] += 0.5
            n = np.linalg.norm(out[i])
            if n:
                out[i] /= n
        return out


class STEmbedder:
    def __init__(self, name="sentence-transformers/all-MiniLM-L6-v2"):
        from sentence_transformers import SentenceTransformer

        self.m = SentenceTransformer(name)

    def embed(self, texts):
        return np.asarray(self.m.encode(texts, normalize_embeddings=True), dtype=np.float32)


def make_embedder(kind: str) -> Embedder:
    return STEmbedder() if kind == "st" else HashEmbedder()


SCHEMA = """CREATE TABLE IF NOT EXISTS parts(
 mpn TEXT PRIMARY KEY, lcsc_id TEXT, category TEXT, package TEXT, description TEXT, manufacturer TEXT,
 value TEXT, price REAL, stock INTEGER, voltage_max REAL, footprint_ref TEXT, pins TEXT, emb BLOB)"""


def _doc(p: Part) -> str:
    return f"{p.category} {p.value} {p.package} {p.description} {p.mpn} {p.manufacturer}"


def _row_to_part(r: Mapping[str, Any], score: float | None = None) -> Part:
    raw_pins = r["pins"]
    pins_list = json.loads(raw_pins) if isinstance(raw_pins, str) else (raw_pins or [])
    return Part(
        mpn=r["mpn"],
        lcsc_id=r["lcsc_id"] or "",
        category=r["category"] or "",
        package=r["package"] or "",
        description=r["description"] or "",
        manufacturer=r["manufacturer"] or "",
        value=r["value"] or "",
        price=r["price"] or 0.0,
        stock=r["stock"] or 0,
        voltage_max=r["voltage_max"],
        footprint_ref=r["footprint_ref"] or "",
        pins=[Pin(**x) for x in pins_list],
        score=score,
    )


class PartsDB:
    """SQLite-backed component store (offline fallback when DATABASE_URL is unset)."""

    def __init__(self, path, embedder: Embedder):
        self.con = sqlite3.connect(str(path), check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.execute(SCHEMA)
        self.emb = embedder

    def count(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM parts").fetchone()[0]

    def upsert(self, parts: list[Part]):
        if not parts:
            return
        vecs = self.emb.embed([_doc(p) for p in parts])
        rows = [
            (
                p.mpn,
                p.lcsc_id,
                p.category,
                p.package,
                p.description,
                p.manufacturer,
                p.value,
                p.price,
                p.stock,
                p.voltage_max,
                p.footprint_ref,
                json.dumps([x.model_dump() for x in p.pins]),
                v.tobytes(),
            )
            for p, v in zip(parts, vecs)
        ]
        self.con.executemany("INSERT OR REPLACE INTO parts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.con.commit()

    @staticmethod
    def _part(r: sqlite3.Row, score=None) -> Part:
        return _row_to_part(r, score=score)

    def get_exact_ci(self, mpn: str) -> Part | None:
        r = self.con.execute(
            f"SELECT {_PART_COLS} FROM parts WHERE LOWER(mpn)=LOWER(?)",
            (mpn,),
        ).fetchone()
        return self._part(r) if r else None

    def get(self, mpn: str) -> Part | None:
        r = self.con.execute(f"SELECT {_PART_COLS} FROM parts WHERE mpn=?", (mpn,)).fetchone()
        if r:
            return self._part(r)
        from . import octopart

        if octopart.available():
            p = octopart.get_octopart_part(mpn, local_db=None)
            if p:
                if p.pins and p.footprint_ref:
                    try:
                        self.upsert([p])
                    except Exception:
                        pass
                return p
        return None

    def search(
        self,
        query: str,
        filters: dict[str, Any] | None = None,
        k: int = 5,
        require_pins: bool = True,
        engine: str = "auto",
    ) -> list[Part]:
        from . import octopart

        if engine == "octopart" and octopart.available():
            octo_parts = octopart.search_octopart(query, filters, limit=k, local_db=self)
            valid = [p for p in octo_parts if (not require_pins or (p.pins and p.footprint_ref))]
            if valid:
                try:
                    self.upsert(valid)
                except Exception:
                    pass
            return valid

        f, where, args = filters or {}, [], []
        if require_pins:
            where.append("pins != '[]' AND footprint_ref != ''")
        if f.get("in_stock"):
            where.append("stock >= 5")
        if f.get("v_in_min") is not None:
            where.append("voltage_max >= ?")
            args.append(float(f["v_in_min"]))
        if f.get("package"):
            pk = f["package"] if isinstance(f["package"], list) else [f["package"]]
            where.append("package IN (%s)" % ",".join("?" * len(pk)))
            args += pk
        if f.get("category"):
            where.append("category LIKE ?")
            args.append(f"%{f['category']}%")
        if f.get("value"):
            where.append("LOWER(value)=LOWER(?)")
            args.append(str(f["value"]))

        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        if not query.strip():
            sql = f"SELECT {_PART_COLS} FROM parts{where_sql} ORDER BY stock DESC LIMIT ?"
            rows = self.con.execute(sql, [*args, k]).fetchall()
            return [self._part(r) for r in rows]

        sql = f"SELECT {_PART_COLS}, emb FROM parts{where_sql} LIMIT 5000"
        rows = self.con.execute(sql, args).fetchall()
        if not rows:
            if engine == "auto" and octopart.available():
                octo_parts = octopart.search_octopart(query, filters, limit=k, local_db=self)
                valid = [p for p in octo_parts if (not require_pins or (p.pins and p.footprint_ref))]
                if valid:
                    try:
                        self.upsert(valid)
                    except Exception:
                        pass
                return valid
            return []
        M = np.vstack([np.frombuffer(r["emb"], dtype=np.float32) for r in rows])
        sims = M @ self.emb.embed([query])[0]
        idx = np.argsort(-sims)[:k]
        return [self._part(rows[i], float(sims[i])) for i in idx]


class PostgresPartsDB:
    """Lakebase Postgres component store using Neon's pooled DATABASE_URL via psycopg 3."""

    def __init__(self, embedder: Embedder):
        pgdb.init_db()
        self.emb = embedder

    def count(self) -> int:
        pool = pgdb.get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS cnt FROM parts")
                row = cur.fetchone()
                return int(row["cnt"]) if row else 0

    def upsert(self, parts: list[Part]):
        if not parts:
            return
        vecs = self.emb.embed([_doc(p) for p in parts])
        rows = [
            (
                p.mpn,
                p.lcsc_id,
                p.category,
                p.package,
                p.description,
                p.manufacturer,
                p.value,
                p.price,
                p.stock,
                p.voltage_max,
                p.footprint_ref,
                Jsonb([x.model_dump() for x in p.pins]),
                v.tobytes(),
            )
            for p, v in zip(parts, vecs)
        ]
        sql = """
            INSERT INTO parts (
                mpn, lcsc_id, category, package, description, manufacturer,
                value, price, stock, voltage_max, footprint_ref, pins, emb, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (mpn) DO UPDATE SET
                lcsc_id = EXCLUDED.lcsc_id,
                category = EXCLUDED.category,
                package = EXCLUDED.package,
                description = EXCLUDED.description,
                manufacturer = EXCLUDED.manufacturer,
                value = EXCLUDED.value,
                price = EXCLUDED.price,
                stock = EXCLUDED.stock,
                voltage_max = EXCLUDED.voltage_max,
                footprint_ref = EXCLUDED.footprint_ref,
                pins = EXCLUDED.pins,
                emb = EXCLUDED.emb,
                updated_at = NOW()
        """
        pool = pgdb.get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
            conn.commit()

    @staticmethod
    def _part(r: Mapping[str, Any], score=None) -> Part:
        return _row_to_part(r, score=score)

    def get_exact_ci(self, mpn: str) -> Part | None:
        pool = pgdb.get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_PART_COLS} FROM parts WHERE LOWER(mpn) = LOWER(%s) LIMIT 1",
                    (mpn,),
                )
                row = cur.fetchone()
        return self._part(row) if row else None

    def get(self, mpn: str) -> Part | None:
        pool = pgdb.get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {_PART_COLS} FROM parts WHERE mpn = %s", (mpn,))
                row = cur.fetchone()
        if row:
            return self._part(row)

        from . import octopart

        if octopart.available():
            p = octopart.get_octopart_part(mpn, local_db=None)
            if p:
                if p.pins and p.footprint_ref:
                    try:
                        self.upsert([p])
                    except Exception:
                        pass
                return p
        return None

    def search(
        self,
        query: str,
        filters: dict[str, Any] | None = None,
        k: int = 5,
        require_pins: bool = True,
        engine: str = "auto",
    ) -> list[Part]:
        from . import octopart

        if engine == "octopart" and octopart.available():
            octo_parts = octopart.search_octopart(query, filters, limit=k, local_db=self)
            valid = [p for p in octo_parts if (not require_pins or (p.pins and p.footprint_ref))]
            if valid:
                try:
                    self.upsert(valid)
                except Exception:
                    pass
            return valid

        f, where, args = filters or {}, [], []
        if require_pins:
            where.append("jsonb_array_length(pins) > 0 AND footprint_ref <> ''")
        if f.get("in_stock"):
            where.append("stock >= 5")
        if f.get("v_in_min") is not None:
            where.append("voltage_max >= %s")
            args.append(float(f["v_in_min"]))
        if f.get("package"):
            pk = f["package"] if isinstance(f["package"], list) else [f["package"]]
            where.append("package = ANY(%s)")
            args.append(pk)
        if f.get("category"):
            where.append("category ILIKE %s")
            args.append(f"%{f['category']}%")
        if f.get("value"):
            where.append("LOWER(value) = LOWER(%s)")
            args.append(str(f["value"]))

        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        pool = pgdb.get_pool()

        if not query.strip():
            # Omit BYTEA emb column on browse queries to minimize egress
            sql = f"SELECT {_PART_COLS} FROM parts{where_sql} ORDER BY stock DESC LIMIT %s"
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, [*args, k])
                    rows = cur.fetchall()
            return [self._part(r) for r in rows]

        sql = f"SELECT {_PART_COLS}, emb FROM parts{where_sql} LIMIT 5000"
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                rows = cur.fetchall()

        if not rows:
            if engine == "auto" and octopart.available():
                octo_parts = octopart.search_octopart(query, filters, limit=k, local_db=self)
                valid = [p for p in octo_parts if (not require_pins or (p.pins and p.footprint_ref))]
                if valid:
                    try:
                        self.upsert(valid)
                    except Exception:
                        pass
                return valid
            return []

        M = np.vstack([np.frombuffer(r["emb"], dtype=np.float32) for r in rows])
        sims = M @ self.emb.embed([query])[0]
        idx = np.argsort(-sims)[:k]
        return [self._part(rows[i], float(sims[i])) for i in idx]


def create_parts_db(embedder: Embedder | None = None) -> PartsDB | PostgresPartsDB:
    """Instantiate Lakebase Postgres PartsDB when DATABASE_URL is set, else SQLite fallback."""
    emb = embedder or make_embedder(config.EMBEDDER)
    if pgdb.is_postgres_enabled():
        return PostgresPartsDB(emb)
    return PartsDB(config.DB_PATH, emb)
