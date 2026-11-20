"""Stop values: registry, bounds, ledger rules, one projection everywhere. All calls are simulated."""
import copy
import io
import json
import random
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import stops
from harness.run_context import RunContext
from harness.util import HarnessError, ProcessTimeout, read_json, write_json
from test_harness import ROOT, RepoCase, commit_recorder, no_command_exit
from test_operations import OperationCase
from test_runtime_changes import ScriptedBackend

REMOVED = ("error", "error_type", "last_runtime_error")


def plan():
    return read_json(ROOT / "examples" / "plan.json")


def notices(stderr):
    return [json.loads(line.split(" ", 1)[1]) for line in stderr.splitlines()
            if line.startswith("[harness-json] ") and '"event":"stop"' in line]


class RegistryTests(unittest.TestCase):
    def test_registry_shape_and_projection(self):
        self.assertEqual(len(stops.REGISTRY), 65)
        self.assertLessEqual({("plan_approval", "initial_plan"), ("interrupted", "signal"),
                              ("unclassified", "untyped_harness_error"), ("unclassified", "unexpected_exception")},
                             set(stops.REGISTRY))
        codes = {key: entry["exit_code"] for key, entry in stops.REGISTRY.items()}
        self.assertEqual(codes[("plan_approval", "initial_plan")], 0)
        self.assertEqual(codes[("interrupted", "signal")], 130)
        self.assertEqual(codes[("unclassified", "untyped_harness_error")], 1)
        self.assertEqual(codes[("unclassified", "unexpected_exception")], 1)
        with self.assertRaises(KeyError):
            stops.make("budget", "no_such_reason")
        with self.assertRaises(KeyError):
            stops.make("interrupted", "signal", data={"unknown": 1})
        with self.assertRaises(KeyError):
            stops.make("plan_approval", "initial_plan", source={"task_id": "t"})
        value = stops.make("plan_approval", "initial_plan", data={"revision": 0, "task_count": 2},
                           source={"artifact": "/runs/r/plans/0.json", "artifact_digest": "a" * 64})
        stops.validate(value)
        projected = stops.project(value, "run_ab12")
        self.assertEqual(projected["exits"], ["status run_ab12 --full", "resume run_ab12 --approve-plan"])
        self.assertEqual((projected["title"], projected["exit_code"]), ("计划等待明确批准", 0))
        self.assertEqual(projected["data"], {"revision": 0, "task_count": 2})
        self.assertNotIn("detail", stops.project(value, "run_ab12", omit_detail=True))

    def test_transport_failure_row_projects_like_the_other_provider_failures(self):
        source = {"dispatch_id": "w1", "task_id": "greet", "artifact": "/runs/r/dispatches/w1/process.stdout",
                  "artifact_digest": "b" * 64}
        value = stops.make("provider_failure", "transport", data={"role": "worker", "provider": "claude"},
                           source=source)
        stops.validate(value)
        projected = stops.project(value, "run_ab12")
        self.assertEqual((projected["exits"], projected["exit_code"]), (["resume run_ab12"], 3))
        self.assertEqual((projected["data"], projected["source"]), ({"role": "worker", "provider": "claude"}, source))
        failed = stops.REGISTRY[("provider_failure", "invocation_failed")]
        row = stops.REGISTRY[("provider_failure", "transport")]
        self.assertEqual((set(row["data"]), row["source"], row["exits"]), (set(failed["data"]), failed["source"], failed["exits"]))
        bad = stops.make("provider_failure", "transport", data={"role": "worker", "provider": "gemini", "http_status": "x"})
        self.assertEqual(sorted(bad["data"]["dropped_fields"]), ["http_status", "provider"])

    def test_call_failure_facts_are_registered_on_the_ten_failure_rows_and_checked(self):
        facts = {"api_error", "api_error_type", "terminal_reason", "unrecognized_labels", "http_status_state",
                 "exit_confirmed", "attempt_charged"}
        rows = {key for key, entry in stops.REGISTRY.items() if facts <= set(entry["data"])}
        self.assertEqual(rows, {("provider_quota", "usage_limit"), ("provider_quota", "rate_limit"),
                                ("provider_failure", "overloaded"), ("provider_failure", "invocation_failed"),
                                ("provider_failure", "local_start"), ("provider_failure", "unknown"),
                                ("provider_failure", "authentication"), ("provider_failure", "transport"),
                                ("budget", "agent_timeout"), ("budget", "agent_turns")})
        self.assertFalse(any(set(entry["data"]) & facts for key, entry in stops.REGISTRY.items() if key not in rows))
        source = {"dispatch_id": "w1", "task_id": "greet"}
        shown = {"role": "worker", "provider": "claude", "api_error": "brand_new_kind", "api_error_type": "billing_error",
                 "terminal_reason": "api_error", "unrecognized_labels": ["api_error_type", "terminal_reason"],
                 "http_status_state": "null", "exit_confirmed": True, "attempt_charged": False}
        value = stops.make("provider_failure", "transport", data=dict(shown), source=source)
        stops.validate(value)
        projected = stops.project(value, "run_ab12")
        self.assertEqual((projected["data"], projected["exits"]), (shown, ["resume run_ab12"]))
        self.assertEqual(stops.make("budget", "agent_turns", data={"role": "worker", "exit_confirmed": False})["data"],
                         {"role": "worker", "exit_confirmed": False})
        # A label off the identifier shape, a body type outside the CLI's table, an unknown, repeated or empty
        # list of names, an unknown HTTP state or a non-boolean fact is dropped and named, never kept.
        for name, item in (("api_error", "UNRECOGNIZED_SECRET\n"), ("api_error", "x" * 65), ("api_error", "9lives"),
                           ("api_error", ["x"]), ("terminal_reason", None), ("terminal_reason", "api-error"),
                           ("api_error_type", "private_project_olive"), ("api_error_type", "rate_limit_error\n"),
                           ("unrecognized_labels", []), ("unrecognized_labels", ["subtype"]),
                           ("unrecognized_labels", ["api_error", "api_error"]), ("unrecognized_labels", "api_error"),
                           ("unrecognized_labels", [["api_error"]]), ("http_status_state", "not_provided"),
                           ("exit_confirmed", 1), ("attempt_charged", "false")):
            with self.subTest(field=name, value=item):
                made = stops.make("provider_failure", "invocation_failed", data={"role": "worker", name: item},
                                  source=source)
                stops.validate(made)
                self.assertEqual((made["kind"], made["data"]["dropped_fields"]), ("provider_failure", [name]))
                self.assertNotIn(name, made["data"])
                with self.assertRaises(HarnessError):
                    stops.validate({**made, "data": {"role": "worker", name: item}})

    def test_strings_map_drops_bad_values_keeps_heads_and_fits_the_whole_value(self):
        key = ("scope_extension", "revision")
        bad = stops.make(*key, data={"gaps": {"not an id!": ["x"]}, "suggested_scope": ["x"]})
        self.assertEqual((bad["kind"], bad["data"]["dropped_fields"]), ("scope_extension", ["gaps"]))
        bad = stops.make(*key, data={"gaps": {"t": [1]}})
        self.assertEqual(bad["data"]["dropped_fields"], ["gaps"])
        big = {f"t{i}": [f"dir{i}/" + "x" * 300 + f"/{j}" for j in range(20)] for i in range(5)}
        value = stops.make(*key, data={"gaps": big})
        kept = value["data"]["gaps"]
        self.assertLessEqual(len(json.dumps(kept, ensure_ascii=True)), stops.LIST_BYTES)
        self.assertEqual(sum(map(len, kept.values())) + value["data"]["gaps_omitted"], 100)
        self.assertEqual(list(kept)[0], "t0")  # task order, then pattern order
        self.assertEqual(kept["t0"], big["t0"][:len(kept["t0"])])
        # Five full lists exceed the whole value's limit: the later-listed ones shrink first.
        full = ["p/" + "y" * 200 + f"/{i}" for i in range(200)]
        value = stops.make(*key, data={"suggested_scope": full, "gaps": big, "scope": full, "widened": full,
                                       "missing_commands": {"t0": [{"cwd": ".", "argv": ["cmd", "z" * 100]}] * 20}})
        error = HarnessError("\u4e2d" * 5000)
        error.stop = value
        final = stops.of(error)
        self.assertEqual((final["kind"], final["reason"]), key)
        self.assertLessEqual(len(json.dumps(final, ensure_ascii=True)), stops.STOP_LIMIT)
        self.assertEqual(final["data"]["widened"], [])
        self.assertGreater(len(final["data"]["suggested_scope"]), 0)

    def test_detail_total_length_is_bounded_including_the_note(self):
        for length in (0, 1, 1999, 2000, 2001, 2017, 5000, 200000):
            with self.subTest(length=length):
                text = "x" * length
                excerpt = stops.bounded(text)
                self.assertLessEqual(len(excerpt), 2000)
                if length <= 2000:
                    self.assertEqual(excerpt, text)
                else:
                    self.assertIn(f"原文 {length} 字符", excerpt)
                    self.assertTrue(excerpt.startswith("x" * 100))
                value = stops.of(HarnessError(text))
                stops.validate(value)
                self.assertLessEqual(len(value["data"]["detail"]), 2000)

    def test_of_validates_for_arbitrary_text_and_exception_types(self):
        alphabet = ["a", "\0", "\ud800", "\U0001F600", '"', "\\", "\n", "停", " "]
        generator = random.Random(4)
        kinds = (HarnessError, ValueError, OSError, KeyboardInterrupt)
        for index in range(400):
            text = "".join(generator.choice(alphabet) for _ in range(generator.randint(0, 3000)))
            error = kinds[index % len(kinds)](text)
            value = stops.of(error)
            stops.validate(value)
            self.assertLessEqual(len(value["data"]["detail"]), 2000)
        self.assertEqual(stops.of(HarnessError("x"))["reason"], "untyped_harness_error")
        self.assertEqual(stops.of(ValueError("x"))["data"]["error_type"], "ValueError")
        self.assertEqual(stops.of(KeyboardInterrupt())["kind"], "interrupted")
        self.assertEqual(stops.of(SystemExit(2))["kind"], "interrupted")

    def test_unfit_field_values_are_dropped_not_raised(self):
        value = stops.make("plan_approval", "initial_plan", data={"revision": -1, "task_count": True},
                           source={"artifact": "relative/path", "artifact_digest": "not-a-digest"})
        stops.validate(value)
        self.assertEqual(value["data"]["dropped_fields"], ["artifact", "artifact_digest", "revision", "task_count"])
        self.assertEqual(value["source"], {})
        # A carried value that is not a valid stop never breaks the boundary.
        error = HarnessError("text", stop={"kind": "nonsense"})
        self.assertEqual(stops.of(error)["data"]["error_type"], "InvalidStop")

    def test_omission_counts_belong_to_this_rows_list_fields(self):
        key, base = ("provider_quota", "usage_limit"), {"role": "worker", "provider": "claude"}
        windows = [{"bucket": f"b{n}", "used_percent": 100, "resets_at": None} for n in range(3)]
        for bad in (-1, True, "2", 1.5, None):
            with self.subTest(count=bad):
                value = stops.make(*key, data={**base, "full_windows": windows, "full_windows_omitted": bad})
                stops.validate(value)
                self.assertEqual((value["data"]["full_windows"], value["data"]["dropped_fields"]),
                                 (windows, ["full_windows_omitted"]))
                self.assertNotIn("full_windows_omitted", value["data"])
        # Only a list field of this row carries a count: not a scalar, the common dropped_fields, or another row's list.
        for row, name in ((key, "role_omitted"), (key, "dropped_fields_omitted"),
                          (("budget", "agent_dispatches"), "full_windows_omitted")):
            with self.subTest(row=row, field=name), self.assertRaises(KeyError):
                stops.make(*row, data={name: 1})
        foreign = stops.make("budget", "agent_dispatches", data={"used": 1, "limit": 1})
        foreign["data"]["full_windows_omitted"] = 1
        with self.assertRaises(HarnessError):
            stops.validate(foreign)
        # An over-long dropped_fields is dropped like any unfit value, never shortened into a count no row accepts.
        value = stops.make(*key, data={**base, "dropped_fields": ["x" * 4000] * 3})
        stops.validate(value)
        self.assertEqual(value["data"]["dropped_fields"], ["dropped_fields"])
        self.assertEqual(stops.make(*key, data=value["data"]), value)


