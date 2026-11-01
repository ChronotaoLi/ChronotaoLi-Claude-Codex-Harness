"""Initialize Git ignore protection before private runtime writes."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import sys

from . import git
from .util import HarnessError


def _path(path, *, directory=False):
    path = Path(os.path.abspath(path))
    for parent in reversed(path.parents):
        system_alias = sys.platform == "darwin" and parent in (Path("/var"), Path("/tmp"), Path("/etc"))
        if parent.is_symlink() and not system_alias:
            raise HarnessError(f"私有状态路径不能包含符号链接：{parent}")
        if parent.exists() and not parent.is_dir():
            raise HarnessError(f"私有状态父路径必须为目录：{parent}")
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return path
    if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
        raise HarnessError(f"私有状态控制路径类型无效：{path}")
    return path


@contextmanager
def private_state_lock(repo):
    """A content-free common-Git lock, shared by all initializing writers."""
    repo = Path(repo)
    common = Path(git.git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir').removesuffix('\n'))
    _path(common, directory=True)
    path = _path(common / 'harness-private-state.lock')
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise HarnessError(f"私有状态锁必须为普通文件：{path}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield common
    finally:
        os.close(descriptor)


def _initialize(repo, state, common):
    repo = _path(repo, directory=True)
    metadata = repo / ".git"
    _path(metadata, directory=metadata.is_dir())
    root = _path(repo / '.harness', directory=True)
    state = _path(state if state is not None else root, directory=True)
    # A run checkout must never become a new private-state authority.
    for parent in repo.parents:
        if parent.name == '.harness':
            raise HarnessError('不能在 Harness 私有工作树内初始化私有状态')
    if state != root and state.is_relative_to(repo):
        raise HarnessError('仓库内私有状态必须使用根目录 .harness')
    tracked = git.git_bytes(repo, 'ls-files', '--cached', '-z', '--', '.harness')
    committed = git.git_process(repo, 'ls-tree', '-r', '--name-only', '-z', 'HEAD', '--', '.harness')
    if tracked or committed.returncode == 0 and committed.stdout:
        raise HarnessError('私有路径 .harness 已被跟踪或暂存；请人工处理，保留现场')
    control = _path(root / '.gitignore')
    previous = control.read_bytes() if control.exists() else None
    if previous is not None:
        rules = [line.strip() for line in previous.splitlines() if line.strip() and not line.lstrip().startswith(b'#')]
        if rules != [b'*']:
            raise HarnessError(f'私有忽略控制文件冲突：{control}；请人工将有效规则设置为单独一行 *，原字节已保留')
    info = _path(common / 'info', directory=True)
    exclude = _path(info / 'exclude')
    old = exclude.read_bytes() if exclude.exists() else b''
    root.mkdir(parents=True, exist_ok=True)
    if previous is None:
        with control.open('xb') as output:
            output.write(b'*\n')
    info.mkdir(exist_ok=True)
    if b'/.harness/' not in old.splitlines():
        with exclude.open('ab') as output:
            output.write((b'\n' if old and not old.endswith(b'\n') else b'') + b'/.harness/\n')
    probes = ['.harness/.gitignore', '.harness/private-probe', '.harness/AGENTS.md',
              '.harness/probe/', '.harness/probe/deep/AGENTS.md', '.harness/probe/deep/new.json']
    for probe in probes:
        if git.git_process(repo, 'check-ignore', '-q', '--', probe).returncode != 0:
            raise HarnessError(f'私有 Git 忽略规则核验失败：{probe}')
    visible = git.git_bytes(repo, 'ls-files', '--others', '--exclude-standard', '-z', '--', '.harness')
    if visible:
        raise HarnessError('已有私有文件仍可被 Git 看见；停止私有状态写入')
    state.mkdir(parents=True, exist_ok=True)
    return state


def ensure_private_state(repo, state=None):
    """Recheck protection on each writable entry; read-only callers must abstain."""
    with private_state_lock(repo) as common:
        return _initialize(repo, state, common)


def initialize_private_state_locked(repo, state, common):
    """For operation creation holding private_state_lock through O_EXCL."""
    return _initialize(repo, state, common)
