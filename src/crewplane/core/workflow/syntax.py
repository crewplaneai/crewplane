from __future__ import annotations

import re

NODE_ID_PATTERN = re.compile(r"^[a-z0-9._-]+$")
INPUT_SOURCE_PATTERN = re.compile(r"^\{\{\s*file:[^}]+\}\}$")
NODE_ARTIFACT_REFERENCE_PATTERN = re.compile(
    r"\{\{\s*([a-z0-9._-]+)\.([A-Za-z0-9_-]+)\s*\}\}"
)
TEMPLATE_TOKEN_PATTERN = re.compile(r"\{\{[^{}]*\}\}")
KEY_VALUE_TEMPLATE_PATTERN = re.compile(r"\{\{([a-zA-Z_]+):([^}]+)\}\}")
PARAM_TEMPLATE_PATTERN = re.compile(r"\{\{\s*param:([^}]+)\}\}")
SUPPORTED_TEMPLATE_TYPES = frozenset({"env", "file", "var"})
COMPOSITION_ONLY_TEMPLATE_TYPES = frozenset({"param"})
