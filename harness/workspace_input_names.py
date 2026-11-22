"""Read-only target layout checks before freezing a workspace input manifest."""
from __future__ import annotations

from collections import deque
from contextlib import ExitStack, contextmanager
import ctypes
import errno
import fcntl
from functools import cache
from itertools import islice
import os
import stat
import sys
import unicodedata

from .util import HarnessError

NAME_ERRORS = (errno.ENOTDIR, errno.EINVAL, errno.EILSEQ, errno.ENAMETOOLONG)
_UNSUPPORTED = (errno.EINVAL, errno.ENOTTY, errno.ENOSYS, errno.EOPNOTSUPP)
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_PROBE_LIMIT = 64
_PROBE_RECORD_LIMIT = 256
_PROBE_HEADER = b'Harness input name observation v1\n'


class _DarwinStatfs(ctypes.Structure):
    # XNU sys/mount.h, __DARWIN_STRUCT_STATFS64.
    _fields_ = [('block_size', ctypes.c_uint32), ('io_size', ctypes.c_int32),
                ('counts', ctypes.c_uint64 * 5), ('fsid', ctypes.c_int32 * 2),
                ('volume_info', ctypes.c_uint32 * 4), ('type_name', ctypes.c_char * 16),
                ('mount_names', ctypes.c_char * 2048), ('flags_ext', ctypes.c_uint32 * 8)]