class FitProperty(unittest.TestCase):
    """The whole stop value fits 32 KiB without losing its type, exit code or registered exits (code-6 review)."""

    # BMP, CJK, a supplementary character (12 escaped bytes), a lone surrogate and plain ASCII.
    ALPHABET = ["a", "Z", "/", " ", "'", "-", "中", "é", "\U0001F600", "\U00010348", "\ud800", "x"]

    def text(self, rng, low, high):
        return "".join(rng.choice(self.ALPHABET) for _ in range(rng.randint(low, high)))

    def ident(self, rng):
        return rng.choice("Ab9") + "".join(rng.choice("abcXYZ019_.:-") for _ in range(rng.randint(0, 119)))

    def value(self, rng, check):
        """A well-formed, often oversized value of the type ``check``; single items stay within one field's limit."""
        many = rng.choice((1, 5, 40, 200))
        if check is stops._int:
            return rng.randint(0, 10 ** 12)
        if check is stops._bool:
            return rng.choice((True, False))
        if check is stops._id:
            return self.ident(rng)
        if check is stops._key:
            return self.text(rng, 1, 120)
        if check is stops._text:
            return self.text(rng, 0, 2000)
        if check is stops._relpath:
            return "p" + self.text(rng, 0, 1023).replace("\0", "")
        if check is stops._superseded:
            return {"kind": self.ident(rng), "reason": self.ident(rng)}
        if hasattr(check, "values"):
            return rng.choice(check.values)
        if check is stops._label:
            return rng.choice("abz") + "".join(rng.choice("az09_") for _ in range(rng.randint(0, 63)))
        if check is stops._label_names:
            return rng.sample(stops.LABEL_FIELDS, rng.randint(1, len(stops.LABEL_FIELDS)))
        if check is stops._strings:
            return [self.text(rng, 1, 600) for _ in range(many)]
        if check is stops._command_list:
            return [{"cwd": self.text(rng, 1, 60), "argv": [self.text(rng, 1, 60) for _ in range(rng.randint(1, 6))]}
                    for _ in range(many)]
        if check is stops._command_map:
            return {self.ident(rng): self.value(rng, stops._command_list) for _ in range(rng.randint(1, 6))}
        if check is stops._strings_map:
            return {self.ident(rng): [self.text(rng, 1, 600) for _ in range(many)] for _ in range(rng.randint(1, 6))}
        if check is stops._quota_windows:
            return [{"bucket": self.text(rng, 1, 160), "used_percent": rng.choice((100, 250.5)),
                     "resets_at": rng.choice((None, self.text(rng, 1, 40)))} for _ in range(rng.choice((1, 16, 40)))]
        raise AssertionError(f"no generator for {check}")

    def source(self, rng, name):
        if name == "artifact":
            return "/" + self.text(rng, 0, 1023).replace("\0", "")
        if name == "artifact_digest":
            return "".join(rng.choice("0123456789abcdef") for _ in range(64))
        return self.ident(rng)

    def test_every_row_keeps_its_type_exit_code_and_exits(self):
        rng = random.Random(20261114)
        for key, entry in stops.REGISTRY.items():
            conditions = [item[1] for item in entry["exits"] if isinstance(item, tuple)]
            for trial in range(20):
                data = {name: self.value(rng, check) for name, check in entry["data"].items()}
                if rng.random() < 0.5:
                    data["superseded"] = self.value(rng, stops._superseded)
                source = {name: self.source(rng, name) for name in entry["source"]}
                with self.subTest(row=key, trial=trial):
                    made = stops.make(*key, data=copy.deepcopy(data), source=source)
                    self.assertEqual((made["kind"], made["reason"]), key)
                    # Half the diagnostics are supplementary characters only: 12 escaped bytes each.
                    error = HarnessError(self.text(rng, 0, 5000) if trial % 2 else "\U0001F600" * rng.randint(1500, 5000))
                    error.stop = made
                    final = stops.of(error)
                    stops.validate(final)
                    self.assertLessEqual(len(json.dumps(final, ensure_ascii=True)), stops.STOP_LIMIT)
                    self.assertEqual((final["kind"], final["reason"]), key)
                    self.assertEqual(stops.exit_code(error), entry["exit_code"])
                    # Every exit whose condition held in the input still applies, and every exit can be filled.
                    for flag in conditions:
                        name = flag.lstrip("!")
                        held = data[name] is False if flag.startswith("!") else bool(data[name])
                        self.assertEqual(stops._holds(final["data"], flag), held, (key, flag))
                    filled = {**final["data"], **final["source"]}
                    self.assertTrue(all(name in filled for name in stops.required_fields(*key, final["data"])))
                    projected = stops.project(final, "run_x")
                    self.assertEqual(len(projected["exits"]), len(list(stops.active_exits(entry, final["data"]))))

    def test_shrinking_keeps_heads_and_counts_what_it_omits(self):
        key = ("scope_extension", "revision")
        smile = "\U0001F600"
        gaps = {f"t{i}": [f"docs/{i}_{j}_" + smile * 50 + ".md" for j in range(30)] for i in range(3)}
        suggested = ["docs/**", "notes/" + smile * 40 + "/**"]
        made = stops.make(*key, data={"gaps": gaps, "suggested_scope": suggested, "scope": ["hello.py"],
                                      "widened": ["docs/**"]})
        error = HarnessError(smile * 5000)
        error.stop = made
        final = stops.of(error)
        data = final["data"]
        kept = [pattern for patterns in data["gaps"].values() for pattern in patterns]
        flat = [pattern for patterns in gaps.values() for pattern in patterns]
        self.assertEqual(kept, flat[:len(kept)])                    # a head, task order then pattern order
        self.assertEqual(len(kept) + data["gaps_omitted"], len(flat))
        self.assertEqual(data["suggested_scope"], suggested)          # the exit's field is the last to shrink
        self.assertTrue(stops.project(final, "run_x")["exits"][0].startswith("resume run_x --scope='docs/**'"))

    def long(self, check, start):
        """Forty distinct items of the list type ``check``, more than any list keeps; a map has ten per key."""
        texts = [f"item/{i}/" + "x" * 300 for i in range(start, start + 40)]
        if check is stops._strings:
            return texts
        if check is stops._command_list:
            return [{"cwd": "app", "argv": ["cmd", text]} for text in texts]
        if check is stops._quota_windows:
            return [{"bucket": text[:160], "used_percent": 100, "resets_at": None} for text in texts]
        if check is stops._strings_map:
            return {f"t{start + k}": texts[10 * k:10 * k + 10] for k in range(4)}
        if check is stops._command_map:
            return {f"t{start + k}": [{"cwd": "app", "argv": ["cmd", text]} for text in texts[10 * k:10 * k + 10]]
                    for k in range(4)}
        raise AssertionError(f"no items for {check}")

    @staticmethod
    def count(value):
        """A list's items; a map's are the entries under all its keys, the unit ``<field>_omitted`` counts."""
        return sum(len(entries) for entries in value.values()) if type(value) is dict else len(value)

    def assertHead(self, kept, offered):
        """A list's first items; a map's first keys, all whole but the last, which keeps its first entries."""
        if type(offered) is list:
            self.assertEqual(kept, offered[:len(kept)])
            return
        keys = list(kept)
        self.assertEqual(keys, list(offered)[:len(keys)])
        for key in keys[:-1]:
            self.assertEqual(kept[key], offered[key])
        if keys:
            self.assertEqual(kept[keys[-1]], offered[keys[-1]][:len(kept[keys[-1]])])

    def test_a_made_value_is_made_again_unchanged_and_omissions_add_up(self):
        # Callers rebuild a stop from its own data (the quota wait's decision, the failure sample).
        rng = random.Random(20261117)
        rows = {key: [name for name, check in entry["data"].items() if hasattr(check, "shape")]
                for key, entry in stops.REGISTRY.items()}
        rows = {key: names for key, names in rows.items() if names}
        self.assertIn(("provider_quota", "usage_limit"), rows)
        for key, names in rows.items():
            entry = stops.REGISTRY[key]
            data = {name: (self.long(check, 0) if name in names else self.value(rng, check))
                    for name, check in entry["data"].items()}
            source = {name: self.source(rng, name) for name in entry["source"]}
            with self.subTest(row=key):
                made = stops.make(*key, data=copy.deepcopy(data), source=source)
                stops.validate(made)
                self.assertEqual((made["kind"], made["reason"]), key)
                for name in names:
                    self.assertHead(made["data"][name], data[name])
                    self.assertEqual(self.count(made["data"][name]) + made["data"][f"{name}_omitted"], 40)
                again = stops.make(*key, data=copy.deepcopy(made["data"]), source=dict(made["source"]))
                stops.validate(again)
                self.assertEqual(again, made)
                # Each list grows again (a map by new keys): the count adds what this call cuts to what the input carried.
                grown = copy.deepcopy(made["data"])
                for name in names:
                    more = self.long(entry["data"][name], 1000)
                    grown[name] = {**grown[name], **more} if type(more) is dict else grown[name] + more
                remade = stops.make(*key, data=copy.deepcopy(grown), source=dict(made["source"]))
                stops.validate(remade)
                self.assertEqual((remade["kind"], remade["reason"]), key)
                for name in names:
                    kept, offered = remade["data"][name], grown[name]
                    self.assertHead(kept, offered)
                    self.assertGreater(self.count(offered), self.count(kept))
                    self.assertEqual(remade["data"][f"{name}_omitted"],
                                     made["data"][f"{name}_omitted"] + self.count(offered) - self.count(kept))


