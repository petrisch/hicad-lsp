from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

import tree_sitter
from lsprotocol import types
from pygls.lsp.server import LanguageServer
from tree_sitter import Language, Parser

DEFAULT_LANGUAGE_PATH = os.environ.get(
    "HICAD_TS_LIB",
    os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "tree-sitter-hicad",
            "build",
            "hicad.dll",
        )
    ),
)


class TS_LIB:
    def __init__(self, path: str):
        self.path = path


ts_lib = TS_LIB(DEFAULT_LANGUAGE_PATH)

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


class ParsedDocument:
    def __init__(self, uri, source, tree):
        self.uri = uri
        self.source = source
        self.source_bytes = source.encode("utf-8")
        self.tree = tree


PARSED_DOCUMENTS = {}

LANGUAGE_NAME = "hicad"

# Keep all native Tree-sitter objects alive.
_LIB_HANDLE = None
_LANGUAGE_CAPSULE = None
_LANGUAGE = None
_parser = None
_parser_lib_path = None


SERVER_NAME = "static-resource-language-server"
SERVER_VERSION = "0.1.0"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUILTIN_VARIABLES_FILE = PROJECT_ROOT / "data" / "builtin_variables.json"
HICAD_MENUS_FILE = PROJECT_ROOT / "data" / "hicad_menus.json"

WORD_PATTERN = re.compile(r"[$@A-Za-z_][@$A-Za-z0-9_]*")


