import ast
import unittest
from pathlib import Path


class PipelineGuardRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse((Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8"))
        cls.functions = {
            node.name: node for node in cls.tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def test_editor_export_always_releases_download_lock(self):
        node = self.functions["editor_export"]
        release_in_finally = False
        for child in ast.walk(node):
            if isinstance(child, ast.Try):
                for stmt in child.finalbody:
                    for sub in ast.walk(stmt):
                        if (
                            isinstance(sub, ast.Call)
                            and isinstance(sub.func, ast.Attribute)
                            and isinstance(sub.func.value, ast.Name)
                            and sub.func.value.id == "DOWNLOAD_LOCK"
                            and sub.func.attr == "release"
                        ):
                            release_in_finally = True
        self.assertTrue(release_in_finally, "editor_export must release the shared media lock on every exit path")

    def test_instagram_preserves_source_until_hdr_detection(self):
        node = self.functions["download"]
        guarded = False
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "ensure_mp4_container":
                for keyword in child.keywords:
                    if keyword.arg == "preserve_source":
                        guarded = "is_instagram" in ast.unparse(keyword.value)
        self.assertTrue(guarded, "Instagram source must not be transcoded before HDR metadata is inspected")


if __name__ == "__main__":
    unittest.main()
