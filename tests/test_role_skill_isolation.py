import itertools
import tempfile
import tomllib
import unittest
from pathlib import Path

from harness.adapters import CLIBackend
from harness.config import Config
from harness.util import HarnessError


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "operator" / "harness-operator" / "SKILL.md"
ROLE_KEYS = ("commander", "fable", "worker", "reviewer")
# (dispatch role, --commander choice): the role keys a run can dispatch with.
DISPATCHES = (("commander", "astra"), ("commander", "fable"), ("worker", "astra"), ("reviewer", "astra"))


class RoleSkillIsolationTests(unittest.TestCase):
    """Every role Harness launches starts with the outer session's skill switched off."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.jobs = 0

    def config(self, clis):
        config = Config.load(ROOT / "harness.toml")
        for key, cli in zip(ROLE_KEYS, clis):
            config.data["roles"][key] = {"cli": cli, "model": f"exact-{cli}-{key}-model", "effort": "high"}
        Config.validate(config.data)
        return config

    def argv(self, config, role, commander="astra"):
        self.jobs += 1
        job = self.root / f"job-{self.jobs}"
        job.mkdir()
        return CLIBackend(config, self.root / ".harness").command(
            role, config.role(role, commander), self.root, job, {}, None)

    @staticmethod
    def codex_skill_overrides(argv):
        """Parse each `-c skills.config=…` the way Codex reads an override: a dotted key and a TOML value."""
        overrides = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-c"]
        return [tomllib.loads(value)["skills"]["config"] for value in overrides
                if value.split("=", 1)[0].strip() == "skills.config"]

    def operator_skill_name(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        header = text[len("---\n"):].split("\n---\n", 1)[0]
        names = [line.split(":", 1)[1].strip() for line in header.splitlines() if line.startswith("name:")]
        self.assertEqual(len(names), 1, header)
        return names[0]

    def test_every_allowed_role_and_cli_launches_with_the_operator_skill_disabled(self):
        disabled = [{"name": self.operator_skill_name(), "enabled": False}]
        covered = set()
        for clis in itertools.product(("claude", "codex"), repeat=len(ROLE_KEYS)):
            try:
                config = self.config(clis)
            except HarnessError:
                continue
            for role, commander in DISPATCHES:
                argv = self.argv(config, role, commander)
                cli = config.role(role, commander)["cli"]
                covered.add((role, cli))
                with self.subTest(roles=dict(zip(ROLE_KEYS, clis)), role=role, commander=commander):
                    self.assertEqual(argv[0], cli)
                    if cli == "claude":
                        self.assertIn("--disable-slash-commands", argv)
                    else:
                        self.assertEqual(self.codex_skill_overrides(argv), [disabled])
        # Configuration allows Claude in all three roles and Codex only in the read-only ones.
        self.assertEqual(covered, {("commander", "claude"), ("worker", "claude"), ("reviewer", "claude"),
                                   ("commander", "codex"), ("reviewer", "codex")})

    def test_adapter_disables_the_skill_named_by_the_operator_skill_file(self):
        config = self.config(("codex", "claude", "claude", "codex"))
        overrides = self.codex_skill_overrides(self.argv(config, "reviewer"))
        self.assertEqual(len(overrides), 1, overrides)
        disabled = [entry["name"] for entry in overrides[0] if entry.get("enabled") is False]
        name = self.operator_skill_name()
        self.assertEqual(disabled, [name])
        self.assertEqual(name, SKILL.parent.name)


if __name__ == "__main__":
    unittest.main()
