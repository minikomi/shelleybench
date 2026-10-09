"""Server-rendered pages. Datastar only polls for fresh HTML while work is active."""
from __future__ import annotations

import html
import hashlib
import json
import secrets
from urllib.parse import urlencode

ACTIVE = {"queued", "building", "running", "preparing"}
NAV = [("/", "Benchmarks"), ("/tasks", "Tasks"), ("/settings", "Settings")]


def e(value):
    return html.escape("" if value is None else str(value), quote=True)


def q(params, key, default=""):
    value = params.get(key, [default])
    return value[0] if isinstance(value, list) and value else default


def duration(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{value:.0f}s" if value < 90 else f"{value / 60:.1f}m"


def number(value):
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return "—"


def money(value):
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return "—"


def tokens(run):
    values = [run.get(k) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")]
    return sum(int(v or 0) for v in values) if any(v is not None for v in values) else None


def status(value):
    return f'<span class="status s-{e(value)}">{e(str(value or "unknown").replace("_", " "))}</span>'


def verdict(run):
    if run.get("status") == "complete":
        return '<span class="pass">pass</span>' if run.get("passed") else '<span class="fail">fail</span>'
    return status(run.get("status"))


def delta(before, after, fmt, invert=False):
    if before is None or after is None:
        return "—"
    change = after - before
    sign = "+" if change > 0 else "−" if change < 0 else ""
    return f"{sign}{fmt(abs(change))}"


# ----- layout ----------------------------------------------------------------

def refresh_attrs(live=False, revision=""):
    if not live and not revision:
        return ""
    request = "window.location.pathname + window.location.search + (window.location.search ? '&' : '?') + 'fragment=poll'"
    if revision:
        request += " + '&revision=' + el.dataset.revision"
    return f' data-revision="{e(revision)}" data-on-interval__duration.4s="{e("@get(" + request + ")")}"'


def benchmark_revision(app, key):
    bench = app.store.require("benchmarks", key)
    versions = [(bench["id"], bench.get("updated_at"))]
    for kind in ("runs", "discussions"):
        versions.extend((row["id"], row.get("updated_at")) for row in app.store.list(kind, benchmark_id=key))
    return hashlib.sha256(json.dumps(versions).encode()).hexdigest()[:20]


def document(title, body, path, live=False, revision=""):
    nonce = secrets.token_urlsafe(12)
    links = "".join(f'<a href="{href}" class="{"active" if (path == href or (href != "/" and path.startswith(href))) else ""}">{label}</a>' for href, label in NAV)
    poll = refresh_attrs(live, revision)
    return nonce, f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)} · Shelley Bench</title><link rel="stylesheet" href="/static/style.css">
<script type="module" src="/static/vendor/datastar-1.0.4.js" nonce="{nonce}"></script></head>
<body data-on:click="const a = evt.target.closest('a[data-swap]'); if (a) {{ evt.preventDefault(); const h = a.getAttribute('href'); history.replaceState(null, '', h); @get(h + '&fragment=1') }}"><header><a class="brand" href="/">shelley bench</a><nav>{links}</nav></header>
<main id="page"{poll}>{body}</main></body></html>'''


def fragment(body, live=False, revision=""):
    return f'<main id="page"{refresh_attrs(live, revision)}>{body}</main>'


def send(handler, raw, kind, nonce=None, status=200):
    data = raw.encode()
    handler.send_response(status)
    handler.send_header("Content-Type", kind)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    if nonce:
        handler.send_header("Content-Security-Policy", f"default-src 'self'; script-src 'self' 'nonce-{nonce}' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; connect-src 'self'; form-action 'self'; frame-ancestors 'self'; base-uri 'none'")
    handler.end_headers()
    handler.wfile.write(data)


def redirect(handler, location):
    handler.send_response(303)
    handler.send_header("Location", location)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def patch(handler, body):
    lines = "\n".join("data: elements " + line for line in body.splitlines())
    send(handler, f"event: datastar-patch-elements\n{lines}\n\n", "text/event-stream")


def discussion(app, key):
    from . import md
    app.store.require("benchmarks", key)
    turns = app.store.list("discussions", benchmark_id=key)
    pending = any(t["status"] in {"queued", "running"} for t in turns)
    poll = f' data-on-interval__duration.2s="{e("@get(" + json.dumps("/ui/chat-status?" + urlencode({"benchmark": key})) + ")")}"' if pending else ""
    history = ""
    for turn in turns:
        answer = md.render(turn.get("reply") or "") if turn["status"] == "complete" else (
            f'<p class="error">{e(turn.get("error") or "Answer interrupted. Submit the question again to retry.")}</p>'
            if turn["status"] not in {"queued", "running"} else '<p class="muted">Analyzing recorded results…</p>')
        history += f'<article class="discussion-turn"><p class="discussion-question">{e(turn["question"])}</p><div class="md discussion-answer">{answer}</div><small class="muted">{e(turn.get("model"))} · {e(turn.get("created_at", "")[:16].replace("T", " "))}</small></article>'
    request = secrets.token_hex(16)
    submit = "evt.preventDefault(); @post('/ui/discussion', {contentType:'form', retry:'never', retryMaxCount:0, requestCancellation:'disabled'})"
    composer = "" if pending else f'''<form method="post" action="/ui/discussion" class="discussion-form" data-on:submit="{e(submit)}" data-indicator:_sending>
