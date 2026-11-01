"""Explicit real-account schema + stream fixture; never called by normal runs."""
import argparse
import copy
import json
import subprocess
import uuid
from pathlib import Path

from .adapters import CLIBackend, provider_usage
from .config import Config
from .private_state import ensure_private_state
from .stream_capture import permission_denial_projection
from .util import HarnessError, read_json, write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path,
                        help='New directory inside an already protected .harness private root')
    parser.add_argument('--case', choices=('success', 'denial'), default='success')
    args = parser.parse_args(argv)
    destination = args.output.resolve()
    if '.harness' not in destination.parts:
        parser.error('--output must be under protected .harness private state')
    private_root = next(parent for parent in destination.parents if parent.name == ".harness")
    ensure_private_state(private_root.parent)
    destination.mkdir(parents=True, exist_ok=False)
    config = Config.load(args.config)
    config = Config(config.path, copy.deepcopy(config.data))
    config.data['runtime']['claude_output_mode'] = 'stream_metadata_v1'
    config.data['runtime']['agent_timeout_seconds'] = 180
    config.data['runtime']['claude_max_turns'] = 6
    empty_mcp = destination / 'mcp-empty.json'
    write_json(empty_mcp, {'servers': {}})
    config.data['shared']['mcp'] = str(empty_mcp)
    repo = destination / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    (repo / 'fixture.txt').write_text('fixture marker: bluebird\n')
    job = destination / str(uuid.uuid4())
    job.mkdir()
    model = config.role('worker')
    if model['cli'] != 'claude':
        raise HarnessError('Fixture requires configured Claude worker; model is never substituted')
    schema = {'type': 'object', 'properties': {
        'dispatch_id': {'type': 'string', 'enum': [job.name]},
        'marker': {'type': 'string', 'enum': ['bluebird']},
        'status': {'type': 'string', 'enum': ['completed']}},
        'required': ['dispatch_id', 'marker', 'status'], 'additionalProperties': False}
    prompt = ('This is an isolated integration fixture. Use Read once to read fixture.txt. '
              'Return the required structured JSON marker and status. ')
    if args.case == 'denial':
        prompt += ('First attempt Bash with the exact harmless command: printf stream-fixture-denial . '
                   'If denied, continue with Read and return JSON. Do not claim a denial unless the tool reports it. ')
    prompt += 'dispatch_id=' + job.name
    class FixtureBackend(CLIBackend):
        def command(self, *positional, **keywords):
            command = super().command(*positional, **keywords)
            if args.case == 'denial':
                index = command.index('--settings') + 1
                settings = json.loads(command[index])
                settings['permissions']['deny'] = ['Bash(printf stream-fixture-denial *)']
                command[index] = json.dumps(settings)
                write_json(job / 'fixture-permission-policy.json', {'deny': ['Bash(printf stream-fixture-denial *)']})
            return command

    backend = FixtureBackend(config, destination, allow_stream_fixture=True)
    receipt = {'version': 1, 'dispatch_id': job.name, 'model': model, 'case': args.case,
               'job': str(job), 'integration_verified': False}
    try:
        value = backend.invoke(role='reviewer', model=model, cwd=repo, job=job, schema=schema,
                               prompt=prompt, run_path=None, on_start=lambda pid: receipt.update(pid=pid))
        # Exact schema contract, not the model's verbal report, is checked locally.
        if value != {'dispatch_id': job.name, 'marker': 'bluebird', 'status': 'completed'}:
            raise HarnessError('Fixture response does not satisfy exact schema contract')
        terminal = read_json(job / 'stream-terminal.json')
        denials = permission_denial_projection(terminal)
        metadata = [json.loads(line) for line in (job / 'stream-metadata.jsonl').read_text().splitlines()]
        starts = {event.get('tool_id') for event in metadata
                  if event.get('event') == 'tool_start' and event.get('tool') == 'Bash'}
        ends = {event.get('tool_id') for event in metadata
                if event.get('event') == 'tool_end' and event.get('status') == 'error'}
        matched_denial = any(item.get('tool_id') in starts & ends for item in denials)
        receipt.update(tool_denial_verified=matched_denial,
                       response=value, usage=provider_usage(job, 'claude'),
                       denials=permission_denial_projection(terminal),
                       capture=read_json(job / 'stream-capture.json'), integration_verified=True)
    except BaseException as error:
        receipt.update(error_type=type(error).__name__, usage=provider_usage(job, 'claude'))
        write_json(destination / 'receipt.json', receipt)
        raise
    write_json(destination / 'receipt.json', receipt)
    print(json.dumps({'receipt': str(destination / 'receipt.json'), 'integration_verified': True}))


if __name__ == '__main__':
    main()
