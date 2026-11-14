"""Quota probes against recording stubs: normalization, the zero-call proof, and the probe's own cleanup.

Every CLI here is a stub from quota_stubs (synthetic numbers only). Signals go only to
this test process or to the stubs it started; each test checks that the probe's process
group, any descendant and the temporary directory are gone.
"""
import contextlib
import os
import signal
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import quota_stubs
from harness import quota
from harness.adapters import CLIBackend


def environment():
    return CLIBackend.process_environment()


def gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def group_gone(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def epoch(*fields):
    return datetime(*fields, tzinfo=timezone.utc).timestamp()


class TrackTemp:
    """Records the probe's temporary directories (prefix harness-quota-)."""

    def __init__(self, case):
        self.paths = []
        real = tempfile.mkdtemp

        def tracked(*args, **kwargs):
            path = real(*args, **kwargs)
            if kwargs.get("prefix") == "harness-quota-":
                self.paths.append(path)
            return path
        patcher = patch("harness.quota.tempfile.mkdtemp", side_effect=tracked)
        patcher.start()
        case.addCleanup(patcher.stop)

    def removed(self):
        return bool(self.paths) and not any(os.path.lexists(path) for path in self.paths)


class ProbeCase(unittest.TestCase):
    def assert_clean(self, calls, temp, provider):
        """The probe's group (with every logged descendant) and its directory are gone."""
        self.assertTrue(group_gone(calls.probes(provider)[-1]["pid"]))
        for pid in calls.children(provider):
            self.assertTrue(gone(pid), pid)
        self.assertTrue(temp.removed(), temp.paths)


class SnapshotTests(ProbeCase):
    def test_claude_reads_windows_version_and_the_zero_call_proof(self):  # P1
        calls = quota_stubs.install(self)
        temp = TrackTemp(self)
        record = quota.snapshot("claude", environment())
        self.assertTrue(record["available"], record)
        self.assertEqual(record["source"], {"command": list(quota.CLAUDE_ARGV), "cli_version": "9.9.901"})
        self.assertEqual([(w["kind"], w["scope_model"], w["used_percent"]) for w in record["windows"]],
                         [("session", None, 37), ("weekly_all", None, 61), ("weekly_scoped", "Model Z", 100)])
        self.assertEqual([w["resets_at"] for w in record["windows"]],
                         [epoch(2031, 1, 2, 3), epoch(2031, 1, 6, 3), epoch(2031, 1, 6, 3)])  # "Z" is +00:00
        self.assertEqual(record["zero_call"], {"verified": True, "num_turns": 0, "total_cost_usd": 0,
                                               "duration_api_ms": 0, "local_command": "usage"})
        self.assertEqual(record["extra_usage"], {"is_enabled": False, "monthly_limit": None, "used_credits": None,
                                                 "utilization": None, "currency": None})
        self.assertEqual({key: record["process"][key] for key in ("started", "exit_confirmed", "kill_sent",
                                                                 "temp_removed", "termination")},
                         {"started": True, "exit_confirmed": True, "kill_sent": True, "temp_removed": True,
                          "termination": "normal"})
        call = calls.probes("claude")[0]
        self.assertEqual(call["auto_memory"], "1")
        self.assertTrue(call["cwd"].startswith(os.path.realpath(temp.paths[0])))
        self.assertTrue(temp.removed())
        self.assertEqual(calls.unexpected(), [])
        self.assertEqual(quota.full_windows(record), [{"bucket": "weekly_scoped（Model Z）", "used_percent": 100,
                                                       "resets_at": "2031-01-06T03:00:00+00:00"}])

    def test_codex_reads_its_window_length_and_credits_as_reported(self):  # P2
        quota_stubs.install(self)
        record = quota.snapshot("codex", environment())
        self.assertTrue(record["available"], record)
        # The slot is a position only: its length comes from the data, never assumed.
        self.assertEqual(record["windows"], [{"limit_id": "codex", "slot": "primary", "window_minutes": 4320,
                                              "used_percent": 73, "resets_at": 1925000000,
                                              "resets_at_raw": 1925000000}])
        self.assertEqual(record["limits"], [{"limit_id": "codex", "limit_name": None, "rate_limit_reached_type": None,
                                             "spend_control_reached": False,
                                             "credits": {"has_credits": True, "unlimited": False, "balance": "41.25"}}])
        self.assertEqual(record["source"]["cli_version"], "0.900.1")
        self.assertIs(record["ordinary_usage_allowed"], True)
        self.assertEqual(quota.bucket_key("codex", record["windows"][0]), ("codex", "codex", "primary", 4320))
        self.assertIsNone(record["zero_call"])

    def test_failures_are_unavailable_with_a_reason_and_leave_nothing(self):  # P3
        cases = [("claude", "not_zero_call", "not_zero_call", {"field": "result.num_turns"}),
                 ("claude", "no_local_command", "not_zero_call", {"field": "result.local_command"}),
                 ("claude", "shape", "shape_mismatch", {"field": "usage_report.rate_limits.limits[0].percent"}),
                 ("claude", "exit", "exited", {"field": "result", "exit_code": 3}),
                 ("claude", "big", "output_limit", None), ("claude", "hang", "timeout", None),
                 ("claude", "provider_error", "provider_error", None),
                 ("codex", "rpc_error", "provider_error", {"method": "account/rateLimits/read", "code": -32600}),
                 ("codex", "shape", "shape_mismatch", {"field": "result.rateLimits.primary.usedPercent"}),
                 ("codex", "hang", "timeout", None)]
        for provider, mode, reason, detail in cases:
            with self.subTest(provider=provider, mode=mode), patch.object(quota, "QUERY_SECONDS", 1.0):
                calls = quota_stubs.install(self, **{provider: mode})
                temp = TrackTemp(self)
                begun = time.monotonic()
                record = quota.snapshot(provider, environment())
                self.assertLess(time.monotonic() - begun, quota.QUERY_SECONDS + quota.CLEANUP_SECONDS + 1)
                self.assertEqual((record["available"], record["reason"], record["detail"]), (False, reason, detail))
                self.assertEqual((record["windows"], record["limits"]), ([], []))
                self.assertTrue(record["process"]["exit_confirmed"], record["process"])
                self.assert_clean(calls, temp, provider)

    def test_codex_that_ignores_eof_or_leaves_a_child_is_ended(self):  # P4
        for mode in ("ignore_eof", "orphan"):
            with self.subTest(mode=mode):
                calls = quota_stubs.install(self, codex=mode)
                temp = TrackTemp(self)
                record = quota.snapshot("codex", environment())
                self.assertTrue(record["available"], record)
                self.assertTrue(record["process"]["exit_confirmed"])
                self.assert_clean(calls, temp, "codex")

    def test_a_missing_cli_is_not_installed_and_its_directory_is_removed(self):  # P5
        temp = TrackTemp(self)
        with patch.dict(os.environ, {"PATH": "/nonexistent"}):
            record = quota.snapshot("claude", environment())
        self.assertEqual(record["reason"], "not_installed")
        self.assertEqual((record["process"]["started"], record["process"]["termination"]), (False, "start_failed"))
        self.assertTrue(temp.removed())

    def test_an_interrupt_while_reading_propagates_and_ends_the_group(self):  # P6
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                calls = quota_stubs.install(self, **{provider: "sigint_parent"})
                temp = TrackTemp(self)
                with self.assertRaises(KeyboardInterrupt):
                    quota.snapshot(provider, environment())
                self.assert_clean(calls, temp, provider)

    def test_an_unknown_window_kind_is_kept_as_reported(self):  # P7
        quota_stubs.install(self, claude="unknown_kind")
        record = quota.snapshot("claude", environment())
        self.assertTrue(record["available"], record)
        self.assertEqual((record["windows"][-1]["kind"], record["windows"][-1]["severity"],
                          record["windows"][-1]["resets_at"]), ("monthly_new", "sev-9", None))

    def test_snapshots_by_limit_id_add_buckets_or_are_set_aside(self):  # P8
        quota_stubs.install(self, codex="by_limit_id")
        record = quota.snapshot("codex", environment())
        self.assertEqual([limit["limit_id"] for limit in record["limits"]], ["codex", "codex_other"])
        self.assertEqual([quota.bucket_label("codex", w) for w in record["windows"]],
                         ["codex.primary（4320 分钟）", "codex_other.primary（4320 分钟）"])
        self.assertEqual(record["unparsed"], [])
        quota_stubs.install(self, codex="by_limit_id_bad")
        record = quota.snapshot("codex", environment())
        self.assertTrue(record["available"], record)
        self.assertEqual(([limit["limit_id"] for limit in record["limits"]], record["unparsed"]),
                         (["codex"], ["result.rateLimitsByLimitId"]))


def claude_lines():
    """A synthetic /usage stream: init, report and result lines."""
    limits = [{"kind": "session", "group": "session", "percent": 37, "resets_at": "2031-01-02T03:00:00+00:00",
               "scope": None, "severity": "sev-1", "is_active": True}]
    report = {"type": "assistant", "usage_report": {"rate_limits": {"limits": limits, "extra_usage": None}}}
    result = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 0, "total_cost_usd": 0,
              "duration_api_ms": 0, "local_command": "usage"}
    return [{"type": "system", "subtype": "init", "claude_code_version": "9.9.901"}, report, result]


