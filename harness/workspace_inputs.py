"""Explicit immutable dependency copies; ledger binding is authoritative.

Manifest v1: schema_version, run_id, head, source_root ('.'), max_files,
max_bytes, files [{source, destination, sha256, mode, type:'file',
ownership:'input_copy'}]. All paths are exact relative ordinary files.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from .versions import parse_current_ledger, precheck_run
from . import git, stops
from .cleanup import _canonical, read_run_state, with_run_lock
from .path_evidence import validate_dispatch_indexes
from .input_projection import validate_dispatch_indexes as validate_input_dispatch_indexes
from .worker_receipts import validate_worker_receipt_links
from .workspace_input_names import NAME_ERRORS, check_destination_layout
from .run_store import RunStore
from .state_files import StateFiles
from .util import (HarnessError, ensure_dispatches_exited, fold_name, group_alive,
                   validate_command_directories, write_json)

_RESERVED = frozenset(map(fold_name, (*git.PROTECTED_ROOTS, 'agents.md', 'claude.md')))
_CREDENTIAL_NAMES = frozenset(('.env', '.ssh', '.aws', '.gnupg', 'credentials', 'credentials.json'))
_COPY_TEMP_PREFIX = '.harness-input-'
_PENDING = ('pending_task', 'pending_acceptance', 'pending_post_accept',
            'pending_revision', 'pending_directive', 'worker_recovery', 'worker_receipts')

def _fail(message, stop=None):
    raise HarnessError('workspace inputs: ' + message, stop=stop)

def _manifest_failure(message):
    _fail(message, stops.make('invalid_input', 'input_schema', data={'option': 'manifest'}))

def _evidence_failure(message):
    _fail(message, stops.make('evidence', 'runtime_integrity'))

def _relative(value):
    if (not isinstance(value, str) or not value or '\\' in value or '\x00' in value
            or any(p in ('', '.', '..') for p in value.split('/'))
            or Path(value).is_absolute() or any(fold_name(p) in _RESERVED for p in value.split('/'))):
        _fail('invalid or reserved relative path')
    try:
        os.fsencode(value)
    except UnicodeEncodeError:
        _fail('relative path cannot be encoded for the filesystem')
    return value

@contextmanager
def _parent(path, *, create=False):
    """Walk every directory with no-follow descriptors before a mutation."""
    _canonical(path)
    with _directories(os.open('/', os.O_RDONLY | os.O_DIRECTORY), path.parent.parts[1:], create=create) as fd:
        yield fd

@contextmanager
def _directories(fd, parts, *, create):
    """Walk from an owned descriptor, closing it and any subsequently opened descriptor."""
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)

def _unlink(path):
    with _parent(path) as fd:
        os.unlink(path.name, dir_fd=fd)

def _copy_temp_name():
    return _COPY_TEMP_PREFIX + uuid.uuid4().hex

def _publish(temp, dest):
    with _parent(dest) as fd:
        os.link(temp.name, dest.name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)

def _identity(path, *, allowed_links=(1,)):
    _canonical(path)
    with _parent(path) as parent_fd:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink not in allowed_links:
            _fail('ordinary independent file required: ' + str(path))
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(fd)
        stable = lambda st: (st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        if stable(before) != stable(after) or stable(path.stat()) != stable(after):
            _fail('file changed during inspection: ' + str(path))
        return {'device': after.st_dev, 'inode': after.st_ino,
                'size': after.st_size, 'mode': stat.S_IMODE(after.st_mode),
                'sha256': digest.hexdigest()}
    finally:
        os.close(fd)

@stops.reads_evidence
def _registered_identity(path, *, allowed_links=(1,)):
    return _identity(path, allowed_links=allowed_links)

def _read(path):
    """Read the operator's manifest, whose path and contents are CLI input."""
    _canonical(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        _fail(f'manifest must be a regular file: {path}')
    return json.loads(path.read_bytes())

def _gate(run, directory, *, terminal=False):
    repo, checkout = Path(run['repo']), Path(run['worktree'])
    for p in (repo, checkout, directory):
        _canonical(p)
    if (directory != repo / '.harness' / 'runs' / run['id']
            or checkout != repo / '.harness' / 'worktrees' / run['id']):
        _fail('run path binding mismatch', stops.make('workspace_mismatch', 'binding_changed'))
    if terminal:
        if run.get('status') not in ('completed', 'abandoned'):
            _fail('cleanup requires terminal run',
                  stops.make('invalid_input', 'run_state', data={'status': str(run.get('status'))}))
    elif (run.get('status') not in ('awaiting_approval', 'paused')
          or any(run.get(k) for k in _PENDING)):
        _fail('prepare requires paused/awaiting_approval with no pending stage',
              stops.make('invalid_input', 'run_state', data={'status': str(run.get('status'))}))
    validate_command_directories(run['dispatches'], directory)
    validate_dispatch_indexes(directory, run['dispatches'])
    validate_input_dispatch_indexes(directory, run['dispatches'])
    validate_worker_receipt_links(run, directory)
    ensure_dispatches_exited(run['dispatches'], state=repo / '.harness', directory=directory, observe=group_alive)
    git.check_worktree_binding(checkout, run['worktree_binding'])
    if git.head(checkout, binding=run['worktree_binding']) != run['expected_head']:
        _fail('HEAD changed', stops.make('workspace_mismatch', 'head_changed', data={'expected_head': run['expected_head']}))
    git.check_worktree_state(checkout, binding=run['worktree_binding'])
    return git.snapshot(checkout, binding=run['worktree_binding'])

def _destination(run, name):
    path = Path(run['worktree']) / _relative(name)
    _canonical(path)
    tracked = git.git(Path(run['worktree']), 'ls-files', '-z', '--', name, effect=git.READ,
                      binding=run['worktree_binding'])
    if tracked or not git.git(Path(run['worktree']), 'check-ignore', '--no-index', '--', name, effect=git.READ,
                              binding=run['worktree_binding']).strip():
        _fail('destination must be ignored and untracked: ' + name)
    return path

def _check_source_names(parts):
    for part in parts:
        name = part.lower()
        if name in _CREDENTIAL_NAMES or name.endswith(('.pem', '.key')):
            _fail('credential-like source is not a dependency input')

def manifest_shape(ident, value):
    """Validate the operator's declaration without consulting the run or filesystem."""
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'run_id', 'head',
            'source_root', 'max_files', 'max_bytes', 'files'}
            or value['schema_version'] != 1 or value['run_id'] != ident or value['source_root'] != '.'
            or not isinstance(value['head'], str) or not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', value['head'])
            or type(value['max_files']) is not int or value['max_files'] < 1
            or type(value['max_bytes']) is not int or value['max_bytes'] < 0
            or not isinstance(value['files'], list)
            or not 0 < len(value['files']) <= value['max_files']):
        _fail('invalid manifest v1 or run/HEAD binding')
    sources, destinations = set(), set()
    for entry in value['files']:
        if (not isinstance(entry, dict) or set(entry) != {'source', 'destination', 'sha256', 'mode', 'type', 'ownership'}
                or entry['type'] != 'file' or entry['ownership'] != 'input_copy'
                or not isinstance(entry['sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', entry['sha256'])
                or type(entry['mode']) is not int or entry['mode'] not in (0o600, 0o644, 0o700, 0o755)):
            _fail('invalid file declaration')
        source = _relative(entry['source'])
        dest = _relative(entry['destination'])
        _check_source_names(source.split('/'))
        if source in sources or dest in destinations:
            _fail('duplicate or overlapping input')
        sources.add(source)
        destinations.add(dest)
    for field, names in (('sources', sources), ('destinations', destinations)):
        for name in names:
            if any(parent.as_posix() in names for parent in Path(name).parents):
                _fail(f'overlapping file {field}')

def _manifest(run, value):
    manifest_shape(run['id'], value)
    if value['head'] != run['expected_head']:
        _fail('invalid manifest v1 or run/HEAD binding')
    destinations, total = [], 0
    for entry in value['files']:
        source = Path(run['repo']) / entry['source']
        try:
            dest = _destination(run, entry['destination'])
        except OSError as error:
            if error.errno not in NAME_ERRORS:
                raise
            _fail(f'invalid destination on this volume: {entry["destination"]!r} ({error.strerror})')
        destinations.append(dest)
        _check_source_names(source.parts)
        if source == dest:
            _fail('duplicate or overlapping input')
        identity = _identity(source)
        if identity['sha256'] != entry['sha256'] or identity['mode'] != entry['mode']:
            _fail('source digest/mode differs from declaration')
        total += identity['size']
    if total > value['max_bytes']:
        _fail('manifest byte limit exceeded')
    return destinations

@stops.reads_evidence
def _registered(run, directory):
    binding = run.get('workspace_inputs')
    if binding is None:
        if 'workspace_inputs' not in run:
            _fail('missing authoritative workspace_inputs field')
        return None, None
    if (not isinstance(binding, dict) or set(binding) != {'schema_version', 'manifest_sha256'}
            or binding['schema_version'] != 1):
        _fail('invalid input ledger binding')
    files = StateFiles(directory.parent.parent).under('runs', directory.name)
    outcome = files.read(('workspace-inputs-manifest.json',), limit=None)
    raw = outcome.evidence({'artifact': outcome.path})
    if hashlib.sha256(raw).hexdigest() != binding['manifest_sha256']:
        _fail('frozen manifest missing or changed')
    manifest = json.loads(raw)
    if (not isinstance(manifest, dict) or manifest.get('run_id') != run['id']
            or manifest.get('source_root') != '.' or not isinstance(manifest.get('files'), list)):
        _fail('frozen manifest binding changed')
    outcome = files.read(('workspace-inputs.json',), limit=None)
    journal = json.loads(outcome.evidence({'artifact': outcome.path}))
    if (not isinstance(journal, dict) or journal.get('manifest_sha256') != binding['manifest_sha256']
            or journal.get('run_id') != run['id'] or journal.get('schema_version') != 1
            or not isinstance(journal.get('files'), list) or len(journal['files']) != len(manifest['files'])
            or not all(isinstance(receipt, dict) for receipt in journal['files'])):
        _fail('input journal binding invalid')
    return manifest, journal

@stops.reads_evidence
def validate_inputs(run, directory, *, allow_removed=False):
    """Read-only gate, called on resume and after local preparation, before the worker is counted."""
    directory = Path(directory)
    manifest, journal = _registered(run, directory)
    if manifest is None:
        return
    for entry, receipt in zip(manifest['files'], journal['files']):
        dest = _destination(run, entry['destination'])
        if allow_removed and receipt.get('stage') == 'removed' and not dest.exists():
            continue
        if receipt.get('stage') != 'copied' or _identity(dest) != receipt.get('identity'):
            _fail('input copy incomplete or modified: ' + entry['destination'])
        if receipt['identity']['sha256'] != entry['sha256'] or receipt['identity']['mode'] != entry['mode']:
            _fail('input declaration/receipt mismatch')

def prepare_run(repo, ident, manifest):
    """Prepare explicitly declared files. Acquires the run lock itself."""
    repo = git.repository(Path(repo))
    state = repo / '.harness'
    precheck_run(state, ident)
    directory = state / 'runs' / ident
    for path in (directory, directory / 'run.lock'):
        with stops.evidence_reads(source={'artifact': str(path)}):
            _canonical(path)
    with stops.classify('invalid_input', 'input_schema', data={'option': 'manifest'}):
        if isinstance(manifest, (str, Path)):
            try:
                value = _read(Path(manifest).absolute())
            except (OSError, ValueError, RecursionError) as error:
                # The operator's file (missing, unreadable, or unparsable JSON): this call's input is refused.
                raise HarnessError(f'manifest cannot be read as JSON: {error}') from error
        else:
            value = manifest
        manifest_shape(ident, value)
    from .private_state import ensure_private_state
    ensure_private_state(repo)
    files = StateFiles(state).under('runs', ident)

    def prepare_locked(locked):
        store = RunStore(state)
        store.bind_directory(locked.identity)
        with stops.evidence_reads(source={'artifact': str(directory / 'state.json')}):
            run = parse_current_ledger(read_run_state(files, locked))
        if run.get('id') != ident or run.get('repo') != str(repo):
            _fail('run ledger does not belong to this repository',
                  stops.make('invalid_input', 'cli_arguments', data={'option': 'run_id'}))
        store.attach(run)
        before = _gate(run, directory)
        with stops.classify('invalid_input', 'input_schema', data={'option': 'manifest'}):
            destinations = _manifest(run, value)
        frozen, journal = _registered(run, directory)
        journal_path = directory / 'workspace-inputs.json'
        if frozen is None:
            frozen_path = directory / 'workspace-inputs-manifest.json'
            for name in (frozen_path.name, journal_path.name):
                outcome = files.probe((name,)).or_local_io()
                if outcome.state == 'absent' and outcome.step.leaf:
                    continue
                if outcome.state in ('present', 'refused'):
                    _fail('unbound preparation evidence already exists',
                          stops.make('evidence', 'runtime_integrity', source={'artifact': outcome.path}))
                outcome.evidence({'artifact': outcome.path})
            with stops.classify('invalid_input', 'input_schema', data={'option': 'manifest'}):
                name_observations = check_destination_layout(
                    destinations, Path(run['worktree']), state, _copy_temp_name(), run_id=ident)
            # Ledger authority is saved first. A crash is fail-closed: missing
            # frozen evidence cannot silently revert to legacy dispatch behavior.
            raw = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()
            digest = hashlib.sha256(raw).hexdigest()
            run['workspace_inputs'] = {'schema_version': 1, 'manifest_sha256': digest}
            store.commit()
            with frozen_path.open('xb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            journal = {'schema_version': 1, 'run_id': ident, 'manifest_sha256': digest,
                       'files': [{'stage': 'declared'} for _ in value['files']],
                       'destination_name_observations': name_observations}
            write_json(journal_path, journal)
        elif frozen != value:
            _manifest_failure('registered manifest is immutable')
        for entry, receipt in zip(value['files'], journal['files']):
            source = repo / entry['source']
            dest = _destination(run, entry['destination'])
            if receipt['stage'] == 'copied':
                if _registered_identity(dest) != receipt['identity']:
                    _evidence_failure('completed copy changed')
                continue
            if receipt['stage'] == 'published_intent':
                # Publication uses rename through hardlink: while both names
                # exist nlink=2. Never infer ownership from bytes alone.
                temp = Path(receipt['temp'])
                with stops.evidence_reads():
                    _canonical(temp)
                if temp.parent != dest.parent or not temp.name.startswith(_COPY_TEMP_PREFIX):
                    _evidence_failure('temporary path binding changed')
                if dest.exists():
                    if temp.exists():
                        ds, ts = dest.lstat(), temp.lstat()
                        if (ds.st_dev, ds.st_ino) != (ts.st_dev, ts.st_ino):
                            _evidence_failure('destination is not the owned published inode')
                        if _registered_identity(temp, allowed_links=(2,)) != receipt['identity']:
                            _evidence_failure('published temporary inode is unknown or modified')
                        _unlink(temp)
                    elif _registered_identity(dest) != receipt['identity']:
                        _evidence_failure('destination saved inode identity changed')
                elif temp.exists():
                    if _registered_identity(temp) != receipt['identity']:
                        _evidence_failure('temporary input changed')
                    _publish(temp, dest)
                    _unlink(temp)
                else:
                    _evidence_failure('publication identity evidence missing')
                if _registered_identity(dest) != receipt['identity']:
                    _evidence_failure('published identity changed')
                receipt['stage'] = 'copied'; write_json(journal_path, journal)
                continue
            if receipt['stage'] != 'declared':
                _evidence_failure('interrupted temporary copy needs explicit inspection')
            if dest.exists():
                _manifest_failure('unknown destination collision')
            source_identity = _identity(source)
            if (source_identity['sha256'] != entry['sha256'] or source_identity['mode'] != entry['mode']):
                _manifest_failure('source changed since manifest preflight')
            receipt.update(stage='copy_intent', source_identity=source_identity)
            write_json(journal_path, journal)
            with _parent(dest, create=True):
                pass
            temp = dest.with_name(_copy_temp_name())
            # Save name before exclusive creation; an interrupted creation with
            # no saved inode is deliberately not claimed on retry.
            receipt['temp'] = str(temp); write_json(journal_path, journal)
            with _parent(source) as source_parent:
                source_fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_parent)
            with os.fdopen(source_fd, 'rb') as inp:
                opened = os.fstat(inp.fileno())
                if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                        or (opened.st_dev, opened.st_ino, opened.st_size, stat.S_IMODE(opened.st_mode)) !=
                           (source_identity['device'], source_identity['inode'], source_identity['size'], source_identity['mode'])):
                    _manifest_failure('source identity changed before copy')
                with _parent(temp) as temp_parent:
                    temp_fd = os.open(temp.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, entry['mode'], dir_fd=temp_parent)
                with os.fdopen(temp_fd, 'wb') as out:
                    copied = 0
                    while chunk := inp.read(1024 * 1024):
                        copied += len(chunk)
                        if copied > source_identity['size']:
                            _manifest_failure('source grew during copy')
                        out.write(chunk)
                    out.flush(); os.fchmod(out.fileno(), entry['mode']); os.fsync(out.fileno())
            identity = _identity(temp)
            if (_identity(source) != source_identity or identity['sha256'] != entry['sha256']
                    or identity['size'] != source_identity['size']):
                _manifest_failure('source changed while copying')
            receipt.update(stage='published_intent', identity=identity)
            write_json(journal_path, journal)
            _destination(run, entry['destination'])
            _publish(temp, dest)
            _unlink(temp)
            if _registered_identity(dest) != identity:
                _evidence_failure('copy changed while publishing')
            receipt['stage'] = 'copied'; write_json(journal_path, journal)
        if _gate(run, directory) != before:
            _fail('candidate changed during preparation', stops.make('workspace_mismatch', 'candidate_changed'))
        validate_inputs(run, directory)
        return {'id': ident, 'status': 'prepared', 'files': len(value['files']),
                'manifest_sha256': run['workspace_inputs']['manifest_sha256']}

    return with_run_lock(files, prepare_locked, write=True)

