"""Keep the engineering sheet's evidence navigable without browser dependencies.

These checks detect broken references, not whether a diagram proves correctness.
Architecture meaning and layout still require source and visual review.
"""

import ast
from html.parser import HTMLParser
from pathlib import Path
import unittest
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "architecture"


class SheetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.links = []
        self.symbols = []
        self.phases = []
        self.active_content = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(values["id"])
        if tag == "a" and values.get("href"):
            self.links.append(values["href"])
            if values.get("data-symbol"):
                self.symbols.append((values["href"], values["data-symbol"]))
        if values.get("data-phase"):
            self.phases.append(values["data-phase"])
        if tag in {"script", "iframe", "object", "embed"}:
            self.active_content.append(tag)


class ArchitectureDocumentationTest(unittest.TestCase):
    def setUp(self):
        self.sheet = DOCS / "system-sheet.html"
        self.parser = SheetParser()
        self.parser.feed(self.sheet.read_text(encoding="utf-8"))

    def test_six_distinct_panels_and_complete_feedback_cycle(self):
        self.assertEqual(len(self.parser.ids), len(set(self.parser.ids)))
        self.assertTrue(set("ABCDEF").issubset(self.parser.ids))
        self.assertEqual(self.parser.phases,
                         ["Trigger", "Execute", "Check", "Feedback", "Memory", "Next"])

    def test_offline_sheet_links_resolve_inside_repository(self):
        self.assertFalse(self.parser.active_content)
        self.assertGreater(len(self.parser.links), 12)
        for href in self.parser.links:
            with self.subTest(href=href):
                link = urlsplit(href)
                self.assertFalse(link.scheme or link.netloc)
                if not link.path:
                    self.assertIn(link.fragment, self.parser.ids)
                    continue
                path = (DOCS / unquote(link.path)).resolve()
                self.assertIn(ROOT, path.parents)
                self.assertTrue(path.is_file(), href)

    def test_source_symbols_still_exist(self):
        self.assertGreaterEqual(len(self.parser.symbols), 15)
        trees = {}
        for href, symbol in self.parser.symbols:
            with self.subTest(symbol=symbol):
                path = (DOCS / urlsplit(href).path).resolve()
                if path not in trees:
                    trees[path] = ast.parse(path.read_text(encoding="utf-8"))
                definitions = {node.name for node in ast.walk(trees[path])
                               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                    ast.ClassDef))}
                self.assertIn(symbol, definitions, f"stale diagram reference: {href}:{symbol}")

    def test_architecture_entry_and_readme_link(self):
        document = (DOCS / "README.md").read_text(encoding="utf-8")
        self.assertGreaterEqual(document.count("```mermaid"), 3)
        for relative in ("system-sheet.html", "system-sheet.png"):
            self.assertIn(relative, document)
            self.assertTrue((DOCS / relative).is_file())
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/architecture/README.md", readme)


if __name__ == "__main__":
    unittest.main()