def codex_answers():
    initialize = {"id": 0, "result": {"userAgent": "codex_cli_rs/0.900.1 (synthetic)"}}
    snapshot = {"limitId": "codex", "primary": {"usedPercent": 73, "windowDurationMins": 4320, "resetsAt": 1925000000},
                "secondary": None, "credits": {"hasCredits": True, "unlimited": False, "balance": "41.25"}}
    return initialize, {"id": 1, "result": {"rateLimits": snapshot, "ordinaryUsageAllowed": True}}


class NormalizationTests(unittest.TestCase):
    """3.3.4 one row at a time: the whole sample is unusable and names the path."""

    def claude(self, edit):
        lines = claude_lines()
        edit(lines)
        return quota.normalize_claude(lines, {})

    def codex(self, edit):
        initialize, answer = codex_answers()
        edit(initialize, answer)
        return quota.normalize_codex(initialize, answer)

    def test_shape_table(self):  # P9
        limit = lambda lines: lines[1]["usage_report"]["rate_limits"]["limits"][0]
        rate = lambda lines: lines[1]["usage_report"]["rate_limits"]
        claude_cases = {
            "usage_report": lambda lines: lines[1].pop("usage_report"),
            "usage_report.rate_limits": lambda lines: lines[1]["usage_report"].update(rate_limits=[]),
            "usage_report.rate_limits.limits": lambda lines: rate(lines).update(limits=[limit(lines)] * 33),
            "usage_report.rate_limits.limits[0].kind": lambda lines: limit(lines).pop("kind"),
            "usage_report.rate_limits.limits[0].percent": lambda lines: limit(lines).pop("percent"),
            "usage_report.rate_limits.limits[0].resets_at": lambda lines: limit(lines).update(
                resets_at="2031-01-02T03:00:00"),   # no offset
            "usage_report.rate_limits.limits[0].scope": lambda lines: limit(lines).update(scope="all"),
            "usage_report.rate_limits.limits[0].is_active": lambda lines: limit(lines).update(is_active="yes"),
            "usage_report.rate_limits.limits[1] duplicate bucket": lambda lines: rate(lines)["limits"].append(
                dict(limit(lines))),
            "usage_report.rate_limits.extra_usage": lambda lines: rate(lines).update(extra_usage=[1]),
        }
        for field, edit in claude_cases.items():
            with self.subTest(field=field), self.assertRaises(quota.Shape) as caught:
                self.claude(edit)
            self.assertEqual(caught.exception.field, field)
        with self.assertRaises(quota.Shape) as caught:   # before the epoch: outside the representable range
            self.claude(lambda lines: limit(lines).update(resets_at="1969-12-31T00:00:00+00:00"))
        self.assertEqual(caught.exception.field, "usage_report.rate_limits.limits[0].resets_at")
        verdict = {}
        lines = claude_lines()
        lines.append(dict(lines[2]))
        with self.assertRaises(quota.Unavailable) as two:
            quota.normalize_claude(lines, verdict)
        self.assertEqual((two.exception.reason, verdict.get("zero_call")), ("shape_mismatch", None))
        with self.assertRaises(quota.Unavailable) as none:
            quota.normalize_claude(claude_lines()[:2], {})
        self.assertEqual(none.exception.reason, "exited")
        extra = self.claude(lambda lines: limit(lines).update(future_field={"x": 1}))   # extra fields are ignored
        self.assertEqual(extra["windows"][0]["used_percent"], 37)
        window = lambda initialize, answer: answer["result"]["rateLimits"]["primary"]
        codex_cases = {
            "account/rateLimits/read.result": lambda initialize, answer: answer.pop("result"),
            "initialize.result": lambda initialize, answer: initialize.update(result="ok"),
            "result.rateLimits": lambda initialize, answer: answer["result"].update(rateLimits=None),
            "result.rateLimits.limitId": lambda initialize, answer: answer["result"]["rateLimits"].pop("limitId"),
            "result.rateLimits.primary": lambda initialize, answer: window(initialize, answer).pop("usedPercent"),
            "result.rateLimits.primary.resetsAt": lambda initialize, answer: window(initialize, answer).update(
                resetsAt=10**12),
            "result.rateLimits.primary.windowDurationMins": lambda initialize, answer: window(
                initialize, answer).update(windowDurationMins=0),
            "result.rateLimits.credits.balance": lambda initialize, answer: answer["result"]["rateLimits"][
                "credits"].update(balance="1e5"),
            "result.ordinaryUsageAllowed": lambda initialize, answer: answer["result"].update(ordinaryUsageAllowed=1),
        }
        for field, edit in codex_cases.items():
            with self.subTest(field=field), self.assertRaises(quota.Shape) as caught:
                self.codex(edit)
            self.assertEqual(caught.exception.field, field)
        empty = self.codex(lambda initialize, answer: answer["result"]["rateLimits"].update(primary=None))
        self.assertEqual(empty["windows"], [])   # a null slot is "not reported", not a mismatch
        with self.assertRaises(quota.Unavailable) as error:
            self.codex(lambda initialize, answer: initialize.update(error={"code": -1, "message": "x"}))
        self.assertEqual((error.exception.reason, error.exception.detail), ("provider_error",
                                                                            {"method": "initialize", "code": -1}))


