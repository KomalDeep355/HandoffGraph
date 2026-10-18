"""Graph tools the agent can call. Every tool runs Cypher against FalkorDB and returns:
   {"data": <what the LLM sees>, "cypher": [<queries used>], "viz": {"nodes": [...], "edges": [...]}}"""
from collections import defaultdict
from math import isfinite
from . import config as cfg


def _node(nid, label, group, **kw):
    return {"id": nid, "label": label, "group": group, **kw}


class GraphTools:
    def __init__(self, graph=None, graph_name=None):
        self.graph_name = graph_name or cfg.GRAPH_NAME
        self.g = graph or cfg.get_graph(self.graph_name)
        r = self.g.query("MATCH (m:Meta) RETURN m.repo, m.now, m.active_days").result_set
        if not r:
            raise RuntimeError("Graph is empty - run: python -m busfactor.ingest")
        self.repo, self.now, self.active_days = r[0]

    # ---------- helpers ----------
    def _q(self, cypher, params=None, log=None):
        if log is not None: log.append(cypher.strip())
        return self.g.query(cypher, params or {}).result_set

    def resolve(self, name, log):
        query = name.strip()
        if not query:
            return None, []

        id_rows = self._q(
            """MATCH (c:Contributor)
            WHERE c.id = $q
            RETURN c.id, c.name, c.commits, c.active LIMIT 7""",
            {"q": query},
            log
        )
        if id_rows:
            row = id_rows[0]
            return {"id": row[0], "name": row[1], "commits": row[2], "active": row[3]}, []

        exact_rows = self._q(
            """MATCH (c:Contributor)
            WHERE toLower(c.name) = toLower($q)
            RETURN c.id, c.name, c.commits, c.active LIMIT 7""",
            {"q": query},
            log
        )
        if exact_rows:
            if len(exact_rows) > 1:
                return None, [f"{row[1]} ({row[0]})" for row in exact_rows]
            row = exact_rows[0]
            return {"id": row[0], "name": row[1], "commits": row[2], "active": row[3]}, []

        rows = self._q(
            """MATCH (c:Contributor)
            WHERE toLower(c.name) CONTAINS toLower($q)
            RETURN c.id, c.name, c.commits, c.active
            ORDER BY c.commits DESC LIMIT 7""",
            {"q": query},
            log
        )
        if len(rows) == 1:
            row = rows[0]
            return {"id": row[0], "name": row[1], "commits": row[2], "active": row[3]}, []
        if len(rows) > 1:
            return None, [f"{row[1]} ({row[0]})" for row in rows]
        return None, []

    @staticmethod
    def _resolution_error(name, matches):
        if matches:
            return f"Ambiguous contributor '{name}'. Matches: {', '.join(matches)}. Provide an exact name or contributor ID."
        return f"No contributor matching '{name}'"

    # ---------- tool 1: which modules are fragile ----------
    def module_risk(self, top_n=7):
        log = []
        rows = self._q("""MATCH (m:Module)<-[k:KNOWS]-(c:Contributor)
            WITH m, collect({id:c.id, name:c.name, score:k.score, active:c.active}) AS owners
            RETURN m.name, owners""", log=log)
        out = []
        for name, owners in rows:
            weighted_owners = []
            for owner in owners or []:
                try:
                    score = float(owner.get("score", 0))
                except (TypeError, ValueError):
                    continue
                if isfinite(score) and score > 0:
                    weighted_owners.append({**owner, "score": score})
            total = sum(owner["score"] for owner in weighted_owners)
            if not weighted_owners or total is None or total <= 0:
                continue

            owners = sorted(weighted_owners, key=lambda owner: -owner["score"])
            cum, bf = 0, 0
            for o in owners:
                cum += o["score"]; bf += 1
                if cum >= 0.5 * total: break
            inactive = sum(o["score"] for o in owners if not o["active"]) / total
            top = owners[0]
            share = top["score"] / total
            risk = round(0.5 * share + 0.5 * inactive, 2)
            level = "HIGH" if (bf == 1 and share >= 0.5) or inactive >= 0.5 else "MEDIUM" if bf <= 2 or inactive >= 0.3 else "LOW"
            out.append({"module": name, "bus_factor": bf, "owner_count": len(owners),
                        "total_knowledge_score": round(total, 4),
                        "top_owner": top["name"], "top_owner_id": top["id"],
                        "top_owner_share": round(share, 2), "top_owner_active": top["active"],
                        "inactive_knowledge_share": round(inactive, 2), "risk": level, "risk_score": risk})
        out.sort(key=lambda r: -r["risk_score"])
        out = out[:top_n]
        nodes, edges = [], []
        for r in out:
            nodes += [_node("m:" + r["module"], r["module"], "module"), _node("c:" + r["top_owner_id"], r["top_owner"], "person")]
            edges.append({"from": "c:" + r["top_owner_id"], "to": "m:" + r["module"], "label": f"{int(r['top_owner_share']*100)}%"})
        return {"data": {
                    "modules": out,
                    "risk_score_note": "Heuristic score from 0 to 1; not a probability or validated forecast."
                }, "cypher": log, "viz": {"nodes": nodes, "edges": edges}}

    # ---------- tool 2: what is lost if a person leaves ----------
    def leave_impact(self, person):
        log = []
        p, alts = self.resolve(person, log)
        if not p: return {"data": {"error": self._resolution_error(person, alts)}, "cypher": log, "viz": {"nodes": [], "edges": []}}
        orphans = self._q("""MATCH (p:Contributor {id:$id})-[t:TOUCHED]->(f:File)
            OPTIONAL MATCH (f)<-[o:TOUCHED]-(q:Contributor) WHERE q.id <> $id AND q.active = true
            WITH f, t.score AS mine, coalesce(sum(o.score), 0) AS others, count(q) AS active_peers
            WHERE mine >= 0.75 AND others <= 0.25 * mine
            RETURN f.path, f.module, mine, others, active_peers ORDER BY mine DESC""", {"id": p["id"]}, log)
        totals = dict(self._q("MATCH (f:File) RETURN f.module, count(f)", log=log))
        share = self._q("""MATCH (p:Contributor {id:$id})-[k:KNOWS]->(m:Module)
            OPTIONAL MATCH (other:Contributor)-[k2:KNOWS]->(m)
            WHERE other.id <> $id
            WITH m, k.score AS mine, coalesce(sum(k2.score), 0) AS others
            RETURN m.name, mine, mine + others AS total""", {"id": p["id"]}, log)
        by_mod = defaultdict(list)
        for path, mod, mine, others, peers in orphans: by_mod[mod].append((path, round(mine, 1), round(others, 1), peers))
        mods = []
        for m, mine, total in share:
            if total is None or total <= 0:
                continue
            lst = by_mod.get(m, [])
            if not lst and mine / total < 0.15: continue
            mods.append({"module": m, "knowledge_share": round(mine / total, 2), "orphaned_files": len(lst),
                         "files_in_module": totals.get(m, 0), "examples": [x[0] for x in lst[:4]]})
        mods.sort(key=lambda r: (-r["orphaned_files"], -r["knowledge_share"]))
        nodes = [_node("c:" + p["id"], p["name"], "person")]; edges = []
        for r in mods[:5]:
            nodes.append(_node("m:" + r["module"], r["module"], "module"))
            edges.append({"from": "c:" + p["id"], "to": "m:" + r["module"], "label": f"{int(r['knowledge_share']*100)}% of knowledge"})
            for f in r["examples"][:2]:
                nodes.append(_node("f:" + f, f.split("/")[-1], "orphan_file"))
                edges.append({"from": "c:" + p["id"], "to": "f:" + f, "label": "only active owner"})
                edges.append({"from": "f:" + f, "to": "m:" + r["module"], "label": ""})
        return {"data": {"person": p, "similar_names": alts, "orphaned_files_total": len(orphans), "modules_hit": mods[:6]},
                "cypher": log, "viz": {"nodes": nodes, "edges": edges}}

    # ---------- tool 3: who should inherit a module ----------
    def find_successors(self, person, module):
        log = []
        p, matches = self.resolve(person, log)
        if not p:
            return {"data": {"error": self._resolution_error(person, matches)}, "cypher": log, "viz": {"nodes": [], "edges": []}}
        direct = self._q("""MATCH (q:Contributor)-[o:TOUCHED]->(f:File {module:$m})
            WHERE q.id <> $id AND q.active = true
            RETURN q.id, q.name, count(DISTINCT f), coalesce(sum(o.score), 0),
                   q.last_ts, max(o.last_ts)""", {"id": p["id"], "m": module}, log)
        prox = self._q("""MATCH (p:Contributor {id:$id})-[:TOUCHED]->(f:File {module:$m})-[c:CO_CHANGED]-(g:File)<-[o:TOUCHED]-(q:Contributor)
            WHERE q.id <> $id AND q.active = true
            RETURN q.id, q.name, sum(c.n), count(DISTINCT g),
                   q.last_ts, max(o.last_ts)""", {"id": p["id"], "m": module}, log)
        cand = defaultdict(lambda: {
            "files": 0, "direct_score": 0.0, "cochange_strength": 0,
            "cochanged_files": 0, "last_activity_ts": 0, "last_module_activity_ts": 0
        })
        for i, n, files, score, last_activity, last_module_activity in direct:
            cand[(i, n)].update(
                files=files,
                direct_score=score or 0,
                last_activity_ts=last_activity or 0,
                last_module_activity_ts=last_module_activity or 0
            )
        for i, n, strength, files, last_activity, last_module_activity in prox:
            candidate = cand[(i, n)]
            candidate.update(
                cochange_strength=strength or 0,
                cochanged_files=files or 0,
                last_activity_ts=max(candidate["last_activity_ts"], last_activity or 0),
                last_module_activity_ts=max(candidate["last_module_activity_ts"], last_module_activity or 0)
            )
        if not cand: return {"data": {"module": module, "candidates": []}, "cypher": log, "viz": {"nodes": [], "edges": []}}
        mx_s = max(c["direct_score"] for c in cand.values()) or 1
        mx_c = max(c["cochange_strength"] for c in cand.values()) or 1
        ranked = sorted(
            cand.items(),
            key=lambda item: -(
                0.6 * item[1]["direct_score"] / mx_s
                + 0.4 * item[1]["cochange_strength"] / mx_c
            )
        )[:3]
        res, nodes, edges = [], [_node("c:" + p["id"], p["name"], "person"), _node("m:" + module, module, "module")], []
        edges.append({"from": "c:" + p["id"], "to": "m:" + module, "label": "leaving"})
        for (i, n), candidate in ranked:
            rank_score = (
                0.6 * candidate["direct_score"] / mx_s
                + 0.4 * candidate["cochange_strength"] / mx_c
            )
            res.append({
                "name": n,
                "id": i,
                "files_touched_in_module": candidate["files"],
                "recent_work_score": round(candidate["direct_score"], 1),
                "co_change_strength": candidate["cochange_strength"],
                "co_changed_files": candidate["cochanged_files"],
                "last_activity_ts": candidate["last_activity_ts"],
                "last_module_activity_ts": candidate["last_module_activity_ts"],
                "last_contribution_ts": candidate["last_module_activity_ts"] or None,
                "relative_rank_score": round(rank_score, 3)
            })
            nodes.append(_node("c:" + i, n, "candidate"))
            edges.append({
                "from": "c:" + i,
                "to": "m:" + module,
                "label": f"{candidate['files']} files · {rank_score:.2f} relative"
            })
        return {"data": {
            "leaving": p["name"],
            "module": module,
            "ranking_note": "Relative score within the candidates returned: 60% normalized direct work, 40% normalized co-change history.",
            "candidates": res
        }, "cypher": log, "viz": {"nodes": nodes, "edges": edges}}

    # ---------- tool 4: shortest path (explainability) ----------
    def explain_path(self, person, module):
        log = []
        p, matches = self.resolve(person, log)
        if not p:
            return {"data": {"error": self._resolution_error(person, matches)}, "cypher": log, "viz": {"nodes": [], "edges": []}}
        rows = self._q("""MATCH (a:Contributor {id:$id}), (m:Module {name:$m})
            RETURN shortestPath((a)-[:TOUCHED|IN_MODULE*1..2]->(m))""", {"id": p["id"], "m": module}, log)
        path = rows[0][0] if rows else None
        if path is None: return {"data": {"path": None}, "cypher": log, "viz": {"nodes": [], "edges": []}}
        path_nodes = path.nodes()
        path_edges = path.edges()
        steps = [
            node.properties.get("name") or node.properties.get("path") or node.properties.get("id")
            for node in path_nodes
        ]
        groups = [
            "person" if "Contributor" in node.labels
            else "module" if "Module" in node.labels
            else "file" if "File" in node.labels
            else "file"
            for node in path_nodes
        ]
        nodes = [
            _node(f"p:{i}", str(step).split("/")[-1], groups[i], title=str(step))
            for i, step in enumerate(steps)
        ]
        relationships = [
            {"from": steps[i], "to": steps[i + 1], "type": edge.relation}
            for i, edge in enumerate(path_edges)
        ]
        edges = [
            {"from": f"p:{i}", "to": f"p:{i + 1}", "label": edge.relation}
            for i, edge in enumerate(path_edges)
        ]
        return {
            "data": {"path": steps, "relationships": relationships},
            "cypher": log,
            "viz": {"nodes": nodes, "edges": edges}
        }

    # ---------- tool 5: graph algorithm (PageRank) ----------
    def key_people(self, top_n=6):
        log = []
        try:
            rows = self._q("""CALL algo.pageRank('Contributor', 'COLLABORATES') YIELD node, score
                RETURN node.id, node.name, node.active, score ORDER BY score DESC LIMIT $n""",
                {"n": top_n}, log)
            ranking_method = "PageRank"
        except Exception as error:
            fallback_rows = self._q("""MATCH (c:Contributor)-[r:COLLABORATES]->(:Contributor)
                WITH c, sum(coalesce(r.w, 1)) AS score
                RETURN c.id, c.name, c.active, score ORDER BY score DESC LIMIT $n""",
                {"n": top_n}, log)
            rows = fallback_rows
            ranking_method = "weighted collaboration degree (PageRank unavailable)"
            fallback_reason = str(error)
        res = [{
            "name": n,
            "id": i,
            "active": a,
            "pagerank": round(s, 4) if ranking_method == "PageRank" else None,
            "centrality_score": round(s, 4)
        } for i, n, a, s in rows]
        nodes = [_node("c:" + r["id"], r["name"], "person") for r in res]
        data = {"most_central_collaborators": res, "ranking_method": ranking_method}
        if ranking_method != "PageRank":
            data["fallback_reason"] = fallback_reason
        return {"data": data, "cypher": log, "viz": {"nodes": nodes, "edges": []}}

    def meta(self):
        return {"repo": self.repo, "graph": self.graph_name}
