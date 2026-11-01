"""Explicit one-shot Harness CLI supervision; never restart or infer model liveness."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from .notifications import NotificationStore
from .operations import OPERATION_ID
from .private_state import ensure_private_state, _path
from .util import HarnessError, uid, write_json


def _linked_run(state, operation_id):
    """An optional relationship, not proof that ledger progress matches exit."""
    path = state / 'operations' / (operation_id + '.json')
    try:
        _path(path)
        if path.stat().st_size > 1024 * 1024:
            return None
        value = json.loads(path.read_bytes())
        run_id = value.get('run_id') if value.get('operation_id') == operation_id else None
        if isinstance(run_id, str) and run_id.startswith('run_') and run_id[4:].isalnum():
            return run_id
    except (OSError, ValueError, AttributeError, HarnessError):
        pass
    return None


def supervise(repo, arguments, *, config_path=None, operation_id=None, popen=None):
    """Launch run/resume exactly once with inherited stdio and record real wait().

    Caller supplies subcommand arguments only; the executable and module are fixed.
    The resulting observation makes no assertion about broker-child exit.
    """
    arguments = list(arguments)
    if not arguments or arguments[0] not in ('run', 'resume') or any(not isinstance(v, str) for v in arguments):
        raise HarnessError('监督入口只接受 Harness run 或 resume 参数')
    if any(v == '--operation-id' or v.startswith('--operation-id=') for v in arguments):
        raise HarnessError('监督入口独占 operation ID；请使用监督入口的 operation_id 参数')
    operation_id = uid('op') if operation_id is None else operation_id
    if not isinstance(operation_id, str) or not OPERATION_ID.fullmatch(operation_id):
        raise HarnessError('无效 supervision operation ID')
    state = ensure_private_state(Path(repo))
    # Fail before launch if another invocation already owns the operation ID.
    operation_path = state / 'operations' / (operation_id + '.json')
    if operation_path.exists() or operation_path.is_symlink():
        raise HarnessError('监督 operation ID 已存在')
    supervision_id = uid('supervision')
    directory = _path(state / 'supervision', directory=True)
    directory.mkdir(exist_ok=True)
    path = _path(directory / (supervision_id + '.json'))
    claim = _path(directory / (operation_id + '.claim'))
    try:
        claim_fd = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError as error:
        raise HarnessError('监督 operation ID 已被监督实例认领') from error
    os.close(claim_fd)
    receipt = dict(schema_version=1, supervision_id=supervision_id, operation_id=operation_id,
                   run_id=None, source='harness_supervisor_wait', process_observation='launching',
                   observer_connection='supervisor_local', broker_processes='unknown',
                   started_at=time.time(), observed_at=None, pid=None, exit_code=None)
    # Exclusive ownership before any process can be launched.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
        json.dump(receipt, output)
        output.write('\n')
    command = [sys.executable, '-m', 'harness', '--repo', str(Path(repo).resolve())]
    if config_path is not None:
        command += ['--config', str(Path(config_path).absolute())]
    command += arguments + ['--operation-id', operation_id]
    child = None
    handlers = {}
    interrupted = None
    try:
        child = (popen or subprocess.Popen)(command, start_new_session=True)
        receipt.update(pid=child.pid, process_observation='running')
        write_json(path, receipt)
        if threading.current_thread() is threading.main_thread():
            def forward(signum, frame):
                nonlocal interrupted
                interrupted = signum
                try:
                    os.killpg(child.pid, signum)
                except ProcessLookupError:
                    pass
                if signum == signal.SIGINT:
                    raise KeyboardInterrupt
                raise SystemExit(128 + signum)
            for signum in (signal.SIGINT, signal.SIGTERM):
                handlers[signum] = signal.signal(signum, forward)
        receipt['exit_code'] = child.wait()
        receipt['process_observation'] = 'exited'
    except BaseException as error:
        if child is None and isinstance(error, OSError):
            receipt.update(process_observation='launch_failed', launch_error=type(error).__name__)
        elif child is not None:
            # On local cancellation, reap the supervised process group. This still
            # does not prove separately grouped broker children have terminated.
            try:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            except ProcessLookupError:
                child.wait()
            receipt.update(exit_code=child.returncode, process_observation='exited')
            raise
        else:
            raise
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        receipt.update(run_id=_linked_run(state, operation_id), observed_at=time.time(),
                       forwarded_signal=interrupted)
        write_json(path, receipt)
        with NotificationStore(repo) as store:
            receipt['notification_id'] = store.add_process_observation(receipt)
        write_json(path, receipt)
    return receipt
