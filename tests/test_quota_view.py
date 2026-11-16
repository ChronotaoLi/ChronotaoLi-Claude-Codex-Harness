"""The observed account quota during a run: reset cycles, completeness, flags, credits and decimals.

Every sample is synthetic (spec-7 3.8, one test per row of the boundary table, and the
named examples of 8.3). ``quota_view`` is pure: it reads samples already in their order.
"""
import decimal
import json
import unittest

from harness import quota

T = 1900000000.0
WEEK = 10080


def claude(at, windows, *, revision=None, available=True, reason=None):
    """One Claude sample observed at ``at``; ``windows``: (kind, percent, reset[, display name])."""
    rows = []
    for item in windows:
        kind, percent, reset = item[:3]
        model = item[3] if len(item) > 3 else None
        scope = {"model": {"display_name": model}, "surface": None} if model else None
        rows.append({"kind": kind, "group": "weekly", "scope": scope, "scope_model": model, "used_percent": percent,
                     "resets_at": reset, "resets_at_raw": None, "is_active": True, "severity": None})
    return _sample("claude", at, revision, available, reason, windows=rows,
                   extra_usage={"is_enabled": False, "monthly_limit": None, "used_credits": None,
                                "utilization": None, "currency": None} if available else None)


def codex(at, windows=(), credits=None, *, limit="codex", revision=None, available=True, reason=None,
          reported=True):
    """One Codex sample; ``windows``: (slot, minutes, percent, reset); ``credits``: (unlimited, balance)."""
    rows = [{"limit_id": limit, "slot": slot, "window_minutes": minutes, "used_percent": percent,
             "resets_at": reset, "resets_at_raw": reset} for slot, minutes, percent, reset in windows]
    limits = []
    if available and reported:
        limits = [{"limit_id": limit, "limit_name": None, "rate_limit_reached_type": None,
                   "spend_control_reached": False,
                   "credits": None if credits is None else {"has_credits": True, "unlimited": credits[0],
                                                            "balance": credits[1]}}]
    return _sample("codex", at, revision, available, reason, windows=rows, limits=limits,
                   ordinary_usage_allowed=True if available else None)


_revisions = iter(range(1, 10**6))


def _sample(provider, at, revision, available, reason, **body):
    head = {"version": 1, "provider": provider, "trigger": "stop", "dispatch_id": None, "role": None,
            "operation_id": None, "ledger_revision": revision if revision is not None else next(_revisions),
            "process_started_at": T - 3600, "sequence": 1}
    observed = {"provider": provider, "source": {"command": [], "cli_version": None}, "queried_at": at,
                "observed_at": at, "available": available, "reason": None if available else (reason or "timeout"),
                "detail": None, "process": None, "zero_call": None, "windows": [], "limits": [],
                "ordinary_usage_allowed": None, "extra_usage": None, "unparsed": []}
    if available:
        observed.update(body)
    record = quota._assemble(head, observed)
    assert record["reason"] != "internal_error", record   # a fixture the reader would refuse
    return record


def view(samples, truncated=False):
    return quota.quota_view(samples, truncated)


def bucket(samples, provider="codex", index=0, truncated=False):
    return view(samples, truncated)["providers"][provider]["buckets"][index]


def changes(segments):
    return [(segment["observations"], segment["change_points"]) for segment in segments]


