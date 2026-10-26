"""Projection retains retrievable history while isolating task assumptions."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from harness.environment import EnvironmentRecovery
from harness.recovery import snapshot_digest
from harness.util import HarnessError, write_json


class Projection(EnvironmentRecovery):
    def environment_source(self, *args):
        return {}


class EnvironmentProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = Projection()
        self.env.directory = Path(self.temp.name).resolve()
        self.env.run = {'id': 'run', 'schema_version': 10, 'environment_updates': [], 'environment_checks': []}

    def add(self, task, summary, checks=None, passed=True):
        checks = checks or []
        update = dict(schema_version=1, task_id=task, blocker_dispatch_id='dispatch', summary=summary, checks=checks)
        digest = snapshot_digest({'run_id': 'run', 'update': update})
        record = dict(update, id='env_' + digest, sequence=len(self.env.run['environment_updates']) + 1,
                      source={}, update_digest=digest)
        artifact = self.env.directory / (record['id'] + '.json')
        write_json(artifact, record)
        row = dict(record, artifact=str(artifact), artifact_digest=hashlib.sha256(artifact.read_bytes()).hexdigest())
        self.env.run['environment_updates'].append(row)
        if checks:
            report = dict(update_id=record['id'], task_id=task, observed_at=1, passed=passed,
                          results=[dict(check=check, passed=passed) for check in checks], limitation='read only')
            path = self.env.directory / ('checks-' + str(record['sequence']) + '.json')
            write_json(path, report)
            self.env.run['environment_checks'].append(dict(update_id=record['id'], task_id=task, passed=passed,
                artifact=str(path), artifact_digest=hashlib.sha256(path.read_bytes()).hexdigest()))
        return row

    def test_task_ready_replan_and_final_scope_preserve_contract(self):
        self.add('a', 'Only a remains read only')
        self.add('b', 'Dependency ready')
        self.add('a', 'New a facts')
        for kwargs, packet, expected in [
                ({'task_id': 'a', 'role': 'worker'}, {}, ['a']),
                ({'task_id': 'b', 'role': 'reviewer'}, {}, ['b']),
                ({}, {'task': {'id': 'a'}}, ['a']),
                ({'kind': 'dispatch'}, {'ready': [{'id': 'b'}]}, ['b']),
                ({'kind': 'replan'}, {'trigger': {'task_id': 'a'}}, ['a']),
                ({'kind': 'final_review'}, {}, ['b', 'a']),
                ({'kind': 'replan'}, {'trigger': {'task_id': 'final'}}, ['b', 'a'])]:
            packet.update(acceptance=['must pass'], tool_constraints={'permissions': []}, blockers=['unresolved'])
            result = self.env.environment_packet(packet, **kwargs)
            self.assertEqual([r['task_id'] for r in result['environment_updates']], expected)
            for key in packet:
                self.assertEqual(result[key], packet[key])
            self.assertEqual({r['task_id'] for r in result['environment_history']}, set(expected))
            index = result['environment_index']
            raw = Path(index['artifact']).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), index['artifact_digest'])
            self.assertEqual(len(json.loads(raw)['updates']), 3)
            for row in result['environment_history']:
                self.assertEqual(hashlib.sha256(Path(row['artifact']).read_bytes()).hexdigest(), row['artifact_digest'])

    def test_long_commands_compact_and_failed_checks_retained(self):
        checks = [dict(kind='worker_command', argv=['tool', 'x' * 4000])] * 12
        self.add('a', 'Ready except check failure', checks, passed=False)
        result = self.env.environment_packet({}, task_id='a')
        self.assertLess(len(json.dumps(result)), 7000)
        self.assertEqual(result['environment_updates'][0]['check_count'], 12)
        self.assertEqual(result['environment_observations'][0]['failed_check_indices'], list(range(12)))
        self.assertFalse(result['environment_observations'][0]['passed'])
        artifact = result['environment_updates'][0]['artifact']
        self.assertEqual(json.loads(Path(artifact).read_text())['checks'], checks)

    def test_omitted_update_and_old_report_tampering_fail_closed(self):
        first = self.add('a', 'Old facts', [dict(kind='executable', name='python3')])
        self.add('a', 'New facts')
        for row in (first, self.env.run['environment_checks'][0]):
            path = Path(row['artifact'])
            original = path.read_bytes()
            path.write_bytes(original + b' ')
            with self.assertRaises(HarnessError):
                self.env.environment_packet({}, task_id='unrelated')
            path.write_bytes(original)

    def test_repeated_checks_have_bounded_projection_and_immutable_full_index(self):
        self.add('a', 'A', [dict(kind='executable', name='python3')], passed=False)
        self.add('b', 'B', [dict(kind='executable', name='python3')])
        first = self.env.environment_packet({}, task_id='a')
        old_index = first['environment_index']
        old_raw = Path(old_index['artifact']).read_bytes()
        original = dict(self.env.run['environment_checks'][0])
        report = json.loads(Path(original['artifact']).read_bytes())
        for i in range(100):
            path = self.env.directory / f'repeated-{i}.json'
            write_json(path, dict(report, observed_at=i + 2))
            self.env.run['environment_checks'].append(dict(original, artifact=str(path),
                artifact_digest=hashlib.sha256(path.read_bytes()).hexdigest()))
        packet = self.env.environment_packet({}, task_id='a')
        self.assertEqual(len(packet['environment_check_history']), 1)
        self.assertEqual({r['task_id'] for r in packet['environment_history']}, {'a'})
        self.assertFalse(packet['environment_observations'][0]['passed'])
        self.assertEqual(packet['environment_observations'][0]['failed_check_indices'], [0])
        self.assertLess(abs(len(json.dumps(packet)) - len(json.dumps(first))), 100)
        index = json.loads(Path(packet['environment_index']['artifact']).read_bytes())
        self.assertEqual(len(index['checks']), 102)
        self.assertEqual(Path(old_index['artifact']).read_bytes(), old_raw)
        # Recovery validates reports absent from the current task and latest view.
        for row in (original, self.env.run['environment_checks'][1]):
            path = Path(row['artifact'])
            raw = path.read_bytes()
            path.write_bytes(raw + b' ')
            with self.assertRaises(HarnessError):
                self.env.environment_context()
            with self.assertRaises(HarnessError):
                self.env.environment_packet({}, task_id='unrelated')
            path.write_bytes(raw)

    def test_corrupt_existing_index_is_preserved_and_rejected(self):
        self.add('a', 'A')
        ref = self.env.environment_packet({}, task_id='a')['environment_index']
        path = Path(ref['artifact'])
        path.write_bytes(b'corrupt')
        with self.assertRaises(HarnessError):
            self.env.environment_packet({}, task_id='a')
        self.assertEqual(path.read_bytes(), b'corrupt')
