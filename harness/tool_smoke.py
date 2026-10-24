"""Opt-in real Claude integration probe for closed commands and file evidence tools."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from .adapters import CLIBackend
from .command_server import command_id, ensure_command_processes_exited
from .config import Config
from .permission_smoke import _git
from .util import HarnessError, dump, run_process, write_json


def prepare_fixture(directory):
    directory.mkdir(parents=True, mode=0o700)
    repo, worktree = directory / 'repo', directory / 'worktree'
    repo.mkdir()
    script = ('import json, os, sys\nfrom pathlib import Path\n'
              'Path("argv-marker.json").write_text(json.dumps([os.fsencode(x).hex() for x in sys.argv[1:]]))\n'
              'print("fixture stdout")\nprint("fixture stderr", file=sys.stderr)\n')
    (repo / 'argv_fixture.py').write_text(script, encoding='utf-8')
    known = 'known hash fixture\n'
    (repo / 'known.txt').write_text(known, encoding='utf-8')
    _git(repo, 'init', '-q', '-b', 'main')
    _git(repo, 'add', '.')
    _git(repo, '-c', 'user.name=Harness Tool Smoke', '-c', 'user.email=smoke@localhost',
         '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Tool fixture')
    _git(repo, 'worktree', 'add', '--detach', str(worktree))
    values = ['with spaces', "single'quote", 'double"quote', '*', ';', 'literal\nnewline', '$(printf unintended)']
    commands = [[sys.executable, 'argv_fixture.py', *values],
                ['/bin/sh', '-c', "printf '%s\\n' 'pipeline fixture' | /usr/bin/tr a-z A-Z"]]
    valid, invalid = '{"ok": [1, true]}\n', '{"duplicate":1,"duplicate":2}\n'
    (worktree / 'untracked-valid.json').write_text(valid, encoding='utf-8')
    (worktree / 'untracked-invalid.json').write_text(invalid, encoding='utf-8')
    left, right = directory / 'external-left.txt', directory / 'external-right.txt'
    left.write_text('before\n', encoding='utf-8')
    right.write_text('after\n', encoding='utf-8')
    cases = []
    def case(name, tool, args, expected=None, error=False):
        cases.append(dict(name=name, tool=tool, input=args, expected=expected or {}, error=error))
    case('list', 'mcp__harness_commands__list_commands', {})
    case('exact_argv', 'mcp__harness_commands__run_command', {'command_id': command_id(commands[0])},
         {'exit_code': 0, 'stdout': 'fixture stdout\n', 'stderr': 'fixture stderr\n'})
    case('explicit_pipeline', 'mcp__harness_commands__run_command', {'command_id': command_id(commands[1])},
         {'exit_code': 0, 'stdout': 'PIPELINE FIXTURE\n', 'stderr': ''})
    case('unknown_id', 'mcp__harness_commands__run_command', {'command_id': 'unauthorized-command'}, error=True)
    case('extra_argv', 'mcp__harness_commands__run_command',
         {'command_id': command_id(commands[0]), 'argv': '--ungranted'}, error=True)
    case('hash', 'mcp__harness__file_sha256', {'path': 'known.txt'},
         {'sha256': hashlib.sha256(known.encode()).hexdigest(), 'bytes': len(known.encode())})
    case('valid_json', 'mcp__harness__json_check', {'path': 'untracked-valid.json'}, {'valid': True})
    case('invalid_json', 'mcp__harness__json_check', {'path': 'untracked-invalid.json'}, {'valid': False})
    case('external_diff', 'mcp__harness__file_diff', {'left': str(left), 'right': str(right)},
         {'different': True, 'binary': False, 'diff': '--- left\n+++ right\n@@ -1 +1 @@\n-before\n+after\n'})
    expected = {str(worktree / 'argv-marker.json'): json.dumps([os.fsencode(x).hex() for x in values]),
                str(worktree / '.git'): (worktree / '.git').read_text(),
                str(worktree / 'known.txt'): known, str(worktree / 'argv_fixture.py'): script,
                str(worktree / 'untracked-valid.json'): valid, str(worktree / 'untracked-invalid.json'): invalid,
                str(left): 'before\n', str(right): 'after\n'}
    fixture = dict(worktree=str(worktree), cases=cases, commands=commands, expected_files=expected)
    write_json(directory / 'fixture.json', fixture)
    return fixture


def _value(outcome):
    content = outcome.get('content', [])
    if isinstance(content, str):
        return json.loads(content)
    for item in content:
        if isinstance(item, dict) and item.get('type') == 'text':
            try:
                return json.loads(item['text'])
            except (ValueError, KeyError):
                pass
    return None


def inspect_evidence(path, fixture, job=None):
    calls, results, final, init = {}, {}, None, {}
    try:
        for line in path.read_text(encoding='utf-8').splitlines():
            event = json.loads(line)
            if event.get('type') == 'result':
                final = event
            if event.get('type') == 'system' and event.get('subtype') == 'init':
                init = event
            content = event.get('message', {}).get('content', [])
            if not isinstance(content, list):
                continue
            for block in content:
                if event.get('type') == 'assistant' and block.get('type') == 'tool_use':
                    calls[block['id']] = block
                if event.get('type') == 'user' and block.get('type') == 'tool_result':
                    results[block['tool_use_id']] = block
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise HarnessError(f'Invalid tool smoke provider evidence: {path}') from error
    if final is None:
        raise HarnessError('Tool smoke lacks final provider result')
    checks, covered = [], set()
    denials = final.get('permission_denials', [])
    if not isinstance(denials, list):
        raise HarnessError('Tool smoke permission_denials must be an array')
    for case in fixture['cases']:
        matched = [(key, call) for key, call in calls.items()
                   if call.get('name') == case['tool'] and call.get('input') == case['input']]
        passed = len(matched) == 1
        for key, call in matched:
            covered.add(key)
            result = results.get(key, {})
            passed = passed and key in results and bool(result.get('is_error', False)) == case['error']
            # Broker rejections must reach MCP; CLI permission denials are not broker evidence.
            passed = passed and not any(d.get('tool_use_id') == key for d in denials if isinstance(d, dict))
            if case['error'] and case['name'] in ('unknown_id', 'extra_argv'):
                expected_error = 'Unknown command_id' if case['name'] == 'unknown_id' else 'run_command accepts only command_id'
                passed = passed and expected_error in json.dumps(result.get('content', ''), ensure_ascii=False)
            if not case['error']:
                try:
                    value = _value(result)
                    passed = passed and value is not None
                    passed = passed and all(isinstance(value, dict) and value.get(k) == v for k, v in case['expected'].items())
                    if case['name'] == 'list':
                        passed = passed and value == [{'command_id': command_id(c), 'argv': c} for c in fixture['commands']]
                    if case['tool'].endswith('__run_command') and isinstance(value, dict):
                        passed = passed and value.get('exit_confirmed') is True
                        for stream in ('stdout', 'stderr'):
                            artifact = Path(value.get(stream + '_path', ''))
                            passed = passed and artifact.is_file() and (job is None or artifact.is_relative_to(Path(job) / 'commands'))
                            if artifact.is_file():
                                passed = passed and hashlib.sha256(artifact.read_bytes()).hexdigest() == value.get(stream + '_sha256')
                except (ValueError, TypeError, OSError):
                    passed = False
        checks.append(dict(name=case['name'], passed=bool(passed), tool_use_ids=[k for k, _ in matched]))
    checks.append(dict(name='only_requested_tools', passed=set(calls) == covered))
    for name, expected in fixture['expected_files'].items():
        try:
            passed = not Path(name).is_symlink() and Path(name).read_text(encoding='utf-8') == expected
        except (OSError, UnicodeError):
            passed = False
        checks.append(dict(name='file:' + name, passed=passed))
    if job is not None:
        records = ensure_command_processes_exited(job)
        checks.append(dict(name='exactly_two_command_processes', passed=len(records) == 2 and
                           sorted(r['command_id'] for r in records) == sorted(command_id(c) for c in fixture['commands'])))
    return dict(passed=final.get('subtype') == 'success' and not final.get('is_error', False) and all(c['passed'] for c in checks),
                checks=checks, provider_subtype=final.get('subtype'), provider_model=init.get('model'),
                claude_code_version=init.get('claude_code_version'), total_cost_usd=final.get('total_cost_usd'),
                num_turns=final.get('num_turns'), stream_sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def _model_called(path):
    try:
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and (event.get('type') == 'assistant' or
                    event.get('type') == 'result' and bool(event.get('usage'))):
                return True
    except OSError:
        pass
    return False


def tool_smoke(config, repo, *, timeout=180, max_turns=20, max_budget_usd=1.0):
    if timeout <= 0 or not 0 < max_turns <= 20 or not 0 < max_budget_usd < float('inf'):
        raise HarnessError('Invalid tool smoke timeout, turn limit or budget')
    model = config.role('worker')
    if model['cli'] != 'claude':
        raise HarnessError('Tool smoke requires the configured Claude worker; no model substitution')
    directory = Path(repo).resolve() / '.harness' / 'tool-smoke' / uuid.uuid4().hex
    fixture = prepare_fixture(directory)
    job = directory / str(uuid.uuid4())
    job.mkdir()
    probe = copy.deepcopy(config)
    probe.data['verification']['commands'] = []
    probe.data['permissions']['worker_commands'] = fixture['commands']
    probe.data['runtime']['claude_max_turns'] = max_turns
    write_json(directory / 'external-mcp.json', {'servers': {}})
    probe.data['shared']['mcp'] = str(directory / 'external-mcp.json')
    argv = CLIBackend(probe, directory / 'state').command('worker', model, Path(fixture['worktree']), job, {}, None)
    index = argv.index('--json-schema')
    del argv[index:index + 2]
    argv[argv.index('--output-format') + 1] = 'stream-json'
    argv += ['--verbose', '--max-budget-usd', str(max_budget_usd)]
    prompt = ('Execute this harmless disposable MCP integration probe. Call each exact named MCP tool once '
              'with exactly the listed input, in order. Unknown ID and extra argv must actually be attempted '
              'and rejected by the MCP broker. Invalid JSON is a successful tool result with valid=false. '
              'Continue after errors. Do not ask the user, retry, use agents, change permissions, substitute tools, '
              'or invoke Bash. Use only these MCP calls. Finish briefly; prose is not evidence.\n' +
              dump([{k: c[k] for k in ('name', 'tool', 'input')} for c in fixture['cases']]))
    write_json(job / 'command.json', argv)
    (job / 'prompt.txt').write_text(prompt, encoding='utf-8')
    report = dict(cli_attempted=True, models_called=False, simulation=False, model=model['model'], dispatch_id=job.name, evidence=str(directory),
                  limitations='One CLI version and bounded fixture; not OS isolation or exhaustive coverage')
    print(f"[harness] LIVE TOOL SMOKE model={model['model']} max_budget_usd={max_budget_usd} evidence={directory}", file=sys.stderr, flush=True)
    env = dict(os.environ, CLAUDE_CODE_DISABLE_AUTO_MEMORY='1')
    try:
        code = run_process(argv, Path(fixture['worktree']), job / 'process', stdin=prompt, timeout=timeout, env=env)
        report.update(exit_code=code, stderr=(job / 'process.stderr').read_text(encoding='utf-8', errors='replace')[-4000:])
        report['models_called'] = _model_called(job / 'process.stdout')
        report.update(inspect_evidence(job / 'process.stdout', fixture, job))
        report.update(exit_code=code, passed=report['passed'] and code == 0)
    except (HarnessError, KeyboardInterrupt) as error:
        report.update(passed=False, error=str(error) or 'interrupted')
        raise
    finally:
        try:
            report['command_processes'] = ensure_command_processes_exited(job)
        except HarnessError as error:
            report.update(passed=False, command_process_error=str(error))
        write_json(directory / 'report.json', report)
    return report


def _main(argv=None):
    parser = argparse.ArgumentParser(description='Real Claude MCP tool smoke; consumes account budget')
    parser.add_argument('--config', type=Path, default=Path(__file__).resolve().parents[1] / 'harness.toml')
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--max-turns', type=int, default=20)
    parser.add_argument('--max-budget-usd', type=float, default=1.0)
    args = parser.parse_args(argv)
    if args.timeout <= 0 or not 0 < args.max_turns <= 20 or not 0 < args.max_budget_usd < float('inf'):
        parser.error('timeout/budget must be positive finite values and max-turns must be 1..20')
    try:
        report = tool_smoke(Config.load(args.config), args.repo, timeout=args.timeout,
                            max_turns=args.max_turns, max_budget_usd=args.max_budget_usd)
        print(dump(report))
        return 0 if report['passed'] else 1
    except (HarnessError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f'harness: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def main(argv=None):
    import signal
    def interrupted(signum, frame):
        raise KeyboardInterrupt()
    previous = {}
    try:
        for name in ('SIGTERM', 'SIGHUP'):
            if signum := getattr(signal, name, None):
                previous[signum] = signal.signal(signum, interrupted)
        return _main(argv)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == '__main__':
    raise SystemExit(main())