def _name_rules(fd):
    """Return only rules we can establish for this particular directory."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        fs = _DarwinStatfs()
        fstatfs = getattr(libc, 'fstatfs$INODE64', libc.fstatfs)
        if fstatfs(fd, ctypes.byref(fs)) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        if fs.type_name == b'apfs':
            # XNU sys/unistd.h, _PC_CASE_SENSITIVE.
            ctypes.set_errno(0)
            sensitive = libc.fpathconf(fd, 11)
            if sensitive in (0, 1):
                return 'apfs_sensitive' if sensitive else 'apfs_insensitive'
            code = ctypes.get_errno()
            if code and code not in _UNSUPPORTED:
                raise OSError(code, os.strerror(code))
    elif (sys.platform == 'linux' and ctypes.sizeof(ctypes.c_long) == 8
          and os.uname().machine in ('x86_64', 'aarch64')):
        # These Linux ABIs start struct statfs with a long f_type. Reserve
        # space for the full struct, and only consume the filesystem magic.
        fs = (ctypes.c_long * 32)()
        if libc.fstatfs(fd, ctypes.byref(fs)) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        if fs[0] in (0xEF53, 0x01021994):  # ext family and tmpfs
            # FS_IOC_GETFLAGS, _IOR('f', 1, long), and FS_CASEFOLD_FL.
            try:
                flags = fcntl.ioctl(fd, 0x80086601, bytes(8))
            except OSError as error:
                if error.errno not in _UNSUPPORTED:
                    raise
            else:
                if not int.from_bytes(flags, sys.byteorder) & 0x40000000:
                    return 'byte_exact'
    return 'unobserved'


def _fail(message):
    raise HarnessError('workspace inputs: ' + message)


def _lookup(name, parent_fd=None):
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except NotADirectoryError:
        _fail(f'destination parent is not a directory: {str(name)!r}')
    except OSError as error:
        if error.errno not in NAME_ERRORS:
            raise
        _invalid_name(str(name), error.strerror)


def _invalid_name(name, reason):
    _fail(f'invalid destination on this volume: {name!r} ({reason})')


@cache
def _darwin_casefold():
    # The OS Unicode tables can know pairs that Python's tables predate.
    # This is only a source of possible equivalences on unobserved layouts.
    fold = ctypes.CDLL('/usr/lib/libicucore.A.dylib').u_strFoldCase
    fold.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p,
                     ctypes.c_int32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_int)]
    return fold


def _possible_key(name, rules):
    # A manifest can spell the same bytes as Unicode or as surrogate escapes.
    normalized = unicodedata.normalize('NFD', os.fsdecode(os.fsencode(name)))
    if rules == 'apfs_sensitive':
        return normalized
    folded = normalized.casefold()
    if sys.platform == 'darwin' and not folded.isascii():
        native = 'utf-16-le' if sys.byteorder == 'little' else 'utf-16-be'
        raw = folded.encode(native, 'surrogatepass')
        output = ctypes.create_string_buffer(4 * len(raw))
        error = ctypes.c_int()
        length = _darwin_casefold()(output, len(output) // 2, raw, len(raw) // 2, 0,
                                    ctypes.byref(error))
        if error.value > 0:
            raise OSError(errno.EIO, f'cannot compare Unicode destination name {name!r}')
        folded = output.raw[:2 * length].decode(native, 'surrogatepass')
    return unicodedata.normalize('NFD', folded)


@contextmanager
def _open_directory(path, parent_fd=None):
    fd = os.open(path, _DIRECTORY, dir_fd=parent_fd)
    try:
        yield fd
    finally:
        os.close(fd)


def _descriptor_identity(fd):
    info = os.fstat(fd)
    return {'device': info.st_dev, 'inode': info.st_ino}


def _private_cache_fd(stack, root_fd, device, rules):
    name = f'input-names-{device}-{rules}'
    try:
        os.mkdir(name, mode=0o700, dir_fd=root_fd)
    except FileExistsError:
        pass
    fd = stack.enter_context(_open_directory(name, root_fd))
    info = os.fstat(fd)
    if (info.st_dev != device or _name_rules(fd) != rules
            or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid()):
        return None
    # Independent runs share this directory. Capacity checks and exclusive
    # creations are serialized, with contention treated as unavailable.
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


@contextmanager
def _scratch_for(private_root, device, rules):
    with ExitStack() as stack:
        scratch = None
        try:
            root_fd = stack.enter_context(_open_directory(private_root))
            if os.fstat(root_fd).st_dev == device and _name_rules(root_fd) == rules:
                scratch = _private_cache_fd(stack, root_fd, device, rules)
        except OSError:
            pass
        yield scratch


def _probe_record(fd):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or not 0 < info.st_size <= _PROBE_RECORD_LIMIT):
        return None
    record = os.read(fd, _PROBE_RECORD_LIMIT + 1)
    if (len(record) != info.st_size or not record.startswith(_PROBE_HEADER + b'run_')
            or not record.endswith(b'\n')):
        return None
    context = os.fsdecode(record[len(_PROBE_HEADER) + len(b'run_'):-1])
    return info if context.isalnum() else None


def _same_probe(info, named):
    return (os.path.samestat(info, named) and info.st_mode == named.st_mode
            and info.st_nlink == named.st_nlink and info.st_size == named.st_size
            and info.st_ctime_ns == named.st_ctime_ns)


def _open_probe(name, scratch, remaining):
    flags = os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        return os.open(name, flags | os.O_RDONLY, dir_fd=scratch), False
    except FileNotFoundError:
        if remaining <= 0:
            return None, False
    try:
        return os.open(name, flags | os.O_RDWR | os.O_CREAT | os.O_EXCL,
                       0o600, dir_fd=scratch), True
    except OSError as error:
        if error.errno in NAME_ERRORS:
            _invalid_name(name, error.strerror)
        raise


def _private_keys(names, scratch, run_id):
    """Observe APFS names with bounded retained records and descriptor checks."""
    record = _PROBE_HEADER + os.fsencode(run_id) + b'\n'
    if len(record) > _PROBE_RECORD_LIMIT:
        return None
    records = {}
    try:
        with os.scandir(scratch) as entries:
            remaining = _PROBE_LIMIT - sum(1 for _ in islice(entries, _PROBE_LIMIT))
        for name in names:
            fd, created = _open_probe(name, scratch, remaining)
            if fd is None:
                return None
            try:
                if created:
                    remaining -= 1
                    if os.write(fd, record) != len(record):
                        return None
                    os.lseek(fd, 0, os.SEEK_SET)
                info = _probe_record(fd)
                if info is None:
                    return None
                records[name] = info
            finally:
                os.close(fd)
        for name, info in records.items():
            named = os.stat(name, dir_fd=scratch, follow_symlinks=False)
            if not _same_probe(info, named):
                return None
    except OSError:
        return None
    return {name: info.st_ino for name, info in records.items()}


def _name_length(name, rules):
    raw = os.fsencode(name)
    if rules == 'byte_exact':
        return len(raw)
    if rules.startswith('apfs_'):
        try:
            return len(raw.decode('utf-8').encode('utf-16-le')) // 2
        except UnicodeDecodeError:
            _invalid_name(name, 'APFS requires UTF-8')
    # The unit of NAME_MAX can vary on unobserved filesystems.
    return None


def _check_lengths(names, rules, name_max):
    for name in names:
        length = _name_length(name, rules)
        if length is not None and name_max >= 0 and length > name_max:
            _invalid_name(name, 'name too long')


def _name_max(fd):
    try:
        return os.fpathconf(fd, 'PC_NAME_MAX')
    except OSError as error:
        if error.errno not in _UNSUPPORTED:
            raise
        return -1


def _children(entries):
    children = {}
    for dest, (name, *tail) in entries:
        children.setdefault(name, []).append((dest, tail))
    return children


def _conflict(first, second, *, possible):
    pair = f'{str(first)!r}, {str(second)!r}'
    if possible:
        _fail(f'destination layout may conflict; directory naming semantics are unobserved: {pair}')
    _fail(f'destination layout conflicts on this volume: {pair}')


def _missing_groups(children, rules, scratch, run_id, *, possible):
    """Return groups of missing siblings, checking deeper virtual directories."""
    pending = [children]
    first_level = None
    observed, unobserved = False, False
    while pending:
        siblings = pending.pop()
        keys = None
        unicode_names = any(not name.isascii() for name in siblings)
        if rules == 'byte_exact':
            keys = {name: os.fsencode(name) for name in siblings}
        elif scratch is not None and unicode_names:
            # Even one non-ASCII name can fail creation after a successful lookup.
            keys = _private_keys(siblings, scratch, run_id)
            observed |= keys is not None
        approximate = rules == 'unobserved' or (unicode_names and keys is None)
        unobserved |= approximate
        if keys is None:
            keys = {name: _possible_key(name, rules) for name in siblings}
        groups, unicode_keys = {}, set()
        for name, entries in siblings.items():
            key = keys[name]
            groups.setdefault(key, []).extend(entries)
            if not name.isascii():
                unicode_keys.add(key)
        if first_level is None:
            first_level = groups
        for key, members in groups.items():
            descendants = [(dest, tail) for dest, tail in members if tail]
            if len(descendants) < len(members) and len(members) > 1:
                leaf = next(dest for dest, tail in members if not tail)
                other = next((dest for dest, _ in members if dest != leaf), leaf)
                uncertain = approximate and (rules == 'unobserved' or key in unicode_keys)
                _conflict(leaf, other, possible=possible or uncertain)
            if descendants:
                pending.append(_children(descendants))
    return first_level, observed, unobserved


def _existing_children(parent, fd, entries, *, possible):
    actual, missing = {}, {}
    for name, members in _children(entries).items():
        info = _lookup(name, fd)
        if info is None:
            missing[name] = members
            continue
        child = parent / name
        for dest, tail in members:
            if not tail or not stat.S_ISDIR(info.st_mode):
                _conflict(dest, child, possible=possible)
        _, paths = actual.setdefault((info.st_dev, info.st_ino), (child, []))
        paths.extend(members)
    return actual, missing


def _possible_existing_dirs(parent, fd, missing_groups, actual):
    for name in os.listdir(fd):
        members = missing_groups.get(_possible_key(name, 'unobserved'))
        if not members:
            continue
        child = parent / name
        info = _lookup(name, fd)
        if info is None:
            continue
        for dest, tail in members:
            if not tail or not stat.S_ISDIR(info.st_mode):
                _conflict(dest, child, possible=True)
        _, direct = actual.get((info.st_dev, info.st_ino), (child, []))
        # A missing directory spelling might reuse this existing directory.
        # Verify that its children and any direct candidates can coexist there.
        yield child, direct + members, True


def check_destination_layout(destinations, checkout, private_root, copy_temp_name, *, run_id):
    """Validate a fresh batch without creating any object in a worktree."""
    entries = []
    for dest in destinations:
        if _lookup(dest) is not None:
            _fail(f'unknown destination already exists: {dest}')
        # Recovery and identity checks also use the absolute temporary path.
        _lookup(dest.with_name(copy_temp_name))
        entries.append((dest, dest.relative_to(checkout).parts))

    evidence, scratches = [], {}
    pending = deque([(checkout, entries, False)])
    with ExitStack() as stack:
        while pending:
            parent, paths, possible = pending.popleft()
            with _open_directory(parent) as fd:
                actual, missing = _existing_children(parent, fd, paths, possible=possible)
                pending.extend((child, direct, possible) for child, direct in actual.values())
                if not missing:
                    continue
                rules, name_max = _name_rules(fd), _name_max(fd)
                to_publish = [(dest, (name, *tail))
                              for name, members in missing.items() for dest, tail in members]
                target = _descriptor_identity(fd)
                scratch = None
                names = [name for _, parts in to_publish for name in parts]
                if not possible:
                    _check_lengths((*names, copy_temp_name), rules, name_max)
                    if rules.startswith('apfs_') and any(not name.isascii() for name in names):
                        key = (target['device'], rules)
                        if key not in scratches:
                            scratches[key] = stack.enter_context(_scratch_for(private_root, *key))
                        scratch = scratches[key]
                groups, observed, unobserved = _missing_groups(
                    missing, rules, scratch, run_id, possible=possible)
                if rules == 'unobserved':
                    pending.extend(_possible_existing_dirs(parent, fd, groups, actual))
                if not possible:
                    comparison = {'byte_exact': 'target_byte_exact', 'unobserved': 'unobserved'}.get(
                        rules, 'target_apfs')
                    if unobserved:
                        comparison = 'unobserved'
                    elif observed:
                        comparison = 'private_apfs_volume'
                    row = {'parent': parent.relative_to(checkout).as_posix(),
                           'destinations': [dest.relative_to(checkout).as_posix()
                                            for dest, _ in to_publish],
                           'target_directory': target, 'target_rule': rules, 'comparison': comparison}
                    if observed:
                        row['private_observation'] = {
                            'directory': _descriptor_identity(scratch), 'rule': rules,
                            'retention': 'bounded',
                        }
                    evidence.append(row)
    return evidence
