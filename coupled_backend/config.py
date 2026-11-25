import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
DB_PATH = Path(os.getenv("PARTS_DB", str(DATA_DIR / "parts.sqlite")))
WORKER_URL = os.getenv("WORKER_URL", "").rstrip("/")          # empty -> run router in-process (dev mode)
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-2.5-flash")
LLM_FALLBACK_MODELS = [m.strip() for m in os.getenv("LLM_FALLBACK_MODELS", "gemini-2.5-flash-lite,gemini-3.1-flash-lite,gemini-flash-latest").split(",") if m.strip()]
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
NEXAR_ACCESS_TOKEN = os.getenv("NEXAR_ACCESS_TOKEN", "")
NEXAR_GRAPHQL_URL = os.getenv("NEXAR_GRAPHQL_URL", "https://api.nexar.com/graphql")
PARTS_SEARCH_ENGINE = os.getenv("PARTS_SEARCH_ENGINE", "auto") # auto | local | octopart
EMBEDDER = os.getenv("EMBEDDER", "hash")                       # hash | st (sentence-transformers)
ROUTER_ENGINE = os.getenv("ROUTER_ENGINE", "auto")             # auto | pcbworld | mock
CHECKPOINT_DIR = Path(os.getenv("CHECKPOINT_DIR", "var/checkpoints/pcbworld"))
PCBWORLD_REPO = Path(os.getenv("PCBWORLD_REPO", "/opt/PCBWorld"))
KICAD_CLI = os.getenv("KICAD_CLI", "kicad-cli")
KICAD_FOOTPRINTS_DIR = Path(os.getenv("KICAD_FOOTPRINTS_DIR", "/usr/share/kicad/footprints"))
KICAD_SYMBOLS_DIR = Path(os.getenv("KICAD_SYMBOLS_DIR", "/usr/share/kicad/symbols"))
FREEROUTING_JAR = os.getenv("FREEROUTING_JAR", "/opt/freerouting.jar")
MAX_NETS = int(os.getenv("MAX_NETS", "64"))                    # PCBWorld checkpoint slot cap
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()           # Neon Lakebase Postgres pooled (-pooler) URL
DATABASE_URL_UNPOOLED = os.getenv("DATABASE_URL_UNPOOLED", "").strip()  # Direct URL for DDL/migrations
NEON_BRANCH = os.getenv("NEON_BRANCH", "").strip()
NEON_BRANCH_ID = os.getenv("NEON_BRANCH_ID", "").strip()
NEON_AUTH_BASE_URL = os.getenv("NEON_AUTH_BASE_URL", "").strip().rstrip("/")
NEON_AUTH_JWKS_URL = os.getenv("NEON_AUTH_JWKS_URL", "").strip() or (
    f"{NEON_AUTH_BASE_URL}/.well-known/jwks.json" if NEON_AUTH_BASE_URL else ""
)
AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "true").strip().lower() in ("1", "true", "yes")
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()] or ["*"]
DATA_DIR.mkdir(parents=True, exist_ok=True)