class CycleTests(unittest.TestCase):
    """Rows of the boundary table (spec-7 3.8) that split or join reset cycles."""

    def test_a_single_sample_is_one_complete_point(self):
        segment = bucket([codex(T, [("primary", 300, 40, T + 600)])])["segments"]
        self.assertEqual(changes(segment), [(1, None)])
        self.assertTrue(segment[0]["complete"])

    def test_a_missing_baseline_is_the_first_available_observation(self):
        samples = [codex(T, available=False), codex(T + 10, [("primary", 300, 40, T + 600)]),
                   codex(T + 20, [("primary", 300, 45, T + 600)])]
        segment = bucket(samples)["segments"][0]
        self.assertEqual((segment["incomplete_start"], segment["change_points"], segment["complete"]),
                         ("first_available_observation", 5, False))

    def test_truncation_marks_older_samples_not_read(self):
        samples = [codex(T, [("primary", 300, 40, T + 600)]), codex(T + 10, [("primary", 300, 42, T + 600)])]
        shown = view(samples, truncated=True)
        segment = shown["providers"]["codex"]["buckets"][0]["segments"][0]
        self.assertEqual((segment["incomplete_start"], segment["change_points"]), ("older_samples_not_read", 2))
        self.assertIn(quota.TRUNCATED_NOTE, shown["notes"])
        self.assertNotIn(quota.TRUNCATED_NOTE, view(samples)["notes"])

    def test_reaching_the_reset_starts_a_new_cycle_whatever_the_reset_says(self):
        # The second reset is within the tolerance of the first, but the observation passed the first reset.
        samples = [codex(T, [("primary", WEEK, 40, T + 1000)]), codex(T + 1005, [("primary", WEEK, 2, T + 1060)])]
        first, second = bucket(samples)["segments"]
        self.assertEqual(changes([first, second]), [(1, None), (1, None)])
        self.assertEqual((first["incomplete_end"], second["incomplete_start"]), ("before_reset", "after_reset"))
        self.assertNotIn("decrease", first["flags"] + second["flags"])
        # Each side measured on its own, never across the reset and never "up to 100".
        samples = [codex(T, [("primary", WEEK, 40, T + 1000)]), codex(T + 500, [("primary", WEEK, 70, T + 1000)]),
                   codex(T + 1005, [("primary", WEEK, 2, T + 9000)]), codex(T + 2000, [("primary", WEEK, 9, T + 9000)])]
        self.assertEqual(changes(bucket(samples)["segments"]), [(2, 30), (2, 7)])

    def test_a_reset_moved_beyond_the_tolerance_before_the_old_one_is_a_new_cycle(self):
        samples = [codex(T, [("primary", 1, 95, T + 60)]), codex(T + 10, [("primary", 1, 5, T + 100)])]
        first, second = bucket(samples)["segments"]
        self.assertEqual(changes([first, second]), [(1, None), (1, None)])
        self.assertIn("reset_changed_within_cycle", second["flags"])
        self.assertNotIn("decrease", first["flags"] + second["flags"])

    def test_a_reset_moved_earlier_but_still_ahead_stays_in_the_cycle(self):
        samples = [claude(T, [("weekly_all", 40, T + 100)]), claude(T + 50, [("weekly_all", 45, T + 80)])]
        segments = bucket(samples, "claude")["segments"]
        self.assertEqual(changes(segments), [(2, 5)])
        self.assertEqual(segments[0]["resets_at"], quota._iso(T + 80))

    def test_a_reset_moved_earlier_and_already_past_is_never_subtracted(self):
        # The confirmation review's counter-example: never -90, never "decrease".
        samples = [claude(T, [("weekly_all", 95, T + 100)]), claude(T + 90, [("weekly_all", 5, T + 80)])]
        first, second = bucket(samples, "claude")["segments"]
        self.assertEqual(changes([first, second]), [(1, None), (1, None)])
        self.assertIn("reset_already_passed", second["flags"])
        self.assertIsNone(second["resets_at"])
        self.assertNotIn("decrease", first["flags"] + second["flags"])
        self.assertNotIn("-90", json.dumps(view(samples)))
        # A reset equal to its own observation time has already been reached as well.
        samples = [claude(T, [("weekly_all", 95, T + 100)]), claude(T + 90, [("weekly_all", 5, T + 90)])]
        self.assertEqual(changes(bucket(samples, "claude")["segments"]), [(1, None), (1, None)])

    def test_a_short_window_reset_is_a_new_cycle_not_a_decrease(self):
        samples = [codex(T, [("primary", 1, 95, T + 30)]), codex(T + 40, [("primary", 1, 5, T + 90)])]
        segments = bucket(samples)["segments"]
        self.assertEqual(changes(segments), [(1, None), (1, None)])
        self.assertFalse([flag for segment in segments for flag in segment["flags"] if flag == "decrease"])

    def test_chained_drift_is_measured_against_the_anchor(self):
        reset = T + 100000
        samples = [codex(T + i, [("primary", WEEK, 10 + i, reset + 100 * i)]) for i in range(5)]
        self.assertEqual([segment["observations"] for segment in bucket(samples)["segments"]], [2, 2, 1])
        # The tolerance follows the reported window length, capped at 120 s; unknown length: 60 s.
        self.assertEqual([quota.reset_tolerance({"window_minutes": minutes}) for minutes in (1, 60, 300, WEEK, None)],
                         [1, 60, 120, 120, 60])

    def test_a_decrease_within_a_cycle_is_flagged_and_counted(self):
        samples = [codex(T, [("primary", 300, 50, T + 600)]), codex(T + 10, [("primary", 300, 47, T + 600)])]
        segment = bucket(samples)["segments"][0]
        self.assertEqual((segment["change_points"], segment["flags"]), (-3, ["decrease"]))

    def test_an_increase_within_a_cycle(self):
        samples = [codex(T, [("primary", 300, 42, T + 600)]), codex(T + 10, [("primary", 300, 47, T + 600)])]
        segment = bucket(samples)["segments"][0]
        self.assertEqual((segment["change_points"], segment["complete"], segment["flags"]), (5, True, []))

    def test_a_bucket_reported_by_only_some_samples(self):
        both = [("weekly_all", 30, T + 900), ("session", 10, T + 300)]
        samples = [claude(T, both[:1]), claude(T + 10, both), claude(T + 20, both[:1])]
        session = next(item for item in view(samples)["providers"]["claude"]["buckets"] if item["bucket"] == "session")
        segment = session["segments"][0]
        self.assertEqual((segment["incomplete_start"], segment["incomplete_end"]),
                         ("first_available_observation", "later_samples_missing_bucket_or_unavailable"))
        self.assertEqual(bucket(samples, "claude")["segments"][0]["complete"], True)

    def test_an_unavailable_sample_in_between_keeps_the_cycle(self):
        samples = [codex(T, [("primary", 300, 40, T + 600)]), codex(T + 5, available=False, reason="timeout"),
                   codex(T + 10, [("primary", 300, 44, T + 600)])]
        shown = view(samples)["providers"]["codex"]
        self.assertEqual(changes(shown["buckets"][0]["segments"]), [(2, 4)])
        self.assertTrue(shown["buckets"][0]["segments"][0]["complete"])
        self.assertEqual(shown["unavailable"], {"timeout": 1})

    def test_unknown_or_expired_resets_are_their_own_unknown_segments(self):
        samples = [codex(T, [("primary", 300, 40, T + 600)]), codex(T + 10, [("primary", 300, 41, None)]),
                   codex(T + 20, [("primary", 300, 45, None)]), codex(T + 30, [("primary", 300, 46, T + 600)])]
        segments = bucket(samples)["segments"]
        self.assertEqual(changes(segments), [(1, None), (2, None), (1, None)])
        self.assertEqual(segments[1]["flags"], ["resets_at_unknown"])
        expired = [codex(T, [("primary", 300, 40, T - 5)]), codex(T + 10, [("primary", 300, 30, T - 5)])]
        segment = bucket(expired)["segments"][0]
        self.assertEqual((segment["change_points"], segment["flags"]), (None, ["reset_already_passed"]))

    def test_a_clock_rollback_is_flagged_and_the_order_stays_by_revision(self):
        samples = [codex(T + 100, [("primary", 300, 40, T + 600)], revision=1),
                   codex(T + 50, [("primary", 300, 44, T + 600)], revision=2)]
        shown = view(samples)["providers"]["codex"]
        self.assertTrue(shown["clock_rollback"])
        self.assertEqual(changes(shown["buckets"][0]["segments"]), [(2, 4)])
        self.assertEqual(shown["buckets"][0]["latest"]["used_percent"], 44)
        self.assertFalse(view(list(reversed(samples)))["providers"]["codex"]["clock_rollback"])


