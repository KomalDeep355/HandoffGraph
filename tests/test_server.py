import gzip
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

from busfactor import server


class RepositorySelectionTests(unittest.TestCase):
    def test_github_url_maps_to_stable_isolated_graph_name(self):
        repo, graph_name = server._github_repository(
            "https://github.com/KomalDeep355/HandoffGraph.git"
        )

        self.assertEqual(repo, "KomalDeep355/HandoffGraph")
        self.assertRegex(graph_name, r"^repo_[0-9a-f]{16}$")
        self.assertEqual(
            graph_name,
            server._github_repository(
                "https://github.com/komaldeep355/handoffgraph"
            )[1],
        )

    def test_repository_url_rejects_non_github_hosts_and_paths(self):
        for url in (
            "https://example.com/owner/repo",
            "https://github.com/owner/repo/tree/main",
            "https://user@github.com/owner/repo",
            "http://github.com/owner/repo",
        ):
            with self.subTest(url=url), self.assertRaises(HTTPException):
                server._github_repository(url)

    def test_ask_forwards_selected_graph_and_conversation(self):
        result = {"answer": "grounded reply"}
        fake_agent = Mock()
        fake_agent.ask.return_value = result
        body = server.Ask(
            question="What about that module?",
            open_issues=True,
            graph_name="repo_0123456789abcdef",
            history=[
                {"role": "user", "content": "Which module is riskiest?"},
                {"role": "assistant", "content": "pkg/core"},
            ],
        )

        with patch.object(server, "agent", return_value=fake_agent) as get_agent:
            response = server.ask(body)

        get_agent.assert_called_once_with("repo_0123456789abcdef")
        fake_agent.ask.assert_called_once_with(
            "What about that module?",
            True,
            [
                {"role": "user", "content": "Which module is riskiest?"},
                {"role": "assistant", "content": "pkg/core"},
            ],
        )
        self.assertIs(response, result)

    def test_repository_loading_extracts_and_ingests_into_separate_graph(self):
        commit = {"h": "abc", "n": "Contributor", "id": "123", "t": 100, "f": ["src/app.py"]}
        fake_agent = SimpleNamespace(tools=SimpleNamespace(repo="owner/repo"))

        def verify_dataset(dataset, graph_name, repo):
            with gzip.open(dataset, "rt", encoding="utf-8") as stream:
                self.assertEqual(json.loads(stream.readline()), commit)
            self.assertEqual(graph_name, server._github_repository("https://github.com/owner/repo")[1])
            self.assertEqual(repo, "owner/repo")
            return {"commits": 1, "contributors": 1, "files": 1, "modules": 1}

        with (
            patch.object(server.subprocess, "run") as git,
            patch.object(server.extract, "run", return_value=[commit]) as extract_run,
            patch.object(server.ingest, "ingest", side_effect=verify_dataset),
            patch.object(server, "agent", return_value=fake_agent),
        ):
            result = server.load_repository(
                server.RepositoryRequest(repo_url="https://github.com/owner/repo")
            )

        git.assert_called_once()
        self.assertEqual(git.call_args.kwargs["timeout"], 180)
        extract_run.assert_called_once()
        self.assertEqual(result["repo"], "owner/repo")
        self.assertEqual(result["commits"], 1)
        self.assertRegex(result["graph_name"], r"^repo_[0-9a-f]{16}$")


if __name__ == "__main__":
    unittest.main()