<input type="hidden" name="benchmark" value="{e(key)}"><input type="hidden" name="request_id" value="{request}">
<label for="chat-question">Ask about these results</label><textarea id="chat-question" name="message" rows="2" maxlength="4000" required data-ignore-morph placeholder="What caused the tool errors? Did the thread reduce coordination?"></textarea>
<div class="discussion-footer"><span>Analysis only · {e(app.store.settings()["assistant_model"])} · never starts benchmark runs</span><button data-attr:disabled="$_sending">Ask</button></div></form>'''
    return f'<section id="benchmark-chat" class="discussion"{poll}><h2>Follow-up discussion</h2><div class="discussion-history">{history}</div><div id="chat-notice"></div>{composer}</section>'


def respond(handler, path, params):
    app = handler.app
    if path == "/ui/chat-status":
        return patch(handler, discussion(app, q(params, "benchmark")))
    parts = [p for p in path.split("/") if p]
    revision = benchmark_revision(app, parts[1]) if len(parts) == 2 and parts[0] == "benchmarks" else ""
    if revision and q(params, "fragment") == "poll" and q(params, "revision") == revision:
        return send(handler, "", "text/event-stream", status=204)
    if not parts:
        title, body, live = benchmarks_page(app, params)
    elif parts[0] == "benchmarks" and len(parts) == 2:
        title, body, live = benchmark_page(app, parts[1], params)
    elif parts[0] == "runs" and len(parts) == 2:
        title, body, live = run_page(app, parts[1], params)
    elif parts[0] == "tasks" and len(parts) == 1:
        title, body, live = tasks_page(app)
    elif parts[0] == "tasks" and len(parts) == 2:
        title, body, live = task_page(app, parts[1])
    elif parts[0] == "settings":
        title, body, live = settings_page(app)
    else:
        return handler.json({"error": "Not found"}, 404)
    if q(params, "fragment"):
        if q(params, "fragment") == "poll" and not live and not revision:
            return send(handler, "", "text/event-stream", status=204)
        lines = "\n".join("data: elements " + line for line in fragment(body, live, revision).splitlines())
        return send(handler, f"event: datastar-patch-elements\n{lines}\n\n", "text/event-stream")
    nonce, page = document(title, body, "/" + "/".join(parts[:1]) if parts else "/", live, revision)
    send(handler, page, "text/html; charset=utf-8", nonce)


def action(handler, path, body):
    app = handler.app
    name = path.removeprefix("/ui/")
    if name == "discussion":
        try:
            app.ask(body.get("benchmark"), body)
        except (ValueError, KeyError) as exc:
            return patch(handler, '<div id="chat-notice" class="error">' + e(str(exc)) + '</div>')
        return patch(handler, discussion(app, body["benchmark"]))
    if name == "resume":
        app.resume_benchmark(body["id"]); return redirect(handler, f"/benchmarks/{body['id']}")
    if name == "findings":
        app.regenerate_findings(body["id"]); return redirect(handler, f"/benchmarks/{body['id']}")
    if name == "cancel":
        app.cancel_benchmark(body["id"]); return redirect(handler, f"/benchmarks/{body['id']}")
    if name == "delete-benchmark":
        app.delete_benchmark(body["id"]); return redirect(handler, "/")
    if name == "prepare":
        app.prepare_task(body["id"]); return redirect(handler, f"/tasks/{body['id']}")
    if name == "delete-task":
        app.delete_task(body["id"]); return redirect(handler, "/tasks")
    if name == "benchmark-meta":
        from .server import REPORT_FIELDS
        fields = {"title": body.get("title", ""), "group": body.get("group", ""), "notes": body.get("notes", "")}
        if "report_editor" in body:
            fields["report"] = {key: body.get("report_" + key, "") for key in REPORT_FIELDS}
        app.update_benchmark(body["id"], fields)
        return redirect(handler, f"/benchmarks/{body['id']}")
    if name == "settings":
        app.settings({k: body[k] for k in ("shelley_binary", "shelley_repo", "build_command", "binary_path", "assistant_model", "reasoning", "timeout_seconds") if k in body})
        return redirect(handler, "/settings")
    handler.json({"error": "Unknown action"}, 404)


def post_button(action, fields, label, danger=False):
    hidden = "".join(f'<input type="hidden" name="{e(k)}" value="{e(v)}">' for k, v in fields.items())
    return f'<form method="post" action="/ui/{action}" class="inline">{hidden}<button class="{"danger" if danger else ""}">{e(label)}</button></form>'


# ----- benchmarks list ---------------------------------------------------------

def benchmarks_page(app, params):
    items = [app.benchmark_view(b) for b in app.store.list("benchmarks")]
    tasks = {t["id"]: t for t in app.store.list("tasks")}
    filters = {k: q(params, k) for k in ("group", "task", "model", "version", "status", "q")}
    def keep(b):
        if filters["group"] and b.get("group") != filters["group"]:
            return False
        if filters["task"] and filters["task"] not in b.get("task_ids", []):
            return False
        if filters["model"] and filters["model"] not in b.get("models", []):
            return False
        if filters["version"] and filters["version"] not in [v["label"] for v in b.get("versions", [])] + [v["sha"][:10] for v in b.get("versions", [])]:
            return False
        if filters["status"] and b.get("status") != filters["status"]:
            return False
        if filters["q"] and filters["q"].lower() not in json.dumps({k: b.get(k) for k in ("title", "group", "notes", "tags")}).lower():
            return False
        return True
    shown = [b for b in reversed(items) if keep(b)]
    groups = sorted({b.get("group") for b in items if b.get("group")})
    models = sorted({m for b in items for m in b.get("models", [])})
    labels = sorted({v["label"] for b in items for v in b.get("versions", [])})
    def options(values, current, blank):
        return f'<option value="">{e(blank)}</option>' + "".join(f'<option value="{e(v)}" {"selected" if v == current else ""}>{e(v)}</option>' for v in values)
    apply_filters = "const form = evt.target.closest('form'); const url = '/?' + new URLSearchParams(new FormData(form)); history.replaceState(null, '', url); @get(url + '&fragment=1')"
    form = f'''<form method="get" action="/" class="filters" data-on:change="{e("if (evt.target.tagName === 'SELECT') { " + apply_filters + " }")}" data-on:submit="{e("evt.preventDefault(); " + apply_filters)}">
