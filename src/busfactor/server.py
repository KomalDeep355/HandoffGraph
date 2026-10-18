import gzip
import hashlib
import json
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config as cfg
from . import extract, ingest
from .agent import Agent

app = FastAPI(title="BusFactor Graph")
_agents = {}
_agents_lock = threading.Lock()
_graph_locks = {}
_graph_locks_lock = threading.Lock()


def _graph_lock(graph_name):
    with _graph_locks_lock:
        return _graph_locks.setdefault(graph_name, threading.Lock())


def agent(graph_name=None):
    name = graph_name or cfg.GRAPH_NAME
    if name != cfg.GRAPH_NAME and not re.fullmatch(r"repo_[0-9a-f]{16}", name):
        raise HTTPException(status_code=400, detail="Unknown graph name.")
    with _agents_lock:
        if name not in _agents:
            try:
                _agents[name] = Agent(graph_name=name)
            except RuntimeError as exc:
                raise HTTPException(status_code=404, detail="Repository graph is not loaded.") from exc
        return _agents[name]


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=1200)


class Ask(BaseModel):
    question: str = Field(..., min_length=1, max_length=1200)
    open_issues: bool = False
    graph_name: str | None = None
    history: list[HistoryMessage] = Field(default_factory=list, max_length=12)


class RepositoryRequest(BaseModel):
    repo_url: str = Field(..., min_length=1, max_length=500)


def _github_repository(repo_url):
    parsed = urlsplit(repo_url.strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="The GitHub URL has an invalid port.") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(status_code=400, detail="Enter an HTTPS URL from github.com.")

    parts = parsed.path.strip("/").split("/")
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="Use a repository URL like https://github.com/owner/repo.")
    owner, repository = parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    valid_segment = re.compile(r"[A-Za-z0-9_.-]{1,100}")
    if (
        not valid_segment.fullmatch(owner)
        or not valid_segment.fullmatch(repository)
        or owner in {".", ".."}
        or repository in {".", ".."}
    ):
        raise HTTPException(status_code=400, detail="The GitHub owner or repository name is invalid.")

    repo = f"{owner}/{repository}"
    graph_suffix = hashlib.sha256(repo.casefold().encode("utf-8")).hexdigest()[:16]
    return repo, f"repo_{graph_suffix}"


@app.get("/api/meta")
def meta(graph_name: str | None = None):
    name = graph_name or cfg.GRAPH_NAME
    with _graph_lock(name):
        a = agent(name)
        return {**a.tools.meta(), "mode": "llm" if a.llm else "offline"}


@app.post("/api/ask")
def ask(body: Ask):
    name = body.graph_name or cfg.GRAPH_NAME
    with _graph_lock(name):
        return agent(name).ask(
            body.question,
            body.open_issues,
            [{"role": message.role, "content": message.content} for message in body.history],
        )


@app.post("/api/repositories")
def load_repository(body: RepositoryRequest):
    repo, graph_name = _github_repository(body.repo_url)
    with _graph_lock(graph_name):
        try:
            with tempfile.TemporaryDirectory(prefix="handoffgraph-") as temporary_dir:
                checkout = Path(temporary_dir) / "repository"
                subprocess.run(
                    [
                        "git", "clone", "--depth=500", "--no-tags",
                        "--single-branch", "--filter=blob:none",
                        f"https://github.com/{repo}.git", str(checkout),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                commits = extract.run(checkout, source_prefixes=[], timeout=60)
                if not commits:
                    raise HTTPException(
                        status_code=422,
                        detail="No Python source-file commits were found in the recent repository history.",
                    )

                dataset = Path(temporary_dir) / "commits.jsonl.gz"
                with gzip.open(dataset, "wt", encoding="utf-8") as output:
                    for commit in commits:
                        output.write(json.dumps(commit) + "\n")

                stats = ingest.ingest(dataset, graph_name=graph_name, repo=repo)
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(status_code=504, detail="Repository loading timed out.") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "Git command failed.").strip()[-500:]
            raise HTTPException(status_code=422, detail=f"Could not load repository: {detail}") from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=503, detail="Git is not installed or is unavailable.") from exc

        with _agents_lock:
            _agents.pop(graph_name, None)
        loaded_agent = agent(graph_name)
        return {
            "repo": loaded_agent.tools.repo,
            "graph_name": graph_name,
            **stats,
        }


app.mount("/", StaticFiles(directory=cfg.ROOT / "web", html=True), name="web")
