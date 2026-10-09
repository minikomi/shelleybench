"""Tiny Markdown subset for assistant-written findings: headings, tables, nested lists, bold/code."""
import html
import re


def _inline(text):
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    return text


def _table(lines):
    rows = [[c.strip() for c in line.strip().strip("|").split("|")] for line in lines]
    rows = [r for r in rows if not all(re.fullmatch(r":?-{2,}:?", c or "") for c in r)]
    if not rows:
        return ""
    head = "".join(f"<th>{_inline(c)}</th>" for c in rows[0])
    body = "".join("<tr>" + "".join(f'<td class="{"num" if re.fullmatch(r"[−+\-−]?[\d.,]+%?[a-z]*|n/a|—", c or "") else ""}">{_inline(c)}</td>' for c in r) + "</tr>" for r in rows[1:])
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


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
