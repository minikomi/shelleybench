"""Reports for frozen external evidence, without invented actors or timelines."""
from __future__ import annotations

import json
from statistics import median

from . import md
from .ui import e, number, verdict


def editing_counts(run):
    metrics = run.get("editing_metrics") or {}
    return (metrics.get("patch_calls", metrics.get("edit_calls")),
            metrics.get("rejected_patch_calls", metrics.get("failed_edit_calls")))


def seconds(value):
    return "Not recorded" if value is None else f"{value:.3f}s"


def fraction(runs, field):
    known = [r["editing_metrics"][field] for r in runs
             if isinstance(r.get("editing_metrics", {}).get(field), bool)]
    return f"{sum(known)}/{len(known)}" if known else "Not recorded"


def summary(bench, runs):
    out = []
    for model in bench["models"]:
        for version in bench["versions"]:
            mine = [r for r in runs if r["model"] == model and r["version"] == version["label"]]
            if not mine:
                continue
            counts = [editing_counts(r) for r in mine]
            attempts = sum(c[0] for c in counts) if all(c[0] is not None for c in counts) else None
            rejects = sum(c[1] for c in counts) if all(c[1] is not None for c in counts) else None
            elapsed = [r["wall_seconds"] for r in mine if r.get("wall_seconds") is not None]
            out.append({"model": model, "version": version["label"], "runs": len(mine),
                        "passed": sum(r["passed"] for r in mine),
                        "one_shot": fraction(mine, "one_shot_success"),
                        "attempts": attempts, "rejected": rejects,
                        "median_seconds": median(elapsed) if len(elapsed) == len(mine) else None})
    return out


def source_links(bench, run_id):
    items = []
    for source in bench["import_source"]["sources"]:
        path = source["path"]
        filename = path.rsplit("/", 1)[-1]
        label = "Experiment configuration" if filename.endswith(".toml") else (
            path.split("/")[-2] if filename == "comparison.json" else "Published results")
        items.append(f'<li><a href="/artifacts/{e(run_id)}/{e(source["artifact"])}" download>{e(label)} · {e(filename)}</a>'
                     f' · <code title="SHA-256 {e(source["sha256"])}">{e(source["sha256"][:12])}…</code></li>')
    items.append(f'<li><a href="/artifacts/{e(run_id)}/import-manifest.json" download>Import manifest</a></li>')
    return '<ul class="artifacts">' + "".join(items) + "</ul>"


def editor(bench):
    from .server import REPORT_FIELDS
    prose = bench.get("report") or {}
    labels = {"hypothesis": "Question", "abstract": "Results", "setup": "What we know",
              "conclusions": "What we've decided", "followups": "Still unknown"}
    fields = "".join(f'<label>{e(labels[field])}<textarea name="report_{field}" rows="4">{e(prose.get(field))}</textarea></label>'
                     for field in REPORT_FIELDS)
    return f'<details class="report-editor"><summary>Edit report text</summary><form method="post" action="/ui/benchmark-meta" class="stack"><input type="hidden" name="id" value="{e(bench["id"])}"><input type="hidden" name="report_editor" value="1"><label>Title<input name="title" value="{e(bench["title"])}"></label><label>Group<input name="group" value="{e(bench["group"])}"></label>{fields}<label>Notes<textarea name="notes">{e(bench.get("notes"))}</textarea></label><button>Save report</button></form></details>'


