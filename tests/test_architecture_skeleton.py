"""架构骨架与公共契约的无外部服务安全网。"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from kylin.ports.data.knowledge_search import KnowledgeDocument


PROJECT_ROOT = Path(__file__).resolve().parents[1]
KYLIN_ROOT = PROJECT_ROOT / "kylin"


def _python_files(relative_directory: str) -> list[Path]:
    return sorted((KYLIN_ROOT / relative_directory).rglob("*.py"))


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
    return imported_names


def _top_level_definitions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _class_methods(path: Path, class_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                child.name
                for child in node.body
                if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef)
                )
            }
    return set()


class ArchitectureSkeletonTests(unittest.TestCase):
    def test_core_layers_do_not_import_upper_layers(self) -> None:
        forbidden_imports = {
            "applications": ("kylin.adapters", "kylin.bootstrap"),
            "ports": ("kylin.adapters", "kylin.applications", "kylin.bootstrap"),
            "platform": (
                "kylin.adapters",
                "kylin.applications",
                "kylin.bootstrap",
            ),
            "adapters/outbound": ("kylin.applications", "kylin.bootstrap"),
        }

        violations: list[str] = []
        for relative_directory, forbidden in forbidden_imports.items():
            for path in _python_files(relative_directory):
                for imported_name in _imports(path):
                    if any(
                        imported_name == prefix
                        or imported_name.startswith(f"{prefix}.")
                        for prefix in forbidden
                    ):
                        violations.append(f"{path.relative_to(PROJECT_ROOT)} -> {imported_name}")

        self.assertEqual([], violations)

    def test_http_bootstrap_has_no_module_level_app_creation(self) -> None:
        path = KYLIN_ROOT / "bootstrap" / "http.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        module_level_calls = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "create_app"
        ]

        self.assertEqual([], module_level_calls)

    def test_http_adapters_use_model_port_instead_of_outbound_factory(self) -> None:
        violations: list[str] = []
        for path in _python_files("adapters/inbound/http"):
            for imported_name in _imports(path):
                if imported_name.startswith("kylin.adapters.outbound.model"):
                    violations.append(f"{path.relative_to(PROJECT_ROOT)} -> {imported_name}")

        self.assertEqual([], violations)

    def test_knowledge_qa_protocol_receives_service_instance(self) -> None:
        path = KYLIN_ROOT / "adapters" / "inbound" / "http" / "knowledge_qa_protocol.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        constructors = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        ]

        self.assertEqual(1, len(constructors))
        self.assertEqual(
            ["self", "service", "model_gateway"],
            [argument.arg for argument in constructors[0].args.args],
        )
        self.assertNotIn("service_factory", ast.unparse(constructors[0]))

    def test_kb_service_split_keeps_compatibility_surface(self) -> None:
        service_root = KYLIN_ROOT / "adapters" / "outbound" / "knowledge_base" / "kb_service"
        base_definitions = _top_level_definitions(service_root / "base.py")
        package_tree = ast.parse(
            (service_root / "__init__.py").read_text(encoding="utf-8"),
            filename=str(service_root / "__init__.py"),
        )

        exported_names: set[str] = set()
        for node in package_tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "__all__":
                        exported_names.update(ast.literal_eval(node.value))

        self.assertTrue((service_root / "factory.py").is_file())
        self.assertTrue((service_root / "views.py").is_file())
        self.assertNotIn("KBServiceFactory", base_definitions)
        self.assertNotIn("get_kb_details", base_definitions)
        self.assertNotIn("get_kb_file_details", base_definitions)
        self.assertIn("KBServiceFactory", exported_names)
        self.assertIn("get_kb_details", exported_names)
        self.assertIn("get_kb_file_details", exported_names)

    def test_application_ports_keep_their_minimum_method_contracts(self) -> None:
        ingestion_methods = _class_methods(
            KYLIN_ROOT / "ports" / "application" / "knowledge_ingestion.py",
            "KnowledgeIngestionPort",
        )
        search_methods = _class_methods(
            KYLIN_ROOT / "ports" / "data" / "knowledge_search.py",
            "KnowledgeSearchPort",
        )

        self.assertTrue(
            {
                "list_knowledge_bases",
                "create_knowledge_base",
                "delete_knowledge_base",
                "upload_files",
                "update_files",
                "delete_files",
                "rebuild",
            }.issubset(ingestion_methods)
        )
        self.assertEqual({"exists", "ensure_ready", "search"}, search_methods)

    def test_knowledge_search_document_updates_are_immutable(self) -> None:
        document = KnowledgeDocument(
            page_content="内容",
            metadata={"source": "a.md"},
            document_id="doc-1",
        )

        enriched = document.with_metadata(section="第一章")
        rescored = document.with_score(0.42)

        self.assertEqual({"source": "a.md"}, document.metadata)
        self.assertEqual({"source": "a.md", "section": "第一章"}, enriched.metadata)
        self.assertEqual("doc-1", enriched.document_id)
        self.assertEqual(0.42, rescored.score)
        self.assertEqual(0.42, rescored.metadata["rrf_score"])


if __name__ == "__main__":
    unittest.main()
