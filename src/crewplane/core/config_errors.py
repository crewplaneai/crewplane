"""Value-free diagnostics at the configuration loading boundary."""

from types import UnionType
from typing import Annotated, Union, get_args, get_origin

import yaml
from pydantic import BaseModel, ValidationError

_SAFE_CUSTOM_MESSAGES = frozenset(
    {
        "cli_cmd must contain at least one token",
        "cli_cmd cannot contain blank tokens",
        "extra_args cannot contain blank tokens",
        "command argument cannot be blank",
        "prompt_transport_arg is required when prompt_transport is argv",
        "prompt_transport_arg cannot be blank",
        "prompt_transport must be one of: stdin, argv",
        "provider_kind must match [a-z][a-z0-9_-]*",
        "exit codes must be between 0 and 255",
        "invocation timeout values must be greater than 0",
        "implementation must be a non-empty string",
        "allowed_template_paths cannot contain blank paths",
        "agent names must be strings",
        "settings.workspace.cache_root must be absolute when workspace isolation is enabled",
    }
)
_SAFE_TYPE_MESSAGES = {
    "missing": "Field required",
    "extra_forbidden": "Extra inputs are not permitted",
    "string_type": "Expected a string",
    "int_type": "Expected an integer",
    "int_parsing": "Expected an integer",
    "int_from_float": "Expected an integer",
    "float_type": "Expected a number",
    "float_parsing": "Expected a number",
    "finite_number": "Expected a finite number",
    "bool_type": "Expected a boolean",
    "bool_parsing": "Expected a boolean",
    "dict_type": "Expected a mapping",
    "list_type": "Expected a list",
    "greater_than": "Value must exceed the configured minimum",
    "greater_than_equal": "Value must meet the configured minimum",
    "literal_error": "Unsupported option",
}


def model_error_message(error: ValidationError, schema: type[BaseModel]) -> str:
    details: list[str] = []
    for record in error.errors(
        include_input=False, include_context=False, include_url=False
    ):
        location = _safe_location(schema, record["loc"])
        message = _SAFE_TYPE_MESSAGES.get(record["type"], "Invalid configuration value")
        custom = record["msg"].removeprefix("Value error, ")
        if custom in _SAFE_CUSTOM_MESSAGES:
            message = custom
        elif record["loc"] == ("version",):
            message = "Unsupported config version; run 'crewplane init' to regenerate config files"
        details.append(f"{location}: {message}")
    return "Invalid configuration: " + "; ".join(details)


def _safe_location(schema: type[BaseModel], location: tuple[int | str, ...]) -> str:
    annotation: object = schema
    parts: list[str] = []
    for segment in location:
        annotation = _unwrap_annotation(annotation)
        origin = get_origin(annotation)
        if isinstance(segment, int) and origin in (list, tuple):
            parts.append(str(segment))
            annotation = get_args(annotation)[0]
        elif (
            isinstance(segment, str)
            and isinstance(annotation, type)
            and issubclass(annotation, BaseModel)
        ):
            field = annotation.model_fields.get(segment)
            parts.append(segment if field is not None else "<key>")
            annotation = field.annotation if field is not None else None
        else:
            parts.append("<key>")
            annotation = get_args(annotation)[1] if origin is dict else None
    return ".".join(parts) or "configuration"


def _unwrap_annotation(annotation: object) -> object:
    if get_origin(annotation) is Annotated:
        return _unwrap_annotation(get_args(annotation)[0])
    if get_origin(annotation) in (Union, UnionType):
        alternatives = [item for item in get_args(annotation) if item is not type(None)]
        return _unwrap_annotation(alternatives[0]) if len(alternatives) == 1 else None
    return annotation


def yaml_error_message(error: yaml.YAMLError) -> str:
    category = "Invalid configuration YAML"
    if isinstance(error, yaml.constructor.ConstructorError):
        category += " (construction error)"
    elif isinstance(error, yaml.parser.ParserError):
        category += " (syntax error)"
    elif isinstance(error, yaml.scanner.ScannerError):
        category += " (scanner error)"
    if isinstance(error, yaml.MarkedYAMLError):
        mark = error.problem_mark or error.context_mark
        if mark is not None:
            category += f" at line {mark.line + 1}, column {mark.column + 1}"
    return category + "."