class BucketTests(unittest.TestCase):
    def test_two_overlapping_weekly_windows_are_not_added(self):
        reset = T + 500000
        samples = [claude(T, [("weekly_all", 30, reset), ("weekly_scoped", 40, reset, "Model Z")]),
                   claude(T + 60, [("weekly_all", 33, reset), ("weekly_scoped", 45, reset, "Model Z")])]
        shown = view(samples)
        found = {item["bucket"]: item["segments"][0]["change_points"] for item in shown["providers"]["claude"]["buckets"]}
        self.assertEqual(found, {"weekly_all": 3, "weekly_scoped（Model Z）": 5})
        self.assertNotIn("total", json.dumps(shown))

    def test_an_unrelated_models_full_window_is_its_own_bucket(self):
        reset = T + 500000
        samples = [claude(T, [("weekly_all", 30, reset), ("weekly_scoped", 100, reset, "Model Y")]),
                   claude(T + 60, [("weekly_all", 34, reset), ("weekly_scoped", 100, reset, "Model Y")])]
        found = {item["bucket"]: item for item in view(samples)["providers"]["claude"]["buckets"]}
        self.assertEqual(found["weekly_all"]["segments"][0]["change_points"], 4)
        self.assertEqual((found["weekly_scoped（Model Y）"]["latest"]["used_percent"],
                          found["weekly_scoped（Model Y）"]["segments"][0]["change_points"]), (100, 0))

    def test_codex_slots_and_window_lengths_are_separate_buckets_as_reported(self):
        samples = [codex(T, [("primary", 300, 20, T + 600), ("secondary", WEEK, 60, T + 90000)]),
                   codex(T + 10, [("primary", 4320, 25, T + 600)])]
        labels = [item["bucket"] for item in view(samples)["providers"]["codex"]["buckets"]]
        self.assertEqual(labels, ["codex.primary（300 分钟）", "codex.secondary（10080 分钟）",
                                  "codex.primary（4320 分钟）"])


