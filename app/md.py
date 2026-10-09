"""Tiny Markdown subset for assistant-written findings: headings, tables, nested lists, bold/code."""
import html
import re


def _inline(text):
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    def link(match):
        href = html.unescape(match.group(2))
        if not (href.startswith(("https://", "http://")) or href.startswith("/") and not href.startswith("//")):
            return match.group(0)
        return f'<a href="{html.escape(href, quote=True)}">{match.group(1)}</a>'
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, text)
    return text


def _table(lines):
    rows = [[c.strip() for c in line.strip().strip("|").split("|")] for line in lines]
    if not rows:
        return ""
    separator = rows[1] if len(rows) > 1 and all(re.fullmatch(r":?-{2,}:?", c or "") for c in rows[1]) else []
    body_rows = rows[2:] if separator else rows[1:]
    classes = []
    for column in range(len(rows[0])):
        marker = separator[column] if column < len(separator) else ""
        numeric = column > 0 and any(re.fullmatch(r"[+\-−]?\d[\d.,]*[a-z%]*(?:\s*\([^)]*\))?(?:\s*/\s*[+\-−]?\d[\d.,]*[a-z%]*)?",
                                                 row[column].replace("**", "").replace("`", ""), re.I)
                                    for row in body_rows if column < len(row))
        if marker.startswith(":"):
            classes.append("md-center" if marker.endswith(":") else "")
        else:
            classes.append("num" if marker.endswith(":") or numeric else "")
    def cell(tag, text, column):
        return f'<{tag} class="{classes[column]}">{_inline(text)}</{tag}>'
    head = "".join(cell("th", text, column) for column, text in enumerate(rows[0]))
    body = "".join("<tr>" + "".join(cell("td", row[column] if column < len(row) else "", column)
                                   for column in range(len(rows[0]))) + "</tr>" for row in body_rows)
    return f'<div class="table-scroll md-table-scroll"><table class="md-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def render(text):
    out, lines, i = [], text.splitlines(), 0
    stack = []  # list nesting levels (indent sizes)
    def close_lists(to=0):
        while len(stack) > to:
            stack.pop(); out.append("</ul>")
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            close_lists(); i += 1; continue
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            close_lists(); level = min(len(m.group(1)) + 1, 4)
            out.append(f"<h{level}>{_inline(m.group(2).strip())}</h{level}>"); i += 1; continue
        if line.lstrip().startswith("|"):
            close_lists(); block = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                block.append(lines[i]); i += 1
            out.append(_table(block)); continue
        m = re.match(r"^(\s*)([-*•]|\d+[.)])\s+(.*)", line)
        if m:
            indent = len(m.group(1).expandtabs(4))
            while stack and indent < stack[-1]:
                stack.pop(); out.append("</ul>")
            if not stack or indent > stack[-1]:
                stack.append(indent); out.append("<ul>")
            out.append(f"<li>{_inline(m.group(3).strip())}</li>"); i += 1; continue
        close_lists(); out.append(f"<p>{_inline(line.strip())}</p>"); i += 1
    close_lists()
    return "".join(out)
