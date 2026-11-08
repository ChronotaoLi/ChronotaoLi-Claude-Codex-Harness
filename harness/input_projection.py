"""Immutable, retrievable role-input projections; never command authority."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .context import prompt_json
from .util import HarnessError, dump, write_json

INLINE_CHARS = 4000


def _path(directory, digest):
    path = Path(directory) / 'input-indexes' / (digest + '.json')
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise HarnessError('input index path is unsafe')
    return path


def validate_indexes(directory, references):
    if not isinstance(references, list):
        raise HarnessError('invalid input index references')
    for ref in references:
        if not isinstance(ref, dict) or set(ref) != {'artifact', 'artifact_digest', 'count'}:
            raise HarnessError('invalid input index reference')
        digest = ref['artifact_digest']
        if (not isinstance(digest, str) or len(digest) != 64
                or any(c not in '0123456789abcdef' for c in digest)
                or type(ref['count']) is not int or ref['count'] < 0):
            raise HarnessError('invalid input index identity')
        path = _path(directory, digest)
        if ref['artifact'] != str(path):
            raise HarnessError('input index reference path changed')
        if not path.is_file():
            raise HarnessError('input index must be a regular file')
        try:
            raw = path.read_bytes()
            value = json.loads(raw)
        except (OSError, ValueError) as error:
            raise HarnessError('input index missing or unreadable') from error
        if (hashlib.sha256(raw).hexdigest() != digest or not isinstance(value, dict)
                or set(value) != {'version', 'records'} or type(value['version']) is not int or value['version'] != 1
                or not isinstance(value['records'], list) or len(value['records']) != ref['count']):
            raise HarnessError('input index changed')


def _index(directory, records):
    value = {'version': 1, 'records': records}
    raw = (dump(value) + '\n').encode('utf-8')
    digest = hashlib.sha256(raw).hexdigest()
    path = _path(directory, digest)
    if not path.exists():
        write_json(path, value)
    ref = {'artifact': str(path), 'artifact_digest': digest, 'count': len(records)}
    validate_indexes(directory, [ref])
    return ref


def project_history(packet, directory):
    """Call only after source validation/applicability; inspection remains read-only."""
    result = deepcopy(packet)
    indexes = result.setdefault('input_evidence_indexes', {})
    if not isinstance(indexes, dict) or any(not isinstance(ref, dict) or key != ref.get('artifact_digest') for key, ref in indexes.items()):
        raise HarnessError('invalid input index map')
    validate_indexes(directory, list(indexes.values()))
    for key, active_key in (('continuation_history', 'pending_continuation'),
                            ('continuation_rejections', 'active_continuation_rejection'),
                            ('execution_guidance', None)):
        rows = result.get(key, [])
        if not isinstance(rows, list) or len(prompt_json(rows)) <= INLINE_CHARS:
            continue
        active = [r for r in rows if (r.get('id') == result.get(active_key) if active_key
                                      else r.get('applicability', {}).get('active', True))]
        history = [r for r in rows if r not in active]
        # Applicable guidance is never silently dropped: oversized active text
        # becomes a mandatory full read together with its applicability facts.
        if key == 'execution_guidance' and len(prompt_json(active)) > INLINE_CHARS:
            history, active = rows, []
        if not history:
            continue
        ref = _index(directory, history)
        indexes[ref['artifact_digest']] = ref
        result[key] = active
        result[key + '_index'] = {'input_index': ref['artifact_digest'], 'count': len(history),
            'read_instruction': ('Read the complete indexed execution guidance, including applicable constraints and source identities, before planning or execution; '
                'a successor dispatch does not waive historical constraints. Resolve applicability and conflicts explicitly.'
                if key == 'execution_guidance' else
                'Retrieve complete indexed historical records when assessing unfinished work or prior resolutions; active conditions remain inline.')}
    if not indexes:
        result.pop('input_evidence_indexes', None)
    return result


def project_commands(packet, directory):
    """Deduplicate argv only in input views; grants and broker config stay exact."""
    result = deepcopy(packet)
    indexes = result.get('input_evidence_indexes', {})
    if not isinstance(indexes, dict) or any(not isinstance(ref, dict) or key != ref.get('artifact_digest') for key, ref in indexes.items()):
        raise HarnessError('invalid input index map')
    validate_indexes(directory, list(indexes.values()))
    occurrences = {}
    def visit(value):
        if isinstance(value, dict):
            for key, child in list(value.items()):
                if key == 'argv' and isinstance(child, list) and all(isinstance(x, str) for x in child):
                    occurrences.setdefault(prompt_json(child), []).append((value, key, child))
                elif key in ('readonly_commands', 'worker_commands', 'verification_commands', 'commands', 'required_commands', 'self_checks', 'missing_commands') and isinstance(child, list):
                    for i, argv in enumerate(child):
                        if isinstance(argv, list) and all(isinstance(x, str) for x in argv):
                            occurrences.setdefault(prompt_json(argv), []).append((child, i, argv))
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(result)
    records = []
    for encoded, rows in occurrences.items():
        identity = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
        replacement = {'exact_command_ref': identity}
        # Count both the catalog entry and every replacement before deciding.
        if len(encoded) <= INLINE_CHARS and len(encoded) * len(rows) <= len(prompt_json(replacement)) * len(rows) + len(encoded) + len(identity) + 60:
            continue
        records.append({'id': identity, 'argv': rows[0][2]})
        for parent, key, _ in rows:
            parent[key] = replacement.copy()
    if not records:
        return result
    catalog = records
    if len(prompt_json(records)) > INLINE_CHARS:
        ref = _index(directory, records)
        result.setdefault('input_evidence_indexes', {})[ref['artifact_digest']] = ref
        catalog = {'input_index': ref['artifact_digest'], 'count': len(records)}
    result['exact_command_catalog'] = catalog
    result['exact_command_policy'] = ('Resolve exact_command_ref through the catalog, reading indexed argv in full before use. '
        'This is a role-input view only; current exact configuration is the sole command authority. '
        'References do not grant commands or allow argument changes.')
    # Include metadata and policy costs; small payloads retain their old shape.
    return result if len(prompt_json(result)) < len(prompt_json(packet)) else deepcopy(packet)


def validate_dispatch_indexes(directory, dispatches):
    """Read-only validation of every durable input projection on recovery."""
    for entry in dispatches:
        if 'input_evidence' in entry:
            validate_indexes(directory, entry['input_evidence'])
