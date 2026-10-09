"""Descriptive comparisons from recorded runs, without inference."""
import json
import re
from collections import Counter
from statistics import median
from urllib.parse import quote

from .ui import delta, duration, e, number, verdict


def delegation(run):
    counts = Counter()
    known = False
    roots = {a.get('id') for a in run.get('actors') or [] if not a.get('parent_id')}
    for ev in run.get('events') or []:
        if ev.get('kind') != 'tool' or ev.get('tool_name') != 'subagent':
            continue
        if roots and ev.get('actor_id') not in roots:
            continue
        try:
            raw = str(ev.get('detail') or '').split('\nresult:', 1)[0].removeprefix('input:').strip()
            match = re.search(r'"slug"\s*:\s*("(?:[^"\\]|\\.)*")', raw)
            slug = json.loads(match.group(1)) if match else None
            if slug:
                known = True
                counts[slug] += 1
        except (ValueError, TypeError, AttributeError):
            pass
    if not known and run.get('subagents') not in (0, None):
        return None
    return sum(max(0, n - 1) for n in counts.values()) if known or run.get('subagents') == 0 else None


def parent_calls(run):
    roots = [a for a in run.get('actors') or [] if not a.get('parent_id')]
    values = [a.get('llm_calls') for a in roots]
    return sum(values) if values and all(isinstance(v, (int, float)) for v in values) else None


def helper_calls(run):
    own = parent_calls(run)
    return run['llm_calls'] - own if own is not None and run.get('llm_calls') is not None else None


def pair_key(run):
    return (run.get('task_id'), run.get('model'), run.get('comparison_attempt', run.get('attempt')))


def anchor(task, model, attempt):
    import hashlib
    return 'attempt-' + hashlib.sha256(f'{task}:{model}:{attempt}'.encode()).hexdigest()[:12]


