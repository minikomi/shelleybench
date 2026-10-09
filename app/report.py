"""Matched, per-attempt report groups and server-rendered comparison bars."""
from __future__ import annotations

from html import escape

_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
_PALETTE = ("#8091a6", "#39866e", "#4778bb")


def display_versions(versions):
    if {"Normal nested", "Simple patch"} <= {v.get("label") for v in versions}:
        roles = {"Normal nested": "Baseline", "Simple patch": "Experiment"}
        return [{**v, "role": roles.get(v["label"])} for v in sorted(versions, key=lambda v: v["label"] != "Normal nested")]
    return versions


def patch_counts(run):
    if not run or not run.get("events"):
        return None
    calls = [event for event in run["events"] if event.get("kind") == "tool" and event.get("tool_name") in {"patch", "apply_patch"}]
    return {"calls": len(calls), "rejected": sum(event.get("status") == "error" for event in calls)}


def _version_labels(bench):
    return [v.get("label") for v in bench.get("versions") or [] if isinstance(v, dict) and v.get("label") is not None]


def _key(run):
    return (run.get("task_id"), run.get("model"), run.get("comparison_attempt", run.get("attempt")))


def current_runs(runs):
    visible = [dict(run) for run in runs]
    archived = []
    for run in runs:
        if run.get("status") != "infrastructure_failure":
            continue
        replacements = [other for other in visible
                        if other.get("status") == "complete"
                        and all(other.get(key) == run.get(key) for key in ("task_id", "model", "version"))
                        and other.get("attempt", 0) > run.get("attempt", 0)]
        if not replacements:
            continue
        replacement = min(replacements, key=lambda other: other["attempt"])
        replacement["comparison_attempt"] = min(replacement.get("comparison_attempt", replacement["attempt"]), run["attempt"])
        visible = [other for other in visible if other["id"] != run["id"]]
        archived.append(run)
    return visible, archived


def groups(bench, runs):
    """Return task/model/attempt groups, with only configured version runs.

    Configured combinations come first so an as-yet-unrecorded combination remains
    visible.  Observed combinations not in the current configuration follow them.
    """
    bench = bench if isinstance(bench, dict) else {}
    runs = runs if isinstance(runs, (list, tuple)) else []
    labels = _version_labels(bench)
    order = {label: index for index, label in enumerate(labels)}
    known = set(labels)
    observed = [run for run in runs if isinstance(run, dict) and run.get("version") in known]
    retired = {(run.get("task_id"), run.get("model"), run.get("attempt")) for run in observed
               if run.get("comparison_attempt", run.get("attempt")) != run.get("attempt")}
    present = {_key(run) for run in observed}

    entries = []
    configured = set()
    tasks = bench.get("frozen_tasks") or []
    models = bench.get("models") or []
    try:
        attempts = max(0, int(bench.get("attempts", 0)))
    except (TypeError, ValueError):
        attempts = 0
    for task in tasks:
        if not isinstance(task, dict) or task.get("id") is None:
            continue
        for model in models:
            for attempt in range(1, attempts + 1):
                key = (task["id"], model, attempt)
                if key in retired and key not in present:
                    continue
                if key in configured:
                    continue
                configured.add(key)
                entries.append({"task_id": task["id"], "task_title": task.get("title") or task["id"],
                                "model": model, "attempt": attempt})

    seen = set(configured)
    for run in observed:
        key = _key(run)
        if key in seen:
            continue
        seen.add(key)
        entries.append({"task_id": run.get("task_id"), "task_title": run.get("task_title") or run.get("task_id"),
                        "model": run.get("model"), "attempt": run.get("comparison_attempt", run.get("attempt"))})

    result = []
    for entry in entries:
        key = (entry["task_id"], entry["model"], entry["attempt"])
        matched = [run for run in observed if _key(run) == key]
        # Python's sort is stable: duplicate externally recorded runs remain exact
        # records rather than being pooled or arbitrarily discarded.
        matched.sort(key=lambda run: order[run.get("version")])
        result.append({**entry, "runs": matched})
    return result


