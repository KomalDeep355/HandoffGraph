import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from busfactor import actions, config


class HandoffTools:
    repo = "owner/repository"

    def leave_impact(self, person):
        return {
            "cypher": [],
            "data": {
                "person": {"name": person},
                "orphaned_files_total": 1,
                "modules_hit": [{
                    "module": "pkg/core",
                    "knowledge_share": 0.5,
                    "orphaned_files": 1,
                    "files_in_module": 2,
                    "examples": ["pkg/core.py"],
                }],
            },
            "viz": {"nodes": [], "edges": []},
        }

    def find_successors(self, person, module):
        return {
            "cypher": [],
            "data": {"candidates": [{"name": "Successor"}]},
            "viz": {"nodes": [], "edges": []},
        }


class HandoffIssueTests(unittest.TestCase):
    def run_plan(self, open_issues):
        with tempfile.TemporaryDirectory() as output_dir:
            with patch.object(config, "OUT_DIR", Path(output_dir)):
                return actions.plan_handoff(HandoffTools(), "Leaving Contributor", open_issues)

    def test_issue_creation_defaults_to_dry_run_even_when_ui_is_checked(self):
        with (
            patch.object(config, "GITHUB_ISSUES_ENABLED", False),
            patch.object(config, "GITHUB_TOKEN", "test-token"),
            patch.object(config, "GITHUB_ISSUE_REPO", "owner/repository"),
            patch("busfactor.actions.requests.post") as post,
        ):
            data = self.run_plan(open_issues=True)["data"]

        post.assert_not_called()
        self.assertTrue(data["issues_status"].startswith("dry_run"))
        self.assertEqual(data["issue_results"], [])

    def test_rate_limit_is_reported_as_failure_not_created(self):
        response = Mock(
            status_code=403,
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "123"},
        )
        response.json.return_value = {"message": "API rate limit exceeded"}
        with (
            patch.object(config, "GITHUB_ISSUES_ENABLED", True),
            patch.object(config, "GITHUB_TOKEN", "test-token"),
            patch.object(config, "GITHUB_ISSUE_REPO", "owner/repository"),
            patch("busfactor.actions.requests.post", return_value=response),
        ):
            data = self.run_plan(open_issues=True)["data"]

        self.assertTrue(data["issues_status"].startswith("failed"))
        self.assertEqual(data["issue_results"][0]["status"], "failed")
        self.assertIn("rate limit", data["issue_results"][0]["error"].lower())
        self.assertIn("rate limit", data["issues_failed"][0].lower())
        self.assertFalse(any(result["status"] == "created" for result in data["issue_results"]))

    def test_http_error_with_non_object_json_is_reported_as_failure(self):
        response = Mock(status_code=500, headers={})
        response.json.return_value = ["unexpected payload"]
        with (
            patch.object(config, "GITHUB_ISSUES_ENABLED", True),
            patch.object(config, "GITHUB_TOKEN", "test-token"),
            patch.object(config, "GITHUB_ISSUE_REPO", "owner/repository"),
            patch("busfactor.actions.requests.post", return_value=response),
        ):
            data = self.run_plan(open_issues=True)["data"]

        self.assertTrue(data["issues_status"].startswith("failed"))
        self.assertEqual(data["issue_results"][0]["status"], "failed")
        self.assertIn("HTTP 500", data["issue_results"][0]["error"])

    def test_only_http_201_with_issue_url_is_counted_created(self):
        response = Mock(status_code=201, headers={})
        response.json.return_value = {"html_url": "https://github.com/owner/repository/issues/1"}
        with (
            patch.object(config, "GITHUB_ISSUES_ENABLED", True),
            patch.object(config, "GITHUB_TOKEN", "test-token"),
            patch.object(config, "GITHUB_ISSUE_REPO", "owner/repository"),
            patch("busfactor.actions.requests.post", return_value=response) as post,
        ):
            data = self.run_plan(open_issues=True)["data"]

        self.assertEqual(data["issues_status"], "created")
        self.assertEqual(data["issue_results"][0]["status"], "created")
        self.assertEqual(data["issues"][0], "https://github.com/owner/repository/issues/1")
        self.assertEqual(data["issues_failed"], [])
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            "Bearer test-token"
        )


    def test_missing_credentials_are_reported_as_failure_when_creation_is_enabled(self):
        with (
            patch.object(config, "GITHUB_ISSUES_ENABLED", True),
            patch.object(config, "GITHUB_TOKEN", ""),
            patch.object(config, "GITHUB_ISSUE_REPO", ""),
            patch("busfactor.actions.requests.post") as post,
        ):
            data = self.run_plan(open_issues=True)["data"]

        post.assert_not_called()
        self.assertEqual(data["issues_status"], "failed")
        self.assertIn("GITHUB_TOKEN", data["issues_failed"][0])
        self.assertIn("GITHUB_ISSUE_REPO", data["issues_failed"][0])


if __name__ == "__main__":
    unittest.main()
