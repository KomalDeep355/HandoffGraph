"""data/commits.jsonl.gz -> FalkorDB graph.   usage: python -m busfactor.ingest"""
import gzip, json, itertools
from collections import defaultdict
from . import config as cfg


def batches(rows, n=400):
    for i in range(0, len(rows), n):
        yield rows[i:i + n]


def ingest(data_file=None, graph_name=None, repo=None):
    data_file = data_file or cfg.DATA_FILE
    graph_name = graph_name or cfg.GRAPH_NAME
    commits = [json.loads(line) for line in gzip.open(data_file, "rt", encoding="utf-8") if line.strip()]
    if not commits:
        raise ValueError("No commits with tracked source files were found.")
    now = max(c["t"] for c in commits)
    hl = cfg.HALF_LIFE_DAYS * 86400
    active_cut = now - cfg.ACTIVE_DAYS * 86400

    contrib, cochange = {}, defaultdict(int)
    edge = defaultdict(lambda: [0, 0.0, 0])   # (cid, path) -> [commits, score, last_ts]
    fstat = defaultdict(lambda: [0, 0.0])     # path -> [commits, score]
    for c in sorted(commits, key=lambda c: c["t"]):
        w = 0.5 ** ((now - c["t"]) / hl)       # recent work counts more than old work
        d = contrib.setdefault(c["id"], {"name": c["n"], "first": c["t"], "last": c["t"], "commits": 0})
        d["name"], d["last"], d["commits"] = c["n"], c["t"], d["commits"] + 1
        files = sorted(set(c["f"]))
        for p in files:
            e = edge[(c["id"], p)]; e[0] += 1; e[1] += w; e[2] = c["t"]
            s = fstat[p]; s[0] += 1; s[1] += w
        if 2 <= len(files) <= 15:
            for a, b in itertools.combinations(files, 2): cochange[(a, b)] += 1

    modules = sorted({cfg.module_of(p) for p in fstat})
    knows = defaultdict(float)
    for (cid, p), e in edge.items(): knows[(cid, cfg.module_of(p))] += e[1]

    by_file = defaultdict(list)   # regular contributors sharing a file -> COLLABORATES (used by PageRank)
    for (cid, p), e in edge.items():
        if e[0] >= 2 and contrib[cid]["commits"] >= 3: by_file[p].append(cid)
    collab = defaultdict(int)
    for p, ids in by_file.items():
        for a, b in itertools.permutations(ids, 2): collab[(a, b)] += 1

    g = cfg.get_graph(graph_name)
    if g.query("MATCH (n) RETURN n LIMIT 1").result_set:
        g.delete()
        g = cfg.get_graph(graph_name)
    for q in ["CREATE INDEX FOR (c:Contributor) ON (c.id)", "CREATE INDEX FOR (f:File) ON (f.path)",
              "CREATE INDEX FOR (m:Module) ON (m.name)"]:
        try: g.query(q)
        except Exception: pass

    g.query("CREATE (:Meta {repo:$r, now:$n, active_days:$a, half_life:$h})",
            {"r": repo or cfg.REPO, "n": now, "a": cfg.ACTIVE_DAYS, "h": cfg.HALF_LIFE_DAYS})
    rows = [{"id": i, "name": d["name"], "first": d["first"], "last": d["last"], "commits": d["commits"],
             "active": d["last"] >= active_cut} for i, d in contrib.items()]
    for b in batches(rows): g.query("UNWIND $rows AS r CREATE (:Contributor {id:r.id,name:r.name,first_ts:r.first,last_ts:r.last,commits:r.commits,active:r.active})", {"rows": b})
    rows = [{"path": p, "m": cfg.module_of(p), "n": s[0], "s": s[1]} for p, s in fstat.items()]
    for b in batches(rows): g.query("UNWIND $rows AS r CREATE (:File {path:r.path,module:r.m,commits:r.n,score:r.s})", {"rows": b})
    g.query("UNWIND $rows AS r CREATE (:Module {name:r.name})", {"rows": [{"name": m} for m in modules]})
    g.query("MATCH (f:File),(m:Module {name:f.module}) CREATE (f)-[:IN_MODULE]->(m)")
    rows = [{"c": c, "f": p, "n": e[0], "s": e[1], "t": e[2]} for (c, p), e in edge.items()]
    for b in batches(rows): g.query("UNWIND $rows AS r MATCH (c:Contributor {id:r.c}),(f:File {path:r.f}) CREATE (c)-[:TOUCHED {commits:r.n,score:r.s,last_ts:r.t}]->(f)", {"rows": b})
    rows = [{"c": c, "m": m, "s": s} for (c, m), s in knows.items()]
    for b in batches(rows): g.query("UNWIND $rows AS r MATCH (c:Contributor {id:r.c}),(m:Module {name:r.m}) CREATE (c)-[:KNOWS {score:r.s}]->(m)", {"rows": b})
    rows = [{"a": a, "b": b, "n": n} for (a, b), n in cochange.items() if n >= 2]
    for b in batches(rows): g.query("UNWIND $rows AS r MATCH (a:File {path:r.a}),(b:File {path:r.b}) CREATE (a)-[:CO_CHANGED {n:r.n}]->(b)", {"rows": b})
    n_co = len(rows)
    rows = [{"a": a, "b": b, "w": w} for (a, b), w in collab.items()]
    for b in batches(rows): g.query("UNWIND $rows AS r MATCH (a:Contributor {id:r.a}),(b:Contributor {id:r.b}) CREATE (a)-[:COLLABORATES {w:r.w}]->(b)", {"rows": b})
    print(f"graph '{graph_name}': {len(contrib)} contributors, {len(fstat)} files, {len(modules)} modules, "
          f"{len(commits)} commits, {n_co} co-change edges, {len(rows)} collaboration edges")
    return {"commits": len(commits), "contributors": len(contrib), "files": len(fstat), "modules": len(modules)}


def main():
    ingest()


if __name__ == "__main__":
    main()