class CleanupTests(ProbeCase):
    def test_a_descendant_ignoring_sigterm_is_killed_whoever_exits_first(self):  # C1
        for provider, mode in (("claude", "child_ignores_term_leader_exits"),
                               ("claude", "child_ignores_term_leader_waits"),
                               ("codex", "child_ignores_term"), ("codex", "leader_waits_child_ignores")):
            with self.subTest(provider=provider, mode=mode):
                calls = quota_stubs.install(self, **{provider: mode})
                temp = TrackTemp(self)
                record = quota.snapshot(provider, environment())
                self.assertTrue(record["available"], record)
                self.assertTrue(record["process"]["exit_confirmed"], record["process"])
                self.assertEqual(len(calls.children(provider)), 1)
                self.assert_clean(calls, temp, provider)

    def test_an_interrupt_in_the_grace_cuts_it_short_and_still_kills_the_group(self):  # C2
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                calls = quota_stubs.install(self, **{provider: "eof_then_sigint"})
                temp = TrackTemp(self)
                begun = time.monotonic()
                # A 30 s grace makes the interrupt land inside it; only SIGKILL ends these stubs.
                with patch.object(quota, "TERM_GRACE_SECONDS", 30), patch.object(quota, "CLEANUP_SECONDS", 30), \
                        self.assertRaises(KeyboardInterrupt):
                    quota.snapshot(provider, environment())
                self.assertLess(time.monotonic() - begun, 15)
                self.assert_clean(calls, temp, provider)

    def test_codex_receives_exactly_three_messages(self):  # C3
        calls = quota_stubs.install(self)
        self.assertTrue(quota.snapshot("codex", environment())["available"])
        self.assertEqual(calls.rpc(), [("initialize", 0), ("initialized", None), ("account/rateLimits/read", 1)])
        self.assertTrue(all("jsonrpc" not in call["keys"] for call in calls.all() if call["cli"] == "codex-rpc"))

    def test_server_requests_get_no_answer(self):  # C4
        quota_stubs.install(self, codex="server_request")
        self.assertTrue(quota.snapshot("codex", environment())["available"])
        calls = quota_stubs.install(self, codex="server_request_only")
        temp = TrackTemp(self)
        with patch.object(quota, "QUERY_SECONDS", 1.0):
            record = quota.snapshot("codex", environment())
        self.assertEqual(record["reason"], "timeout")
        self.assertEqual(calls.rpc(), [("initialize", 0), ("initialized", None), ("account/rateLimits/read", 1)])
        self.assert_clean(calls, temp, "codex")

    def test_a_second_interrupt_in_cleanup_keeps_the_first_interrupt_object(self):  # C5
        calls = quota_stubs.install(self, claude="sigint_parent")
        real = os.killpg

        def killpg(pgid, sig):
            if sig == signal.SIGTERM:
                os.kill(os.getpid(), signal.SIGINT)   # a second Ctrl-C, certainly inside the cleanup
            return real(pgid, sig)
        with patch.object(os, "killpg", killpg), self.assertRaises(KeyboardInterrupt) as caught:
            quota.snapshot("claude", environment())
        self.assertIsNone(caught.exception.__context__)   # the first interrupt, not a new one raised over it
        self.assertTrue(group_gone(calls.probes("claude")[-1]["pid"]))


