"""Immutable, retrievable role-input projections; never command authority."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from . import stops
from .commands import command_id
from .context import prompt_json
from .state_files import StateFiles
from .util import HarnessError, dump, write_json

INLINE_CHARS = 4000


def _components(digest):
    return 'input-indexes', digest + '.json'


@stops.reads_evidence
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
        components = _components(digest)
        path = Path(directory).joinpath(*components)
        if ref['artifact'] != str(path):
            raise HarnessError('input index reference path changed')
        raw = StateFiles.nofollow_directory(directory).read(components, limit=None).evidence()
        try:
            value = json.loads(raw)
        except ValueError as error:
            raise HarnessError('input index missing or unreadable') from error
        if (hashlib.sha256(raw).hexdigest() != digest or not isinstance(value, dict)
                or set(value) != {'version', 'records'} or type(value['version']) is not int or value['version'] != 1
                or not isinstance(value['records'], list) or len(value['records']) != ref['count']):
            raise HarnessError('input index changed')


def _index(directory, records):
    value = {'version': 1, 'records': records}
    raw = (dump(value) + '\n').encode('utf-8')
    digest = hashlib.sha256(raw).hexdigest()
    components = _components(digest)
    path = Path(directory).joinpath(*components)
    with stops.evidence_reads():
        probe = StateFiles.nofollow_directory(directory).probe(components).or_local_io()
    if probe.state == 'absent':
        write_json(path, value)
    elif probe.state != 'present':
        probe.evidence()
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
        # How to read the index is shared protocol (RULES), not packet data.
        result[key + '_index'] = {'input_index': ref['artifact_digest'], 'count': len(history)}
    if not indexes:
        result.pop('input_evidence_indexes', None)
    return result


def project_review_keys(packet, directory, records):
    """Publish past reviews' mappings and dispositions as one digest-bound index.

    Call only with records a history traversal has checked; prior_reviews keeps
    them inline for its latest entry alone, and no runtime decision reads this.
    """
    if not records:
        return packet
    ref = _index(directory, records)
    indexes = {**packet.get('input_evidence_indexes', {}), ref['artifact_digest']: ref}
    # How to read the index is shared protocol (RULES), not packet data.
    return {**packet, 'input_evidence_indexes': indexes,
            'prior_review_keys_index': {'input_index': ref['artifact_digest'], 'count': len(records)}}


def _command(value):
    return (isinstance(value, dict) and set(value) == {'cwd', 'argv'} and isinstance(value['cwd'], str)
            and isinstance(value['argv'], list) and all(isinstance(x, str) for x in value['argv']))


def project_commands(packet, directory):
    """Deduplicate whole commands ({cwd, argv}) only in input views; grants and broker config stay exact."""
    result = deepcopy(packet)
    indexes = result.get('input_evidence_indexes', {})
    if not isinstance(indexes, dict) or any(not isinstance(ref, dict) or key != ref.get('artifact_digest') for key, ref in indexes.items()):
        raise HarnessError('invalid input index map')
    validate_indexes(directory, list(indexes.values()))
    occurrences = {}
    def visit(value):
        children = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
        for key, child in list(children):
            if _command(child):
                # Keyed by command ID, so key order never splits one command into two entries.
                occurrences.setdefault(command_id(child), []).append((value, key, child))
            else:
                visit(child)
    visit(result)
    records = []
    for identity, rows in occurrences.items():
        encoded = prompt_json(rows[0][2])
        replacement = {'exact_command_ref': identity}
        # Count both the catalog entry and every replacement before deciding.
        if len(encoded) <= INLINE_CHARS and len(encoded) * len(rows) <= len(prompt_json(replacement)) * len(rows) + len(encoded) + len(identity) + 60:
            continue
        records.append({'id': identity, 'command': rows[0][2]})
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
    # Include catalog costs; small payloads retain their old shape.
    return result if len(prompt_json(result)) < len(prompt_json(packet)) else deepcopy(packet)


@stops.reads_evidence
def validate_dispatch_indexes(directory, dispatches):
    """Read-only validation of every durable input projection on recovery."""
    for entry in dispatches:
        if 'input_evidence' in entry:
            validate_indexes(directory, entry['input_evidence'])