def _numeric(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _token_total(run):
    values = [_numeric(run.get(field)) for field in _TOKEN_FIELDS]
    return sum(values) if all(value is not None for value in values) else None


def _state(run):
    if run is None:
        return "Pending"
    if run.get("status") == "complete":
        return None
    status = run.get("status")
    if status in (None, "", "queued", "building", "running", "preparing"):
        return "Pending"
    return str(status).replace("_", " ").capitalize()


def _bar_rows(versions, by_version, title, read, fmt, *, primary=False):
    values = [read(run) if run and run.get("status") == "complete" else None for run in by_version]
    recorded = [value for value in values if value is not None]
    maximum = max(recorded, default=0)
    rows = []
    for index, (version, run, value) in enumerate(zip(versions, by_version, values)):
        state = _state(run)
        label = version.get("label") if isinstance(version, dict) else version
        label = "" if label is None else str(label)
        if isinstance(version, dict) and version.get("role"):
            label += " · " + version["role"]
        if state:
            rendered, fill, now, maximum_attr = state, 0, "", ""
            aria = f"{label}: {state}"
        elif value is None:
            rendered, fill, now, maximum_attr = "Not recorded", 0, "", ""
            aria = f"{label}: Not recorded"
        else:
            rendered = fmt(value, run)
            fill = 0 if maximum == 0 else value / maximum * 100
            now, maximum_attr = f' aria-valuenow="{escape(str(value), quote=True)}"', f' aria-valuemax="{escape(str(maximum), quote=True)}"'
            aria = f"{label}: {rendered}"
        color = _PALETTE[index % len(_PALETTE)]
        detail = f' title="{escape(rendered, quote=True)}"' if " · " in rendered else ""
        rows.append(
            f'<div class="report-bar-row"><span class="report-bar-label">{escape(label)}</span>'
            f'<div class="report-bar-track" role="progressbar" aria-label="{escape(aria, quote=True)}" aria-valuemin="0"{maximum_attr}{now}>'
            f'<span class="report-bar-fill" style="width:{fill:.4f}%;background:{color}"></span></div>'
            f'<span class="report-bar-value"{detail}>{escape(rendered.split(" · ", 1)[0])}</span></div>')
    return f'<div class="report-chart{" patch-primary" if primary else ""}"><h4>{escape(title)}</h4>{"".join(rows)}</div>'


def charts(group, versions, *, patch_focus=False):
    """Render accessible, exact per-run bars for one matched report group."""
    from .ui import duration, number  # ui imports this module lazily from benchmark_page

    group = group if isinstance(group, dict) else {}
    versions = versions if isinstance(versions, (list, tuple)) else []
    labels = [version.get("label") if isinstance(version, dict) else version for version in versions]
    run_buckets = {}
    for run in group.get("runs") or []:
        if isinstance(run, dict) and run.get("version") in labels:
            run_buckets.setdefault(run.get("version"), []).append(run)
    # A duplicate external record gets its own bar rather than silently changing a
    # reported measurement. Missing configured versions get an honest pending row.
    expanded_versions, aligned = [], []
    for version in versions:
        label = version.get("label") if isinstance(version, dict) else version
        bucket = run_buckets.get(label) or [None]
        for run in bucket:
            expanded_versions.append({"label": label, "role": version.get("role") if isinstance(version, dict) else None})
            aligned.append(run)

    title = " · ".join(str(group.get(key) if group.get(key) is not None else "") for key in ("task_title", "model"))
    attempt = group.get("attempt")
    if attempt is not None:
        title += f" · attempt {attempt}"

    def time_value(value, run):
        return duration(value)

    def token_value(value, run):
        pieces = " · ".join((
            f"input {number(run.get('input_tokens'))}", f"output {number(run.get('output_tokens'))}",
            f"cache read {number(run.get('cache_read_tokens'))}", f"cache write {number(run.get('cache_write_tokens'))}"))
        return f"{number(value)} total · {pieces}"

    body = ""
    if patch_focus:
        def rejected(run):
            counts = patch_counts(run)
            return counts["rejected"] if counts else None
        body += _bar_rows(expanded_versions, aligned, "Rejected patch calls · primary outcome", rejected,
                         lambda value, run: f'{number(value)} / {number(patch_counts(run)["calls"])} calls', primary=True)
    body += _bar_rows(expanded_versions, aligned, "Actor wall time · parent + helpers", lambda run: _numeric(run.get("wall_seconds")), time_value)
    body += _bar_rows(expanded_versions, aligned, "Tokens · parent + helpers", _token_total, token_value)
    return f'<section class="report-charts" aria-label="{escape(title, quote=True)}">{body}</section>'