class LedgerStopTests(OperationCase):
    def blocked(self):
        """A run stopped by the dispatch limit: blocked, with an unclassified stop in 4a."""
        self.config.data["runtime"]["max_agent_dispatches"] = 1
        engine = self.engine(ScriptedBackend())
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", plan())
        return engine

    def test_load_rejects_stop_status_mismatch(self):
        engine = self.blocked()
        path = engine.directory / "state.json"
        saved = read_json(path)
        self.assertEqual(saved["stop"]["kind"], "budget")
        plan_stop = stops.make("plan_approval", "initial_plan")
        cases = {
            "blocked without stop": lambda run: run.pop("stop"),
            "running with stop": lambda run: run.update(status="running"),
            "awaiting_approval with unclassified": lambda run: run.update(status="awaiting_approval"),
            "interrupted with plan_approval": lambda run: run.update(status="interrupted", stop=plan_stop),
            "blocked with interrupted": lambda run: run.update(stop=stops.make("interrupted", "signal")),
            "unregistered reason": lambda run: run["stop"].update(reason="nonsense"),
            "extra key": lambda run: run["stop"].update(extra=1),
            "detail too long": lambda run: run["stop"]["data"].update(detail="x" * 2001),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                run = copy.deepcopy(saved)
                mutate(run)
                write_json(path, run)
                before = path.read_bytes()
                for action in (lambda: self.engine().resume(run["id"]), lambda: self.engine().abandon(run["id"])):
                    with self.assertRaisesRegex(HarnessError, "停止记录与状态不符"):
                        action()
                    self.assertEqual(path.read_bytes(), before)

    def test_every_commit_keeps_stop_with_its_status(self):
        recorder, states = commit_recorder()
        path = self.outside / "plan.json"
        write_json(path, plan())
        self.config.data["runtime"]["max_agent_dispatches"] = 1
        with recorder:
            code, _, _ = self.invoke(["run", "Implement greet", "--plan", str(path), "--pause-after-plan"])
            self.assertEqual(code, 0)
            run_id = states[-1]["id"]
            self.assertEqual(states[-1]["stop"]["kind"], "plan_approval")
            code, _, err = self.invoke(["resume", run_id])
            self.assertEqual(code, 0)  # a plain resume reports the waiting plan again
            self.assertEqual(self.ledger(run_id)["stop"]["kind"], "plan_approval")
            code, _, _ = self.invoke(["resume", run_id, "--approve-plan"])
            self.assertEqual(code, 3)
            self.assertEqual((states[-1]["status"], states[-1]["stop"]["kind"]), ("blocked", "budget"))
            self.config.data["runtime"]["max_agent_dispatches"] = 50
            code, _, _ = self.invoke(["resume", run_id])
            self.assertEqual(code, 0)
        self.assertEqual(states[-1]["status"], "completed")
        for state in states:
            with self.subTest(status=state["status"], revision=state["store_revision"]):
                self.assertEqual("stop" in state, state["status"] in stops.STOP_STATUSES)
                stops.validate_ledger(state)
                self.assertFalse(set(REMOVED) & set(state), "the removed error records came back")
        running = next(state for state in states if state["status"] == "running")
        self.assertNotIn("stop", running)

    def test_awaiting_approval_records_the_plan_stop(self):
        engine = self.engine(ScriptedBackend())
        result = engine.start("Implement greet", plan(), pause_after_plan=True)
        self.assertEqual(result["status"], "awaiting_approval")
        stop = read_json(engine.directory / "state.json")["stop"]
        first = result["plan_history"][0]
        self.assertEqual((stop["kind"], stop["reason"]), ("plan_approval", "initial_plan"))
        self.assertEqual(stop["source"], {"artifact": first["artifact"], "artifact_digest": first["artifact_digest"]})
        self.assertEqual(stop["data"], {"revision": 0, "task_count": len(result["tasks"])})
        code, out, _ = self.invoke(["status", result["id"]])
        self.assertEqual(code, 0)
        self.assertIn(f"resume {result['id']} --approve-plan", json.loads(out)["stop"]["exits"])

    def test_interrupt_between_stop_save_and_event(self):
        self.config.data["runtime"]["max_agent_dispatches"] = 1
        backend = ScriptedBackend()
        engine = self.engine(backend)
        original = RunContext.event

        def event(context, name, **fields):
            if name == "run_stopped":
                raise KeyboardInterrupt
            return original(context, name, **fields)
        with patch.object(RunContext, "event", event), self.assertRaises(KeyboardInterrupt):
            engine.start("Implement greet", plan())
        saved = read_json(engine.directory / "state.json")
        self.assertEqual((saved["status"], saved["stop"]["kind"]), ("blocked", "budget"))
        self.assertNotIn('"run_stopped"', (engine.directory / "events.jsonl").read_text())
        self.config.data["runtime"]["max_agent_dispatches"] = 50
        recorder, states = commit_recorder()
        with recorder:
            result = self.engine(backend).resume(saved["id"])
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("stop", next(state for state in states if state["status"] == "running"))

    def test_untyped_error_is_unclassified_and_exits_1(self):
        path = self.outside / "plan.json"
        write_json(path, plan())

        def boom(*args, **kwargs):
            raise HarnessError("no type here")
        code, _, err = self.invoke(["run", "Implement greet", "--plan", str(path)],
                                   **{"harness.finalization.FinalAcceptance.final_review": boom})
        self.assertEqual(code, 1)
        run_id = next((self.state / "runs").iterdir()).name
        stop = self.ledger(run_id)["stop"]
        self.assertEqual((stop["kind"], stop["reason"], stop["data"]["error_type"]),
                         ("unclassified", "untyped_harness_error", "HarnessError"))
        lines = err.splitlines()
        first = next(index for index, line in enumerate(lines) if line == "harness: no type here")
        notice = json.loads(lines[first + 1].split(" ", 1)[1])
        self.assertEqual((notice["event"], notice["run_id"], notice["stop"]["kind"]), ("stop", run_id, "unclassified"))
        self.assertNotIn("detail", notice["stop"])
        record = self.record(notice["operation_id"])
        self.assertEqual((record["exit_status"], record["stop"]["kind"], record["stop"]["exit_code"]),
                         (1, "unclassified", 1))
        self.assertNotIn("error_type", record)
        # Row unclassified-harness-error: no recovery action is guessed.
        no_command_exit(self, notice["stop"], ("unclassified", "untyped_harness_error"))

    def test_interrupt_records_signal_stop_and_130(self):
        path = self.outside / "plan.json"
        write_json(path, plan())

        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt
        code, _, err = self.invoke(["run", "Implement greet", "--plan", str(path)],
                                   **{"harness.finalization.FinalAcceptance.final_review": interrupt})
        self.assertEqual(code, 130)
        run_id = next((self.state / "runs").iterdir()).name
        saved = self.ledger(run_id)
        self.assertEqual((saved["status"], saved["stop"]["kind"], saved["stop"]["reason"]),
                         ("interrupted", "interrupted", "signal"))
        self.assertIn("harness: 已中断；运行状态和 worktree 已保留", err)
        notice = notices(err)[-1]
        # A simulated call's exit is not confirmed: abandon is listed once resume has confirmed it.
        self.assertEqual((notice["stop"]["exit_code"], notice["stop"]["exits"]), (130, [f"resume {run_id}"]))
        record = self.record(notice["operation_id"])
        self.assertEqual((record["exit_status"], record["stop"]["kind"]), (130, "interrupted"))

    def test_file_input_record_keeps_stop_without_text(self):
        engine = self.blocked()
        update = self.outside / "update.json"
        write_json(update, {"schema_version": 1, "secret": "VALUE-THAT-MUST-NOT-LEAK"})
        code, _, err = self.invoke(["resume", engine.run["id"], "--environment-update", str(update)])
        self.assertEqual(code, 2)
        record = self.record(notices(err)[-1]["operation_id"])
        self.assertIsNone(record["error"])
        self.assertTrue(record["error_omitted"])
        self.assertEqual((record["stop"]["kind"], record["stop"]["exit_code"]), ("invalid_input", 2))
        self.assertIn("reason", record["stop"])
        self.assertNotIn("detail", record["stop"])
        self.assertNotIn("VALUE-THAT-MUST-NOT-LEAK", json.dumps(record))

    def test_propagated_exceptions_still_record_their_projection(self):
        path = self.outside / "plan.json"
        write_json(path, plan())
        cases = (("before run creation", RuntimeError("internal fault"), "harness.runtime.Runtime.start"),
                 ("before run creation", sqlite3.ProgrammingError("db misuse"), "harness.runtime.Runtime.start"),
                 ("during the run", RuntimeError("internal fault after acceptance"),
                  "harness.finalization.FinalAcceptance.final_review"))
        for index, (moment, error, target) in enumerate(cases):
            with self.subTest(moment=moment, error=type(error).__name__):
                operation_id = f"op-propagated-{index}"

                def fail(*args, **kwargs):
                    raise error
                errors = io.StringIO()
                with self.assertRaises(type(error)):
                    self.invoke(["run", "Implement greet", "--plan", str(path), "--operation-id", operation_id],
                                ScriptedBackend(), errors=errors, **{target: fail})
                self.assertNotIn("harness: ", errors.getvalue())  # propagated: no diagnostic, as before
                self.assertNotIn('"event":"stop"', errors.getvalue())
                record = self.record(operation_id)
                self.assertEqual((record["exit_status"], record["error"]), (1, None))  # file input: text omitted
                stop = record["stop"]
                expected = (("runtime_failure", "database") if isinstance(error, sqlite3.Error)
                            else ("unclassified", "unexpected_exception"))
                self.assertEqual((stop["kind"], stop["reason"], stop["data"]["error_type"], stop["exit_code"]),
                                 (*expected, type(error).__name__, 1))
                self.assertNotIn("detail", stop)
                if moment == "during the run":
                    ledger = stops.project_ledger(self.ledger(record["run_id"]), omit_detail=True)
                    self.assertEqual(stop, ledger)

    def test_no_save_writes_the_removed_error_records(self):
        """Stops, worker failures and invalid blockers never bring back run error, error_type or last_runtime_error."""
        class FailingThenBlocking(ScriptedBackend):
            def invoke(self, **kwargs):
                workers = sum(call["role"] == "worker" for call in self.calls)
                if kwargs["role"] == "worker" and workers == 0:
                    self.calls.append({"role": "worker", "kind": kwargs["kind"], "packet": None, "task_id": "greet"})
                    raise HarnessError("worker service unavailable")
                return super().invoke(**kwargs)

        def invalid_blocker(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(call["role"] == "worker" for call in backend.calls) == 2:
                result.update(status="blocked", blocker_kind="environment", blockers=["Docker stopped"], summary="x" * 32001)
        backend = FailingThenBlocking(invalid_blocker)
        recorder, states = commit_recorder()
        with recorder:
            engine = self.engine(backend)
            with self.assertRaises(HarnessError):
                engine.start("Implement greet", plan())
            with self.assertRaisesRegex(HarnessError, "环境受阻"):
                self.engine(backend).resume(engine.run["id"])
        self.assertGreater(len(states), 5)
        for state in states:
            with self.subTest(revision=state["store_revision"]):
                self.assertFalse(set(REMOVED) & set(state))
                for task_id, recovery in state.get("worker_recovery", {}).items():
                    self.assertFalse({"error", "error_type"} & set(recovery), task_id)

    def test_abandon_clears_the_stop(self):
        engine = self.blocked()
        recorder, states = commit_recorder()
        with recorder:
            result = self.engine().abandon(engine.run["id"])
        self.assertEqual(result["status"], "abandoned")
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0]["status"], "abandoned")
        self.assertNotIn("stop", states[0])