class CreditTests(unittest.TestCase):
    def runs(self, samples, limit="codex"):
        return view(samples)["providers"]["codex"]["credits"][limit]

    def test_credits_turning_unlimited_end_the_balance_run(self):
        samples = [codex(T, credits=(False, "41.25")), codex(T + 10, credits=(False, "40.00")),
                   codex(T + 20, credits=(True, "99.00")), codex(T + 30, credits=(False, "12.50")),
                   codex(T + 40, credits=(False, "12.25"))]
        credits = self.runs(samples)
        self.assertEqual([(run["first"], run["last"], run["net_change"]) for run in credits["runs"]],
                         [("41.25", "40.00", "-1.25"), ("12.50", "12.25", "-0.25")])
        self.assertEqual(credits["latest"]["balance"], "12.25")

    def test_a_sample_without_credits_does_not_end_the_run(self):
        samples = [codex(T, credits=(False, "5.00")), codex(T + 10, credits=None),
                   codex(T + 20, reported=False), codex(T + 25, available=False),
                   codex(T + 30, credits=(False, "4.50"))]
        self.assertEqual([(run["observations"], run["net_change"]) for run in self.runs(samples)["runs"]],
                         [(2, "-0.50")])

    def test_a_full_window_with_falling_credits(self):
        samples = [codex(T, [("primary", 300, 100, T + 600)], (False, "10.3")),
                   codex(T + 10, [("primary", 300, 100, T + 600)], (False, "10.1"))]
        shown = view(samples)["providers"]["codex"]
        self.assertEqual(changes(shown["buckets"][0]["segments"]), [(2, 0)])
        self.assertEqual(shown["credits"]["codex"]["runs"][0]["net_change"], "-0.2")

    def test_one_balance_observation_has_no_change(self):
        self.assertEqual([(run["observations"], run["net_change"]) for run in
                          self.runs([codex(T, credits=(False, "7"))])["runs"]], [(1, None)])

    def test_balance_differences_are_exact_at_the_schema_limits(self):
        nines = "9" * 30 + "." + "9" * 30
        reference = decimal.Context(prec=200)
        before = decimal.getcontext().prec
        for first, last in (("0", "0.123456789012345678901234567891"), ("-" + nines, nines), (nines, "-" + nines),
                            ("10.3", "10.1"), ("5.00", "5"), ("-0.000000000000000000000000000001", "0"),
                            ("0", "-0.000")):
            with self.subTest(first=first, last=last):
                exact = reference.subtract(decimal.Decimal(last), decimal.Decimal(first))
                expected = format(exact.copy_abs() if exact.is_zero() else exact, "f")
                self.assertEqual(quota.decimal_difference(last, first), expected)
                shown = self.runs([codex(T, credits=(False, first)), codex(T + 10, credits=(False, last))])
                self.assertEqual(shown["runs"][0]["net_change"], expected)
        self.assertEqual(quota.decimal_difference("10.1", "10.3"), "-0.2")
        self.assertEqual(quota.decimal_difference("0", "-0"), "0")
        self.assertIsNone(quota.decimal_difference("1" * 70, "0"))   # beyond 64 digits: unknown, never rounded
        self.assertIsNone(quota.decimal_difference("x", "0"))
        self.assertEqual(decimal.getcontext().prec, before)


class SummaryTests(unittest.TestCase):
    def test_states_notes_and_no_totals(self):
        samples = [codex(T, available=False, reason="timeout"), codex(T + 10, available=False, reason="exited"),
                   claude(T, [("session", 10, T + 300)])]
        shown = view(samples)
        self.assertEqual(shown["providers"]["codex"]["state"], "all_unavailable")
        self.assertEqual(shown["providers"]["codex"]["unavailable"], {"timeout": 1, "exited": 1})
        self.assertEqual((shown["providers"]["codex"]["buckets"], shown["providers"]["codex"]["credits"]), ([], {}))
        self.assertEqual(view([])["providers"], {"claude": {"state": "no_samples"}, "codex": {"state": "no_samples"}})
        self.assertEqual(shown["notes"], list(quota.NOTES) + list(quota.VIEW_NOTES))
        text = json.dumps(shown, ensure_ascii=False)
        for word in ("total", "合计", "current", "当前"):
            self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
