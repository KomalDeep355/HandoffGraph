"""Agent loop. LLM mode: OpenAI-compatible tool-calling (Gemini / Ollama / OpenAI...). Offline mode: deterministic router."""
from datetime import datetime, timezone
import json, re
from . import config as cfg
from .graph_tools import GraphTools
from .actions import plan_handoff

SYSTEM = """You are BusFactor, an agent that protects open-source projects from key-person risk.
You have NO knowledge of the repository except what the graph tools return. Always call tools before answering.
Typical flow: module_risk -> leave_impact(person) -> find_successors(person, module) -> explain_path. Use plan_handoff
when asked to act/plan/fix. Use key_people for graph-centrality questions.
Answer in under 180 words. Finish with a 'Graph evidence' line naming the path you followed
(e.g. Person -> TOUCHED -> File -> IN_MODULE -> Module) and the numbers that drove each conclusion.
Risk scores are heuristic indicators, not probabilities. Never invent names or numbers."""

SPEC = [
    {"name": "module_risk", "description": "Rank modules by heuristic key-person risk (bus factor, top owner share, share of knowledge held by inactive people). The 0-1 risk score is not a probability.", "parameters": {"type": "object", "properties": {}}},
    {"name": "leave_impact", "description": "What knowledge is orphaned (no other active contributor) if this person leaves.", "parameters": {"type": "object", "properties": {"person": {"type": "string"}}, "required": ["person"]}},
    {"name": "find_successors", "description": "Rank active successor candidates using direct work and co-change history. Returns the relative rank score components, last activity, directly touched files, and co-changed files.", "parameters": {"type": "object", "properties": {"person": {"type": "string"}, "module": {"type": "string"}}, "required": ["person", "module"]}},
    {"name": "explain_path", "description": "Return the directed contributor-to-module path and the actual relationship type at each step.", "parameters": {"type": "object", "properties": {"person": {"type": "string"}, "module": {"type": "string"}}, "required": ["person", "module"]}},
    {"name": "key_people", "description": "Rank central collaborators with PageRank, or weighted collaboration degree if PageRank is unavailable.", "parameters": {"type": "object", "properties": {}}},
    {"name": "plan_handoff", "description": "ACTION: write a knowledge hand-off plan (markdown) and issue drafts for a person who may leave.", "parameters": {"type": "object", "properties": {"person": {"type": "string"}}, "required": ["person"]}},
]