class WorkerRecoveryProjectionTests(RepoCase):
    def test_worker_recovery_projection_reads_the_source_entry(self):
        class FailingOnce(ScriptedBackend):
            failed = False

            def invoke(self, **kwargs):
                if kwargs["role"] == "worker" and not self.failed:
                    self.failed = True
                    self.calls.append({"role": "worker", "kind": kwargs["kind"], "packet": None, "task_id": "greet"})
                    raise HarnessError("worker service unavailable: " + "x" * 2500)
                return super().invoke(**kwargs)
        backend = FailingOnce()
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet", plan())
        saved = read_json(engine.directory / "state.json")
        failed = next(entry for entry in saved["dispatches"] if entry["role"] == "worker")
        recovery = saved["worker_recovery"]["greet"]
        self.assertNotIn("error", recovery)
        self.assertNotIn("error_type", recovery)
        self.assertEqual(recovery["dispatch_id"], failed["id"])
        result = self.engine(backend).resume(saved["id"])
        self.assertEqual(result["status"], "completed")
        packet = next(call["packet"] for call in backend.calls if call["role"] == "worker" and call["packet"])
        projected = packet["recovery"]
        self.assertNotIn("error_type", projected)
        self.assertEqual(projected["source"]["dispatch_id"], failed["id"])
        self.assertEqual(projected["source"]["error"], stops.bounded(failed["error"]))
        self.assertLessEqual(len(projected["source"]["error"]), 2000)
        # The failure text is carried once: not again on the latest worker.
        self.assertNotIn("error", projected["latest"]["worker"])
        self.assertEqual(json.dumps(packet, ensure_ascii=False).count("worker service unavailable"), 1)

    def test_invalid_environment_blocker_recovery_reads_the_judgment_from_its_entry(self):
        """The one visible difference: the recovery text is the judgment, not the stop that followed it.

        Before, the dispatch's exception handler overwrote the recovery record with the
        environment stop text (blocker excerpt, artifact path and digest). The text now
        comes from the judged entry; feedback already carries it, so the packet holds it once.
        """
        def change(backend, kwargs, packet, result):
            if kwargs["role"] == "worker" and sum(call["role"] == "worker" for call in backend.calls) == 1:
                result.update(status="blocked", blocker_kind="environment", blockers=["Docker stopped " + "d" * 1500],
                              summary="x" * 32001)
        backend = ScriptedBackend(change)
        engine = self.engine(backend)
        with self.assertRaisesRegex(HarnessError, "环境受阻") as caught:
            engine.start("Implement greet", plan())
        stop_text = str(caught.exception)
        worker = next(entry for entry in engine.run["dispatches"] if entry["role"] == "worker")
        self.assertEqual((worker["status"], worker["error"]), ("invalid_blocked", engine.run["feedback"]["output_error"]))
        self.assertNotEqual(worker["error"], stop_text)
        update = self.outside / "environment-update.json"
        write_json(update, {"schema_version": 1, "task_id": "greet", "blocker_dispatch_id": worker["id"],
                            "summary": "Restored"})
        result = self.engine(backend).resume(engine.run["id"], environment_update=update)
        self.assertEqual(result["status"], "completed")
        packet = [call["packet"] for call in backend.calls if call["role"] == "worker"][-1]
        whole = json.dumps(packet, ensure_ascii=False)
        self.assertEqual(packet["feedback"]["output_error"], worker["error"])
        self.assertEqual(whole.count(worker["error"]), 1)
        self.assertNotIn("error", packet["recovery"]["source"])
        self.assertNotIn("环境受阻", whole)
        self.assertEqual(packet["recovery"]["source"]["dispatch_id"], worker["id"])


