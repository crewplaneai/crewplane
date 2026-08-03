from __future__ import annotations

import ast
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "src"
TESTS_ROOT = REPO_ROOT / "tests"


@dataclass(frozen=True)
class ForbiddenImportRule:
    name: str
    roots: tuple[Path, ...]
    forbidden_prefixes: tuple[str, ...]


@dataclass(frozen=True)
class PrivateApiRule:
    name: str
    roots: tuple[Path, ...]
    allowed_references: frozenset[str] = frozenset()


@dataclass(frozen=True)
class AllowedTextReference:
    path: Path
    term: str
    reason: str


@dataclass(frozen=True)
class ForbiddenTextRule:
    name: str
    paths: tuple[Path, ...]
    forbidden_terms: frozenset[str]
    allowed_references: tuple[AllowedTextReference, ...] = ()


def python_files(root: Path) -> tuple[Path, ...]:
    if root.is_file():
        return (root,) if root.suffix == ".py" else ()
    return tuple(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def parse_python(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def walk_ast(node: ast.AST) -> Iterator[ast.AST]:
    todo: deque[ast.AST] = deque([node])
    while todo:
        current = todo.popleft()
        yield current
        for field in current._fields:
            value = getattr(current, field, None)
            if isinstance(value, ast.AST):
                todo.append(value)
            elif isinstance(value, list):
                todo.extend(child for child in value if isinstance(child, ast.AST))


def call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def expression_chain(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        return (*expression_chain(node.value), node.attr)
    return ()


def import_from_module_name(path: Path, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""

    module_parts = module_name_for_path(path).split(".")
    package_parts = module_parts[:-1]
    keep_count = max(0, len(package_parts) - node.level + 1)
    base_parts = package_parts[:keep_count]
    if node.module:
        base_parts.extend(node.module.split("."))
    return ".".join(base_parts)


def find_forbidden_imports(rule: ForbiddenImportRule) -> list[str]:
    offenders: list[str] = []
    for root in rule.roots:
        for path in python_files(root):
            for node in walk_ast(parse_python(path)):
                for imported_module in imported_modules(path, node):
                    if any(
                        imported_module == prefix
                        or imported_module.startswith(f"{prefix}.")
                        for prefix in rule.forbidden_prefixes
                    ):
                        offenders.append(offender(path, node.lineno, imported_module))
    return offenders


def find_private_imports(rule: PrivateApiRule) -> list[str]:
    offenders: list[str] = []
    for root in rule.roots:
        for path in python_files(root):
            for node in walk_ast(parse_python(path)):
                references = private_import_references(path, node)
                offenders.extend(
                    offender(path, node.lineno, reference)
                    for reference in references
                    if reference not in rule.allowed_references
                )
    return offenders


def find_private_attribute_access(rule: PrivateApiRule) -> list[str]:
    offenders: list[str] = []
    for root in rule.roots:
        for path in python_files(root):
            module = parse_python(path)
            imported_aliases = imported_repo_aliases(path, module)
            for node in walk_ast(module):
                if not isinstance(node, ast.Attribute):
                    continue
                reference = ".".join(expression_chain(node))
                if (
                    is_single_underscore_name(node.attr)
                    and is_repo_import_expression(node.value, imported_aliases)
                    and reference not in rule.allowed_references
                ):
                    offenders.append(offender(path, node.lineno, reference))
    return offenders


def find_private_patch_targets(rule: PrivateApiRule) -> list[str]:
    offenders: list[str] = []
    for root in rule.roots:
        for path in python_files(root):
            module = parse_python(path)
            imported_aliases = imported_repo_aliases(path, module)
            for node in walk_ast(module):
                if not isinstance(node, ast.Call):
                    continue
                target = private_patch_target(node, imported_aliases)
                if target is not None and target not in rule.allowed_references:
                    offenders.append(offender(path, node.lineno, target))
    return offenders


def find_forbidden_text(rule: ForbiddenTextRule) -> list[str]:
    allowed = {
        (reference.path.resolve(), reference.term): reference.reason
        for reference in rule.allowed_references
    }
    if any(not reason.strip() for reason in allowed.values()):
        raise ValueError(f"Text allowlist reasons must be non-empty for {rule.name}.")

    offenders: list[str] = []
    for path in text_rule_files(rule.paths):
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            offenders.extend(
                offender(path, line_number, term)
                for term in rule.forbidden_terms
                if term in line and (path.resolve(), term) not in allowed
            )
    return offenders


def offender(path: Path, line: int, detail: str | None = None) -> str:
    location = f"{path.relative_to(REPO_ROOT)}:{line}"
    return f"{location}: {detail}" if detail else location


def imported_modules(path: Path, node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return (import_from_module_name(path, node),)
    return ()


def text_rule_files(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    files: list[Path] = []
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(f"Text rule path does not exist: {path}")
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(
                candidate for candidate in path.rglob("*") if candidate.is_file()
            )
    return tuple(files)


def module_name_for_path(path: Path) -> str:
    if path.is_relative_to(SRC_ROOT):
        return ".".join(path.relative_to(SRC_ROOT).with_suffix("").parts)
    if path.is_relative_to(TESTS_ROOT):
        relative = path.relative_to(TESTS_ROOT).with_suffix("")
        return ".".join(("tests", *relative.parts))
    raise ValueError(f"Unsupported Python path: {path}")


def is_single_underscore_name(name: str) -> bool:
    return name.startswith("_") and not name.startswith("__")


def is_repo_module_name(module_name: str) -> bool:
    return (
        module_name == "crewplane"
        or module_name.startswith("crewplane.")
        or module_name == "tests"
        or module_name.startswith("tests.")
    )


def imported_repo_aliases(path: Path, module: ast.Module) -> set[str]:
    aliases: set[str] = set()
    for node in walk_ast(module):
        if isinstance(node, ast.Import):
            aliases.update(
                alias.asname or alias.name.split(".")[0]
                for alias in node.names
                if is_repo_module_name(alias.name)
            )
        elif isinstance(node, ast.ImportFrom):
            module_name = import_from_module_name(path, node)
            if is_repo_module_name(module_name):
                aliases.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name != "*" and (alias.asname or alias.name)[:1].islower()
                )
    return aliases


def is_repo_import_expression(node: ast.AST, imported_aliases: set[str]) -> bool:
    chain = expression_chain(node)
    return bool(chain) and (
        chain[0] in imported_aliases or chain[0] in {"crewplane", "tests"}
    )


def private_import_references(path: Path, node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(
            alias.name
            for alias in node.names
            if any(is_single_underscore_name(part) for part in alias.name.split("."))
        )
    if not isinstance(node, ast.ImportFrom):
        return ()
    module_name = import_from_module_name(path, node)
    references = [
        module_name
        for part in module_name.split(".")
        if is_single_underscore_name(part)
    ]
    references.extend(
        f"{module_name}.{alias.name}"
        for alias in node.names
        if is_single_underscore_name(alias.name)
    )
    return tuple(references)


def private_patch_target(
    node: ast.Call,
    imported_aliases: set[str],
) -> str | None:
    call_chain = expression_chain(node.func)
    if call_chain[-1:] == ("patch",) and node.args:
        target = string_value(node.args[0])
        if target is not None and has_private_dotted_part(target):
            return target
    if (
        call_chain[-1:] == ("setattr",)
        and len(node.args) >= 2
        and is_repo_import_expression(node.args[0], imported_aliases)
    ):
        return private_name_argument(node.args[1])
    if (
        call_chain[-2:] == ("patch", "object")
        and len(node.args) >= 2
        and is_repo_import_expression(node.args[0], imported_aliases)
    ):
        return private_name_argument(node.args[1])
    return None


def private_name_argument(node: ast.AST) -> str | None:
    name = string_value(node)
    return name if name is not None and is_single_underscore_name(name) else None


def string_value(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def has_private_dotted_part(value: str) -> bool:
    return any(is_single_underscore_name(part) for part in value.split("."))
