"""git history -> data/commits.jsonl.gz   (usage: python -m busfactor.extract --clone)"""
import argparse, gzip, hashlib, json, re, subprocess
from pathlib import Path
from . import config as cfg

BOT = re.compile(r"\[bot\]|dependabot|pre-commit-ci|github-actions|latest-changes|pr-submit", re.I)


def run(repo_dir: Path, source_prefixes=None, timeout=None):
    cmd = ["git", "-C", str(repo_dir), "log", "--no-merges", "--no-renames",
           "--pretty=format:C|%H|%an|%ae|%at", "--name-only"]
    out = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=True, timeout=timeout
    ).stdout
    prefixes = cfg.SRC_PREFIXES if source_prefixes is None else source_prefixes
    commits, cur = [], None
    for line in out.splitlines():
        if line.startswith("C|"):
            if cur: commits.append(cur)
            _, h, name, email, ts = line.split("|", 4)
            cur = None if BOT.search(name) or BOT.search(email) else {
                "h": h[:10], "n": name, "id": hashlib.sha1(email.lower().encode()).hexdigest()[:10],
                "t": int(ts), "f": []}
        elif line.strip() and cur is not None:
            p = line.strip()
            if p.endswith(cfg.SRC_EXTS) and (
                not prefixes or any(p.startswith(prefix) for prefix in prefixes)
            ):
                cur["f"].append(p)
    if cur: commits.append(cur)
    return [c for c in commits if c["f"] and len(c["f"]) <= cfg.MAX_FILES_PER_COMMIT]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clone", action="store_true", help="git clone the target repo into .cache/repo")
    ap.add_argument("--repo-dir", default=str(cfg.ROOT / ".cache" / "repo"))
    a = ap.parse_args()
    rd = Path(a.repo_dir)
    if a.clone and not rd.exists():
        rd.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", f"https://github.com/{cfg.REPO}.git", str(rd)], check=True)
    commits = run(rd)
    cfg.DATA_FILE.parent.mkdir(exist_ok=True)
    with gzip.open(cfg.DATA_FILE, "wt", encoding="utf-8") as f:
        for c in commits: f.write(json.dumps(c) + "\n")
    print(f"wrote {len(commits)} commits -> {cfg.DATA_FILE}")


if __name__ == "__main__":
    main()