<input name="q" aria-label="Search benchmarks" value="{e(filters["q"])}" placeholder="Search title, notes, tags" data-on:input__debounce.250ms="{e(apply_filters)}">
<select name="group" aria-label="Topic">{options(groups, filters["group"], "Any topic")}</select>
<select name="task" aria-label="Task"><option value="">Any task</option>{"".join(f'<option value="{e(k)}" {"selected" if k == filters["task"] else ""}>{e(t["title"])}</option>' for k, t in tasks.items())}</select>
<select name="model" aria-label="Model">{options(models, filters["model"], "Any model")}</select>
<select name="version" aria-label="Version">{options(labels, filters["version"], "Any version")}</select>
<select name="status" aria-label="Status">{options(["queued", "building", "running", "complete", "failed", "cancelled", "interrupted"], filters["status"], "Any status")}</select>
<a href="/">Clear</a></form>'''
    topics = {}
    for b in shown:
        topic = (b.get("group") or "Ungrouped").split(" · ", 1)[0]
        summary = b.get("summary", {})
        cells = "".join(f'<div><span class="muted">{e(v["label"])}</span> <strong>{summary.get(v["label"], {}).get("passed", 0)}/{summary.get(v["label"], {}).get("complete", 0)}</strong></div>' for v in b.get("versions", []))
        configurations = e(" vs ".join(v["label"] for v in b.get("versions", []))) or "—"
        task_names = e(" · ".join(tasks.get(t, {}).get("title", t) for t in b.get("task_ids", []))) or "No tasks"
        row = (f'<tr><td><a class="benchmark-title" href="/benchmarks/{e(b["id"])}">{e(b["title"])}</a><small>{task_names}</small></td>'
               f'<td>{configurations}<small>{e(", ".join(b.get("models", []))) or "No models"}</small></td>'
               f'<td class="benchmark-results">{cells or "—"}</td><td>{status(b["status"])}'
               f'<small>{e(b.get("created_at", "")[:16].replace("T", " "))}</small>'
               f'{("<small>" + e(b["progress"]) + "</small>") if b.get("progress") else ""}</td></tr>')
        topics.setdefault(topic, []).append(row)
    sections = ""
    for topic, rows in sorted(topics.items(), key=lambda item: item[0].casefold()):
        title = topic.replace("-", " ").capitalize() if topic.islower() else topic
        sections += f'<section class="benchmark-topic"><h2>{e(title)} <small>{len(rows)}</small></h2><div class="table-scroll"><table class="benchmark-index"><thead><tr><th>Benchmark / task</th><th>Versions / models</th><th>Passed / complete</th><th>Status / created</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></section>'
    if not sections:
        sections = '<p class="muted">No benchmarks match these filters.</p>' if items else '<p class="muted">No benchmarks recorded. Create one through the API (see Settings for the skill).</p>'
    live = any(b["status"] in ACTIVE for b in items)
    return "Benchmarks", f'<h1>Benchmarks <small>{len(shown)} of {len(items)}</small></h1>{form}{sections}', live


# ----- benchmark detail --------------------------------------------------------

def findings_section(text, name):
    import re
    for match in re.finditer(r"^## (.+)\n(.*?)(?=^## |\Z)", text or "", re.M | re.S):
        if match.group(1).split(" — ", 1)[0].strip().lower() == name.lower():
            return match.group(2).strip()
    return ""


def benchmark_page(app, key, params):
    from . import md, report, results, timeline as tl
    from .server import REPORT_FIELDS
    bench = app.store.require("benchmarks", key)
    runs = app.store.list("runs", benchmark_id=key)
    if bench.get("import_source"):
        from . import import_report
        return import_report.benchmark_page(app, bench, runs, params)
    runs, archived = report.current_runs(runs)
    versions = report.display_versions(bench.get("versions", []))
    patch_focus = any(v.get("role") == "Baseline" for v in versions)
    prose = bench.get("report") or {}
    groups = report.groups({**bench, "versions": versions}, runs)
    if patch_focus:
        order = {v["label"]: i for i, v in enumerate(versions)}
        runs.sort(key=lambda run: order.get(run.get("version"), len(order)))
    def written(field, fallback=""):
        return md.render(prose.get(field) or fallback)
    def section(number, ident, title, body):
        return f'<section class="report-section" id="{ident}"><h2><small>{number:02}</small>{title}</h2>{body}</section>'

    finished = [r for r in runs if r.get("status") == "complete"]
    graded = [r for r in finished if isinstance(r.get("passed"), bool)]
    passed = sum(r["passed"] for r in graded)
    reused = sum(bool(r.get("_source_run_id") or r.get("source_run_id")) for r in runs)
    outcome = f'{passed}/{len(graded)} graded runs passed; {len(finished)}/{len(runs)} recorded runs complete.' if runs else 'No runs recorded yet.'
    abstract = written("abstract", findings_section(bench.get("findings"), "Verdict")) or f'<p>{e(outcome)}</p><p>Compare configurations within each task/model/attempt group below. No performance conclusion has been written yet.</p>'
    hypothesis = written("hypothesis") if prose.get("hypothesis") else f'<p>{e(bench["title"])}</p><p class="note">Study question inferred from the title; no explicit hypothesis was recorded. Add one using the report editor below.</p>'
    limit = f'{bench.get("attempts", 1)} configured attempt(s) per task/model/configuration.'
    if archived:
        limit = 'Completed retries replace interrupted attempts in comparisons. Actual attempt numbers remain in the run records.'
    if reused:
        limit += f' {reused} reused run(s).'
    header = f'<p class="crumb"><a href="/">Benchmarks</a>{(" / " + e(bench.get("group"))) if bench.get("group") else ""}</p><p class="report-eyebrow">Experiment report</p><h1>{e(bench["title"])}</h1><p class="report-meta">{status(bench["status"])} · {e(bench.get("created_at", "")[:16].replace("T", " "))} · {len(groups)} comparison group(s)</p>'
    header += f'<div class="report-hypothesis"><h3>Hypothesis being tested</h3><div class="md">{hypothesis}</div></div><section class="report-abstract"><h2>Abstract</h2><div class="md">{abstract}</div><p class="report-caveat">{e(limit)}</p></section>'
    links = [("setup", "01 · Setup"), ("runs", "02 · What was run"), ("results", "03 · Results"), ("details", "04 · Detailed evidence"), ("conclusions", "05 · Conclusions & follow-ups")]
    contents = '<nav class="report-contents" aria-label="Report sections">' + ''.join(f'<a href="#{anchor}">{label}</a>' for anchor, label in links) + '</nav>'

    provenance = ""
    for v in versions:
        d = v.get("describe") if isinstance(v.get("describe"), dict) else {"subject": str(v.get("describe") or "")}
        profile = v.get("runtime_profile") or {}
        details = e(d.get("subject") or "")
        if profile:
            details += f' · {e(profile.get("name"))} guidance/tools snapshot'
        if d.get("dirty"):
            details += f' · {e(d.get("changed_files"))} uncommitted files'
        provenance += f'<tr><th>{e(v["label"])}</th><td><code>{e(str(v.get("sha") or "")[:10])}</code></td><td>{details}</td></tr>'
    setup = f'<div class="report-setup-grid"><div><h3>Execution</h3><p>{e(", ".join(bench.get("models", []))) or "Not recorded"} · {e(bench.get("reasoning") or "reasoning not recorded")}</p></div><div><h3>Measurement</h3><p>Actor time excludes setup and grading. Tokens include parent and helpers.</p></div></div>'
    setup = written("setup") + setup
    setup += f'<details class="report-provenance"><summary>Exact versions and configuration</summary><div class="table-scroll"><table><thead><tr><th>Configuration</th><th>Revision</th><th>Snapshot</th></tr></thead><tbody>{provenance}</tbody></table></div>'
    if bench.get("agent_note"):
        setup += f'<h3>Additional actor instruction</h3><pre>{e(bench["agent_note"])}</pre>'
    if bench.get("notes"):
        setup += f'<h3>Recorded experiment notes</h3><p class="notes">{e(bench["notes"])}</p>'
    setup += '</details>'

    tasks = "".join(f'<article class="report-task"><h3><a href="/tasks/{e(t["id"])}">{e(t["title"])}</a></h3><p>{e(t.get("blurb") or "Open the task for its full prompt, acceptance criteria, and qualification controls.")}</p><p class="note">Acceptance command: <code>{e(t.get("acceptance_command") or "Not recorded")}</code></p></article>' for t in bench.get("frozen_tasks", []))
    run_rows = ""
    for r in runs:
        source = r.get("_source_run_id") or r.get("source_run_id")
        origin = f'<a href="/runs/{e(source)}">Reused earlier run</a>' if source else 'Recorded run'
        actors = ', '.join(f'{a.get("model") or "Unknown model"} ({"parent" if not a.get("parent_id") else "helper"})' for a in r.get("actors") or [])
        run_rows += f'<tr><th><a href="/runs/{e(r["id"])}">{e(r.get("version"))}</a></th><td>{e(r.get("task_title") or r.get("task_id"))}<small>{e(r.get("model"))} · attempt {e(r.get("attempt"))}</small></td><td>{verdict(r)}</td><td>{origin}<small>{e(actors)}</small></td></tr>'
    run_table = f'<div class="table-scroll"><table><thead><tr><th>Configuration · full run</th><th>Task / parent / attempt</th><th>Outcome</th><th>Evidence / observed models</th></tr></thead><tbody>{run_rows}</tbody></table></div>' if runs else '<p class="muted">No runs recorded yet.</p>'
    if archived:
        links = ''.join(f'<li><a href="/runs/{e(r["id"])}">{e(r["version"])} · attempt {e(r["attempt"])}</a></li>' for r in archived)
        run_table += f'<details class="report-archive"><summary>Earlier interrupted attempts · archived</summary><ul>{links}</ul></details>'

    charts = ""
    evidence = ""
    sel = {"event": q(params, "event"), "gap": q(params, "gap")}
    span = max([1.0, *[float(r.get("wall_seconds") or 0) for r in runs], *[float(ev.get("end") or ev.get("start") or 0) for r in runs for ev in r.get("events") or [] if isinstance(ev, dict) and isinstance(ev.get("start"), (int, float))]])
    for index, group in enumerate(groups, 1):
        slot = 'comparison' if archived else 'attempt'
        title = f'{group["task_title"]} · {group["model"]} · {slot} {group["attempt"]}'
        anchor = results.anchor(group["task_id"], group["model"], group["attempt"])
        charts += f'<div class="report-result-group"><h3>Comparison group {index}: {e(title)}</h3>{report.charts(group, versions, patch_focus=patch_focus)}</div>'
        decision, table = results.comparison(group["runs"], versions)
        if any(r.get("events") for r in group["runs"]):
            activity = tl.render(group["runs"], {r["id"]: r["version"] for r in group["runs"]}, {}, sel, span=span,
                                 group_labels={r["id"]: f'{slot.title()} {group["attempt"]} · same task and model' for r in group["runs"]},
                                 patch_focus=patch_focus, roles={v["label"]: v.get("role") for v in versions})
            activity += tl.charts(group["runs"], {r["id"]: r["version"] for r in group["runs"]}, span)
        else:
            activity = '<p class="muted">No timeline events recorded for this group.</p>'
        evidence += f'<div class="report-evidence-group" id="{anchor}"><h3>Comparison group {index}: {e(title)}</h3><details><summary>Observed differences</summary>{decision}</details><details class="report-exact"><summary>Exact matched measurements · tables</summary>{table}</details>{activity}</div>'
    chart_note = '<p class="note">Per-attempt measurements. Token volume is not dollar cost; exact categories are in the tables below.</p>'
    reading = '<p><strong>A comparison group is the same task × parent model × comparison slot.</strong> A completed retry replaces an interrupted attempt in its slot; the execution attempt number stays in the run record.</p><p><strong>Timeline lanes:</strong> each configuration has a parent lane and any helper lanes. Every run clock starts at zero on the same scale; the runs need not have happened simultaneously. Colors identify activity, not configuration.</p><p><strong>History:</strong> each tool call has its own block and history entry. Click a block to open that timeline’s history. Model request timing is collapsed metadata; older responses without timing appear as points.</p>'
    conclusion = written("conclusions", findings_section(bench.get("findings"), "Verdict")) or '<p>No conclusion has been written yet. Use the recorded measurements above rather than treating missing commentary as a result.</p>'
    caveats = findings_section(bench.get("findings"), "Caveats")
    if caveats:
        conclusion += '<details><summary>Recorded limitations</summary>' + md.render(caveats) + '</details>'
    followups = written("followups", findings_section(bench.get("findings"), "Next")) or '<p class="muted">No follow-ups recorded.</p>'
    conclusions = f'<div class="report-conclusion md">{conclusion}</div><h3>Follow-ups</h3><div class="md">{followups}</div>'
    findings = ""
    if bench.get("findings"):
        findings = f'<details class="report-source"><summary>Original generated findings and provenance</summary><div class="md">{md.render(bench["findings"])}</div><p class="note">Written by {e(app.store.settings().get("assistant_model"))} · {e((bench.get("findings_at") or "")[:16].replace("T", " "))}</p>{post_button("findings", {"id": key}, "Rewrite findings") if bench["status"] not in ACTIVE else ""}</details>'
    elif bench.get("findings_error"):
        findings = f'<p class="error">Findings: {e(bench["findings_error"])}</p>{post_button("findings", {"id": key}, "Retry findings")}'
    elif bench["status"] == "complete" and not bench.get("progress"):
        findings = post_button("findings", {"id": key}, "Write findings")

    editor_fields = ''.join(f'<label>{e(field.title())}<textarea name="report_{field}" rows="{2 if field == "hypothesis" else 4}">{e(prose.get(field) or "")}</textarea></label>' for field in REPORT_FIELDS)
    meta = f'<details class="report-editor"><summary>Edit report text and benchmark metadata</summary><form method="post" action="/ui/benchmark-meta" class="stack"><input type="hidden" name="id" value="{e(key)}"><input type="hidden" name="report_editor" value="1"><label>Title<input name="title" value="{e(bench.get("title"))}"></label><label>Group<input name="group" value="{e(bench.get("group"))}"></label>{editor_fields}<label>Recorded notes<textarea name="notes" rows="4">{e(bench.get("notes"))}</textarea></label><button>Save report</button></form></details>'
    unfinished = any(r["status"] in {"interrupted", "cancelled", "queued", "infrastructure_failure", "timeout"} for r in runs)
    controls = post_button("cancel", {"id": key}, "Cancel") if bench["status"] in ACTIVE else (post_button("resume", {"id": key}, "Resume unfinished runs") if unfinished else "") + post_button("delete-benchmark", {"id": key}, "Delete", danger=True)
    progress = f'<p class="progress">{e(bench["progress"])}</p>' if bench.get("progress") else ""
    error = f'<p class="error">{e(bench["error"])}</p>' if bench.get("error") else ""
    body = header + progress + error + contents
    body += section(1, "setup", "How it was set up", setup)
    body += section(2, "runs", "What was run", tasks + run_table)
    body += section(3, "results", "Results", charts + chart_note)
    body += section(4, "details", "Detailed results and timelines", '<p class="note">Group = task × parent model × attempt. One timeline lane per agent.</p><details class="report-reading-guide"><summary>How to read the timelines</summary>' + reading + '</details>' + evidence)
    body += section(5, "conclusions", "Conclusions & follow-ups", conclusions + findings)
    body += '<details class="report-discussion"><summary>Follow-up discussion</summary>' + discussion(app, key) + '</details>' + meta + '<div class="report-controls">' + controls + '</div>'
    return bench["title"], '<article class="bench-paper">' + body + '</article>', bench["status"] in ACTIVE or bool(bench.get("progress"))


# ----- run detail ------------------------------------------------------------

KINDS = {"subagent": "delegate", "apply_patch": "edit", "patch": "edit", "edit": "edit", "write": "edit", "read": "read", "search": "read", "browser": "browser"}


def event_kind(event):
    tool = str(event.get("tool_name") or event.get("label") or "").lower()
    for key, kind in KINDS.items():
        if key in tool:
            return kind
    detail = str(event.get("detail") or "")
    command = detail[len("input:"):].split("\nresult:", 1)[0].lower() if detail.startswith("input:") else ""
    if any(w in command for w in ("go test", "pytest", "unittest", "vitest", "playwright", "npm test", "pnpm test")):
        return "test"
    if any(w in command for w in ("go build", "make", "npm install", "pnpm install", "tsc")):
        return "build"
    if any(w in command for w in ("cat ", "rg ", "grep ", "sed -n", "find ", "ls ", "head ", "tail ", "git diff", "git log", "git show", "git status")):
        return "read"
    return "command"


def timeline(run):
    events = [ev for ev in run.get("events") or [] if isinstance(ev, dict) and isinstance(ev.get("start"), (int, float))]
    actors = run.get("actors") or []
    if not events:
        return '<p class="muted">No timeline events recorded.</p>'
    end = max(max(float(ev.get("end") or ev["start"]) for ev in events), float(run.get("wall_seconds") or 0), 1.0)
    lane_ids = [a.get("id") for a in actors] or [None]
    names = {a.get("id"): ("main" if not a.get("parent_id") else str(a.get("slug") or a.get("id"))[:24]) for a in actors}
    lanes = ""
    for index, lane in enumerate(lane_ids):
        mine = [ev for ev in events if ev.get("actor_id") == lane or (lane is None)]
        bars = ""
        for ev in mine:
            start = float(ev["start"]); stop = float(ev.get("end") or ev["start"])
            left = start / end * 100; width = max((stop - start) / end * 100, 0.15)
            kind = "model" if ev.get("kind") == "model" else event_kind(ev)
            title = f'{ev.get("label")} {duration(stop - start)} @ {start:.0f}s' + (" · error" if ev.get("status") == "error" else "")
            bars += f'<a class="bar k-{kind} {"err" if ev.get("status") == "error" else ""}" style="left:{left:.2f}%;width:{width:.2f}%" href="#ev-{e(ev.get("id"))}" title="{e(title)}"></a>'
        lanes += f'<div class="lane"><div class="lane-name">{e(names.get(lane, "main"))}</div><div class="track">{bars}</div></div>'
    ticks = "".join(f'<span style="left:{p}%">{duration(end * p / 100)}</span>' for p in (0, 25, 50, 75, 100))
    legend = "".join(f'<span class="k-{k}">{k}</span>' for k in ("model", "read", "edit", "command", "test", "build", "delegate", "browser"))
    return f'<div class="timeline"><div class="axis">{ticks}</div>{lanes}<div class="legend">{legend}</div></div>'


def run_page(app, key, params):
    from . import timeline as tl
    run = app.store.require("runs", key)
    bench = app.store.get("benchmarks", run.get("benchmark_id")) or {}
    if run.get("import_source"):
        from . import import_report
        return import_report.run_page(app, run, bench, params)
    metrics = [("Result", verdict(run)), ("Actor time", e(duration(run.get("wall_seconds")))), ("Tokens", e(number(tokens(run)))),
               ("Input / output", f'{e(number(run.get("input_tokens")))} / {e(number(run.get("output_tokens")))}'),
               ("Cache read / write", f'{e(number(run.get("cache_read_tokens")))} / {e(number(run.get("cache_write_tokens")))}'),
               ("LLM calls", e(number(run.get("llm_calls")))), ("Tool calls / errors", f'{e(number(run.get("tool_calls")))} / {e(number(run.get("tool_errors")))}'),
               ("Subagents", e(number(run.get("subagents")))), ("Cost", e(money(run.get("cost_usd"))))]
    stats = '<table class="kv"><tbody>' + "".join(f'<tr><th>{e(k)}</th><td>{v}</td></tr>' for k, v in metrics) + "</tbody></table>"
    rows = ""
    for ev in run.get("events") or []:
        if not isinstance(ev, dict):
            continue
        start = ev.get("start"); stop = ev.get("end")
        length = duration(float(stop) - float(start)) if isinstance(start, (int, float)) and isinstance(stop, (int, float)) else "—"
        rows += f'<tr id="ev-{e(ev.get("id"))}"><td class="num">{e(f"{float(start):.0f}s" if isinstance(start, (int, float)) else "—")}</td><td class="num">{e(length)}</td><td>{e(ev.get("kind"))}</td><td>{e(ev.get("label"))}</td><td>{e(ev.get("status"))}</td><td><pre>{e(str(ev.get("detail") or "")[:1500])}</pre></td></tr>'
    events = f'<details><summary>Events ({len(run.get("events") or [])})</summary><table class="events"><thead><tr><th class="num">At</th><th class="num">Length</th><th>Kind</th><th>Label</th><th>Status</th><th>Detail</th></tr></thead><tbody>{rows}</tbody></table></details>'
    artifacts = "".join(f'<li><a href="/artifacts/{e(key)}/{e(a.get("path") or a.get("name"))}">{e(a.get("name") or a.get("path"))}</a></li>' for a in run.get("artifacts") or [] if isinstance(a, dict)) if run.get("_dir") else ""
    grader = run.get("grader") or {}
    grader_html = f'<details><summary>Grader output ({e(grader.get("status"))})</summary><pre>{e((grader.get("stdout") or "")[-6000:])}\n{e((grader.get("stderr") or "")[-6000:])}</pre></details>' if grader else ""
    error = f'<p class="error">{e(run["error"])}</p>' if run.get("error") else ""
    header = f'<p class="crumb"><a href="/">Benchmarks</a> / <a href="/benchmarks/{e(bench.get("id"))}">{e(bench.get("title") or run.get("benchmark_id"))}</a></p><h1>{e(run.get("task_title"))}</h1><p class="sub">{e(run.get("model"))} · {e(run.get("version"))} · attempt {e(run.get("attempt"))} · {status(run.get("status"))}</p>{error}'
    return f'{run.get("task_title")} · {run.get("version")}', header + f'<section class="two"><div>{stats}</div><div>{f"<ul class=artifacts>{artifacts}</ul>" if artifacts else ""}</div></section><section><h2>Timeline</h2>{tl.render([run], {run["id"]: run.get("version")}, {}, {"event": q(params, "event"), "gap": q(params, "gap")})}{tl.charts([run], {run["id"]: run.get("version")}, max(1.0, float(run.get("wall_seconds") or 0), *[float(ev.get("end") or ev.get("start") or 0) for ev in run.get("events") or [] if isinstance(ev, dict) and isinstance(ev.get("start"), (int, float))]))}</section>{grader_html}{events}', run.get("status") in ACTIVE


# ----- tasks ---------------------------------------------------------------

def tasks_page(app):
    rows = ""
    for t in app.store.list("tasks"):
        rows += f'<tr><td><a href="/tasks/{e(t["id"])}">{e(t["title"])}</a><small>{e(t.get("blurb") or "")[:160]}</small></td><td><code>{e(t.get("base_commit", "")[:10])}</code> → <code>{e(t.get("reference_commit", "")[:10])}</code></td><td>{status(t["status"])}<small>{e(t.get("error") or "")[:200]}</small></td><td>{e(t.get("created_at", "")[:10])}</td></tr>'
    live = any(t["status"] == "preparing" for t in app.store.list("tasks"))
    return "Tasks", f'<h1>Tasks</h1><table><thead><tr><th>Task</th><th>Base → reference</th><th>Status</th><th>Created</th></tr></thead><tbody>{rows or "<tr><td colspan=4 class=muted>No tasks. Create them through the API.</td></tr>"}</tbody></table>', live


def preparation_log(job):
    rows = []
    for item in job.get("log") or []:
        label = item.get("progress") or item.get("phase") or item.get("label") or item.get("text")
        if not label:
            if item.get("kind") == "progress" and not item.get("detail"):
                continue
            label = item.get("kind") or item.get("status")
        if not label and not item.get("detail"):
            continue
        detail = " — " + e(item["detail"]) if item.get("detail") else ""
        rows.append(f'<li>{e(label or "")}{detail}</li>')
    return "".join(rows) or "<li class=muted>No log.</li>"


def task_page(app, key):
    t = app.store.require("tasks", key)
    job = app.store.get("jobs", t.get("job_id") or "") or {}
    log = preparation_log(job)
    controls = t.get("controls") or {}
    control_rows = "".join(f'<tr><th>{e(name)}</th><td>{e(c.get("kind"))}</td><td>{e(c.get("status"))}</td><td>{e("pass" if c.get("passed") else "fail" if c.get("passed") is False else "—")}</td></tr>' for name, c in controls.items() if isinstance(c, dict))
    actions = post_button("prepare", {"id": key}, "Prepare" if t["status"] == "proposed" else "Re-prepare") if t["status"] != "preparing" else ""
    actions += post_button("delete-task", {"id": key}, "Delete", danger=True)
    benches = [b for b in app.store.list("benchmarks") if key in b.get("task_ids", [])]
    used = "".join(f'<li><a href="/benchmarks/{e(b["id"])}">{e(b["title"])}</a> {status(b["status"])}</li>' for b in benches)
    error = f'<p class="error">{e(t["error"])}</p>' if t.get("error") else ""
    body = f'''<p class="crumb"><a href="/tasks">Tasks</a></p><div class="head"><div><h1>{e(t["title"])}</h1><p class="sub">{status(t["status"])} · <code>{e(t.get("base_commit", "")[:12])}</code> → <code>{e(t.get("reference_commit", "")[:12])}</code></p>{error}<p>{e(t.get("blurb"))}</p></div><div>{actions}</div></div>
