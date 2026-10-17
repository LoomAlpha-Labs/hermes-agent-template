import json
import subprocess
import tempfile
import unittest

from scripts.request_redeploy import (
    DeploymentReceiptTimeout,
    _pid1_environment,
    request_redeploy,
)


HEAD = "a" * 40
TREE = "b" * 40
NEW = "c" * 40
ENV_ID = "environment-id"
PROJECT_NAME = "railway-project"
ENV_NAME = "production"


class FakeRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        returncode, payload = self.responses.pop(0)
        if isinstance(payload, (dict, list)):
            stdout, stderr = json.dumps(payload), ""
        else:
            stdout, stderr = "", payload
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class RequestRedeployTests(unittest.TestCase):
    def test_platform_metadata_is_read_from_pid1_source(self):
        with tempfile.NamedTemporaryFile() as stream:
            stream.write(
                b"RAILWAY_GIT_COMMIT_SHA=trusted-platform-sha\0"
                b"RAILWAY_PROJECT_NAME=railway-project\0"
            )
            stream.flush()
            values = _pid1_environment(stream.name)
        self.assertEqual(values["RAILWAY_GIT_COMMIT_SHA"], "trusted-platform-sha")
        self.assertEqual(values["RAILWAY_PROJECT_NAME"], "railway-project")

    def test_dry_run_reads_branch_and_parent_without_mutation(self):
        runner = FakeRunner(
            [
                (0, {"object": {"sha": HEAD}}),
                (0, {"tree": {"sha": TREE}}),
            ]
        )
        result = request_redeploy(
            repo="owner/repo",
            branch="main",
            live_sha=HEAD,
            railway_environment_id=ENV_ID,
            railway_project_name=PROJECT_NAME,
            railway_environment_name=ENV_NAME,
            reason="operator test",
            execute=False,
            run=runner,
        )
        self.assertEqual(result["status"], "planned")
        self.assertEqual(len(runner.calls), 2)
        self.assertTrue(all(call[0][4] == "GET" for call in runner.calls))

    def test_execute_requires_deployment_receipt(self):
        runner = FakeRunner(
            [
                (0, {"object": {"sha": HEAD}}),
                (0, {"tree": {"sha": TREE}}),
                (0, {"sha": NEW}),
                (0, {"object": {"sha": NEW}}),
                (0, [{
                    "id": 123,
                    "sha": NEW,
                    "environment": f"{PROJECT_NAME} / {ENV_NAME}",
                    "creator": {"login": "railway-app[bot]"},
                    "payload": {"environmentId": ENV_ID},
                }]),
                (0, [{
                    "creator": {"login": "railway-app[bot]"},
                }]),
            ]
        )
        result = request_redeploy(
            repo="owner/repo",
            branch="main",
            live_sha=HEAD,
            railway_environment_id=ENV_ID,
            railway_project_name=PROJECT_NAME,
            railway_environment_name=ENV_NAME,
            reason="clear PID 1 process debt",
            execute=True,
            run=runner,
        )
        self.assertEqual(
            result["status"],
            "project_deployment_created_pending_target_verification",
        )
        self.assertEqual(result["commit_sha"], NEW)
        self.assertEqual(result["deployment_id"], 123)

        create_command, create_kwargs = runner.calls[2]
        self.assertEqual(create_command[4], "POST")
        create_payload = json.loads(create_kwargs["input"])
        self.assertEqual(create_payload["tree"], TREE)
        self.assertEqual(create_payload["parents"], [HEAD])

        update_command, update_kwargs = runner.calls[3]
        self.assertEqual(update_command[4], "PATCH")
        self.assertEqual(
            json.loads(update_kwargs["input"]), {"sha": NEW, "force": False}
        )
        self.assertIn(f"deployments?sha={NEW}", runner.calls[4][0][2])

    def test_empty_or_malformed_live_sha_refuses_before_network(self):
        for live_sha in ("", "abc", "A" * 40, "g" * 40):
            with self.subTest(live_sha=live_sha):
                runner = FakeRunner([])
                with self.assertRaisesRegex(ValueError, "full 40-character"):
                    request_redeploy(
                        repo="owner/repo",
                        branch="main",
                        live_sha=live_sha,
                        railway_environment_id=ENV_ID,
                        railway_project_name=PROJECT_NAME,
                        railway_environment_name=ENV_NAME,
                        reason="unsafe request",
                        execute=True,
                        run=runner,
                    )
                self.assertEqual(runner.calls, [])

    def test_refuses_when_live_deployment_is_not_branch_head(self):
        runner = FakeRunner([(0, {"object": {"sha": HEAD}})])
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            request_redeploy(
                repo="owner/repo",
                branch="main",
                live_sha="d" * 40,
                railway_environment_id=ENV_ID,
                railway_project_name=PROJECT_NAME,
                railway_environment_name=ENV_NAME,
                reason="unsafe stale request",
                execute=True,
                run=runner,
            )
        self.assertEqual(len(runner.calls), 1)

    def test_missing_deployment_receipt_reports_mutated_commit(self):
        runner = FakeRunner(
            [
                (0, {"object": {"sha": HEAD}}),
                (0, {"tree": {"sha": TREE}}),
                (0, {"sha": NEW}),
                (0, {"object": {"sha": NEW}}),
                (0, []),
                (0, []),
            ]
        )
        clock = Clock()
        with self.assertRaises(DeploymentReceiptTimeout) as raised:
            request_redeploy(
                repo="owner/repo",
                branch="main",
                live_sha=HEAD,
                railway_environment_id=ENV_ID,
                railway_project_name=PROJECT_NAME,
                railway_environment_name=ENV_NAME,
                reason="watch path test",
                execute=True,
                receipt_timeout=2,
                poll_interval=1,
                run=runner,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
            )
        self.assertEqual(raised.exception.commit_sha, NEW)

    def test_refuses_missing_reason_before_network(self):
        runner = FakeRunner([])
        with self.assertRaisesRegex(ValueError, "reason is required"):
            request_redeploy(
                repo="owner/repo",
                branch="main",
                live_sha=HEAD,
                railway_environment_id=ENV_ID,
                railway_project_name=PROJECT_NAME,
                railway_environment_name=ENV_NAME,
                reason="   ",
                execute=True,
                run=runner,
            )
        self.assertEqual(runner.calls, [])

    def test_api_failure_is_fail_closed(self):
        runner = FakeRunner([(1, "permission denied")])
        with self.assertRaisesRegex(RuntimeError, "permission denied"):
            request_redeploy(
                repo="owner/repo",
                branch="main",
                live_sha=HEAD,
                railway_environment_id=ENV_ID,
                railway_project_name=PROJECT_NAME,
                railway_environment_name=ENV_NAME,
                reason="test failure",
                execute=True,
                run=runner,
            )


if __name__ == "__main__":
    unittest.main()