def observed_findings(groups, medians):
    labels = list(groups)
    n = len(groups[labels[0]]) if labels else 0
    if not n:
        return '<p>No matched completed results yet.</p>'
    passed = {label: sum(r.get('passed') is True for r in group) for label, group in groups.items()}
    outcome = 'Both versions passed every matched attempt.' if len(labels) == 2 and all(v == n for v in passed.values()) else '; '.join(
        f'{label} passed {value}/{n}' for label, value in passed.items()) + '.'
    behaviour, links = [], []
    for label, group in groups.items():
        threads = [(r, a) for r in group for a in r.get('artifacts') or []
                   if str(a.get('name', '')).startswith('agent/threads/') and str(a['name']).endswith('.md')]
        if threads:
            used = len({r['id'] for r, _ in threads})
            if n == 1 and group[0].get('subagents') is not None:
                count = group[0]['subagents']
                behaviour.append(f'{label} used a shared thread with {count} subagent{"s" if count != 1 else ""}.')
            else:
                behaviour.append(f'{label} saved a shared thread in {used}/{len(group)} runs.')
            for run, artifact in threads:
                href = '/artifacts/' + quote(run['id'], safe='') + '/' + quote(artifact['name'], safe='/')
                text = f'{label} · {run.get("model")} · attempt {run.get("attempt")} · {artifact["name"].split("/")[-1]}'
                links.append(f'<a href="{e(href)}">{e(text)}</a>')
    def theme(title, rating, text):
        return f'<article class="finding-theme"><h3>{e(title)} <span class="assessment a-{rating.lower()}">{e(rating)}</span></h3><p>{e(text)}</p></article>'

    def judgement(changes):
        if not changes:
            return 'Unclear'
        if all(c == 0 for c in changes):
            return 'Unchanged'
        if all(c <= 0 for c in changes):
            return 'Good'
        if all(c >= 0 for c in changes):
            return 'Bad'
        return 'Mixed'

    def metric_sentences(specs, before, after):
        phrases, changes = [], []
        for metric, noun, fmt in specs:
            values = medians[metric]
            if values[before] is None or values[after] is None:
                continue
            change = values[after] - values[before]
            changes.append(change)
            if change:
                if metric == 'Actor time':
                    phrases.append(f'{fmt(abs(change))} {"lower" if change < 0 else "higher"} actor time')
                else:
                    phrases.append(f'{fmt(abs(change))} {"fewer" if change < 0 else "more"} {noun}')
        return phrases, changes

    prose = '<div class="finding-themes">'
    if len(labels) == 2:
        before, after = labels
        correctness = 'Unchanged' if passed[after] == passed[before] else 'Good' if passed[after] > passed[before] else 'Bad'
        prose += theme('Task outcome', correctness, outcome)
        elapsed = lambda v: f'{v:.1f}s' if v < 1 else duration(v)
        counts = lambda v: f'{v:g}'
        efficiency, changes = metric_sentences([
            ('Actor time', 'actor time', elapsed), ('Parent LLM calls', 'parent calls', counts)
        ], before, after)
        detail = after + ' had ' + ', '.join(efficiency) + f' than {before}.' if efficiency else 'No measured difference.' if changes else 'Time and parent-call measurements are unavailable.'
        prose += theme('Execution efficiency', judgement(changes), detail)
        repeat, repeat_changes = metric_sentences([
            ('Repeat messages to a subagent', 'repeat subagent messages', counts)
        ], before, after)
        coordination = ' '.join(behaviour)
        if repeat:
            coordination += (' ' if coordination else '') + after + ' made ' + ', '.join(repeat) + f' than {before}.'
        if coordination:
            has_new_thread = any(a for r in groups[after] for a in r.get('artifacts') or [] if str(a.get('name','')).startswith('agent/threads/') and str(a['name']).endswith('.md')) and not any(a for r in groups[before] for a in r.get('artifacts') or [] if str(a.get('name','')).startswith('agent/threads/') and str(a['name']).endswith('.md'))
            rating = 'Good' if has_new_thread and all(c <= 0 for c in repeat_changes) else judgement(repeat_changes)
            coordination += ' Shared records improve traceability; fewer subagent messages do not necessarily mean less coordination.' if has_new_thread else 'This counts calls to the subagent tool, not every coordination exchange.'
            prose += theme('Delegation and traceability', rating, coordination)
        token_phrases, token_changes = metric_sentences([
            ('Output tokens', 'output tokens', number), ('Cache-read tokens', 'cache-read tokens', number)
        ], before, after)
        if token_changes:
            token_detail = after + ' used ' + ', '.join(token_phrases) + f' than {before}.' if token_phrases else 'Token measurements were unchanged.'
            prose += theme('Token use', judgement(token_changes), token_detail + ' Monetary cost is not reported.')
        errors, error_changes = metric_sentences([('Tool errors', 'tool errors', counts)], before, after)
        if error_changes:
            detail = after + ' had ' + ', '.join(errors) + f' than {before}.' if errors else 'The tool-error count was unchanged.'
            prose += theme('Tool reliability', judgement(error_changes), detail + ' Tool errors are not necessarily defects in the submitted code.')
    else:
        prose += theme('Task outcome', 'Unclear', outcome)
        for text in behaviour:
            prose += theme('Delegation', 'Unclear', text)
    prose += '</div>'
    models = sorted({str(r.get('model')) for group in groups.values() for r in group})
    count = 'One matched attempt' if n == 1 else f'{n} matched attempts'
    prose += f'<p class="note">{e(count)} per version · {e(", ".join(models))}. Descriptive results, not a proven effect.</p>'
    if links:
        prose += '<div class="thread-evidence"><span>Shared threads</span>' + ''.join(links) + '</div>'
    return prose


