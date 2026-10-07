"""Verify unchanged installed files against a fresh source from this invocation.

Maven may elide an identical copy. Mere destination existence, or a successful
install log without both byte-bound files, never supplies this proof.
"""
from pathlib import PurePosixPath
import re

from .requirements import bound_file, repository_artifact_path


def validate_source(item):
    value = item.get('install_source_path')
    if value is None:
        return
    if not isinstance(value, str):
        raise ValueError('Installed continuity requires a reviewed checkout-relative source')
    path = PurePosixPath(value)
    if (not value or value == '.' or path.is_absolute() or '..' in path.parts or '.git' in path.parts
            or str(path) != value or any(c in value for c in '\r\n\0')
            or not item.get('repository_relative_path') or not item.get('install_source_basis')):
        raise ValueError('Installed continuity requires a reviewed checkout-relative source')


def _fingerprint_matches(ref, value):
    if isinstance(value, dict):
        value = [value.get(k) for k in ('sha256', 'mtime_ns', 'bytes', 'inode', 'device')]
    return (isinstance(value, list) and len(value) == 5
            and value[0] == ref.get('sha256') and value[2] == ref.get('bytes')
            and all(type(v) is int and v >= 0 for v in value[1:]))


def verified_copies(base, expected, invocation, matched):
    root = invocation.get('project_root')
    repository = (invocation.get('local_repository') or {}).get('path')
    if (not root or not repository or not PurePosixPath(root).is_absolute()
            or not PurePosixPath(repository).is_absolute()
            or invocation.get('runner') != 'maven' or invocation.get('status') != 'completed'
            or invocation.get('log_complete') is not True):
        return {}
    segments = [e['segment'] for e in matched if e['goal'] == 'install:install' and e['status'] == 'passed']
    proof = {}
    for item in expected:
        validate_source(item)
        source_path = item.get('install_source_path')
        if not source_path:
            continue
        target = repository_artifact_path(item)
        common = ('role', 'module', 'coordinates', 'extension', 'classifier', 'format')
        candidates = [a for a in invocation.get('artifacts', [])
                      if a.get('repository_relative_path') == target
                      and all(a.get(k) == item.get(k) for k in common)]
        sources = [a for a in invocation.get('artifacts', [])
                   if a.get('installation_source_for') == target
                   and a.get('producer_relative_path') == source_path
                   and all(a.get(k) == item.get(k) for k in common)]
        if len(candidates) != 1 or len(sources) != 1:
            continue
        destination, source = candidates[0], sources[0]
        dest_state, source_state = destination.get('freshness', {}), source.get('freshness', {})
        if (destination.get('fresh') is not False or source.get('fresh') is not True
                or not _fingerprint_matches(destination, dest_state.get('after'))
                or dest_state.get('before') != dest_state['after']
                or not _fingerprint_matches(source, source_state.get('after'))
                or source_state.get('before') == source_state['after']
                or (source.get('sha256'), source.get('bytes')) != (destination.get('sha256'), destination.get('bytes'))):
            continue
        origin = str(PurePosixPath(root) / source_path)
        installed = str(PurePosixPath(repository) / target)
        pattern = r'^\[INFO\]\s+Installing ' + re.escape(origin) + r' to ' + re.escape(installed) + r'\s*$'
        if sum(len(re.findall(pattern, text, re.M)) for text in segments) != 1:
            continue
        if bound_file(base, source).read_bytes() != bound_file(base, destination).read_bytes():
            continue
        proof[target] = {'sha256': destination['sha256'], 'source_path': source_path,
                         'evidence_refs': [source, destination],
                         'basis': 'native_install_completion_and_fresh_source_equal_unchanged_destination'}
    return proof
