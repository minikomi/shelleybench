"""Activity timeline: versions stacked on one axis, a lane per agent, grouped blocks and gaps."""
from __future__ import annotations

import json
import math
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import urlencode

from . import md
from .ui import duration, e, number

TYPES = {"read": "Reading", "edit": "Editing", "test": "Testing", "build": "Building", "delegate": "Delegating",
         "message": "Messaging", "browser": "Browser", "command": "Command", "model": "Model request"}


def _num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def request_duration(event):
    start, end = event.get("start"), event.get("end")
    if event.get("kind") == "model" and _num(start) and _num(end) and end > start:
        return float(end) - float(start)
    return None


def _input(event):
    detail = str(event.get("detail") or "")
    raw = detail.split("\nresult:", 1)[0]
    if raw.startswith("input:"):
        raw = raw[6:].strip()
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def kind_of(event):
    tool = str(event.get("tool_name") or event.get("label") or "").lower()
    command = str(_input(event).get("command") or "").lower()
    if "subagent" in tool:
        return "delegate"
    if any(w in tool for w in ("message_parent", "send_message", "wait")):
        return "message"
    if "browser" in tool:
        return "browser"
    if command.startswith(("rg ", "grep ")) or "search" in tool:
        return "search"
    if any(w in tool for w in ("apply_patch", "patch", "edit", "write_file")) or tool == "write":
        return "edit"
    if any(w in command for w in ("go test", "pytest", "unittest", "vitest", "playwright test", " npm test", "pnpm test", "bun test")):
        return "test"
    if command.startswith(("go build", "make ", "npm install", "pnpm install", "tsc", "go vet")):
        return "build"
    if any(w in command for w in ("sed -i", "tee ", "cat >", "cat <<")):
        return "edit"
    if command.startswith(("cat", "rg", "grep", "sed -n", "find", "ls", "head", "tail", "pwd", "wc", "git diff", "git show", "git status", "git log")) or "read" in tool or "search" in tool:
        return "read"
    return "command"


def clock(value):
    return f"{float(value):.0f}s" if _num(value) else "—"


def axis_scale(span):
    span = max(1.0, float(span))
    steps = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600)
    step = next((value for value in steps if span / value <= 8), 3600 * math.ceil(span / 28800))
    end = math.ceil(span / step) * step
    return end, range(0, end + 1, step)


def axis_label(seconds):
    if seconds < 60:
        return f"{seconds}s"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}min" + (f" {remainder}s" if remainder else "")
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h" + (f" {minutes}min" if minutes else "") + (f" {remainder}s" if remainder else "")


def windows(run):
    actors = [a for a in run.get("actors") or [] if isinstance(a, dict)] or [{"id": None}]
    out = []
    for index, actor in enumerate(actors):
        aid = actor.get("id")
        events = [ev for ev in run.get("events") or [] if isinstance(ev, dict) and (ev.get("actor_id") == aid or (not ev.get("actor_id") and index == 0))]
        times = [float(p) for ev in events for p in (ev.get("start"), ev.get("end")) if _num(p)]
        out.append({"actor": actor, "index": index, "name": "main" if not actor.get("parent_id") else str(actor.get("slug") or aid)[:28],
                    "events": events, "start": min(times) if times else None, "end": max(times) if times else None})
    return out


def blocks(run, window):
    tools = sorted((ev for ev in window["events"] if (ev.get("kind") == "tool" and _num(ev.get("start"))) or request_duration(ev) is not None), key=lambda ev: float(ev["start"]))
    groups = []
    for ev in tools:
        kind = "model" if ev.get("kind") == "model" else kind_of(ev)
        start = float(ev["start"]); end = max(start, float(ev["end"]) if _num(ev.get("end")) else start)
        last = groups[-1] if groups else None
        if last and last["type"] == kind and start - last["end"] < 10 and kind not in ("delegate", "model"):
            last["events"].append(ev); last["end"] = max(last["end"], end)
        else:
            groups.append({"type": kind, "start": start, "end": end, "events": [ev]})
    for g in groups:
        g["key"] = f'{run["id"]}:{window["index"]}:{g["events"][0].get("id")}'
        g["error"] = any(ev.get("status") == "error" for ev in g["events"])
    return groups