def comparison(runs, versions):
    labels = [v['label'] for v in versions]
    complete = {label: {pair_key(r) for r in runs if r.get('version') == label and r.get('status') == 'complete'} for label in labels}
    matched = set.intersection(*complete.values()) if complete else set()
    groups = {label: [r for r in runs if r.get('version') == label and pair_key(r) in matched and r.get('status') == 'complete'] for label in labels}
    metrics = [
        ('Actor time', lambda r: r.get('wall_seconds'), duration),
        ('Parent LLM calls', parent_calls, number),
        ('Subagent LLM calls', helper_calls, number),
        ('Repeat messages to a subagent', delegation, number),
        ('Input tokens', lambda r: r.get('input_tokens'), number),
        ('Output tokens', lambda r: r.get('output_tokens'), number),
        ('Cache-read tokens', lambda r: r.get('cache_read_tokens'), number),
        ('Cache-write tokens', lambda r: r.get('cache_write_tokens'), number),
        ('Tool errors', lambda r: r.get('tool_errors'), number),
    ]
    heads = ''.join(f'<th class="num">{e(label)}<small>n={len(groups[label])}</small></th>' for label in labels)
    change = '<th class="num">Change<small>second minus first</small></th>' if len(labels) == 2 else ''
    passes = {label: sum(r.get('passed') is True for r in groups[label]) for label in labels}
    cells = ''.join(f'<td class="num">{passes[label]} / {len(groups[label])}</td>' for label in labels)
    rows = f'<tr><th>Passed</th>{cells}' + (f'<td class="num">{e(delta(passes[labels[0]], passes[labels[1]], number))}</td>' if len(labels) == 2 else '') + '</tr>'
    medians = {}
    for title, read, fmt in metrics:
        mids, cells = {}, ''
        for label in labels:
            values = [read(r) for r in groups[label]]
            values = [v for v in values if isinstance(v, (int, float))]
            mids[label] = median(values) if values and len(values) == len(groups[label]) else None
            value = fmt(mids[label]) if mids[label] is not None else 'Not recorded'
            spread = f'{fmt(min(values))}–{fmt(max(values))}' if values and mids[label] is not None else ''
            cells += f'<td class="num"><strong>{e(value)}</strong><small>{e(spread)}</small></td>'
        if len(labels) == 2:
            cells += f'<td class="num">{e(delta(mids[labels[0]], mids[labels[1]], fmt))}</td>'
        rows += f'<tr><th>{e(title)}</th>{cells}</tr>'
        medians[title] = mids
    all_complete = [r for r in runs if r.get('status') == 'complete']
    waiting = len(runs) - len(all_complete)

    evidence = 'Repeated subagent calls count follow-ups to the same slug, not necessarily messages sent while it was busy.'
    table = f'<section><h2>Comparison</h2><div class="table-scroll"><table class="comparison"><thead><tr><th>Measure<small>median · min–max</small></th>{heads}{change}</tr></thead><tbody>{rows}</tbody></table></div><p class="note">Matched task/model/attempt combinations only. Actor time excludes setup and grading. Tokens are separated by billing category; provider cost is not available here.</p><p class="note">{e(evidence)}</p></section>'
    excluded = f'<p class="note">{waiting} unfinished or error run(s) excluded.</p>' if waiting else ''
    decision = f'<section class="decision"><h2>What differed</h2>{observed_findings(groups, medians)}{excluded}</section>'
    return decision, table


def run_cell(run):
    repeat = delegation(run)
    return f'<a class="run-result" href="/runs/{e(run["id"])}">{verdict(run)}</a><dl class="run-metrics"><dt>Time</dt><dd>{e(duration(run.get("wall_seconds")))}</dd><dt>Parent / helper calls</dt><dd>{e(number(parent_calls(run)))} / {e(number(helper_calls(run)))}</dd><dt>Repeat messages</dt><dd>{e(number(repeat))}</dd><dt>Subagents / tool errors</dt><dd>{e(number(run.get("subagents")))} / {e(number(run.get("tool_errors")))}</dd></dl>'
