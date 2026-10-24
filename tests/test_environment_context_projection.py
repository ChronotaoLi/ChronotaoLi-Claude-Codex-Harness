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
        self.env.directory = Path(self.temp.name)
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
                ({'kind': 'final_review'}, {}, ['b', 'a'])]:
            packet.update(acceptance=['must pass'], tool_constraints={'permissions': []}, blockers=['unresolved'])
            result = self.env.environment_packet(packet, **kwargs)
            self.assertEqual([r['task_id'] for r in result['environment_updates']], expected)
            for key in packet:
                self.assertEqual(result[key], packet[key])
            self.assertEqual(len(result['environment_history']), 3)
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
