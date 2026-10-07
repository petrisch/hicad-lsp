from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

from lsprotocol import types
from pygls.lsp.server import LanguageServer


SERVER_NAME = "static-resource-language-server"
SERVER_VERSION = "0.1.0"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUILTIN_VARIABLES_FILE = PROJECT_ROOT / "data" / "builtin_variables.json"

WORD_PATTERN = re.compile(r"[$@A-Za-z_][@$A-Za-z0-9_]*")


class StaticResourceLanguageServer(LanguageServer):
    def __init__(self) -> None:
        super().__init__(SERVER_NAME, SERVER_VERSION)
        self.resources: dict[str, dict[str, Any]] = {}
        self.resource_index: dict[str, dict[str, Any]] = {}

    def load_resources(self, path: Path = BUILTIN_VARIABLES_FILE) -> None:
        try:
            with path.open("r", encoding="utf-8") as resource_file:
                data = json.load(resource_file)
        except FileNotFoundError:
            logging.error("Resource file not found: %s", path)
            self.resources = {}
            self.resource_index = {}
            return
        except json.JSONDecodeError as error:
            logging.error(
                "Invalid JSON in %s at line %s, column %s: %s",
                path,
                error.lineno,
                error.colno,
                error.msg,
            )
            self.resources = {}
            self.resource_index = {}
            return

        if not isinstance(data, dict):
            logging.error("The top level of %s must be a JSON object.", path)
            self.resources = {}
            self.resource_index = {}
            return

        resources: dict[str, dict[str, Any]] = {}
        index: dict[str, dict[str, Any]] = {}

        for resource_key, resource_value in data.items():
            if not isinstance(resource_key, str):
                logging.warning("Ignoring non-string resource key: %r", resource_key)
                continue

            if not isinstance(resource_value, dict):
                logging.warning(
                    "Ignoring resource %r because its value is not an object.",
                    resource_key,
                )
                continue

            resource = dict(resource_value)
            resource["_resource_key"] = resource_key

            resources[resource_key] = resource
            index[resource_key] = resource

            resource_name = resource.get("name")
            if isinstance(resource_name, str) and resource_name:
                index[resource_name] = resource

        self.resources = resources
        self.resource_index = index["builtin_variables"]

        logging.info(
            "Loaded %d resources with %d lookup names from %s",
            len(resources),
            len(index),
            path,
        )


server = StaticResourceLanguageServer()


def get_word_at_position(
    line: str,
    character: int,
) -> tuple[str, int, int] | None:
    """
    Return the word at the given UTF-16-compatible ASCII position.

    Resource identifiers are restricted to ASCII-style identifiers here,
    so Python string offsets and LSP character offsets are equivalent.
    """
    if character < 0:
        return None

    character = min(character, len(line))

    for match in WORD_PATTERN.finditer(line):
        start, end = match.span()

        if start <= character < end:
            return match.group(0), start, end

        # Editors may send the position immediately after the final character.
        if character == end and end > start:
            return match.group(0), start, end

    return None


def escape_markdown(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("*", "\\*")
        .replace("_", "\\_")
    )


def format_hover(resource: dict[str, Any]) -> str:
    resource_key = str(resource.get("_resource_key", ""))
    resource_identifier = str(resource.get("identifier", resource_key))
    description = str(resource.get("description", "No description available."))

    content = [
        f"### `{escape_markdown(resource_identifier)}`",
        "",
        description,
    ]

    if resource_key and resource_key != resource_identifier:
        content.extend(
            [
                "",
                f"**Resource key:** `{escape_markdown(resource_key)}`",
            ]
        )

    resource_type = resource.get("type")
    if resource_type is not None:
        content.extend(
            [
                "",
                f"**Type:** `{escape_markdown(str(resource_type))}`",
            ]
        )

    default_value = resource.get("default")
    if default_value is not None:
        if isinstance(default_value, (dict, list)):
            rendered_default = json.dumps(
                default_value,
                ensure_ascii=False,
                indent=2,
            )

            content.extend(
                [
                    "",
                    "**Default:**",
                    "",
                    "```json",
                    rendered_default,
                    "```",
                ]
            )
        else:
            content.extend(
                [
                    "",
                    f"**Default:** `{escape_markdown(str(default_value))}`",
                ]
            )

    return "\n".join(content)


@server.feature(types.INITIALIZED)
def initialized(params: types.InitializedParams) -> None:
    logging.info("Hi from hicad-lsp")
    server.load_resources()


@server.feature(types.TEXT_DOCUMENT_HOVER)
def hover(params: types.HoverParams) -> types.Hover | None:
    logging.info("Hi from hover")

    document = server.workspace.get_text_document(params.text_document.uri)

    line_number = params.position.line
    character = params.position.character
    logging.info(f"Looking up at character: {character}; on line: {line_number}")

    if line_number < 0 or line_number >= len(document.lines):
        return None

    line = document.lines[line_number]
    logging.info(f"Looking up line: {line}")
    result = get_word_at_position(line, character)

    logging.info(f"Resulting word: {result}")

    if result is None:
        return None

    word, start_character, end_character = result

    resource = server.resource_index.get(word)

    if resource is None:
        return None

    return types.Hover(
        contents=types.MarkupContent(
            kind=types.MarkupKind.Markdown,
            value=format_hover(resource),
        ),
        range=types.Range(
            start=types.Position(
                line=line_number,
                character=start_character,
            ),
            end=types.Position(
                line=line_number,
                character=end_character,
            ),
        ),
    )


def main() -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # Load once before initialization as well. This makes the resources
    # immediately available and reports malformed JSON during startup.
    server.load_resources()
    logging.info("Available keys: %s", list(server.resource_index.keys()))

    # Communicate with the editor using stdin/stdout.
    server.start_io()


if __name__ == "__main__":
    main()
