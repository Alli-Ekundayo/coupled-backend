"""Project & artifact persistence backed by Lakebase Postgres (+ local working dir for KiCad/routing)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import config, db as pgdb


class Project:
    def __init__(self, pid: str, owner_user_id: str | None = None):
        self.id = pid
        self.owner_user_id = owner_user_id
        self.dir = config.DATA_DIR / "projects" / pid
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, name: str) -> Path:
        return self.dir / name

    def _ensure_pg_project(self, conn) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO projects (id, owner_user_id, board_name, updated_at)
                VALUES (%s, %s::uuid, %s, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    owner_user_id = COALESCE(projects.owner_user_id, EXCLUDED.owner_user_id),
                    updated_at = NOW()
                """,
                (self.id, self.owner_user_id, self.id),
            )

    def get_access_info(self) -> tuple[bool, str | None]:
        """Return (exists, owner_user_id) in a single Postgres round-trip."""
        if pgdb.is_postgres_enabled():
            pool = pgdb.get_pool()
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT owner_user_id::text AS owner FROM projects WHERE id = %s",
                        (self.id,),
                    )
                    row = cur.fetchone()
                    if row is not None:
                        return True, row["owner"]
        return (self.dir.is_dir() and any(self.dir.iterdir())), None

    def exists(self) -> bool:
        ex, _ = self.get_access_info()
        return ex

    def get_owner_id(self) -> str | None:
        _, owner = self.get_access_info()
        return owner

    def can_access(self, user_id: str | None) -> bool:
        _, owner = self.get_access_info()
        if owner is None:
            return True
        return user_id is not None and owner == user_id

    def load_json_files(self, names: list[str]) -> dict[str, Any]:
        """Load multiple project JSON files in a single Postgres query (avoids N+1 round-trips)."""
        out: dict[str, Any] = {}
        if not names:
            return out
        if pgdb.is_postgres_enabled():
            pool = pgdb.get_pool()
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT name, content FROM project_files WHERE project_id = %s AND name = ANY(%s)",
                        (self.id, names),
                    )
                    for row in cur.fetchall():
                        out[row["name"]] = json.loads(row["content"])
        for n in names:
            if n not in out:
                p = self.path(n)
                if p.exists():
                    out[n] = json.loads(p.read_text(encoding="utf-8"))
        return out

    def save_text(self, name: str, content: str) -> Path:
        p = self.path(name)
        p.write_text(content, encoding="utf-8")
        if pgdb.is_postgres_enabled():
            pool = pgdb.get_pool()
            with pool.connection() as conn:
                self._ensure_pg_project(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO project_files (project_id, name, content, updated_at)
                        VALUES (%s, %s, %s, NOW())
                        ON CONFLICT (project_id, name) DO UPDATE SET
                            content = EXCLUDED.content,
                            updated_at = NOW()
                        """,
                        (self.id, name, content),
                    )
                    if name in ("board.kicad_pcb", "board_routed.kicad_pcb"):
                        cur.execute(
                            "UPDATE projects SET has_board = TRUE, updated_at = NOW() WHERE id = %s",
                            (self.id,),
                        )
                    elif name == "board.kicad_sch":
                        cur.execute(
                            "UPDATE projects SET has_schematic = TRUE, updated_at = NOW() WHERE id = %s",
                            (self.id,),
                        )
                conn.commit()
        return p

    def load_text(self, name: str) -> str | None:
        if pgdb.is_postgres_enabled():
            pool = pgdb.get_pool()
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT content FROM project_files WHERE project_id = %s AND name = %s",
                        (self.id, name),
                    )
                    row = cur.fetchone()
                    if row is not None:
                        return row["content"]
        p = self.path(name)
        return p.read_text(encoding="utf-8") if p.exists() else None

    def delete_file(self, name: str) -> None:
        self.path(name).unlink(missing_ok=True)
        if pgdb.is_postgres_enabled():
            pool = pgdb.get_pool()
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM project_files WHERE project_id = %s AND name = %s",
                        (self.id, name),
                    )
                conn.commit()

    def sync_from_disk(self, name: str) -> None:
        """Persist a file written directly to disk (e.g. by routing engine/worker) into Postgres."""
        p = self.path(name)
        if p.exists() and pgdb.is_postgres_enabled():
            self.save_text(name, p.read_text(encoding="utf-8", errors="replace"))

    def ensure_local_file(self, name: str) -> Path | None:
        """Ensure a project file exists on local disk, hydrating from Postgres if needed."""
        p = self.path(name)
        if p.exists():
            return p
        if pgdb.is_postgres_enabled():
            content = self.load_text(name)
            if content is not None:
                p.write_text(content, encoding="utf-8")
                return p
        return None

    def save_json(self, name: str, obj: Any) -> None:
        serialized = json.dumps(obj, indent=2)
        self.path(name).write_text(serialized, encoding="utf-8")
        if pgdb.is_postgres_enabled():
            pool = pgdb.get_pool()
            with pool.connection() as conn:
                self._ensure_pg_project(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO project_files (project_id, name, content, updated_at)
                        VALUES (%s, %s, %s, NOW())
                        ON CONFLICT (project_id, name) DO UPDATE SET
                            content = EXCLUDED.content,
                            updated_at = NOW()
                        """,
                        (self.id, name, serialized),
                    )
                    if name == "design_brief.json" and isinstance(obj, dict):
                        cur.execute(
                            """
                            UPDATE projects
                            SET board_name = %s, prompt = %s, updated_at = NOW()
                            WHERE id = %s
                            """,
                            (
                                str(obj.get("board_name") or self.id),
                                str(obj.get("prompt") or ""),
                                self.id,
                            ),
                        )
                    elif name == "parts.json" and isinstance(obj, dict):
                        cur.execute(
                            """
                            UPDATE projects
                            SET component_count = %s, updated_at = NOW()
                            WHERE id = %s
                            """,
                            (len(obj), self.id),
                        )
                    elif name == "drc.json" and isinstance(obj, dict):
                        cur.execute(
                            """
                            UPDATE projects
                            SET drc_passed = %s, updated_at = NOW()
                            WHERE id = %s
                            """,
                            (obj.get("passed"), self.id),
                        )
                conn.commit()

    def load_json(self, name: str, default=None):
        text = self.load_text(name)
        if text is None:
            return default
        return json.loads(text)

    def board(self) -> Path | None:
        for n in ("board_routed.kicad_pcb", "board.kicad_pcb"):
            p = self.ensure_local_file(n)
            if p is not None and p.exists():
                return p
        return None

    def delete_project(self) -> bool:
        deleted = False
        if pgdb.is_postgres_enabled():
            pool = pgdb.get_pool()
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM projects WHERE id = %s", (self.id,))
                    deleted = cur.rowcount > 0
                conn.commit()
        if self.dir.is_dir():
            for child in self.dir.iterdir():
                child.unlink(missing_ok=True)
            try:
                self.dir.rmdir()
                deleted = True
            except OSError:
                pass
        return deleted