class StaticResourceLanguageServer(LanguageServer):
    def __init__(self) -> None:
        super().__init__(SERVER_NAME, SERVER_VERSION)
        self.resources: dict[str, dict[str, Any]] = {}
        self.resource_index: dict[str, dict[str, Any]] = {}
        self.menu_index: dict = {}

    def load_resources(self, path: Path = BUILTIN_VARIABLES_FILE) -> None:
        try:
            with path.open("r", encoding="utf-8") as resource_file:
                data = json.load(resource_file)
        except FileNotFoundError:
            logger.error("Resource file not found: %s", path)
            self.resources = {}
            self.resource_index = {}
            return
        except json.JSONDecodeError as error:
            logger.error(
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
            logger.error("The top level of %s must be a JSON object.", path)
            self.resources = {}
            self.resource_index = {}
            return

        resources: dict[str, dict[str, Any]] = {}
        index: dict[str, dict[str, Any]] = {}

        for resource_key, resource_value in data.items():
            if not isinstance(resource_key, str):
                logger.warning("Ignoring non-string resource key: %r", resource_key)
                continue

            if not isinstance(resource_value, dict):
                logger.warning(
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

        logger.info(
            "Loaded %d resources with %d lookup names from %s",
            len(resources),
            len(index),
            path,
        )

    def build_menu_index(_self, menus):
        index = {}

        def walk(node):
            for child in node.get("submenues", []):
                index[(child.get("button_nr"), node.get("menue_nr"))] = {
                    "parent": node,
                    "child": child,
                }

                walk(child)

        for menu in menus:
            walk(menu)

        return index

    def load_menus(self):
        try:
            with open(HICAD_MENUS_FILE, "r", encoding="utf-8") as file_handle:
                data = json.load(file_handle)

            self.menu_index = self.build_menu_index(data["hicad_menues"])

            logger.info(
                "Loaded %d menu entries",
                len(self.menu_index),
            )

        except Exception:
            logger.exception("Failed to load menu database")


server = StaticResourceLanguageServer()


def get_menu_hover_data(node, parsed_document):

    logger.info(f"Get hover data for menu on node: {node}")
    menu_node = find_parent(
        node,
        "menu",
    )

    if menu_node is None:
        return None

    logger.info(f"Found parent menu: {menu_node}")

    menu_nr = None
    button_nr = None

    for child in menu_node.named_children:
        if child.type == "menu_index":
            logger.info(f"Looking for menu_index: {child}; in {parsed_document}")
            menu_nr = node_text(
                child,
                parsed_document,
            )

        elif child.type == "menu_code":
            button_nr = node_text(
                child,
                parsed_document,
            )
    logger.info(f"Found menu_nr: {menu_nr}; and button_nr: {button_nr}")

    if menu_nr is None:
        return None

    if button_nr is None:
        return None

    try:
        return (
            int(menu_nr),
            int(button_nr),
            menu_node,
        )

    except ValueError:
        return None


def get_node_from_tree(
    parsed_document: ParsedDocument,
    line_number: int,
    character: int,
):
    point = (line_number, character)

    return parsed_document.tree.root_node.descendant_for_point_range(
        point,
        point,
    )


def find_parent(node, target_type):
    current = node

    while current is not None:
        if current.type == target_type:
            return current

        current = current.parent

    return None


def node_text(node, parsed_document):
    logger.info(f"Extracting node from: {node.start_byte}: to {node.end_byte}")
    logger.info(
        f"Returning text from node: {
            parsed_document.source_bytes[node.start_byte : node.end_byte]
        }"
    )
    return parsed_document.source_bytes[node.start_byte : node.end_byte].decode("utf-8")


def get_variable_name_from_node(
    node,
    parsed_document,
):
    current = node

    while current is not None:
        if current.type in {
            "num_sys_var",
            "char_sys_var",
        }:
            return node_text(
                current,
                parsed_document,
            )

        current = current.parent

    return None


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


def format_menu_hover(menu_result):
    parent = menu_result["parent"]
    child = menu_result["child"]

    return "\n".join(
        [
            f"# {child['display_name']}",
            "",
            f"**Menu:** {parent['display_name']}",
            f"**Button:** {child['button_nr']}",
            f"**Menu Nr:** {parent['menue_nr']}",
        ]
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
def initialized(params):
    logger.info("Hi from hicad-lsp")
    server.load_resources()
    server.load_menus()


@server.feature(types.TEXT_DOCUMENT_DID_OPEN)
def did_open(ls, params):
    parser = load_language_parser(ts_lib.path)
    source = params.text_document.text
    tree = parser.parse(source.encode())

    PARSED_DOCUMENTS[params.text_document.uri] = ParsedDocument(
        uri=params.text_document.uri,
        source=source,
        tree=tree,
    )
    logger.info("Updated parsetree")


@server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def did_change(ls, params):
    document = ls.workspace.get_text_document(params.text_document.uri)
    parser = load_language_parser(ts_lib.path)
    source = document.source
    tree = parser.parse(source.encode())

    PARSED_DOCUMENTS[params.text_document.uri] = ParsedDocument(
        uri=params.text_document.uri,
        source=source,
        tree=tree,
    )
    logger.info("Updated parsetree")


@server.feature(types.TEXT_DOCUMENT_HOVER)
def hover(params: types.HoverParams) -> types.Hover | None:
    logger.info("Try to hover")

    document = server.workspace.get_text_document(params.text_document.uri)

    line_number = params.position.line
    character = params.position.character
    logger.info(f"Looking up at character: {character}; on line: {line_number}")

    # Out of bound check
    if line_number < 0 or line_number >= len(document.lines):
        return None

    line = document.lines[line_number]
    logger.info(f"Looking up line: {line}")

    parsed_document = PARSED_DOCUMENTS.get(params.text_document.uri)

    if parsed_document is None:
        return None

    node = get_node_from_tree(
        parsed_document,
        line_number,
        character,
    )

    logger.info(
        "Node type: %s",
        getattr(node, "type", None),
    )

    if node is not None:
        menu_data = get_menu_hover_data(
            node,
            parsed_document,
        )

        if menu_data:
            menu_nr, button_nr, _ = menu_data

            logger.info(f"Getting menu_data for: {menu_data}; in: {server.menu_index}")
            menu_result = server.menu_index.get(
                (
                    menu_nr,
                    button_nr,
                )
            )

            if menu_result:
                return types.Hover(
                    contents=types.MarkupContent(
                        kind=types.MarkupKind.Markdown,
                        value=format_menu_hover(menu_result),
                    )
                )

    result = get_word_at_position(
        line,
        character,
    )

    if result:
        word, start_character, end_character = result

        resource = server.resource_index.get(word)

        if resource:
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

    if node is not None:
        variable_name = get_variable_name_from_node(
            node,
            parsed_document.source,
        )

        if variable_name:
            resource = server.resource_index.get(variable_name)

            if resource:
                return types.Hover(
                    contents=types.MarkupContent(
                        kind=types.MarkupKind.Markdown,
                        value=format_hover(resource),
                    )
                )


def validate_language_abi(
    language: Language,
) -> None:
    abi_version = int(language.abi_version)
    minimum_abi = tree_sitter.MIN_COMPATIBLE_LANGUAGE_VERSION
    maximum_abi = tree_sitter.LANGUAGE_VERSION

    if minimum_abi <= abi_version <= maximum_abi:
        return

    raise RuntimeError(
        "Incompatible Tree-sitter language ABI: "
        f"language={abi_version}, "
        f"supported={minimum_abi}..{maximum_abi}"
    )


def load_language_parser(
    lib_path: str,
) -> Parser:
    global _LIB_HANDLE
    global _LANGUAGE_CAPSULE
    global _LANGUAGE
    global _parser
    global _parser_lib_path

    normalized_path = os.path.abspath(lib_path)

    if _parser is not None:
        if _parser_lib_path != normalized_path:
            raise RuntimeError(
                "A different Tree-sitter parser library is already loaded: "
                f"{_parser_lib_path}"
            )

        return _parser

    if not os.path.exists(normalized_path):
        raise FileNotFoundError(
            f"Tree-sitter parser library not found: {normalized_path}"
        )

    _LIB_HANDLE = ctypes.CDLL(normalized_path)
    symbol_name = f"tree_sitter_{LANGUAGE_NAME}"

    try:
        language_function = getattr(
            _LIB_HANDLE,
            symbol_name,
        )
    except AttributeError as exc:
        raise RuntimeError(
            f"Parser DLL does not export symbol '{symbol_name}'"
        ) from exc

    language_function.argtypes = []
    language_function.restype = ctypes.c_void_p

    language_ptr = language_function()

    if not language_ptr:
        raise RuntimeError("Failed to get Tree-sitter language pointer")

    py_capsule_new = ctypes.pythonapi.PyCapsule_New
    py_capsule_new.restype = ctypes.py_object
    py_capsule_new.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
    ]

    _LANGUAGE_CAPSULE = py_capsule_new(
        language_ptr,
        b"tree_sitter.Language",
        None,
    )

    _LANGUAGE = Language(_LANGUAGE_CAPSULE)
    validate_language_abi(_LANGUAGE)

    _parser = Parser(_LANGUAGE)
    _parser_lib_path = normalized_path

    return _parser


def main() -> None:

    argument_parser = argparse.ArgumentParser()

    argument_parser.add_argument(
        "--ts-lib",
        default=ts_lib,
        help="Path to the Tree-sitter parser library",
    )

    args = argument_parser.parse_args()

    ts_lib.path = args.ts_lib

    # Load once before initialization as well. This makes the resources
    # immediately available and reports malformed JSON during startup.
    server.load_resources()
    logger.info("Available keys: %s", list(server.resource_index.keys()))

    # Communicate with the editor using stdin/stdout.
    server.start_io()


if __name__ == "__main__":
    main()
