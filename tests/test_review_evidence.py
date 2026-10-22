import copy
import tempfile
import unittest
from pathlib import Path

from harness import git
from harness.context import compact_feedback, compact_reviews, review_digest
from harness.util import read_json, write_json
from test_harness import RepoCase, ROOT
from test_runtime_changes import ScriptedBackend, finding


class ReviewEvidenceTests(unittest.TestCase):
    def test_small_older_review_keeps_retrievable_identity_after_deduplication(self):
        review = {"verdict": "request_changes", "summary": "Original independent review",
                  "findings": [finding()], "approved_memory_keys": []}
        later = copy.deepcopy(review)
        later["summary"] = "The same defect remains on the next candidate"
        with tempfile.TemporaryDirectory() as directory:
            first_path = Path(directory) / "first.json"
            second_path = Path(directory) / "second.json"
            write_json(first_path, {"review": review})
            write_json(second_path, {"review": later})
            history = [{"review": review, "artifact": str(first_path)},
                       {"review": later, "artifact": str(second_path)}]
            original = copy.deepcopy(history)
            compacted = compact_reviews(history)
            old_review = compacted[0]["review"]
            self.assertEqual(history, original)
            self.assertEqual(old_review["findings"], [])
            self.assertNotIn("summary", old_review)
            self.assertNotIn("details_truncated", old_review)
            self.assertEqual(compacted[0]["repeated_finding_ids"], [finding()["id"]])
            retrieved = read_json(Path(compacted[0]["artifact"]))["review"]
            self.assertEqual(retrieved, review)
            self.assertEqual(old_review["review_content_sha256"], review_digest(retrieved))

            feedback = {"review": retrieved, "artifact": str(first_path)}
            result = compact_feedback(feedback, compacted)
            self.assertTrue(result["review_in_prior_reviews"])
            self.assertNotIn("review", result)
            changed = copy.deepcopy(feedback)
            changed["review"]["findings"][0]["evidence"].append("Additional independent evidence")
            result = compact_feedback(changed, compacted)
            self.assertEqual(result["review"], changed["review"])
            self.assertNotIn("review_in_prior_reviews", result)

            compacted_again = compact_reviews(compacted)
            self.assertEqual(compacted_again[0]["review"]["review_content_sha256"], review_digest(review))
            self.assertTrue(compact_feedback(feedback, compacted_again)["review_in_prior_reviews"])


class InstructionFileReviewTests(RepoCase):
    def test_instruction_edits_reach_task_and_final_review_without_forbidding_acceptance(self):
        baseline = {"AGENTS.md": "Existing agent guidance\n",
                    "docs/CLAUDE.md": "Existing Claude guidance\n",
                    "docs/CLAUDE.local.md": "Local Claude guidance\n"}
        for name, contents in baseline.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
        git.git(self.repo, "add", ".")
        git.git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost",
                "-c", "commit.gpgsign=false", "commit", "-m", "instruction fixtures")
        plan = read_json(ROOT / "examples" / "plan.json")
        plan["tasks"][0]["allowed_paths"].extend(["**/*.md", "docs/AGENTS.md.bak"])

        def edit_instructions(backend, kwargs, packet, result):
            if kwargs["role"] == "worker":
                root = kwargs["cwd"]
                (root / "AGENTS.md").write_text("Updated agent guidance\n")
                (root / "docs/CLAUDE.md").unlink()
                (root / "docs/CLAUDE.local.md").rename(root / "docs/guide.md")
                (root / "docs/AGENTS.override.md").write_text("Nested agent guidance\n")
                (root / "README.md").write_text("Ordinary project documentation\n")
                (root / "docs/AGENTS.md.bak").write_text("Ordinary backup\n")

        backend = ScriptedBackend(edit_instructions)
        engine = self.engine(backend)
        result = engine.start("Implement greeting and update project instructions", plan)
        self.assertEqual(result["status"], "completed")
        expected = ["AGENTS.md", "docs/AGENTS.override.md", "docs/CLAUDE.local.md", "docs/CLAUDE.md"]
        reviews = [call for call in backend.calls if call["role"] == "reviewer"]
        self.assertEqual([call["kind"] for call in reviews], ["review", "final_review"])
        for call in reviews:
            with self.subTest(kind=call["kind"]):
                diagnostic = call["packet"]["instruction_file_changes"]
                self.assertEqual(diagnostic["paths"], expected)
                self.assertEqual(diagnostic["trust"], "untrusted_candidate_instructions")
                self.assertTrue(diagnostic["review_instruction"])
                self.assertTrue(Path(call["packet"]["diff_artifact"]).is_file())
        self.assertEqual(set(result["accepted"]), {"greet"})
        self.assertEqual((self.repo / "AGENTS.md").read_text(), baseline["AGENTS.md"])
        self.assertTrue((self.repo / "docs/CLAUDE.md").is_file())


if __name__ == "__main__":
    unittest.main()