def cleanup_inputs(run, directory, *, remove=False):
    """Caller holds run lock. Preview/delete only registered unchanged copies."""
    directory = Path(directory)
    _gate(run, directory, terminal=True)
    manifest, journal = _registered(run, directory)
    if manifest is None:
        return {'status': 'none', 'files': []}
    paths = []
    # Complete batch preflight before recording any deletion or unlinking.
    for entry, receipt in zip(manifest['files'], journal['files']):
        dest = _destination(run, entry['destination'])
        if _registered_identity(Path(run['repo']) / entry['source']) != receipt.get('source_identity'):
            _evidence_failure('source changed; retain input copies for explicit review')
        if not dest.exists() and receipt.get('stage') in ('delete_intent', 'removed'):
            paths.append(dest); continue
        if receipt.get('stage') not in ('copied', 'delete_intent') or _registered_identity(dest) != receipt.get('identity'):
            _evidence_failure('unknown or modified input retained: ' + str(dest))
        paths.append(dest)
    if remove:
        for path, receipt in zip(paths, journal['files']):
            if path.exists():
                receipt['stage'] = 'delete_intent'
                write_json(directory / 'workspace-inputs.json', journal)
                if _registered_identity(path) != receipt['identity']:
                    _evidence_failure('input changed before deletion')
                _unlink(path)
            receipt['stage'] = 'removed'
            write_json(directory / 'workspace-inputs.json', journal)
    return {'status': 'removed' if remove else 'eligible',
            'files': [str(path) for path in paths]}
