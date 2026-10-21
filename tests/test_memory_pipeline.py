from harness.adapters import DemoBackend
from test_harness import RepoCase


class ProposedKnowledge(DemoBackend):
    approve_memory = True

    def invoke(self, **kwargs):
        result = super().invoke(**kwargs)
        if kwargs["role"] == "worker":
            result["proposed_memories"] = [{
                "key": "greeting", "abstract": "Greeting contract",
                "content": "greet(name) returns a greeting containing the supplied name.",
                "source_paths": ["hello.py"],
            }]
        elif kwargs["kind"] == "review" and self.approve_memory:
            result["approved_memory_keys"] = ["greeting"]
        return result


class MemoryPipelineTests(RepoCase):
    def test_only_separately_approved_knowledge_is_promoted_after_acceptance(self):
        engine = self.engine(ProposedKnowledge())
        result = engine.start("Implement greet")
        self.assertEqual(result["status"], "completed")
        records = engine.memory.export()
        self.assertEqual(len(records), 2)
        knowledge = next(record for record in records if record["kind"] == "project_knowledge")
        self.assertEqual(knowledge["key"], "knowledge:greeting")
        self.assertEqual(knowledge["provenance"]["commit"], result["accepted"]["greet"]["commit"])
        self.assertFalse(engine.memory.read(knowledge["id"])["possibly_stale"])
        self.assertIsNotNone(knowledge["expires"])

    def test_code_approval_does_not_implicitly_approve_knowledge(self):
        backend = ProposedKnowledge()
        backend.approve_memory = False
        engine = self.engine(backend)
        self.assertEqual(engine.start("Implement greet")["status"], "completed")
        self.assertEqual([record["kind"] for record in engine.memory.export()], ["accepted_task"])

    def test_rejected_code_never_promotes_even_approved_memory_keys(self):
        class RejectFirst(ProposedKnowledge):
            attempts = 0

            def invoke(backend, **kwargs):
                if kwargs["role"] == "worker":
                    backend.attempts += 1
                    if backend.attempts == 2:
                        self.assertEqual(engine.memory.export(), [])
                result = super().invoke(**kwargs)
                if kwargs["kind"] == "review":
                    if backend.attempts == 1:
                        result.update(verdict="request_changes", findings=[{
                            "id": "coverage", "severity": "blocking", "category": "evidence",
                            "location": "hello.py", "message": "Check greeting behavior again",
                            "evidence": ["First review requires an additional independent check."],
                        }])
                    else:
                        result["approved_memory_keys"] = []
                return result

        engine = self.engine(RejectFirst())
        self.assertEqual(engine.start("Implement greet")["status"], "completed")
        self.assertEqual([record["kind"] for record in engine.memory.export()], ["accepted_task"])