def gaps(run, window):
    tools = sorted((ev for ev in window["events"] if ev.get("kind") == "tool" and _num(ev.get("start"))), key=lambda ev: float(ev["start"]))
    out, until, previous = [], None, None
    for ev in tools:
        start = float(ev["start"])
        if until is not None and start - until >= 10:
            out.append({"key": f'{run["id"]}:{window["index"]}:gap:{previous.get("id")}', "start": until, "end": start, "before": previous, "after": ev, "window": window, "run": run})
        end = float(ev["end"]) if _num(ev.get("end")) else start
        if until is None or end >= until:
            until, previous = end, ev
    return out


def _link(base, **changes):
    values = {k: v for k, v in {**base, **changes}.items() if v}
    return "?" + urlencode(values)


# Clicking swaps the page body in place instead of navigating.
CLICK = 'data-swap'


def input_html(data):
    if not isinstance(data, dict):
        return '<details><summary>Raw input</summary><pre>' + e(json.dumps(data, indent=2)) + '</pre></details>'
    labels = {"command": "Command", "expression": "JavaScript", "patch": "Patch",
              "prompt": "Prompt", "path": "File", "url": "URL"}
    parts = []
    for key, value in data.items():
        if key in labels and isinstance(value, str):
            parts.append(f'<div class="fe-h">{labels[key]}</div><pre class="readable-input">{e(value)}</pre>')
        elif not isinstance(value, (dict, list)):
            parts.append(f'<div class="tool-field"><strong>{e(key.replace("_", " "))}</strong> {e(str(value).lower() if isinstance(value, bool) else value)}</div>')
        else:
            parts.append(f'<details><summary>{e(key.replace("_", " "))}</summary><pre>{e(json.dumps(value, indent=2, ensure_ascii=False))}</pre></details>')
    return "".join(parts)


def formatted(event, tools=None):
    if event.get("kind") == "model":
        elapsed = request_duration(event)
        if event.get("timing_source"):
            timing = f'{elapsed:.2f}s' if elapsed is not None else 'Unknown'
            return f'<div class="fe"><p><strong>{e(event.get("label"))}</strong> response lifecycle · {timing}.</p><p>Timing source: {e(event["timing_source"])}. These are Pi lifecycle observations, not provider HTTP request timestamps.</p></div>'
        if elapsed is not None:
            return f'<div class="fe"><p><strong>{e(event.get("label"))}</strong> request: {e(clock(event["start"]))} → {e(clock(event["end"]))} · <strong>{elapsed:.2f}s</strong>.</p><p>Recorded request time includes retries and backoff; it does not isolate model thinking or provider latency.</p></div>'
        return f'<div class="fe"><p>Model response recorded at <strong>{e(clock(event.get("start")))}</strong> ({e(event.get("label"))}). Request start was not recorded, so its duration is unknown.</p></div>'
    key = (event.get("actor_id"), str(event.get("id")).split(":tool:")[-1])
    evidence = (tools or {}).get(key, {})
    data = evidence.get("input", _input(event))
    output = evidence.get("output")
    if not output and "\nresult:" in str(event.get("detail") or ""):
        try:
            output = json.loads(event["detail"].split("\nresult:", 1)[1])
        except ValueError:
            output = None
    texts = [part["Text"] for part in (output or {}).get("ToolResult") or [] if part.get("Text")]
    if output and output.get("Text"):
        texts.append(output["Text"])
    result = f'<pre class="readable-output">{e(chr(10).join(texts))}</pre>' if texts else '<p class="muted">Complete text output unavailable in this export.</p>'
    return f'<article class="fe tool-event"><h4>{e(event.get("tool_name") or event.get("label"))} · {e(clock(event.get("start")))}{(" → " + e(clock(event.get("end")))) if _num(event.get("end")) else ""}{" · failed" if event.get("status") == "error" else ""}</h4>{input_html(data)}<div class="fe-h">Output</div>{result}<details class="raw-evidence"><summary>Raw recorded event</summary><pre>{e(str(event.get("detail") or ""))}</pre></details></article>'