def list_all_projects(owner_user_id: str | None = None) -> list[dict[str, Any]]:
    """List project summaries from Lakebase Postgres (lightweight columns only) or local disk fallback."""
    if pgdb.is_postgres_enabled():
        pool = pgdb.get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                if owner_user_id:
                    cur.execute(
                        """
                        SELECT id, owner_user_id::text AS owner_user_id, board_name, prompt,
                               component_count, has_board, has_schematic, drc_passed, updated_at
                        FROM projects
                        WHERE owner_user_id IS NULL OR owner_user_id = %s::uuid
                        ORDER BY updated_at DESC, id ASC
                        """,
                        (owner_user_id,),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, owner_user_id::text AS owner_user_id, board_name, prompt,
                               component_count, has_board, has_schematic, drc_passed, updated_at
                        FROM projects
                        ORDER BY updated_at DESC, id ASC
                        """
                    )
                rows = cur.fetchall()
        return [
            {
                "project_id": r["id"],
                "owner_user_id": r["owner_user_id"],
                "board_name": r["board_name"] or r["id"],
                "prompt": r["prompt"] or "",
                "component_count": int(r["component_count"] or 0),
                "has_board": bool(r["has_board"]),
                "has_schematic": bool(r["has_schematic"]),
                "drc_passed": r["drc_passed"],
                "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else None,
            }
            for r in rows
        ]

    root = config.DATA_DIR / "projects"
    if not root.is_dir():
        return []
    items = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        prj = Project(d.name)
        brief = prj.load_json("design_brief.json") or {}
        drc = prj.load_json("drc.json") or {}
        parts = prj.load_json("parts.json") or {}
        items.append(
            {
                "project_id": d.name,
                "owner_user_id": None,
                "board_name": brief.get("board_name", d.name),
                "prompt": brief.get("prompt", ""),
                "component_count": len(parts),
                "has_board": prj.board() is not None,
                "has_schematic": prj.path("board.kicad_sch").exists(),
                "drc_passed": drc.get("passed"),
                "updated_at": None,
            }
        )
    return items