<section><h2>Prompt given to the agent</h2><pre>{e(t.get("prompt") or "(generated during preparation)")}</pre></section>
<section><h2>Grading</h2><p><code>{e(t.get("acceptance_command") or "—")}</code></p><table><thead><tr><th>Control</th><th>Kind</th><th>Status</th><th>Result</th></tr></thead><tbody>{control_rows or "<tr><td colspan=4 class=muted>Not qualified yet.</td></tr>"}</tbody></table>
<details><summary>Hidden test patch</summary><pre>{e(t.get("test_patch") or "")}</pre></details></section>
<section><h2>Preparation log</h2><ul class="log">{log}</ul></section>
<section><h2>Used in</h2><ul>{used or "<li class=muted>No benchmarks.</li>"}</ul></section>'''
    return t["title"], body, t["status"] == "preparing"


def settings_page(app):
    s = app.store.settings()
    fields = "".join(f'<label>{e(k)}<input name="{e(k)}" value="{e(s.get(k))}"></label>' for k in ("shelley_binary", "shelley_repo", "build_command", "binary_path", "assistant_model", "reasoning", "timeout_seconds"))
    models = ", ".join(m["id"] for m in app.models) or "none detected"
    return "Settings", f'<h1>Settings</h1><form method="post" action="/ui/settings" class="stack">{fields}<button>Save</button></form><section><h2>Installed models</h2><p>{e(models)}</p></section><section><h2>API</h2><p>Shelley drives this app through <code>/api</code>; run <code>shelley skill cat bench</code> in a conversation for the workflow.</p></section>', False