def recorded_content(run):
    if not run.get("_dir"):
        return {}, {}
    directory = Path(run["_dir"])
    path = directory / "agent/trajectory.full.jsonl"
    if path.is_file():
        with path.open() as stream:
            rows = [json.loads(line) for line in stream]
    else:
        snapshot = directory.parents[1] / "import-trace-snapshots" / str(run.get("benchmark_id")) / f'{run["id"]}.sqlite3'
        if not snapshot.is_file():
            return {}, {}
        with closing(sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            rows = [{**dict(row), "record_type": "message", "llm_data": json.loads(row["llm_data"] or "{}")}
                    for row in db.execute("SELECT conversation_id,sequence_id,type,llm_data FROM messages ORDER BY conversation_id,sequence_id")]
    replies, tools = {}, {}
    for row in rows:
        if row.get("record_type") == "message" and row.get("type") == "agent":
            replies[f'{row["conversation_id"]}:{row["sequence_id"]}:model'] = (row.get("llm_data") or {}).get("Content") or []
        for block in (row.get("llm_data") or {}).get("Content") or []:
            if block.get("ToolName"):
                call = tools.setdefault((row["conversation_id"], block["ID"]), {})
                call["input"] = block.get("ToolInput")
                call["request_id"] = f'{row["conversation_id"]}:{row["sequence_id"]}:model'
            if block.get("ToolUseID"):
                tools.setdefault((row["conversation_id"], block["ToolUseID"]), {})["output"] = block
    return replies, tools


def response_text(content):
    return [{**block, "Text": block.get("Text") or block.get("Thinking")}
            for block in content if block.get("Type") in (2, 3) and (block.get("Text") or block.get("Thinking"))]


def call_label(content):
    for block in content:
        if block.get("Text"):
            return str(block["Text"]).splitlines()[0].strip("*# ")[:120]
    count = sum(bool(block.get("ToolName")) for block in content)
    return f'Requested {count} tool call{"s" if count != 1 else ""}' if count else "Reply content not recorded"


def tool_label(name, data):
    if "apply_patch" in name:
        return "Apply code patch"
    if not isinstance(data, dict):
        return name
    command = str(data.get("command") or "").strip()
    files = list(dict.fromkeys(re.findall(r'\b(?:[\w.-]+/)*[\w.-]+\.(?:go|py|md|json|vue|ts)\b', command)))
    targets = ", ".join(Path(path).name for path in files[:3])
    if command.startswith("shelley skill cat "):
        skill = command.split()[3]
        return f"Read {skill} guidance" + ("; inspect repository" if "git status" in command else "")
    if command.startswith(("rg ", "grep ")):
        return "Search source code" + (f" · {targets}" if targets else "")
    if command.startswith(("sed -n ", "cat ", "head ", "tail ")):
        return "Read files" + (f" · {targets}" if targets else "")
    if command.startswith("find "):
        return "Find project guidance" if "AGENTS.md" in command else "Find files"
    if command.startswith("git diff"):
        return "Review code changes" + (f" · {targets}" if targets else "")
    if command.startswith("git status"):
        return "Check working-tree status"
    if command.startswith("git "):
        return "Git · " + command.split()[1]
    if command.startswith("go test "):
        return "Run tests · " + command.splitlines()[0][8:]
    if command.startswith("gofmt "):
        return "Format Go code" + (" and run tests" if "go test " in command else "")
    if command.startswith("make "):
        return "Build · " + command.splitlines()[0][5:]
    if command.startswith(("python ", "python3 ")):
        return "Run Python script"
    if data.get("patch"):
        return "Apply code patch"
    if data.get("path"):
        return f'{name} · {data["path"]}'
    return "Run shell command" if command else name


def reply_html(content):
    parts = []
    for block in content:
        if block.get("Text"):
            if block.get("Type") == 3:
                parts.append(f'<div class="fe-h">Thinking · recorded model text</div><div class="md">{md.render(block["Text"])}</div>')
            else:
                parts.append(f'<div class="fe-h">Chat</div><div class="md">{md.render(block["Text"])}</div>')
    return "".join(parts) or '<p class="muted">Reply content was not retained for this call.</p>'


ACTIVITY_TYPES = {"thinking": "Thinking", "chat": "Chat", "search": "Searching",
                  "read": "Reading", "edit": "Editing", "test": "Testing",
                  "build": "Building", "delegate": "Delegating", "message": "Messaging",
                  "browser": "Browser", "command": "Command"}


def activities(run, replies, tools):
    sources = {ev.get("id"): ev for ev in run["events"]}
    groups, batches = [], {}
    for ev in run["events"]:
        if ev.get("kind") == "model":
            text = response_text(replies.get(ev.get("id"), []))
            if text:
                kind = "chat" if any(block.get("Type") == 2 for block in text) else "thinking"
                groups.append({"id": ev["id"], "actor_id": ev.get("actor_id"), "kind": kind,
                               "label": call_label(text), "text": text, "request": ev, "tools": [], "segments": [dict(ev)]})
        elif ev.get("kind") == "tool":
            key = (ev.get("actor_id"), str(ev.get("id")).split(":tool:")[-1])
            request_id = tools.get(key, {}).get("request_id")
            batch_id = request_id or ev["id"]
            batches.setdefault(batch_id, []).append(ev)
    for batch_id, events in batches.items():
        events.sort(key=lambda ev: float(ev.get("start") or 0))
        request = sources.get(batch_id)
        text = response_text(replies.get(batch_id, []))
        for index, event in enumerate(events):
            segment = dict(event)
            if index == 0 and request and not text and _num(request.get("start")):
                segment["start"] = request["start"]
            key = (event.get("actor_id"), str(event["id"]).split(":tool:")[-1])
            name = event.get("tool_name") or event.get("label")
            data = tools.get(key, {}).get("input", _input(event))
            label = tool_label(name, data)
            classified = {**event, "detail": "input: " + json.dumps(data, ensure_ascii=False)}
            groups.append({"id": event["id"], "actor_id": event.get("actor_id"), "kind": kind_of(classified),
                           "label": label, "text": [], "request": request, "tools": [event], "segments": [segment]})
    for group in groups:
        group["start"] = min(float(ev.get("start") or 0) for ev in group["segments"])
        group["end"] = max(float(ev["end"]) if _num(ev.get("end")) else float(ev.get("start") or 0) for ev in group["segments"])
        group["failed"] = any(ev.get("status") == "error" for ev in group["tools"])
    return sorted(groups, key=lambda group: group["start"])


def render(runs, labels, base, query, *, heading="Timeline", span=None, group_labels=None, patch_focus=False, roles=None):
    import hashlib
    runs = [run for run in runs if run.get("events")]
    if not runs:
        return f'<section><h2>{e(heading)}</h2><p class="muted">No timeline events recorded.</p></section>'
    span = span or max([1.0, *[float(w["end"] or 0) for run in runs for w in windows(run)], *[float(run.get("wall_seconds") or 0) for run in runs]])
    span, ticks = axis_scale(span)
    axis = ''.join(f'<span style="left:{tick / span * 100:.3f}%">{e(axis_label(tick))}</span>' for tick in ticks)
    versions, present = [], set()
    current_group = None
    for run in runs:
        replies, tools = recorded_content(run)
        groups = activities(run, replies, tools)
        present.update(group["kind"] for group in groups)
        if group_labels:
            group_label = group_labels.get(run["id"])
            if group_label != current_group:
                if current_group is not None:
                    versions.append('</div>')
                versions.append(f'<div class="tl-pair"><h4>{e(group_label)}</h4>')
                current_group = group_label
        rows = []
        rejected = []
        for group in groups:
            kind = group["kind"]
            identity = hashlib.sha256(f'{run["id"]}:{group["id"]}'.encode()).hexdigest()[:16]
            group["row_id"] = row_id = 'activity-' + identity
            group["patch_rejected"] = patch_focus and any(ev.get("tool_name") in {"patch", "apply_patch"} and ev.get("status") == "error" for ev in group["tools"])
            if group["patch_rejected"]:
                rejected.append(group)

            if group["tools"]:
                detail = ''.join(formatted(ev, tools) for ev in group["tools"])
            else:
                detail = reply_html(group["text"])
            request = group.get("request")
            if request:
                elapsed = request_duration(request)
                duration_text = f'{elapsed:.2f}s' if elapsed is not None else 'Unknown'
                timing_label = "Response lifecycle" if request.get("timing_source") else "Request timing"
                detail += f'<details class="model-metadata"><summary>{timing_label} · {e(request.get("label"))} · {duration_text}</summary>{formatted(request)}</details>'
            duration_text = f'{group["end"] - group["start"]:.2f}s' if group["tools"] or request_duration(request) is not None else 'Unknown'
            failure = '<span class="activity-failure">patch rejected</span>' if group["patch_rejected"] else '<span class="activity-failure">failed</span>' if group["failed"] else ''
            toggle = f"if(!evt.target.open){{evt.currentTarget.closest('.tl').querySelectorAll('.blk.sel[data-row=\"{row_id}\"]').forEach(bar=>{{bar.classList.remove('sel');bar.setAttribute('aria-expanded','false');}});}}"
            rows.append(f'<details id="{row_id}" class="activity-row" data-on:toggle="{e(toggle)}"><summary><span class="activity-arrow"></span><span class="activity-at">{group["start"]:.1f}s</span><span class="activity-kind"><i class="sw t-{kind}"></i>{ACTIVITY_TYPES[kind]}</span><span class="activity-label" title="{e(group["label"])}">{e(group["label"])}{failure}</span><span class="activity-duration">{duration_text}</span></summary><div class="activity-detail">{detail}</div></details>')
        lanes = []
        lane_names = []
        for window in windows(run):
            segments = [(ev, group) for group in groups if group["actor_id"] == window["actor"].get("id") for ev in group["segments"]]
            segments.sort(key=lambda item: float(item[0].get("start") or 0))
            slots, tracks = [], []
            for ev, group in segments:
                start = float(ev.get("start") or 0)
                end = max(start, float(ev["end"])) if _num(ev.get("end")) else start
                row_id, kind = group["row_id"], group["kind"]
                click = f"evt.preventDefault();const bar=evt.currentTarget;const tl=bar.closest('.tl');const closing=bar.classList.contains('sel');tl.querySelectorAll('.blk.sel').forEach(other=>{{other.classList.remove('sel');other.setAttribute('aria-expanded','false');}});const row=document.getElementById('{row_id}');if(!closing){{row.closest('.activity-history').open=true;}}row.open=!closing;if(row.open){{bar.classList.add('sel');bar.setAttribute('aria-expanded','true');const table=row.parentElement;table.style.paddingBottom=Math.max(0,table.clientHeight-row.offsetHeight)+'px';table.scrollTop+=row.getBoundingClientRect().top-table.getBoundingClientRect().top;}}"
                width = (end - start) / span * 100
                unknown = not group["tools"] and request_duration(group["request"]) is None
                elapsed = 'duration unknown' if unknown else f'{end - start:.2f}s'
                title = f'{"Rejected patch · " if group["patch_rejected"] else ""}{ACTIVITY_TYPES[kind]} · {group["label"]} · {elapsed}'
                caption = ACTIVITY_TYPES[kind] if width > 5 else ''
                bar = f'<a data-row="{row_id}" data-kind="{kind}" class="blk t-{kind}{" point" if unknown else ""}{" separated" if width > 0.3 else ""}{" err" if ev.get("status") == "error" else ""}{" patch-rejected" if group["patch_rejected"] else ""}" href="#{row_id}" data-on:click="{e(click)}" aria-expanded="false" style="left:{start / span * 100:.3f}%;width:{width:.3f}%" title="{e(title)}">{caption}</a>'
                slot = next((i for i, until in enumerate(slots) if until <= start), len(slots))
                if slot == len(slots):
                    slots.append(end); tracks.append([])
                else:
                    slots[slot] = end
                tracks[slot].append(bar)
            for i, track in enumerate(tracks):
                name = window["name"] if i == 0 else f'Parallel {i}'
                clear = "if(!evt.target.closest('.blk')){evt.currentTarget.closest('.tl').querySelectorAll('.activity-row[open]').forEach(row=>row.open=false);}"
                lane_names.append(f'<div class="lane-label" title="{e(name)}">{e(name)}</div>')
                lanes.append(f'<div class="lane"><div class="track" data-on:click="{e(clear)}">{"".join(track)}</div></div>')
        name = labels.get(run["id"], run.get("version"))
        role = (roles or {}).get(run.get("version"))
        role_label = f'<small class="version-role">{e(role)}</small>' if role else ''
        patch_summary = ""
        if patch_focus:
            from .report import patch_counts
            counts = patch_counts(run)
            if counts is not None:
                links = []
                for i, group in enumerate(rejected, 1):
                    action = f"evt.currentTarget.closest('.version').querySelector('.blk[data-row=\"{group['row_id']}\"]').click();"
                    links.append(f'<button type="button" data-on:click="{e(action)}" title="Open rejected patch at {group["start"]:.1f}s">#{i} · {group["start"]:.1f}s</button>')
                patch_summary = f'<div class="patch-outcome{" has-rejections" if counts["rejected"] else ""}"><strong>{counts["rejected"]} rejected patch calls</strong><span> / {counts["calls"]} attempts</span>{"".join(links)}</div>'
        verdict = 'pass' if run.get('passed') else 'fail' if run.get('passed') is False else run.get('status')

        history = f'<details class="activity-history"><summary>History <span>{len(groups)} activities · {e(name)}</span></summary><section class="activity-table"><div class="activity-table-head"><span></span><span>At</span><span>Activity</span><span>Action / reply</span><span>Duration</span></div><div class="activity-rows">{"".join(rows)}</div></section></details>'
        scroll = "const source=evt.currentTarget;const group=source.closest('.tl-pair')||source.closest('.tl');group.querySelectorAll('.tl-scroll').forEach(peer=>{if(peer!==source&&peer.scrollLeft!==source.scrollLeft){peer.scrollLeft=source.scrollLeft;}});"
        versions.append(f'<div class="version" data-version="{e(run.get("version"))}"><div class="version-head"><strong>{e(name)}</strong>{role_label}<span>{e(duration(run.get("wall_seconds")))} · {e(verdict)}</span></div><div class="timeline-grid"><div class="lane-labels">{"".join(lane_names)}</div><div class="tl-scroll" data-on:scroll="{e(scroll)}"><div class="tl-inner"><div class="axis">{axis}</div>{"".join(lanes)}</div></div></div>{patch_summary}{history}</div>')
    if current_group is not None:
        versions.append('</div>')

    legend = []

    for kind, label in ACTIVITY_TYPES.items():
        if kind not in present:
            continue
        hover = "const tl=evt.currentTarget.closest('.tl');const kind=evt.currentTarget.dataset.kindFilter;tl.dataset.focus=(tl.dataset.off||'').split(' ').includes(kind)?'':kind;"
        leave = "evt.currentTarget.closest('.tl').dataset.focus='';"
        click = "const tl=evt.currentTarget.closest('.tl');const kind=evt.currentTarget.dataset.kindFilter;const selected=new Set((tl.dataset.selected||'').split(' ').filter(Boolean));if(selected.has(kind)){selected.delete(kind);}else{selected.add(kind);}const buttons=[...tl.querySelectorAll('[data-kind-filter]')];const off=selected.size?buttons.map(button=>button.dataset.kindFilter).filter(kind=>!selected.has(kind)):[];tl.dataset.selected=[...selected].join(' ');tl.dataset.off=off.join(' ');tl.dataset.focus='';buttons.forEach(button=>button.setAttribute('aria-pressed',selected.has(button.dataset.kindFilter)));tl.querySelector('.legend-clear').disabled=!selected.size;tl.querySelectorAll('.blk.sel').forEach(bar=>{if(off.includes(bar.dataset.kind)){bar.classList.remove('sel');bar.setAttribute('aria-expanded','false');document.getElementById(bar.dataset.row).open=false;}});"
        legend.append(f'<button type="button" class="legend-filter" data-kind-filter="{kind}" aria-pressed="false" data-on:mouseenter="{e(hover)}" data-on:mouseleave="{e(leave)}" data-on:focus="{e(hover)}" data-on:blur="{e(leave)}" data-on:click="{e(click)}"><i class="sw t-{kind}"></i>{label}</button>')
    clear = "const tl=evt.currentTarget.closest('.tl');tl.dataset.selected='';tl.dataset.off='';tl.dataset.focus='';tl.querySelectorAll('[data-kind-filter]').forEach(button=>button.setAttribute('aria-pressed','false'));evt.currentTarget.disabled=true;"
    legend.append(f'<button type="button" class="legend-filter legend-clear" data-on:click="{e(clear)}" disabled>Clear</button>')
    return f'<section class="tl"><p class="timeline-help">Select a block to open its history below. Hover the legend to focus; select categories to filter. Clear shows everything.</p><div class="legend">{"".join(legend)}</div>{"".join(versions)}</section>'


COLORS = ["#2b4f73", "#b55c31", "#3e8564", "#8e6fb5", "#c08aa5", "#b89a5d"]


def _context(ev):
    u = ev.get("usage") or {}
    return int(u.get("input_tokens") or 0) + int(u.get("cache_read_tokens") or 0) + int(u.get("cache_write_tokens") or 0)


def _total(ev):
    u = ev.get("usage") or {}
    return sum(int(u.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))


def charts(runs, labels, span):
    """Context size per model call and cumulative tokens/cost over time, one line per run, main agent only for context."""
    span, _ = axis_scale(span)
    series = []
    for index, run in enumerate(runs):
        calls = sorted((ev for ev in run.get("events") or [] if isinstance(ev, dict) and ev.get("kind") == "model" and _num(ev.get("start")) and ev.get("usage")), key=lambda ev: float(ev["start"]))
        if not calls:
            continue
        roots = {a.get("id") for a in run.get("actors") or [] if not a.get("parent_id")}
        context = [(float(ev["start"]), _context(ev)) for ev in calls if not roots or ev.get("actor_id") in roots]
        priced = any((ev.get("cost_usd") or 0) > 0 for ev in calls)
        total, cumulative = 0.0, []
        for ev in sorted(calls, key=lambda ev: float(ev["end"]) if request_duration(ev) is not None else float(ev["start"])):
            total += float(ev.get("cost_usd") or 0) if priced else _total(ev)
            cumulative.append((float(ev["end"]) if request_duration(ev) is not None else float(ev["start"]), total))
        series.append({"label": labels.get(run["id"], run.get("version")), "color": COLORS[index % len(COLORS)], "context": context, "cumulative": cumulative, "priced": priced})
    if not series:
        return ""
    priced = all(s["priced"] for s in series)
    W, H, L, B = 1000, 150, 0, 18
    def panel(title, key, fmt):
        top = max([1, *[v for s in series for _, v in s[key]]])
        paths = ""
        for s in series:
            pts = s[key]
            if key == "context":
                d = " ".join(f'{"M" if i == 0 else "L"}{t / span * W:.1f},{H - B - v / top * (H - B - 6):.1f}' for i, (t, v) in enumerate(pts))
                dots = "".join(f'<circle cx="{t / span * W:.1f}" cy="{H - B - v / top * (H - B - 6):.1f}" r="2" fill="{s["color"]}"><title>{e(s["label"])} · {e(clock(t))} · {e(number(v))}</title></circle>' for t, v in pts)
            else:
                d = "M0,%.1f " % (H - B) + " ".join(f'L{t / span * W:.1f},{H - B - v / top * (H - B - 6):.1f}' for t, v in pts)
                dots = ""
            paths += f'<path d="{d}" fill="none" stroke="{s["color"]}" stroke-width="1.5" vector-effect="non-scaling-stroke"/>{dots}'
        grid = "".join(f'<line x1="{p * W / 4:.0f}" y1="0" x2="{p * W / 4:.0f}" y2="{H - B}" stroke="#e3e7eb"/>' for p in range(1, 4))
        ymax = f'<text x="4" y="11" font-size="10" fill="#6b7580">{e(fmt(top))}</text>'
        return f'<div class="chart"><div class="chart-title">{e(title)}</div><svg viewBox="0 0 {W} {H}" preserveAspectRatio="none">{grid}{paths}<line x1="0" y1="{H - B}" x2="{W}" y2="{H - B}" stroke="#dde2e7"/></svg><div class="chart-ymax">{e(fmt(top))}</div></div>'
    legend = "".join(f'<span><i class="sw" style="background:{s["color"]}"></i>{e(s["label"])}</span>' for s in series)
    money_fmt = (lambda v: f"${v:.2f}") if priced else number
    return f'<div class="charts"><div class="legend">{legend}</div>{panel("Context per model call (main agent)", "context", number)}{panel("Cumulative cost" if priced else "Cumulative tokens (all agents; cost unavailable from this provider)", "cumulative", money_fmt)}</div>'
