"""Versioned behavioral grading contracts and qualification receipts."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath

VERSION = 2
MARKER = 'BENCHMARK_ASSERTION:'
SPEC_FIELDS = ('repo_id', 'base_commit', 'reference_commit', 'setup_command', 'task_definition', 'prompt',
               'acceptance_command', 'test_patch', 'grading_contract', 'grading_probe')
WARNING = 'This grader needs behavioral-contract review and implementation-independence controls before another execution. Saved results are historical, not revalidated.'


def fingerprint(task):
    values = {key: task.get(key, '') for key in SPEC_FIELDS}
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _text(value, label, limit=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'Grading contract requires {label}')
    return value


def probe_paths(patch):
    _text(patch, 'an equivalent-implementation patch', 250000)
    before = re.findall(r'^--- a/(.+)$', patch, re.M)
    after = re.findall(r'^\+\+\+ b/(.+)$', patch, re.M)
    if not before or before != after or '/dev/null' in patch:
        raise ValueError('The equivalence probe must modify existing implementation files, not add, remove, or rename files')
    for name in after:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or '.git' in path.parts or '\\' in name or '"' in name:
            raise ValueError('Unsafe equivalence probe path')
        if any(part.lower() in {'test', 'tests', '__tests__'} for part in path.parts) or any(word in path.name.lower() for word in ('test', 'spec', 'benchmark')):
            raise ValueError('The equivalence probe must not edit tests or grader configuration')
    for prefix in ('+', '-'):
        lines = [line[1:].strip() for line in patch.splitlines() if line.startswith(prefix) and not line.startswith(prefix * 3)]
        if not any(line and not line.startswith(('#', '//', '/*', '*')) for line in lines):
            raise ValueError('The equivalence probe must change implementation code, not just comments')
    return after


def test_paths(patch):
    """Accept only new, regular benchmark test files, including manually edited specs."""
    _text(patch, 'an additive hidden-test patch', 250000)
    lines = patch.splitlines()
    paths = []
    i = 0
    while i < len(lines):
        while i < len(lines) and (lines[i].startswith(('diff --git ', 'index ')) or lines[i] == 'new file mode 100644'):
            i += 1
        if i + 2 >= len(lines) or lines[i] != '--- /dev/null' or not lines[i + 1].startswith('+++ b/'):
            raise ValueError('Hidden tests must only add new benchmark-prefixed files; production edits, deletions, and renames are forbidden')
        name = lines[i + 1][6:]
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or '.git' in path.parts or '\\' in name or '"' in name or not path.name.startswith(('benchmark_', 'benchmark-')):
            raise ValueError('Hidden tests require safe, benchmark-prefixed filenames')
        if name in paths or len(paths) >= 10:
            raise ValueError('Hidden tests require 1–10 unique new files')
        hunk = re.fullmatch(r'@@ -0(?:,0)? \+1(?:,(\d+))? @@', lines[i + 2])
        if not hunk:
            raise ValueError('Hidden test patches must contain complete new-file additions')
        count = int(hunk.group(1) or 1)
        added = lines[i + 3:i + 3 + count]
        if not count or len(added) != count or any(not line.startswith('+') for line in added):
            raise ValueError('Hidden test patch contains non-additive content')
        paths.append(name)
        i += 3 + count
    return paths


def validate_spec(task):
    contract = task.get('grading_contract')
    if not isinstance(contract, dict) or contract.get('version') != VERSION:
        raise ValueError('Grader requires a versioned behavioral scoring contract; prepare it again')
    criteria = contract.get('criteria')
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 12:
        raise ValueError('Grading contract requires 1–12 behavioral criteria')
    ids = set()
    if MARKER in task.get('acceptance_command', ''):
        raise ValueError('Acceptance commands must not manufacture assertion markers')
    source = task.get('test_patch', '')
    test_paths(source)
    for criterion in criteria:
        if not isinstance(criterion, dict):
            raise ValueError('Invalid behavioral criterion')
        key = criterion.get('id', '')
        if not isinstance(key, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,59}', key) or key in ids:
            raise ValueError('Behavioral criteria need unique, stable IDs')
        ids.add(key)
        requirement = _text(criterion.get('requirement'), f'{key}: actor-visible behavior')
        _text(criterion.get('check'), f'{key}: the test that checks it', 500)
        if requirement not in task.get('prompt', ''):
            raise ValueError(f'Hidden requirement {key} is absent from the actor prompt')
        if MARKER + key not in source:
            raise ValueError(f'Check {key} needs its own {MARKER}{key} assertion failure marker')
    interfaces = contract.get('interfaces')
    if not isinstance(interfaces, list) or not 1 <= len(interfaces) <= 4:
        raise ValueError('Grading contract requires 1–4 existing public interfaces')
    for interface in interfaces:
        if not isinstance(interface, dict):
            raise ValueError('Invalid grading interface')
        _text(interface.get('path'), 'interface source path', 300)
        _text(interface.get('symbol'), 'interface symbol', 300)
        _text(interface.get('description'), 'observable interface behavior')
        if not isinstance(interface.get('required', False), bool):
            raise ValueError('interface.required must be boolean')
    _text(contract.get('implementation_freedom'), 'implementation freedom')
    probe = task.get('grading_probe')
    if not isinstance(probe, dict):
        raise ValueError('Grader needs an equivalent implementation control')
    _text(probe.get('description'), 'why the alternative preserves required behavior and challenges the grader')
    probe_paths(probe.get('patch'))
    return contract


def actor_prompt(prompt, contract):
    lines = ['\n\nScoring contract (behavior, not a prescribed implementation):']
    lines.extend('- ' + item['requirement'] for item in contract['criteria'])
    lines.append('Observable interfaces:')
    lines.extend(('- Required new API: ' if item.get('required') else '- ') + item['description'] for item in contract['interfaces'])
    lines.append('Implementation freedom: ' + contract['implementation_freedom'])
    return prompt.strip() + '\n'.join(lines)


def review_receipt(task, review, model):
    validate_spec(task)
    if not isinstance(review, dict):
        raise ValueError('Grader reviewer returned no structured assessment')
    flags = ('approved', 'contract_complete', 'implementation_independent', 'probe_preserves_behavior', 'probe_challenges_assumptions', 'model_free')
    coverage = review.get('criteria', [])
    expected = {item['id'] for item in task['grading_contract']['criteria']}
    actual = {item.get('id') for item in coverage if isinstance(item, dict) and item.get('covered') is True and item.get('evidence')}
    if any(review.get(flag) is not True for flag in flags) or review.get('issues') != [] or actual != expected or len(coverage) != len(expected):
        issues = review.get('issues') or ['Missing coverage, undocumented implementation dependency, or an invalid/weak equivalence probe']
        raise ValueError('Grader review rejected: ' + '; '.join(str(issue) for issue in issues)[:3000])
    return {**review, 'version':VERSION, 'spec_sha256':fingerprint(task), 'model':model}


def require_review(task):
    validate_spec(task)
    receipt = task.get('grading_review') or {}
    if receipt.get('version') != VERSION or receipt.get('spec_sha256') != fingerprint(task) or receipt.get('approved') is not True:
        raise ValueError('Grader is missing a current independent review; prepare it again')


def qualified(task):
    try:
        require_review(task)
    except (ValueError, TypeError, KeyError):
        return False
    receipt = task.get('grading_qualification') or {}
    controls = task.get('controls') or {}
    return (receipt.get('version') == VERSION and receipt.get('spec_sha256') == fingerprint(task)
            and (controls.get('base') or {}).get('passed') is False
            and (controls.get('reference') or {}).get('passed') is True
            and (controls.get('equivalent') or {}).get('passed') is True)


def warning(tasks):
    return WARNING if any(not qualified(task) for task in tasks) else ''
