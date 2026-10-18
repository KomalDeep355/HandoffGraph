"""The agent's ACTION: turn what the graph shows into a hand-off plan (markdown file + optional GitHub issues)."""
import re, unicodedata, requests
from . import config as cfg


def _create_issues(issues):
    results = []
    for index, issue in enumerate(issues):
        try:
            response = requests.post(
                f"https://api.github.com/repos/{cfg.GITHUB_ISSUE_REPO}/issues",
                json=issue,
                timeout=20,
                headers={
                    "Authorization": f"Bearer {cfg.GITHUB_TOKEN}",
                    "Accept": "application/vnd.github+json"
                }
            )
        except requests.RequestException as error:
            results.append({
                "title": issue["title"],
                "status": "failed",
                "error": f"GitHub request failed: {error}"
            })
            results.extend({
                "title": remaining["title"],
                "status": "not_attempted",
                "error": "Stopped after a GitHub request failure."
            } for remaining in issues[index + 1:])
            break

        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        rate_limited = response.status_code == 429 or (
            response.status_code == 403 and (
                response.headers.get("X-RateLimit-Remaining") == "0"
                or "rate limit" in str(payload.get("message", "")).lower()
                or "secondary rate" in str(payload.get("message", "")).lower()
            )
        )
        if rate_limited:
            retry_after = response.headers.get("Retry-After")
            reset_at = response.headers.get("X-RateLimit-Reset")
            detail = "GitHub rate limit reached."
            if retry_after:
                detail += f" Retry after {retry_after} seconds."
            elif reset_at:
                detail += f" Rate limit reset timestamp: {reset_at}."
            results.append({"title": issue["title"], "status": "failed", "error": detail})
            results.extend({
                "title": remaining["title"],
                "status": "not_attempted",
                "error": "Stopped after GitHub rate limiting."
            } for remaining in issues[index + 1:])
            break

        if response.status_code != 201:
            message = payload.get("message")
            detail = f"GitHub returned HTTP {response.status_code}"
            if message:
                detail += f": {message}"
            results.append({"title": issue["title"], "status": "failed", "error": detail})
            continue

        url = payload.get("html_url")
        if not url:
            results.append({
                "title": issue["title"],
                "status": "failed",
                "error": "GitHub returned HTTP 201 without an issue URL."
            })
            continue
        results.append({"title": issue["title"], "status": "created", "url": url})
    return results


def plan_handoff(tools, person, open_issues=False):
    impact = tools.leave_impact(person)
    cypher = list(impact["cypher"])
    d = impact["data"]
    if "error" in d: return impact
    nodes, edges = list(impact["viz"]["nodes"]), list(impact["viz"]["edges"])
    tasks = []
    for m in [m for m in d["modules_hit"] if m["orphaned_files"] or m["knowledge_share"] >= 0.4][:3]:
        s = tools.find_successors(person, m["module"])
        cypher += s["cypher"]; nodes += s["viz"]["nodes"]; edges += s["viz"]["edges"]
        cands = s["data"].get("candidates", [])
        tasks.append({"module": m["module"], "share": m["knowledge_share"], "orphaned": m["orphaned_files"],
                      "of": m["files_in_module"], "files": m["examples"], "mentee": cands[0]["name"] if cands else None,
                      "backup": cands[1]["name"] if len(cands) > 1 else None})
    name = d["person"]["name"]
    md = [f"# Knowledge hand-off plan: {name}", "",
          f"Generated from the FalkorDB knowledge graph of `{tools.repo}`. "
          f"{d['orphaned_files_total']} files have no other active contributor with comparable knowledge.", ""]
    issues = []
    for t in tasks:
        md += [f"## {t['module']}  ({int(t['share']*100)}% of module knowledge, {t['orphaned']}/{t['of']} files orphaned)",
               f"- Suggested mentee: **{t['mentee']}**" + (f" (backup: {t['backup']})" if t["backup"] else ""),
               "- Pair-review these files first: " + (", ".join(f"`{f}`" for f in t["files"]) or "n/a"), ""]
        issues.append({"title": f"[hand-off] {t['module']}: pair {t['mentee']} with {name}",
                       "body": f"Graph evidence: {name} holds {int(t['share']*100)}% of recent knowledge in `{t['module']}`; "
                               f"{t['orphaned']} of {t['of']} files have no other active owner.\n\nStart with: "
                               + ", ".join(f"`{f}`" for f in t["files"]) + f"\n\nSuggested mentee: {t['mentee']}."})
    text = "\n".join(md)
    cfg.OUT_DIR.mkdir(exist_ok=True)
    out = cfg.OUT_DIR / f"handoff_{re.sub(r'[^a-z0-9]+', '_', unicodedata.normalize('NFKD', name).encode('ascii','ignore').decode().lower()).strip('_')}.md"
    out.write_text(text, encoding="utf-8")
    creation_requested = open_issues and cfg.GITHUB_ISSUES_ENABLED
    results = []
    failed = []
    created = []
    if creation_requested:
        if not cfg.GITHUB_TOKEN or not cfg.GITHUB_ISSUE_REPO:
            failed.append(
                "Issue creation requested, but GITHUB_TOKEN or "
                "GITHUB_ISSUE_REPO is missing."
            )
        else:
            results = _create_issues(issues)
            created = [
                result["url"] for result in results
                if result["status"] == "created"
            ]
            failed = [
                f"{result['title']}: {result['error']}"
                for result in results
                if result["status"] != "created"
            ]

    if created and not failed:
        status = "created"
    elif created:
        status = "partially_created"
    elif creation_requested:
        status = "failed"
    else:
        status = "dry_run"

    return {"data": {"person": name, "plan_file": str(out), "plan_markdown": text, "issues_status": status,
                     "issues": created or [issue["title"] for issue in issues],
                     "issues_failed": failed, "issue_results": results},
            "cypher": cypher, "viz": {"nodes": nodes, "edges": edges}}
