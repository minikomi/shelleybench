import threading
import unittest
import urllib.request

from app.server import Server


class AgentDocsTests(unittest.TestCase):
    def test_agents_is_a_read_only_plain_text_guide(self):
        server = Server(('127.0.0.1', 0), object())
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'
        with urllib.request.urlopen(base + '/agents.txt') as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), 'text/plain')
            guide = response.read().decode()
        with urllib.request.urlopen(base + '/static/agents.txt') as response:
            self.assertEqual(guide, response.read().decode())
        for expected in ('/api/benchmarks', 'paid inference', 'trusted local',
                         'JSON API', 'GET /api/state', '"agent": "pi"',
                         'reuse_run_id', 'featured_order', '/?view=all',
                         'version_labels', 'TARGET count', 'JSONL receipt timing',
                         'no public bulk-merge', 'automatic revision loop'):
            self.assertIn(expected, guide)
        self.assertNotIn('Historical Imports', guide)
        self.assertNotIn('importing evidence', guide)
        self.assertNotIn('owner', guide.lower())
        with urllib.request.urlopen(base + '/robots.txt') as response:
            robots = response.read().decode()
        self.assertIn('/agents.txt', robots)
        self.assertNotIn('User-agent:', robots)


if __name__ == '__main__':
    unittest.main()
