import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

REPO = os.getenv("TARGET_REPO", "fastapi/fastapi")
GRAPH_NAME = os.getenv("GRAPH_NAME", REPO.replace("/", "__"))  # one isolated graph per repo/tenant
SRC_PREFIXES = [p for p in os.getenv("SRC_PREFIXES", "fastapi/").split(",") if p]
SRC_EXTS = tuple(os.getenv("SRC_EXTS", ".py").split(","))
MODULE_DEPTH = int(os.getenv("MODULE_DEPTH", "2"))
HALF_LIFE_DAYS = float(os.getenv("HALF_LIFE_DAYS", "365"))
ACTIVE_DAYS = int(os.getenv("ACTIVE_DAYS", "365"))
MAX_FILES_PER_COMMIT = 40  # ignore mass-reformat / mass-rename commits
DATA_FILE = Path(os.getenv("DATA_FILE", str(ROOT / "data" / "commits.jsonl.gz")))
OUT_DIR = ROOT / "out"

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-2.5-flash")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_ISSUE_REPO = os.getenv("GITHUB_ISSUE_REPO", "")
GITHUB_ISSUES_ENABLED = os.getenv("GITHUB_ISSUES_ENABLED", "").strip().lower() in {
    "1", "true", "yes"
}


def module_of(path: str) -> str:
    parts = path.split("/")
    d = parts[:-1]
    if len(d) <= 1:
        return (d[0] if d else "root") + "/(core)"
    return "/".join(d[:MODULE_DEPTH])


def get_graph(graph_name=None):
    if os.getenv("FALKORDB_EMBEDDED") == "1":  # test mode only (Linux/macOS)
        from redislite.falkordb_client import FalkorDB
        db = FalkorDB(str(ROOT / "data" / "embedded.db"))
    else:
        from falkordb import FalkorDB
        db = FalkorDB(host=os.getenv("FALKORDB_HOST", "localhost"),
                      port=int(os.getenv("FALKORDB_PORT", "6379")),
                      password=os.getenv("FALKORDB_PASSWORD") or None)
    return db.select_graph(graph_name or GRAPH_NAME)
