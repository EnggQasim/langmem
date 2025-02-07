"""Test runner for executing code examples in docstrings."""

import ast
import importlib
import logging
import textwrap
from pathlib import Path
from typing import Any, Dict, List

import langsmith as ls
import pytest
import vcr

pytestmark = pytest.mark.anyio

logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

vcr_instance = vcr.VCR(
    cassette_library_dir="tests/vcr_cassettes",
    record_mode=vcr.record_mode.RecordMode.NEW_EPISODES,
    match_on=["uri", "method", "body"],
)


def extract_code_blocks(docstring: str) -> List[str]:
    """Extract Python code blocks (```python ... ```) from a docstring.
    Only matches code blocks at the markdown level, not those inside other code blocks.
    """
    if not docstring:
        return []

    # Split content into lines to process line by line
    lines = docstring.split("\n")
    blocks = []
    current_block = []
    in_code_block = False

    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```"):
            if not in_code_block:
                # Start of a code block
                lang = stripped[3:].strip()
                if lang.endswith("skip"):
                    in_code_block = "other"
                elif lang.startswith(("python", "py")) or lang == "":
                    in_code_block = True
                    current_block = []
                else:
                    in_code_block = "other"
            elif in_code_block == "other":
                in_code_block = False
            else:
                in_code_block = False
                if current_block:
                    blocks.append("\n".join(current_block))
                current_block = []
        elif in_code_block:
            current_block.append(line)

    blocks = [textwrap.dedent(block).strip() for block in blocks]
    logger.debug(f"Found {len(blocks)} code blocks in docstring")
    for i, block in enumerate(blocks):
        logger.debug(f"Code block {i}:\n{block}")
    return blocks


class DocstringVisitor(ast.NodeVisitor):
    """AST visitor that finds function definitions and extracts docstring code blocks."""

    def __init__(self):
        super().__init__()
        self.functions = {}
        self.current_class = None
        self.current_module = None

    def visit_FunctionDef(self, node):
        name = node.name
        if self.current_class:
            name = f"{self.current_class}.{name}"
        if self.current_module:
            name = f"{self.current_module}.{name}"

        docstring = ast.get_docstring(node)
        if docstring:
            code_blocks = extract_code_blocks(docstring)
            if code_blocks:
                self.functions[name] = {
                    "name": name,
                    "examples": code_blocks,
                    "module": self.current_module,
                }
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        old_class = self.current_class
        self.current_class = node.name
        self.generic_visit(node)
        self.current_class = old_class


def get_module_functions(module_path: str) -> Dict[str, Any]:
    """Parse a Python file and return docstring code blocks from its functions."""
    try:
        with open(module_path, "r", encoding="utf-8") as f:
            source = f.read()

        tree = ast.parse(source)

        # Construct a module name from the path, so importlib can locate it
        src_dir = Path(__file__).parent.parent / "src"
        rel_path = Path(module_path).relative_to(src_dir)
        module_name = str(rel_path.with_suffix("")).replace("/", ".")

        visitor = DocstringVisitor()
        visitor.current_module = module_name
        visitor.visit(tree)
        return visitor.functions
    except Exception as e:
        logger.error(f"Error processing module {module_path}: {e}")
        return {}


def extract_markdown_examples(file_path: Path) -> List[pytest.param]:
    """Extract Python code blocks from a markdown file."""
    if not file_path.exists():
        logger.warning(f"Markdown file not found at {file_path}")
        return []

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    code_blocks = extract_code_blocks(content)
    python_blocks = [
        block
        for block in code_blocks
        if not block.startswith(("bash", "text", "js", "javascript", "markdown"))
    ]

    if python_blocks:
        logger.info(f"Adding {len(python_blocks)} code blocks from {file_path}")
        return [
            pytest.param(
                None,  # No module
                str(file_path),  # Use file path as function name
                python_blocks,  # All Python blocks together
                id=f"{file_path.name}::examples",
            )
        ]
    return []


def collect_docstring_tests():
    """Collect all docstring Python code blocks from the 'src/' tree and markdown files."""
    src_dir = Path(__file__).parent.parent / "src"
    docs_dir = Path(__file__).parent.parent / "docs" / "docs"
    logger.info(f"Scanning for Python files in {src_dir}")
    py_files = list(src_dir.rglob("*.py"))
    logger.info(f"Found {len(py_files)} Python files")

    test_cases = []

    # Process README
    readme_path = Path(__file__).parent.parent / "README.md"
    # test_cases.extend(extract_markdown_examples(readme_path))

    # Process docs directory
    included = ("delayed_processing", "background_quickstart", "manage_user_profil")

    if docs_dir.exists():
        logger.info(f"Scanning for markdown files in {docs_dir}")
        md_files = list(docs_dir.rglob("*.md"))
        logger.info(f"Found {len(md_files)} markdown files")
        for md_file in md_files:
            if any(include in md_file.name for include in included):
                test_cases.extend(extract_markdown_examples(md_file))
    # for py_file in py_files:
    #     logger.debug(f"Processing file: {py_file}")
    #     funcs = get_module_functions(str(py_file))
    #     logger.debug(f"Found {len(funcs)} functions with examples in {py_file}")

    #     for func_name, details in funcs.items():
    #         test_id = f"{py_file.relative_to(src_dir)}::{func_name}"
    #         logger.info(f"Adding test case: {test_id}")
    #         test_cases.append(
    #             pytest.param(
    #                 details["module"],
    #                 func_name,
    #                 details["examples"],  # Pass all examples together
    #                 id=test_id,
    #             )
    #         )
    logger.info(f"Collected {len(test_cases)} test cases")
    return test_cases


@pytest.mark.parametrize("module_name,func_name,code_blocks", collect_docstring_tests())
@pytest.mark.asyncio_cooperative
@pytest.mark.langsmith
async def test_docstring_example(
    module_name: str | None, func_name: str, code_blocks: List[str]
):
    """Execute all docstring code blocks from a function in sequence, maintaining state."""
    if module_name is None:
        # Don't need to import anythign but we still share a context bcs reasons
        namespace = {
            "__name__": f"docstring_example_{func_name.replace('.', '_')}",
            "__file__": "README.md",
        }
    else:
        # Dynamically import the module
        module = importlib.import_module(module_name)

        # Find the function object inside the module
        obj = module
        func_name_ = (
            func_name[len(obj.__name__) :].lstrip(".")
            if func_name.startswith(obj.__name__)
            else func_name
        )
        for part in func_name_.split("."):
            obj = getattr(obj, part)

        namespace = {
            "__name__": f"docstring_example_{func_name.replace('.', '_')}",
            "__file__": getattr(module, "__file__", None),
            module_name.split(".")[-1]: module,
            func_name.split(".")[-1]: obj,
        }
    with ls.tracing_context(project_name="langmem_docstrings"):
        for i, code_block in enumerate(code_blocks):
            try:
                if "await " in code_block:
                    # For async blocks, we need to capture the locals after execution
                    wrapped_code = f"""
async def _test_docstring():
    global_ns = globals()
{textwrap.indent(code_block, '    ')}
    # Update namespace with all locals
    global_ns.update(locals())
"""
                    exec(wrapped_code, namespace, namespace)
                    await namespace["_test_docstring"]()
                else:
                    exec(code_block, namespace, namespace)

                # Log what was added to namespace
            except Exception as e:
                e.add_note(f"Error executing code block {i} for {func_name}: {e}")
                e.add_note(f"Code block contents:\n{code_block}")
                raise
