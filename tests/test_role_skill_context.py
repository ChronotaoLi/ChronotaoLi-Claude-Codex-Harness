"""Role and task skill routing through real Engine lifecycle, with offline agents."""
import json
import shutil
import tempfile
from pathlib import Path

from harness.shared import read_skill, skills_catalog
from harness.util import read_json, dump
from test_harness import ROOT, RepoCase


class RoleSkillContextTests(RepoCase):
    def setUp(self):
        super().setUp()
        assets = tempfile.TemporaryDirectory()
        self.addCleanup(assets.cleanup)
        self.skills = Path(assets.name) / "skills"
        shutil.copytree(self.config.asset("skills"), self.skills)
        self.config.data["shared"]["skills"] = str(self.skills)
        extra = self.skills / "project-checks"
        extra.mkdir()
        self.extra_text = "---\ndescription: Project specific verification\n---\nPreserve project checks.\n"
        (extra / "SKILL.md").write_text(self.extra_text)

    def prompts(self, engine):
        return [(entry, json.loads((engine.directory / "dispatches" / entry["id"] / "prompt.txt")
                                  .read_text().split("\n\nREFERENCE MEMORY")[0]))
                for entry in engine.run["dispatches"] if entry["role"] != "verification"]

    def test_legacy_role_reference_fits_budget_and_preserves_additional_skill(self):
        # One role copy fits; the historical duplicate exceeds this actual dispatch budget.
        role = self.skills / "harness-worker" / "SKILL.md"
        role.write_text(role.read_text() + "\n" + "role context " * 650)
        self.config.data["runtime"]["context_chars"] = 32000
        plan = read_json(ROOT / "examples" / "plan.json")
        plan["tasks"][0]["skills"] = ["harness-worker", "project-checks"]
        engine = self.engine()
        result = engine.start("Implement greet", plan)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["tasks"][0]["skills"], plan["tasks"][0]["skills"])
        entry, worker = next((e, p) for e, p in self.prompts(engine) if e["role"] == "worker")
        self.assertEqual(worker["role_skill"], read_skill(self.skills, "harness-worker"))
        self.assertEqual(worker["task_skills"], {"project-checks": self.extra_text})
        report = read_json(engine.directory / "dispatches" / entry["id"] / "context-report.json")
        self.assertLessEqual(report["used_chars"], 32000)
        worker["task_skills"]["harness-worker"] = worker["role_skill"]
        self.assertGreater(len(dump(worker)), 32000)
        self.assertEqual(len([e for e in result["dispatches"] if e["role"] == "worker"]), 1)
        self.assertEqual(len(result["accepted"]), 1)

    def test_planning_catalog_only_advertises_task_skills_but_legacy_names_validate(self):
        engine = self.engine()
        result = engine.start("Implement greet")
        self.assertEqual(result["status"], "completed")
        plan_prompt = next(p for e, p in self.prompts(engine) if e["kind"] == "plan")
        self.assertEqual([s["name"] for s in plan_prompt["available_skills"]], ["project-checks"])
        self.assertEqual({s["name"] for s in skills_catalog(self.skills)},
                         {"harness-worker", "harness-commander", "harness-reviewer", "project-checks"})
        for name in ("harness-worker", "harness-commander", "harness-reviewer"):
            plan = read_json(ROOT / "examples" / "plan.json")
            plan["tasks"][0]["skills"] = [name]
            self.assertEqual(engine.validate_tasks(plan)[0]["skills"], [name])
