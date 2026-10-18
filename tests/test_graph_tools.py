import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from busfactor.agent import Agent, SYSTEM
from busfactor.graph_tools import GraphTools


class GraphToolsTests(unittest.TestCase):
    def make_tools(self):
        tools = object.__new__(GraphTools)
        tools._q = Mock()
        tools.resolve = Mock(return_value=({"id": "leaver", "name": "Leaver"}, []))
        return tools

    def test_module_risk_skips_empty_and_zero_weight_modules(self):
        tools = self.make_tools()
        tools._q.return_value = [
            ("empty", []),
            ("zero", [{"id": "zero", "name": "Zero", "score": 0, "active": True}]),
            ("invalid", [{"id": "invalid", "name": "Invalid", "score": float("nan"), "active": True}]),
            ("valid", [
                {"id": "owner", "name": "Owner", "score": 2, "active": True},
                {"id": "inactive", "name": "Inactive", "score": 1, "active": False},
            ]),
        ]

        result = tools.module_risk()["data"]

        self.assertEqual([module["module"] for module in result["modules"]], ["valid"])
        self.assertEqual(result["modules"][0]["bus_factor"], 1)
        self.assertEqual(result["modules"][0]["top_owner_share"], 0.67)
        self.assertEqual(result["modules"][0]["owner_count"], 2)
        self.assertEqual(result["modules"][0]["total_knowledge_score"], 3)
        self.assertIn("not a probability", result["risk_score_note"])

    def test_module_risk_returns_empty_for_only_zero_weight_modules(self):
        tools = self.make_tools()
        tools._q.return_value = [("zero", [{"id": "owner", "name": "Owner", "score": 0, "active": True}])]

        self.assertEqual(tools.module_risk()["data"]["modules"], [])

    def test_resolve_refuses_ambiguous_partial_name_matches(self):
        tools = self.make_tools()
        tools.resolve = GraphTools.resolve.__get__(tools, GraphTools)
        tools._q.side_effect = [
            [],
            [],
            [
                ("id-1", "Alex Smith", 10, True),
                ("id-2", "Alex Jones", 8, True),
            ],
        ]

        person, matches = tools.resolve("Alex", [])

        self.assertIsNone(person)
        self.assertEqual(matches, ["Alex Smith (id-1)", "Alex Jones (id-2)"])

    def test_resolve_accepts_unique_exact_contributor_id(self):
        tools = self.make_tools()
        tools.resolve = GraphTools.resolve.__get__(tools, GraphTools)
        tools._q.return_value = [("id-1", "Alex Smith", 10, True)]

        person, matches = tools.resolve("id-1", [])

        self.assertEqual(person["id"], "id-1")
        self.assertEqual(matches, [])
        self.assertEqual(tools._q.call_count, 1)

    def test_leave_impact_denominator_excludes_leaving_contributor(self):
        tools = self.make_tools()
        tools._q.side_effect = [
            [],
            [("module", 10)],
            [("module", 4, 10), ("zero-total", 2, 0)],
        ]

        result = tools.leave_impact("Leaver")["data"]

        self.assertEqual(result["modules_hit"][0]["knowledge_share"], 0.4)
        self.assertEqual([module["module"] for module in result["modules_hit"]], ["module"])
        share_query = tools._q.call_args_list[2].args[0]
        self.assertIn("WHERE other.id <> $id", share_query)
        self.assertIn("mine + others AS total", share_query)

    def test_successors_return_ranking_evidence_and_activity(self):
        tools = self.make_tools()
        tools._q.side_effect = [
            [
                ("candidate-a", "Candidate A", 2, 4.0, 100, 90),
                ("candidate-b", "Candidate B", 1, 2.0, 80, 70),
            ],
            [
                ("candidate-a", "Candidate A", 6, 2, 100, 90),
                ("candidate-b", "Candidate B", 3, 1, 80, 70),
            ],
        ]

        result = tools.find_successors("Leaver", "module")["data"]

        first, second = result["candidates"]
        self.assertEqual(first["id"], "candidate-a")
        self.assertEqual(first["relative_rank_score"], 1.0)
        self.assertEqual(first["last_activity_ts"], 100)
        self.assertEqual(first["last_module_activity_ts"], 90)
        self.assertEqual(first["last_contribution_ts"], 90)
        self.assertEqual(first["co_changed_files"], 2)
        self.assertEqual(second["relative_rank_score"], 0.5)
        self.assertIn("Relative score", result["ranking_note"])

    def test_explain_path_returns_actual_relationship_types(self):
        tools = self.make_tools()
        path = SimpleNamespace(
            nodes=lambda: [
                SimpleNamespace(labels=["Contributor"], properties={"name": "Leaver"}),
                SimpleNamespace(labels=["File"], properties={"path": "pkg/file.py"}),
                SimpleNamespace(labels=["Module"], properties={"name": "pkg"}),
            ],
            edges=lambda: [
                SimpleNamespace(relation="TOUCHED"),
                SimpleNamespace(relation="IN_MODULE"),
            ],
        )
        tools._q.return_value = [(path,)]

        result = tools.explain_path("Leaver", "pkg")

        self.assertEqual(result["data"]["relationships"], [
            {"from": "Leaver", "to": "pkg/file.py", "type": "TOUCHED"},
            {"from": "pkg/file.py", "to": "pkg", "type": "IN_MODULE"},
        ])
        self.assertEqual([edge["label"] for edge in result["viz"]["edges"]], ["TOUCHED", "IN_MODULE"])
        self.assertIn("*1..2", tools._q.call_args.args[0])

    def test_key_people_falls_back_if_pagerank_is_unavailable(self):
        tools = self.make_tools()
        tools._q.side_effect = [
            RuntimeError("unknown procedure algo.pageRank"),
            [("collaborator", "Collaborator", True, 3.0)],
        ]

        result = tools.key_people()["data"]

        self.assertEqual(result["ranking_method"], "weighted collaboration degree (PageRank unavailable)")
        self.assertEqual(result["most_central_collaborators"][0]["centrality_score"], 3.0)
        self.assertIsNone(result["most_central_collaborators"][0]["pagerank"])
        self.assertIn("unknown procedure", result["fallback_reason"])
        self.assertEqual(tools._q.call_count, 2)

    def test_plan_handoff_issue_permission_comes_only_from_explicit_opt_in(self):
        agent = object.__new__(Agent)
        agent.tools = object()
        result = {"data": {}, "cypher": [], "viz": {"nodes": [], "edges": []}}
        with patch("busfactor.agent.plan_handoff", return_value=result) as plan:
            agent.call("plan_handoff", {"person": "Alex", "open_issues": True}, [], {})
            self.assertFalse(plan.call_args.args[2])

            agent.call(
                "plan_handoff",
                {"person": "Alex"},
                [],
                {},
                allow_issue_creation=True
            )
            self.assertTrue(plan.call_args.args[2])

    def test_offline_agent_formats_fallback_centrality_scores(self):
        agent = object.__new__(Agent)
        agent.call = Mock(return_value={
            "ranking_method": "weighted collaboration degree (PageRank unavailable)",
            "most_central_collaborators": [{"name": "Candidate", "centrality_score": 3.0}],
        })

        answer = agent._offline("Who are the key people?", [], {}, False)

        self.assertIn("weighted collaboration degree", answer)
        self.assertIn("Candidate (3.0)", answer)

    def test_offline_agent_explains_successor_recency_and_evidence(self):
        agent = object.__new__(Agent)
        agent.call = Mock(side_effect=[
            {"modules": [{"top_owner": "Leaver"}]},
            {
                "person": {"name": "Leaver"},
                "orphaned_files_total": 1,
                "modules_hit": [{
                    "module": "pkg",
                    "knowledge_share": 0.4,
                    "orphaned_files": 1,
                    "files_in_module": 2,
                }],
            },
            {"candidates": [{
                "name": "Candidate",
                "relative_rank_score": 0.75,
                "files_touched_in_module": 2,
                "co_changed_files": 3,
                "last_contribution_ts": 1700000000,
            }]},
            {"path": ["Leaver", "pkg/file.py", "pkg"]},
        ])

        answer = agent._offline("If Leaver leaves", [], {}, False)

        self.assertIn("relative score 0.75", answer)
        self.assertIn("2 direct files", answer)
        self.assertIn("3 co-changed files", answer)
        self.assertIn("last module contribution 2023-11-14", answer)

    def test_offline_agent_handles_empty_risk_results(self):
        agent = object.__new__(Agent)
        agent.call = Mock(return_value={"modules": []})

        answer = agent._offline("What is the risk?", [], {}, False)

        self.assertIn("No modules with usable knowledge scores", answer)
        agent.call.assert_called_once_with("module_risk", {}, [], {})

    def test_llm_requires_first_tool_and_cites_returned_tool_results(self):
        agent = object.__new__(Agent)
        result = {
            "modules": [{
                "module": "pkg/core",
                "risk": "HIGH",
                "bus_factor": 1,
                "top_owner_share": 1.0,
            }]
        }

        def record_tool(name, args, trace, viz, allow_issue_creation=False):
            trace.append({"tool": name, "args": args, "data": result})
            return result

        agent.call = Mock(side_effect=record_tool)
        tool_call = SimpleNamespace(
            id="tool-1",
            function=SimpleNamespace(name="module_risk", arguments="{}")
        )

        class Message:
            def __init__(self, content=None, tool_calls=None):
                self.content = content
                self.tool_calls = tool_calls

            def model_dump(self, exclude_none=True):
                return {"role": "assistant", "tool_calls": ["tool-1"]}

        completions = [
            SimpleNamespace(choices=[SimpleNamespace(message=Message(tool_calls=[tool_call]))]),
            SimpleNamespace(choices=[SimpleNamespace(message=Message(content="pkg/core is high risk."))]),
        ]
        client = Mock()
        client.chat.completions.create.side_effect = completions
        history = [
            {"role": "user", "content": "Which module is risky?"},
            {"role": "assistant", "content": "pkg/core"},
        ]
        with patch("openai.OpenAI", return_value=client):
            answer = agent._llm("Assess risk", [], {}, history)

        self.assertEqual(client.chat.completions.create.call_args_list[0].kwargs["tool_choice"], "required")
        self.assertEqual(client.chat.completions.create.call_args_list[1].kwargs["tool_choice"], "auto")
        self.assertEqual(
            client.chat.completions.create.call_args_list[0].kwargs["messages"][:4],
            [
                {"role": "system", "content": SYSTEM},
                *history,
                {"role": "user", "content": "Assess risk"},
            ],
        )
        self.assertIn("[module_risk: pkg/core risk=HIGH bus_factor=1 top_owner_share=1.0]", answer)

    def test_llm_rejects_first_turn_without_tool_call(self):
        agent = object.__new__(Agent)

        class Message:
            content = "Unsupported answer"
            tool_calls = None

        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=Message())]
        )
        with patch("openai.OpenAI", return_value=client):
            answer = agent._llm("Question", [], {})

        self.assertIn("did not call a repository tool", answer)
        self.assertEqual(client.chat.completions.create.call_args.kwargs["tool_choice"], "required")


if __name__ == "__main__":
    unittest.main()
