import unittest
from html.parser import HTMLParser

from app import md


class Cells(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.cells = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        if tag in ("th", "td"):
            self.cells.append((tag, dict(attrs).get("class", "")))


class MarkdownTableTests(unittest.TestCase):
    def test_comparison_alignment_is_column_wide_including_headers(self):
        markup = md.render("| Measure | Shelley | Pi |\n| --- | --- | --- |\n"
                           "| Acceptance | Pass | Pass |\n| Runtime | 120.5s (2min) | 180.8s (3min) |\n"
                           "| Calls | 4 / 1 | 3 / 0 |\n| Tokens | **120,000** | 90,000 |")
        self.assertEqual(Cells(markup).cells, [("th", ""), ("th", "num"), ("th", "num")] +
                         [("td", ""), ("td", "num"), ("td", "num")] * 4)
        self.assertIn('class="table-scroll md-table-scroll"', markup)

    def test_explicit_alignment_and_prose_columns_are_preserved(self):
        markup = md.render("| Label | Prose | Center | Right |\n| --- | :--- | :---: | ---: |\n"
                           "| First | 12 | Yes | — |")
        self.assertEqual(Cells(markup).cells[:4],
                         [("th", ""), ("th", ""), ("th", "md-center"), ("th", "num")])
        self.assertEqual(Cells(md.render("| Label | Explanation |\n| --- | --- |\n| One | Longer prose |")).cells,
                         [("th", ""), ("th", ""), ("td", ""), ("td", "")])

    def test_table_links_render_and_unsafe_destinations_stay_text(self):
        markup = md.render('| Task | Result |\n| --- | --- |\n'
                           '| [**Ordering**](/benchmarks/example) | [site](https://example.test/?a=1&b=2) |\n'
                           '| [bad](javascript:alert) | [bad](//example.test) |')
        self.assertIn('<a href="/benchmarks/example"><strong>Ordering</strong></a>', markup)
        self.assertIn('href="https://example.test/?a=1&amp;b=2"', markup)
        self.assertNotIn('href="javascript:', markup)
        self.assertNotIn('href="//', markup)
        self.assertIn('href="/&quot;onmouseover=&quot;bad"', md.render('[safe](/"onmouseover="bad)'))

    def test_short_rows_are_padded_to_header_columns(self):
        markup = md.render("| Label | One | Two |\n| --- | --- | --- |\n| Short | 1 |")
        self.assertEqual(len(Cells(markup).cells), 6)