def benchmark_page(app, bench, runs, params):
    prose = bench["report"]
    micro = bench["study_type"] == "editing_microbenchmark"
    kind = "Editing microbenchmark" if micro else "Application-task study"
    def section(index, ident, title, content):
        return f'<section class="report-section" id="{ident}"><h2><small>{index:02}</small>{title}</h2>{content}</section>'
    body = f'<p class="crumb"><a href="/">Benchmarks</a> / {e(bench["group"])}</p><p class="report-eyebrow">{kind} · historical import</p><h1>{e(bench["title"])}</h1>'
    body += f'<p class="report-meta">{len(runs)} recorded runs · historical study</p>'
    links = [("results", "Results"), ("setup", "Known"), ("conclusions", "Decided"),
             ("unknown", "Unknown"), ("evidence", "Measurements"), ("runs", "Timelines")]
    body += '<nav class="report-contents">' + "".join(f'<a href="#{k}">{v}</a>' for k, v in links) + '</nav>'
    body += section(1, "results", "Results", '<div class="md">' + md.render(prose["abstract"]) + '</div>')
    body += section(2, "setup", "What we know", '<div class="md">' + md.render(prose["setup"]) + '</div>')
    body += section(3, "conclusions", "What we've decided", '<div class="md">' + md.render(prose["conclusions"]) + '</div>')
    body += section(4, "unknown", "Still unknown", '<div class="md">' + md.render(prose["followups"]) + '</div>')
    rows = summary(bench, runs)
    headings = ["Model", "Tool", "Passed", *(["First try"] if micro else []),
                "Rejected / edit calls", "Median time"]
    table_rows = []
    for row in rows:
        rejected = f'{row["rejected"]}/{row["attempts"]}' if row["rejected"] is not None and row["attempts"] is not None else "Not recorded"
        cells = [row["model"], row["version"], f'{row["passed"]}/{row["runs"]}',
                 *([row["one_shot"]] if micro else []), rejected, seconds(row["median_seconds"])]
        table_rows.append("<tr>" + "".join(f"<td>{e(c)}</td>" for c in cells) + "</tr>")
    table = '<div class="table-scroll"><table><thead><tr>' + "".join(f"<th>{e(h)}</th>" for h in headings) + '</tr></thead><tbody>' + "".join(table_rows) + '</tbody></table></div>'
    charts = ""
    for model in bench["models"]:
        mine = [r for r in rows if r["model"] == model]
        maximum = max((r["median_seconds"] or 0 for r in mine), default=0)
        bars = []
        for index, row in enumerate(mine):
            value = row["median_seconds"]
            fill = 0 if not maximum or value is None else value / maximum * 100
            color = "#8091a6" if index == 0 else "#39866e"
            bars.append(f'<div class="report-bar-row"><span class="report-bar-label">{e(row["version"])}</span><div class="report-bar-track"><span class="report-bar-fill" style="width:{fill:.4f}%;background:{color}"></span></div><span class="report-bar-value">{e(seconds(value))}</span></div>')
        charts += f'<div class="report-chart"><h4>{e(model)} · median elapsed</h4>{"".join(bars)}</div>'
    caveat = "First try = correct file after one patch call, with no rejected calls. Missing data isn't zero." if micro else "Compare like-for-like cases. The tools weren't tested on every task/model combination."
    body += section(5, "evidence", "Measurements", table + f'<p class="note">{caveat}</p>' + charts)
    run_rows = []
    for run in runs:
        attempts, rejects = editing_counts(run)
        run_rows.append(f'<tr><td><a href="/runs/{e(run["id"])}">{e(run["task_title"])}</a><small>{e(run["model"])} · repetition {e(run["attempt"])}</small></td><td>{e(run["version"])}</td><td>{verdict(run)}</td><td>{e(number(rejects))}/{e(number(attempts))}</td><td>{e(seconds(run.get("wall_seconds")))}</td></tr>')
    details = '<details><summary>All preserved runs · exact measurements</summary><div class="table-scroll"><table><thead><tr><th>Task / model / repetition</th><th>Configuration</th><th>Outcome</th><th>Rejected / attempted</th><th>Elapsed</th></tr></thead><tbody>' + "".join(run_rows) + '</tbody></table></div></details>'
    body += section(6, "runs", "Runs & timelines", trace_explorer(bench, runs, params) + details)
    provenance = source_links(bench, runs[0]["id"]) if runs else ""
    provenance += '<p class="note">Original files, verified at import. Each run also has its original measurements and available evaluation.</p>'
    original = {k: bench["import_source"].get(k) for k in ("definitions", "audit_notes")}
    provenance += f'<details><summary>Original definitions and audit notes</summary><pre>{e(json.dumps(original, indent=2))}</pre></details>'
    provenance += '<h3>Question tested</h3>' + md.render(prose["hypothesis"])
    provenance += f'<p class="note">Original summary export: {e(bench["measurement_note"])}</p>'
    recovered = sum(bool(run.get("events")) for run in runs)
    provenance += f'<p class="note">{recovered}/{len(runs)} original event traces recovered. Private model thinking is not included in the trace downloads.</p>'

    related = "".join(f'<li><a href="/benchmarks/{e(s["id"])}">{e((app.store.get("benchmarks", s["id"]) or s)["title"])}</a></li>' for s in bench.get("related_studies", []))
    body += f'<h3>Related studies</h3><ul>{related}</ul><details class="report-source" id="provenance"><summary>Source downloads & audit details</summary>{provenance}</details>' + editor(bench)
    return bench["title"], '<article class="bench-paper">' + body + "</article>", False


