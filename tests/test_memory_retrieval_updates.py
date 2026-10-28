"""Behavior coverage for source binding, retrieval projection and history auditing."""
import contextlib
import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import git
from harness.cli import main
from harness.memory import Memory, MAX_SOURCE_BYTES, bind_source_files, source_status, ranking_group
from harness.mcp_server import handle
from harness.util import HarnessError


class RetrievalUpdatesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        git.git(self.repo, 'init', '-b', 'main')
        git.git(self.repo, 'config', 'user.email', 'test@example.com')
        git.git(self.repo, 'config', 'user.name', 'Test')
        self.file = self.repo / 'docs' / 'x.md'
        self.file.parent.mkdir()
        self.file.write_text('initial\n')
        self.commit()
        self.path = self.repo / '.harness' / 'memory.sqlite3'
        self.memory = Memory(self.path, repo=self.repo)

    def commit(self):
        git.git(self.repo, 'add', 'docs')
        git.git(self.repo, 'commit', '-m', 'fixture')

    def add(self, key='fact', **kwargs):
        fields = dict(key=key, abstract='alpha beta', overview='summary', detail='detail', source='operator')
        fields.update(kwargs)
        return self.memory.remember(**fields)

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--repo', str(self.repo), 'memory', *args])
        return code, out.getvalue(), err.getvalue()

    def test_unbound_matching_changed_unavailable_and_invalid_preserve_reasons(self):
        legacy = self.add('legacy')
        unbound = self.add('unbound', provenance={'scope': 'project'})
        bound = self.add('bound', provenance=bind_source_files(self.repo, ['docs/x.md']))
        invalid = self.add('invalid', kind='project_knowledge')
        self.assertEqual(self.memory.read(legacy)['stale_reasons'], ['missing_provenance'])
        self.assertEqual(self.memory.read(legacy)['source_status'], 'unbound')
        self.assertEqual(self.memory.read(unbound)['source_status'], 'unbound')
        self.assertEqual(self.memory.read(bound)['source_status'], 'matching')
        self.assertEqual(self.memory.read(invalid)['source_status'], 'invalid')
        no_repo = Memory(self.path, read_only=True)
        self.assertEqual(no_repo.read(bound)['source_status'], 'not_checked')
        self.assertEqual(no_repo.read(unbound)['source_status'], 'unbound')
        self.assertEqual(no_repo.read(unbound)['stale_reasons'], ['source_not_checked'])
        self.file.write_text('changed')
        self.assertEqual(self.memory.read(bound)['source_status'], 'changed')
        self.file.unlink()
        self.assertEqual(self.memory.read(bound)['source_status'], 'unavailable')

    def test_status_priority_and_ranking_unknown_reasons(self):
        base = {'provenance': {'commit': 'a' * 40}}
        cases = [(['source_changed:x', 'source_unavailable:y', 'unknown:z'], 'changed'),
                 (['source_unavailable:y', 'unknown:z'], 'unavailable'),
                 (['commit_unavailable', 'commit_not_checked'], 'unavailable'),
                 (['unknown:z'], 'not_checked'), (['invalid_provenance', 'source_changed:x'], 'invalid'),
                 ([], 'matching')]
        for reasons, expected in cases:
            item = {**base, 'stale_reasons': reasons}
            self.assertEqual(source_status(item), expected)
        for reasons in ([], ['missing_provenance'], ['source_not_checked', 'commit_not_checked']):
            self.assertEqual(ranking_group({'stale_reasons': reasons}), 'preferred')
        for reasons in (['unknown'], ['source_not_checked', 'source_changed:x']):
            self.assertEqual(ranking_group({'stale_reasons': reasons}), 'needs_review')

    def test_cli_binding_normalizes_deduplicates_and_keeps_source_text_inert(self):
        code, ident, error = self.invoke('add', '--key', 'bound', '--title', 'Bound', '--text', 'fact',
                                         '--source', 'missing/path', '--source-file', './docs/x.md',
                                         '--source-file', 'docs/./x.md')
        self.assertEqual(code, 0, error)
        record = self.memory.read(ident.strip())
        self.assertEqual(record['source'], 'missing/path')
        self.assertEqual(record['provenance']['commit'], git.git(self.repo, 'rev-parse', 'HEAD').strip())
        self.assertEqual(record['provenance']['source_hashes'],
                         {'docs/x.md': hashlib.sha256(self.file.read_bytes()).hexdigest()})
        self.assertEqual(record['source_status'], 'matching')
        self.assertEqual(self.invoke('add', '--key', 'plain', '--title', 'Plain', '--text', 'x',
                                     '--source', '/nonexistent')[0], 0)

    def test_binding_batch_rejects_before_replacing_current_record(self):
        original = self.add()
        before = self.memory.export()
        bad = ['../docs/x.md', '/docs/x.md', 'docs/../docs/x.md', r'docs\x.md', '.git/config',
               './.harness/memory.sqlite3', 'docs/absent', 'DOCS/x.md', 'docs/\0bad']
        for name in bad:
            code, _, _ = self.invoke('add', '--key', 'fact', '--title', 'New', '--text', 'new',
                                     '--source-file', 'docs/x.md', '--source-file', name)
            self.assertEqual(code, 1, name)
            self.assertEqual(self.memory.export(), before)
        self.assertTrue(self.memory.read(original)['active'])
        self.file.write_bytes(b'initial\r\n')
        self.assertIn('原始字节不同', self.invoke('add', '--key', 'fact', '--title', 'New', '--text', 'new',
                                             '--source-file', 'docs/x.md')[2])
        self.assertEqual(self.memory.export(), before)

    def test_source_limits_and_symlinks_are_rejected(self):
        self.file.write_bytes(b'x' * (MAX_SOURCE_BYTES + 1))
        with self.assertRaisesRegex(HarnessError, '过大'):
            bind_source_files(self.repo, ['docs/x.md'])
        with self.assertRaisesRegex(HarnessError, '十个'):
            bind_source_files(self.repo, [f'docs/{i}' for i in range(11)])
        self.file.unlink()
        self.file.symlink_to('target.md')
        (self.file.parent / 'target.md').write_text('initial\n')
        with self.assertRaisesRegex(HarnessError, '符号链接'):
            bind_source_files(self.repo, ['docs/x.md'])
        self.commit()
        with self.assertRaisesRegex(HarnessError, '符号链接'):
            bind_source_files(self.repo, ['docs/x.md'])

    def test_binding_preserves_case_spaces_unicode_and_rejects_escape(self):
        name = 'docs/大小 写.md'
        (self.repo / name).write_text('内容')
        self.commit()
        self.assertEqual(list(bind_source_files(self.repo, ['./' + name])['source_hashes']), [name])
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / 'external'
            target.write_text('external')
            self.file.unlink()
            self.file.symlink_to(target)
            with self.assertRaises(HarnessError):
                bind_source_files(self.repo, ['docs/x.md'])

    def test_exact_keys_are_raw_and_eligible_and_cli_validates_missing_database(self):
        keys = ['Mixed', 'mixed', 'has space', '中文', 'é', 'e\u0301', 'trail ']
        for key in keys:
            ident = self.add(key)
            self.assertEqual([item['id'] for item in self.memory.search(key=key)], [ident])
            code, out, err = self.invoke('search', '--key', key, '--level', '2')
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)[0]['id'], ident)
            self.assertIn('detail', json.loads(out)[0])
        self.assertEqual(self.memory.search(key='trail'), [])
        old = self.add('version')
        current = self.add('version')
        self.assertEqual([r['id'] for r in self.memory.search(key='version')], [current])
        self.assertFalse(self.memory.read(old)['active'])
        scoped = self.add('run-only', provenance={'scope': 'run', 'run_id': 'r'})
        self.assertEqual(self.memory.search(key='run-only'), [])
        self.assertEqual(self.memory.search(key='run-only', run_id='r')[0]['id'], scoped)
        with self.memory.connect() as db:
            db.execute('UPDATE memories SET expires=1 WHERE key=?', ('version',))
        self.assertEqual(self.memory.search(key='version'), [])
        self.path.unlink()
        for args in [('search',), ('search', 'x', '--key', 'y'), ('search', '--key', '')]:
            self.assertEqual(self.invoke(*args)[0], 1)
        self.assertEqual(json.loads(self.invoke('search', '')[1]), [])
        self.assertFalse(self.path.exists())

    def test_metadata_overlap_is_set_based_and_historical_paths_unchanged(self):
        ident = self.add('knowledge:architecture.storage', abstract='docs/x.md', overview='', detail='',
                         provenance={'source_hashes': {'./docs/x.md': hashlib.sha256(self.file.read_bytes()).hexdigest()}})
        before = self.memory.export()
        for query in ('architecture.storage', 'knowledge:architecture.storage', 'docs/x.md'):
            report = {}
            self.assertEqual(self.memory.search(query, diagnostics=report)[0]['id'], ident)
            self.assertEqual(report['counts']['matched'], 1)
        report = {}
        self.memory.search('docs/x.md', diagnostics=report)
        detail = report['candidates'][0]
        self.assertEqual(detail['match_modes'], ['body', 'path'])
        self.assertEqual(detail['matched_terms'], ['docs/x.md'])
        self.assertLess(detail['score'], 13)
        self.assertEqual(self.memory.search('knowledge'), [])
        body = self.add('unrelated', abstract='knowledge')
        self.assertEqual([r['id'] for r in self.memory.search('knowledge')], [body])
        self.assertEqual(self.memory.read(ident)['provenance'], before[0]['provenance'])

    def test_two_groups_sort_before_limit_and_legacy_has_no_second_penalty(self):
        stale = self.add('stale', abstract='alpha beta gamma delta',
                         provenance=bind_source_files(self.repo, ['docs/x.md']))
        preferred = self.add('preferred', abstract='alpha')
        self.file.write_text('changed')
        self.assertEqual(self.memory.search('alpha beta gamma delta', limit=1)[0]['id'], preferred)
        legacy = self.add('legacy', abstract='alpha beta gamma delta')
        # Use valid bound knowledge and restore its source to matching.
        self.file.write_text('initial\n')
        binding = bind_source_files(self.repo, ['docs/x.md'])
        matching = self.add('matching', abstract='alpha', kind='project_knowledge', provenance=binding)
        with self.memory.connect() as db:
            db.execute('UPDATE memories SET active=0 WHERE id=?', (stale,))
        report = {}
        self.assertEqual(self.memory.search('alpha beta gamma delta', limit=1, diagnostics=report)[0]['id'], legacy)
        self.assertEqual(report['candidates'][0]['ranking_group'], 'preferred')
        self.assertGreater(report['candidates'][0]['score'], 8)

    def test_diagnostics_counts_bounds_scope_and_source_read_cache(self):
        report = {}
        self.memory.search('alpha', diagnostics=report)
        self.assertEqual(report['counts']['active_candidates'], 0)
        self.add('scope', provenance={'scope': 'run', 'run_id': 'other',
                                     **bind_source_files(self.repo, ['docs/x.md'])})
        with patch('harness.memory._source_hash', side_effect=AssertionError('scope must not check sources')):
            self.assertEqual(self.memory.search('alpha', diagnostics=report), [])
        self.assertEqual(report['counts']['scope_excluded'], 1)
        self.assertIsNone(report['candidates'][0]['score'])
        binding = bind_source_files(self.repo, ['docs/x.md'])
        for i in range(45):
            self.add(str(i), provenance=binding)
        from harness.memory import _source_hash
        with patch('harness.memory._source_hash', wraps=_source_hash) as read:
            results = self.memory.search('alpha', diagnostics=report)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(report['counts'], dict(active_candidates=46, matched=46, scope_excluded=1,
                                               applicable=45, returned=6, limit_excluded=39))
        self.assertEqual(len(report['candidates']), 40)
        self.assertTrue(report['details_truncated'])
        self.assertEqual([r['id'] for r in results], [r['id'] for r in report['candidates'][:6]])
        self.memory.search('absent', diagnostics=report)
        self.assertEqual(report['counts']['active_candidates'], 46)
        self.assertEqual(report['counts']['matched'], 0)
        query = 'alpha ' + ' '.join('term' + str(i) for i in range(15)) + ' ' + 'x'*1200
        self.add('large', abstract='alpha', detail=query)
        self.memory.search(query, diagnostics=report)
        self.assertEqual(len(report['query_excerpt']), 1024)
        self.assertTrue(report['query_truncated'])
        self.assertEqual(report['query_sha256'], hashlib.sha256(query.encode()).hexdigest())
        detail = next(item for item in report['candidates'] if item['key'] == 'large')
        self.assertEqual(len(detail['matched_terms']), 10)
        self.assertTrue(detail['terms_truncated'])
        self.assertTrue(all(len(term) <= 256 for term in detail['matched_terms']))

    def test_limit_diagnostics_survive_many_unmatched_records(self):
        for index in range(34):
            self.add(f'noise-{index}', abstract='unrelated')
        matched = {self.add(f'hit-{index}', abstract='needle') for index in range(8)}
        report = {}
        results = self.memory.search('needle', diagnostics=report)
        self.assertEqual(results, self.memory.search('needle'))
        returned = {item['id'] for item in results}
        excluded = {item['id'] for item in report['candidates'] if item['exclusion_stage'] == 'limit'}
        self.assertEqual(excluded, matched - returned)
        self.assertEqual(report['counts'], dict(active_candidates=42, matched=8, scope_excluded=0,
                                               applicable=8, returned=6, limit_excluded=2))
        self.assertEqual([item['exclusion_stage'] for item in report['candidates']],
                         [None] * 6 + ['limit'] * 2 + ['match'] * 32)
        self.assertTrue(report['details_truncated'])

    def test_limit_diagnostics_precede_scope_exclusions_without_changing_retrieval(self):
        for index in range(40):
            self.add(f'noise-{index}', abstract='unrelated')
            self.add(f'scoped-{index}', abstract='needle', provenance={'scope': 'run', 'run_id': 'other'})
        matched = {self.add(f'hit-{index}', abstract='needle') for index in range(8)}
        report = {}
        results = self.memory.search('needle', diagnostics=report)
        self.assertEqual(results, self.memory.search('needle'))
        returned = {item['id'] for item in results}
        excluded = {item['id'] for item in report['candidates'] if item['exclusion_stage'] == 'limit'}
        self.assertEqual(excluded, matched - returned)
        self.assertEqual(report['counts'], dict(active_candidates=88, matched=48, scope_excluded=40,
                                               applicable=8, returned=6, limit_excluded=2))
        self.assertEqual([item['exclusion_stage'] for item in report['candidates']],
                         [None] * 6 + ['limit'] * 2 + ['scope'] * 32)
        self.assertTrue(report['details_truncated'])

    def test_scope_diagnostics_preserve_query_and_exact_key_targets(self):
        for index in range(40):
            self.add(f'aaa-noise-{index}', abstract='unrelated')
        target = self.add('zzz-target', abstract='needle', provenance={'scope': 'run', 'run_id': 'other'})
        for search in ({'query': 'needle'}, {'key': 'zzz-target'}):
            with self.subTest(search=search):
                report = {}
                with patch.object(self.memory, '_staleness', side_effect=AssertionError('excluded source read')):
                    self.assertEqual(self.memory.search(**search, diagnostics=report), [])
                self.assertEqual(report['counts'], dict(active_candidates=41, matched=1, scope_excluded=1,
                                                       applicable=0, returned=0, limit_excluded=0))
                self.assertEqual(report['candidates'][0]['id'], target)
                self.assertEqual(report['candidates'][0]['exclusion_stage'], 'scope')
                self.assertIsNone(report['candidates'][0]['source_status'])
                self.assertEqual(len(report['candidates']), 40)
                self.assertTrue(report['details_truncated'])

    def test_history_audits_all_versions_without_reactivation(self):
        first = self.add('history', provenance=bind_source_files(self.repo, ['docs/x.md']))
        self.file.write_text('second\n')
        self.commit()
        second = self.add('history', provenance=bind_source_files(self.repo, ['docs/x.md']))
        with self.memory.connect() as db:
            db.execute('UPDATE memories SET expires=1 WHERE id=?', (first,))
        self.file.write_text('initial\n')
        before = self.memory.export()
        results = self.memory.history('history')
        self.assertEqual([item['id'] for item in results], [first, second])
        self.assertTrue(results[0]['expired'])
        self.assertFalse(results[0]['active'])
        self.assertEqual(results[0]['source_status'], 'matching')
        self.assertEqual(results[1]['supersedes'], first)
        self.assertEqual(results[1]['source_status'], 'changed')
        self.assertEqual(self.memory.export(), before)
        self.assertEqual(self.memory.search(key='history')[0]['id'], second)
        self.assertEqual(self.memory.history('History'), [])
        code, out, error = self.invoke('history', 'history')
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(out), results)

    def test_mcp_validates_xor_before_missing_database_and_handles_exact_key(self):
        def call(args):
            return handle({'method': 'tools/call', 'params': {'name': 'memory_search', 'arguments': args}},
                          Memory(self.path, read_only=True), self.repo, None)
        ident = self.add('中文 With Space')
        self.assertEqual(json.loads(call({'key': '中文 With Space'})['content'][0]['text'])[0]['id'], ident)
        self.assertEqual(json.loads(call({'query': 'alpha'})['content'][0]['text'])[0]['id'], ident)
        for args in ({}, {'query': 'alpha', 'key': '中文 With Space'}, {'query': 'alpha', 'extra': True}):
            self.assertTrue(call(args)['isError'])
        self.path.unlink()
        for args in ({}, {'query': '', 'key': 'x'}, {'key': ''}, {'query': None}, {'key': None},
                     {'query': '', 'extra': True}):
            self.assertTrue(call(args)['isError'])
        for args in ({'query': ''}, {'key': 'x'}):
            self.assertFalse(call(args)['isError'])
            self.assertEqual(json.loads(call(args)['content'][0]['text']), [])
        self.assertFalse(self.path.exists())

    def test_history_missing_and_legacy_database_are_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            git.git(repo, 'init', '-b', 'main')
            path = repo / '.harness' / 'memory.sqlite3'
            before = (repo / '.git' / 'info' / 'exclude').read_bytes()
            memory = Memory(path, repo=repo, read_only=True)
            self.assertEqual(memory.history('unknown'), [])
            self.assertFalse(path.parent.exists())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(['--repo', str(repo), 'memory', 'history', 'unknown']), 0)
            self.assertEqual(json.loads(out.getvalue()), [])
            self.assertEqual((repo / '.git' / 'info' / 'exclude').read_bytes(), before)
            path.parent.mkdir()
            with contextlib.closing(sqlite3.connect(path)) as db, db:
                db.execute('''CREATE TABLE memories (id TEXT PRIMARY KEY, key TEXT NOT NULL,
                    abstract TEXT NOT NULL, overview TEXT NOT NULL, detail TEXT NOT NULL,
                    source TEXT NOT NULL, kind TEXT NOT NULL, created REAL NOT NULL,
                    expires REAL, supersedes TEXT, active INTEGER NOT NULL DEFAULT 1)''')
                db.execute("INSERT INTO memories VALUES ('old','key','title','','','operator','operator',1,NULL,NULL,1)")
            data = path.read_bytes()
            result = memory.history('key')[0]
            self.assertEqual(result['source_status'], 'unbound')
            self.assertEqual(result['provenance'], {})
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_older_worktree_observes_changed_and_unavailable_main_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            worktree = Path(temp) / 'older'
            git.git(self.repo, 'worktree', 'add', '--detach', str(worktree), 'HEAD')
            self.file.write_text('new commit\n')
            added = self.file.parent / 'new.md'
            added.write_text('new source\n')
            self.commit()
            changed = self.add('changed', provenance=bind_source_files(self.repo, ['docs/x.md']))
            unavailable = self.add('unavailable', provenance=bind_source_files(self.repo, ['docs/new.md']))
            run_memory = Memory(self.path, repo=worktree, read_only=True)
            self.assertEqual(self.memory.read(changed)['source_status'], 'matching')
            self.assertEqual(run_memory.read(changed)['source_status'], 'changed')
            self.assertEqual(run_memory.read(unavailable)['source_status'], 'unavailable')
            ledger = self.path.parent / 'runs' / 'run_old' / 'state.json'
            ledger.parent.mkdir(parents=True)
            ledger.write_text(json.dumps({'id': 'run_old', 'repo': str(self.repo), 'status': 'paused',
                                          'worktree': str(worktree)}))
            code, ident, error = self.invoke('add', '--key', 'run-bound', '--title', 'Bound', '--text', 'x',
                                             '--run', 'run_old', '--source-file', 'docs/new.md')
            self.assertEqual(code, 0, error)
            record = self.memory.read(ident.strip(), run_id='run_old')
            self.assertEqual(record['source_status'], 'matching')
            self.assertEqual(run_memory.read(ident.strip(), run_id='run_old')['source_status'], 'unavailable')
            self.assertFalse((worktree / '.harness').exists())
            git.git(self.repo, 'worktree', 'remove', '--force', str(worktree))

    def test_raw_binding_does_not_invoke_filters_when_git_reports_clean(self):
        attributes = self.repo / 'docs' / '.gitattributes'
        attributes.write_text('x.md text eol=crlf\n')
        self.commit()
        self.file.write_bytes(b'initial\r\n')
        self.assertEqual(git.git(self.repo, 'diff', '--name-only', '--', 'docs/x.md'), '')
        with self.assertRaisesRegex(HarnessError, '原始字节不同'):
            bind_source_files(self.repo, ['docs/x.md'])
        # Install an observable clean filter after commit. Binding reads raw bytes only.
        marker = self.repo / 'filter-ran'
        git.git(self.repo, 'config', 'filter.observer.clean', 'touch ' + str(marker))
        git.git(self.repo, 'config', 'filter.observer.smudge', 'touch ' + str(marker))
        attributes.write_text('x.md filter=observer\n')
        self.file.write_bytes(b'initial\n')
        bind_source_files(self.repo, ['docs/x.md'])
        self.assertFalse(marker.exists())

    def test_source_and_commit_checks_cache_per_call_and_refresh_after_mutation(self):
        binding = bind_source_files(self.repo, ['docs/x.md'])
        self.add('one', provenance=binding)
        self.add('two', provenance=binding)
        original = git.git_process
        with patch('harness.memory.git.git_process', wraps=original) as calls:
            self.memory.search('alpha', diagnostics={})
        checks = [call for call in calls.call_args_list if call.args[1:3] == ('cat-file', '-e')]
        self.assertEqual(len(checks), 1)
        self.file.write_text('mutated')
        self.assertTrue(all(item['source_status'] == 'changed' for item in self.memory.search('alpha')))
        invalid_commit = self.add('missing-commit', provenance={'commit': '0' * 40})
        self.assertEqual(self.memory.read(invalid_commit)['source_status'], 'unavailable')
        with patch('harness.memory.git.git_process', side_effect=OSError('unavailable')):
            self.assertEqual(self.memory.read(invalid_commit)['source_status'], 'not_checked')

    def test_stdio_mcp_exact_keys_and_missing_database_validation(self):
        root = Path(__file__).resolve().parents[1]
        def calls(arguments):
            requests = [{'jsonrpc': '2.0', 'id': index, 'method': 'tools/call',
                         'params': {'name': 'memory_search', 'arguments': args}}
                        for index, args in enumerate(arguments)]
            process = subprocess.run([sys.executable, str(root / 'harness' / 'mcp_entry.py'),
                                      '--state', str(self.path.parent), '--skills', str(root / 'shared' / 'skills')],
                                     cwd=root, text=True, input=''.join(json.dumps(req) + '\n' for req in requests),
                                     capture_output=True, timeout=20)
            self.assertEqual(process.returncode, 0, process.stderr)
            return [json.loads(line)['result'] for line in process.stdout.splitlines()]
        keys = ['大小 写', 'Case', 'trail ', 'é', 'e\u0301']
        ids = [self.add(key) for key in keys]
        responses = calls([{'key': key, 'level': 2} for key in keys] + [{'key': 'case'}, {'key': 'trail'}])
        for result, ident in zip(responses, ids):
            self.assertFalse(result['isError'])
            self.assertEqual(json.loads(result['content'][0]['text'])[0]['id'], ident)
        self.assertTrue(all(json.loads(result['content'][0]['text']) == [] for result in responses[-2:]))
        self.path.unlink()
        responses = calls([{}, {'key': ''}, {'query': 'x', 'key': 'x'}, {'key': 'missing'}, {'query': ''}])
        self.assertTrue(all(result['isError'] for result in responses[:3]))
        self.assertTrue(all(not result['isError'] for result in responses[3:]))
        self.assertFalse(self.path.exists())

    def test_search_groups_mixed_unknown_and_unchecked_reasons_without_changing_scores(self):
        binding = bind_source_files(self.repo, ['docs/x.md'])
        preferred = self.add('preferred', abstract='alpha')
        review = self.add('review', abstract='alpha beta gamma', provenance=binding)
        original = self.memory._staleness
        for reasons in (['unknown:future'], ['source_not_checked', 'source_changed:docs/x.md'],
                        ['missing_provenance', 'invalid_provenance']):
            def staleness(item, *args):
                return {'possibly_stale': True, 'stale_reasons': reasons} if item['id'] == review else original(item, *args)
            with patch.object(self.memory, '_staleness', side_effect=staleness):
                report = {}
                results = self.memory.search('alpha beta gamma', diagnostics=report)
            self.assertEqual([item['id'] for item in results], [preferred, review])
            self.assertEqual(report['candidates'][1]['ranking_group'], 'needs_review')
            expected_status = 'changed' if 'source_changed:docs/x.md' in reasons else 'invalid' if 'invalid_provenance' in reasons else 'not_checked'
            self.assertEqual(results[1]['source_status'], expected_status)
        no_repo = Memory(self.path, read_only=True)
        report = {}
        self.assertEqual(no_repo.search('alpha beta gamma', diagnostics=report)[0]['id'], review)
        self.assertTrue(all(candidate['ranking_group'] == 'preferred' for candidate in report['candidates']))
        self.assertEqual(report['candidates'][0]['source_status'], 'not_checked')