class SignalVerdictTests(ProbeCase):
    """The first user interrupt during cleanup propagates afterwards, whatever else is in flight."""

    def test_after_a_timeout(self):  # C6
        calls = quota_stubs.install(self, claude="hang_sigint_on_term")
        temp = TrackTemp(self)
        with patch.object(quota, "QUERY_SECONDS", 1.0), self.assertRaises(KeyboardInterrupt):
            quota.snapshot("claude", environment())
        self.assert_clean(calls, temp, "claude")

    def test_after_a_shape_error(self):  # C7
        calls = quota_stubs.install(self, claude="badline_sigint_on_term")
        temp = TrackTemp(self)
        with self.assertRaises(KeyboardInterrupt):
            quota.snapshot("claude", environment())
        self.assert_clean(calls, temp, "claude")

    def test_while_an_outer_exception_propagates(self):  # C8
        calls = quota_stubs.install(self, claude="sigint_on_term_after:0")
        temp = TrackTemp(self)
        with self.assertRaises(KeyboardInterrupt):
            try:
                raise RuntimeError("a stop being propagated")
            except RuntimeError:
                quota.snapshot("claude", environment())
        self.assert_clean(calls, temp, "claude")

    def test_nothing_may_reap_the_leader_before_the_group_is_killed(self):  # C9
        names = ("poll", "wait", "communicate", "send_signal", "terminate", "kill", "__exit__")
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                quota_stubs.install(self)
                events, original = [], {name: getattr(subprocess.Popen, name) for name in names}
                real_killpg, real_waitpid = os.killpg, os.waitpid

                def wrap(name):
                    def call(process, *args, **kwargs):
                        events.append(name)
                        return original[name](process, *args, **kwargs)
                    return call

                def killpg(pgid, sig):
                    events.append(("killpg", sig))
                    return real_killpg(pgid, sig)

                def waitpid(*args):
                    events.append("waitpid")
                    return real_waitpid(*args)
                with contextlib.ExitStack() as stack:
                    for name in names:
                        stack.enter_context(patch.object(subprocess.Popen, name, wrap(name)))
                    stack.enter_context(patch.object(os, "killpg", killpg))
                    stack.enter_context(patch.object(os, "waitpid", waitpid))
                    record = quota.snapshot(provider, environment())
                self.assertTrue(record["available"], record)
                kill = events.index(("killpg", signal.SIGKILL))
                self.assertEqual(events[:kill], [("killpg", signal.SIGTERM)])
                self.assertIn("wait", events[kill:])
                self.assertNotIn(("killpg", signal.SIGTERM), events[kill:])
                self.assertNotIn(("killpg", signal.SIGKILL), events[kill + 1:])
                self.assertTrue(record["process"]["kill_sent"] and record["process"]["temp_removed"])

    def test_a_revision_beyond_ten_digits_has_no_name(self):  # C10
        record = {"ledger_revision": quota.REVISION_MAX, "sequence": 1, "process_started_at": 1900000000.0,
                  "provider": "claude", "trigger": "before_use"}
        self.assertEqual(quota.sample_name(record), "r9999999999-p1-20300317T174640.000000Z-s0001-claude-before_use.json")
        for change in ({"ledger_revision": quota.REVISION_MAX + 1}, {"ledger_revision": -1},
                       {"ledger_revision": None}, {"sequence": 10000}, {"process_started_at": float("nan")}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                quota.sample_name({**record, **change})

    def test_an_interrupt_in_the_start_window_is_not_swallowed(self):  # C11
        quota_stubs.install(self)
        temp = TrackTemp(self)
        original, started = subprocess.Popen, []

        def interrupting(argv, *args, **kwargs):
            process = original(argv, *args, **kwargs)
            started.append(process.pid)
            os.kill(os.getpid(), signal.SIGINT)   # the user's Ctrl-C inside the Popen window
            return process
        with patch("harness.quota.subprocess.Popen", side_effect=interrupting), self.assertRaises(KeyboardInterrupt):
            try:
                raise RuntimeError("a stop being propagated")
            except RuntimeError:
                quota.snapshot("claude", environment())
        self.assertEqual(len(started), 1)
        self.assertTrue(group_gone(started[0]))
        self.assertTrue(temp.removed())


if __name__ == "__main__":
    unittest.main()