def trace_explorer(bench, runs, params):
    from . import report, timeline
    from .ui import q
    groups = [g for g in report.groups(bench, runs) if any(r.get("events") for r in g["runs"])]
    if not groups:
        return '<p class="note">Original event traces have not yet been recovered for this study.</p>'
    selected_events = {"event": q(params, "event"), "gap": q(params, "gap")}
    cohorts = {}
    for group in groups:
        cohorts.setdefault((group["model"], group["task_id"]), []).append(group)
    task_titles = {"single": "Single replacement", "double": "Two independent replacements",
                   "context": "Repeated-context disambiguation"}
    task_order = {"single": 0, "double": 1, "context": 2}
    order = {model: index for index, model in enumerate(bench["models"])}
    body = '<p class="note">Every recorded run is shown below. Each task/model panel shares one time scale across its repetitions and configurations. Model replies are points where request timing was unavailable.</p>'
    for (model, task), cohort in sorted(cohorts.items(), key=lambda item: (
            order[item[0][0]], task_order.get(item[0][1], 3), item[0][1])):
        cohort.sort(key=lambda group: group["attempt"])
        trials = [r for group in cohort for r in group["runs"]]
        labels = {r["id"]: r["version"] + (" · control" if r["version"].startswith("Normal") else "") for r in trials}
        chart_labels = {r["id"]: f'{r["version"]} · repetition {r["attempt"]}' for r in trials}
        pair_labels = {r["id"]: f'Matched repetition {r["attempt"]}' for r in trials}
        span = max(1, *(r.get("wall_seconds") or 0 for r in trials))
        title = task_titles.get(task, cohort[0]["task_title"])
        chart = timeline.render(trials, labels, {}, selected_events, span=span, group_labels=pair_labels)
        chart += timeline.charts(trials, chart_labels, span)
        body += f'<div class="report-evidence-group"><h3>{e(model)} · {e(title)}</h3>{chart}</div>'
    return body


def run_page(app, run, bench, params):
    from . import timeline
    from .ui import q
    counts = editing_counts(run)
    metrics = [
        ("Result", verdict(run)), ("Recorded elapsed", e(seconds(run.get("wall_seconds")))),
        ("One-shot", e(run.get("editing_metrics", {}).get("one_shot_success", "Not recorded"))),
        ("Rejected / attempted edits", e(f"{number(counts[1])}/{number(counts[0])}")),
        ("Uncached input / output tokens", e(f'{number(run.get("input_tokens"))}/{number(run.get("output_tokens"))}')),
        ("Cached input / cache write tokens", e(f'{number(run.get("cache_read_tokens"))}/{number(run.get("cache_write_tokens"))}')),
        ("Recorded model calls", e(number(run.get("llm_calls")))),
    ]
    stats = '<table class="kv"><tbody>' + "".join(f"<tr><th>{e(k)}</th><td>{v}</td></tr>" for k, v in metrics) + "</tbody></table>"
    header = f'<p class="crumb"><a href="/benchmarks/{e(bench["id"])}">{e(bench["title"])}</a></p><h1>{e(run["task_title"])}</h1><p>{e(run["model"])} · {e(run["version"])} · repetition {e(run["attempt"])} · historical import</p>'
    downloads = "".join(f'<li><a href="/artifacts/{e(run["id"])}/{e(a["path"])}" download>{e(a["name"])}</a></li>' for a in run["artifacts"])
    detail = f'<h2>Original source record</h2><ul>{downloads}</ul><details><summary>Raw measurements, errors and provenance</summary><pre>{e(json.dumps(run["source_record"], indent=2))}</pre></details>'
    body = header + f'<p class="note">{e(bench["measurement_note"])}</p>' + stats + detail
    if run.get("events"):
        labels = {run["id"]: run["version"]}
        span = max(1, run.get("wall_seconds") or 0)
        body = header + stats + timeline.render([run], labels, {}, {"event": q(params, "event"), "gap": q(params, "gap")}, span=span) + timeline.charts([run], labels, span) + detail
    else:
        body += '<p class="note">Original event traces have not yet been recovered for this run, so its timeline cannot be reconstructed yet.</p>'
    return f'{run["task_title"]} · {run["version"]}', body, False