class Agent:
    def __init__(self, graph_name=None):
        self.tools = GraphTools(graph_name=graph_name)
        self.llm = bool(cfg.LLM_API_KEY and cfg.LLM_BASE_URL)

    def call(self, name, args, trace, viz, allow_issue_creation=False):
        if name == "module_risk": r = self.tools.module_risk()
        elif name == "leave_impact": r = self.tools.leave_impact(args["person"])
        elif name == "find_successors": r = self.tools.find_successors(args["person"], args["module"])
        elif name == "explain_path": r = self.tools.explain_path(args["person"], args["module"])
        elif name == "key_people": r = self.tools.key_people()
        elif name == "plan_handoff":
            r = plan_handoff(self.tools, args["person"], allow_issue_creation)
        else: r = {"data": {"error": "unknown tool"}, "cypher": [], "viz": {"nodes": [], "edges": []}}
        trace.append({"tool": name, "args": args, "cypher": r["cypher"], "data": r["data"]})
        for n in r["viz"]["nodes"]: viz["nodes"][n["id"]] = n
        for e in r["viz"]["edges"]: viz["edges"][(e["from"], e["to"], e["label"])] = e
        return r["data"]

    def ask(self, question, open_issues=False, history=None):
        trace, viz = [], {"nodes": {}, "edges": {}}
        answer = (
            self._llm(question, trace, viz, history or [], open_issues)
            if self.llm else self._offline(question, trace, viz, open_issues)
        )
        return {"answer": answer, "mode": "llm" if self.llm else "offline",
                "trace": trace, "viz": {"nodes": list(viz["nodes"].values()), "edges": list(viz["edges"].values())}}

    # ---------------- LLM mode ----------------
    def _tool_result_citations(self, trace):
        citations = []
        for item in trace:
            tool = item["tool"]
            data = item["data"]
            if "error" in data:
                facts = [f"error={data['error']}"]
            elif tool == "module_risk":
                modules = data.get("modules", [])
                facts = [
                    f"{module['module']} risk={module['risk']} "
                    f"bus_factor={module['bus_factor']} "
                    f"top_owner_share={module['top_owner_share']}"
                    for module in modules[:2]
                ]
                if not facts:
                    facts.append("no modules returned")
            elif tool == "leave_impact":
                modules = data.get("modules_hit", [])
                facts = [
                    f"person={data.get('person', {}).get('name', 'unknown')}; "
                    f"orphaned_files={data.get('orphaned_files_total', 0)}; "
                    f"modules={','.join(module['module'] for module in modules[:2])}"
                ]
            elif tool == "find_successors":
                candidates = data.get("candidates", [])
                facts = [
                    f"{candidate['name']} score={candidate['relative_rank_score']} "
                    f"direct_files={candidate['files_touched_in_module']} "
                    f"cochanged_files={candidate['co_changed_files']} "
                    f"last_module_contribution={candidate.get('last_contribution_ts')}"
                    for candidate in candidates[:2]
                ]
                if not facts:
                    facts.append("no candidates returned")
            elif tool == "explain_path":
                facts = [
                    " -> ".join(
                        f"{edge['from']} -[{edge['type']}]-> {edge['to']}"
                        for edge in data.get("relationships", [])
                    ) or "no path returned"
                ]
            elif tool == "key_people":
                facts = [
                    f"method={data.get('ranking_method')}; "
                    + ", ".join(
                        f"{person['name']}={person.get('pagerank') if person.get('pagerank') is not None else person.get('centrality_score')}"
                        for person in data.get("most_central_collaborators", [])[:3]
                    )
                ]
            elif tool == "plan_handoff":
                facts = [
                    f"person={data.get('person', 'unknown')}; "
                    f"issues_status={data.get('issues_status', 'unknown')}"
                ]
            else:
                facts = ["tool returned data"]
            citations.append(f"[{tool}: {'; '.join(facts)}]")
        return "Graph evidence (tool results): " + " | ".join(citations)

    def _llm(self, question, trace, viz, history=None, open_issues=False):
        from openai import OpenAI
        client = OpenAI(base_url=cfg.LLM_BASE_URL, api_key=cfg.LLM_API_KEY)
        tools = [{"type": "function", "function": s} for s in SPEC]
        msgs = [{"role": "system", "content": SYSTEM}]
        for item in (history or [])[-12:]:
            if item.get("role") in {"user", "assistant"} and item.get("content"):
                msgs.append({"role": item["role"], "content": item["content"]})
        msgs.append({"role": "user", "content": question})
        for attempt in range(8):
            completion = client.chat.completions.create(
                model=cfg.LLM_MODEL,
                messages=msgs,
                tools=tools,
                tool_choice="required" if attempt == 0 else "auto"
            )
            msg = completion.choices[0].message
            if not msg.tool_calls:
                if attempt == 0:
                    return "Analysis stopped: the model did not call a repository tool, so no repository-backed answer was produced."
                answer = msg.content or ""
                if trace:
                    answer = f"{answer.rstrip()}\n\n{self._tool_result_citations(trace)}"
                return answer
            msgs.append(msg.model_dump(exclude_none=True))
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments or "{}")
                if tc.function.name == "plan_handoff":
                    args["open_issues"] = bool(open_issues)
                data = self.call(
                    tc.function.name,
                    args,
                    trace,
                    viz,
                    allow_issue_creation=open_issues
                )
                slim = {k: v for k, v in data.items() if k != "plan_markdown"}
                msgs.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(slim)[:6000]})
        answer = "Stopped after too many tool calls."
        if trace:
            answer = f"{answer}\n\n{self._tool_result_citations(trace)}"
        return answer

    # ---------------- offline mode (no API key needed) ----------------
    def _offline(self, q, trace, viz, open_issues):
        ql = q.lower()
        m = re.search(r"(?:if|when|should)\s+(.+?)\s+(?:leaves|left|quits|quit|disappears|gets hit|moves on)", q, re.I) \
            or re.search(r"(?:about|for|of)\s+([A-Z][\w\-\. ]+)$", q.strip("? ."))
        if "central" in ql or "pagerank" in ql or "key people" in ql:
            d = self.call("key_people", {}, trace, viz)
            score_field = "pagerank" if d["ranking_method"] == "PageRank" else "centrality_score"
            return f"Most central collaborators ({d['ranking_method']} over shared-file collaboration edges): " + ", ".join(
                f"{p['name']} ({p[score_field]})" for p in d["most_central_collaborators"]) + \
                "\n\nGraph evidence: Contributor -[COLLABORATES]-> Contributor."
        risk_result = self.call("module_risk", {}, trace, viz)
        risk = risk_result.get("modules", [])
        if not risk:
            return "No modules with usable knowledge scores were returned by the repository graph."
        person = m.group(1).strip() if m else risk[0]["top_owner"]
        if any(w in ql for w in ["plan", "hand-off", "handoff", "fix", "mitigat", "what should we do"]):
            d = self.call("plan_handoff", {"person": person}, trace, viz, allow_issue_creation=open_issues)
            if "error" in d: return d["error"]
            return f"Hand-off plan for {d['person']} written to {d['plan_file']}.\nIssues: {d['issues_status']}\n\n{d['plan_markdown']}"
        if "central" not in ql and not m and not any(w in ql for w in ["leave", "impact", "who"]):
            top = risk[0]
            return (f"Riskiest module: {top['module']} - {top['top_owner']} holds {int(top['top_owner_share']*100)}% of recent knowledge "
                    f"(bus factor {top['bus_factor']}; heuristic risk score {top['risk_score']}, not a probability).\n\n"
                    f"Graph evidence: Contributor -[KNOWS]-> Module aggregated from TOUCHED edges.")
        imp = self.call("leave_impact", {"person": person}, trace, viz)
        if "error" in imp: return imp["error"]
        hit = imp["modules_hit"][:2]
        lines = [f"If {imp['person']['name']} leaves, {imp['orphaned_files_total']} files lose their last active expert."]
        for h in hit:
            s = self.call("find_successors", {"person": person, "module": h["module"]}, trace, viz)
            candidates = []
            for candidate in s["candidates"][:2]:
                timestamp = candidate.get("last_contribution_ts")
                last_contribution = (
                    datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()
                    if timestamp else "no direct module contribution"
                )
                candidates.append(
                    f"{candidate['name']} (relative score {candidate['relative_rank_score']:.2f}; "
                    f"{candidate['files_touched_in_module']} direct files, "
                    f"{candidate['co_changed_files']} co-changed files; "
                    f"last module contribution {last_contribution})"
                )
            c = ", ".join(candidates) or "nobody obvious"
            self.call("explain_path", {"person": person, "module": h["module"]}, trace, viz)
            lines.append(f"- {h['module']}: {int(h['knowledge_share']*100)}% of knowledge, {h['orphaned_files']}/{h['files_in_module']} files orphaned. Best successors: {c}.")
        lines.append("\nGraph evidence: Person -[TOUCHED]-> File -[IN_MODULE]-> Module; successors ranked by TOUCHED + CO_CHANGED proximity.")
        return "\n".join(lines)