class RecoveryCapacityTests(RepoCase):
    """A resume whose worker context sits just inside the budget completes exactly as before 4a."""
    FAILURE = "service unavailable: " + "x" * 1900
    MARGIN = 600  # smaller than the failure text: a second copy of it would exceed the budget

    def scenario(self, padding, failure):
        backend = ScriptedBackend()

        def fail_first(backend, kwargs, packet):
            if kwargs["role"] == "worker" and len(backend.calls) == 1:
                raise HarnessError(failure)
        backend.before_result = fail_first
        engine = self.engine(backend)
        with self.assertRaises(HarnessError):
            engine.start("Implement greet. " + "g" * padding, plan())
        try:
            result = self.engine(backend).resume(engine.run["id"])
        except HarnessError:
            result = read_json(engine.directory / "state.json")
        workers = [entry for entry in result["dispatches"] if entry["role"] == "worker"]
        report = engine.directory / "dispatches" / workers[-1]["id"] / "context-report.json"
        required = read_json(report)["required_chars"] if len(workers) == 2 else None
        return result, backend, required

    def test_resume_at_the_capacity_edge_completes(self):
        budget = self.config.data["runtime"]["context_chars"]
        _, _, calibrated = self.scenario(60000, "service unavailable")
        padding = 60000 + (budget - self.MARGIN) - (calibrated + len(self.FAILURE) - len("service unavailable"))
        result, backend, required = self.scenario(padding, self.FAILURE)
        self.assertEqual(result["status"], "completed")
        # Within a few characters of the calibrated edge: still far closer than one more copy of the failure.
        self.assertLessEqual(abs(required - (budget - self.MARGIN)), 20)
        self.assertEqual(result["attempts"], {"greet": 2})
        self.assertEqual(result["agent_dispatches"], 5)
        self.assertEqual([call["role"] for call in backend.calls].count("worker"), 2)


if __name__ == "__main__":
    unittest.main()
