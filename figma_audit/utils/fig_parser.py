"""Parse Figma .fig files and convert to REST API format.

A .fig file is a ZIP archive containing canvas.fig (Kiwi binary format)
with the full design tree. This module decodes it and produces a dict
matching the structure returned by FigmaClient.get_file(), so existing
extraction code (_identify_screens, _extract_elements, etc.) works unchanged.

The Kiwi decoder is adapted from figformat/kiwi.py (MIT license, by Sketch).
See: https://github.com/nicolo-ribaudo/fig2sketch
"""

from __future__ import annotations

import codecs
import ctypes
import io
import logging
import struct
import zipfile
import zlib
from collections import OrderedDict
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Kiwi binary decoder (adapted from figformat/kiwi.py, MIT license)
# Only change: zstd → zstandard
# ---------------------------------------------------------------------------


class _KiwiReader:
    def __init__(self, reader: io.BufferedIOBase):
        self._reader = reader

    def byte(self) -> int:
        return self._reader.read(1)[0]

    def bool(self) -> bool:
        return self.byte() > 0

    def uint(self) -> int:
        value = 0
        for shift in range(0, 36, 7):
            b = self.byte()
            value |= (b & 127) << shift
            if b < 128:
                break
        return value

    def uint64(self) -> int:
        value = 0
        for shift in range(0, 64, 7):
            b = self.byte()
            value |= (b & 127) << shift
            if b < 128:
                break
        return value

    def int64(self) -> int:
        v = self.uint64()
        return ~(v >> 1) if v & 1 else v >> 1

    def float(self) -> float:
        b = self.byte()
        if b == 0:
            return 0.0
        bits = b | self.byte() << 8 | self.byte() << 16 | self.byte() << 24
        bits = (bits << 23) | (bits >> 9)
        return ctypes.c_float.from_buffer(ctypes.c_uint32(bits)).value

    def int(self) -> int:
        v = self.uint()
        return ~(v >> 1) if v & 1 else v >> 1

    def string(self) -> str:
        s = ""
        decoder = codecs.lookup("utf8").incrementaldecoder()
        while not (s and s[-1] == "\x00"):
            ch = ""
            while not ch:
                ch = decoder.decode(self._reader.read(1))
            s += ch
        return s[:-1]


class _KiwiSchema:
    def __init__(self, reader: io.BufferedIOBase):
        kw = _KiwiReader(reader)
        self.types: list[dict] = []
        for _ in range(kw.uint()):
            name = kw.string()
            kind = kw.byte()
            fields: OrderedDict[int, dict] = OrderedDict()
            for _ in range(kw.uint()):
                field = {
                    "name": kw.string(),
                    "type": kw.int(),
                    "array": kw.bool(),
                    "value": kw.uint(),
                }
                fields[field["value"]] = field
            self.types.append({"name": name, "kind": kind, "fields": fields})


class _KiwiDecoder:
    TYPES = ["bool", "byte", "int", "uint", "float", "string", "int64", "uint64"]

    def __init__(self, schema: _KiwiSchema, type_converters: dict):
        self.schema = schema
        self.type_converters = type_converters

    def decode(self, reader: io.BufferedIOBase, root: str) -> dict:
        kw = _KiwiReader(reader)
        root_type = next(t for t in self.schema.types if t["name"] == root)
        return self._decode_message(kw, root_type)

    def _decode_message(self, kw: _KiwiReader, typ: dict) -> dict:
        obj: dict = {}
        while (fid := kw.uint()) != 0:
            field = typ["fields"][fid]
            obj[field["name"]] = self._decode_type(kw, field["type"], field["array"])
        return obj

    def _decode_struct(self, kw: _KiwiReader, typ: dict) -> dict:
        return {
            f["name"]: self._decode_type(kw, f["type"], f["array"])
            for f in typ["fields"].values()
        }

    def _decode_enum(self, kw: _KiwiReader, typ: dict) -> str:
        value = kw.uint()
        return typ["fields"][value]["name"]

    def _decode_type(self, kw: _KiwiReader, type_id: int, array: bool) -> object:
        obj = self._decode_type_inner(kw, type_id, array)
        converter = self.type_converters.get(self.schema.types[type_id]["name"])
        if not array and converter:
            obj = converter(obj)
        return obj

    def _decode_type_inner(self, kw: _KiwiReader, type_id: int, array: bool) -> object:
        if array:
            return [self._decode_type(kw, type_id, False) for _ in range(kw.uint())]
        if type_id < 0:
            primitive = self.TYPES[~type_id]
            return getattr(kw, primitive)()
        typ = self.schema.types[type_id]
        kind = typ["kind"]
        if kind == 0:
            return self._decode_enum(kw, typ)
        elif kind == 1:
            return self._decode_struct(kw, typ)
        elif kind == 2:
            return self._decode_message(kw, typ)
        raise ValueError(f"Unknown Kiwi kind: {kind}")


def _kiwi_decode(reader: io.BufferedIOBase, type_converters: dict) -> dict:
    """Decode a Kiwi binary stream (canvas.fig content)."""
    ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
    MIN_VERSION = 15

    header = reader.read(12)
    version = struct.unpack("<I", header[8:12])[0]
    if version < MIN_VERSION:
        raise ValueError(f"Unsupported .fig version {version} (min {MIN_VERSION})")
    if version > 70:
        logger.info(f"fig version {version} is newer than tested (70), proceeding anyway")

    def _decompress(data: bytes) -> io.BytesIO:
        if data.startswith(ZSTD_MAGIC):
            import zstandard

            return io.BytesIO(zstandard.ZstdDecompressor().decompress(data))
        return io.BytesIO(zlib.decompress(data, wbits=-15))

    # Schema chunk
    size = struct.unpack("<I", reader.read(4))[0]
    schema_data = _decompress(reader.read(size))
    schema = _KiwiSchema(schema_data)

    # Data chunk
    size = struct.unpack("<I", reader.read(4))[0]
    data = _decompress(reader.read(size))

    return _KiwiDecoder(schema, type_converters).decode(data, "Message")


# ---------------------------------------------------------------------------
# Font weight mapping
# ---------------------------------------------------------------------------

_FONT_WEIGHT_MAP: dict[str, int] = {
    "thin": 100,
    "hairline": 100,
    "extralight": 200,
    "ultralight": 200,
    "extra light": 200,
    "ultra light": 200,
    "light": 300,
    "regular": 400,
    "normal": 400,
    "": 400,
    "medium": 500,
    "semibold": 600,
    "semi bold": 600,
    "demibold": 600,
    "demi bold": 600,
    "bold": 700,
    "extrabold": 800,
    "extra bold": 800,
    "ultrabold": 800,
    "ultra bold": 800,
    "black": 900,
    "heavy": 900,
}


def _style_to_weight(style_name: str) -> int:
    """Convert font style name to numeric weight (e.g. 'SemiBold' -> 600)."""
    normalized = style_name.lower().strip()
    if normalized in _FONT_WEIGHT_MAP:
        return _FONT_WEIGHT_MAP[normalized]
    # Remove "italic" / "oblique" suffix and retry
    for suffix in ("italic", "oblique"):
        normalized = normalized.replace(suffix, "").strip()
    if normalized in _FONT_WEIGHT_MAP:
        return _FONT_WEIGHT_MAP[normalized]
    return 400


# ---------------------------------------------------------------------------
# Node type mapping .fig → REST API
# ---------------------------------------------------------------------------

_TYPE_MAP: dict[str, str] = {
    "ROUNDED_RECTANGLE": "RECTANGLE",
    "SYMBOL": "COMPONENT",
}


# ---------------------------------------------------------------------------
# Transform / bounding box math
# ---------------------------------------------------------------------------

_IDENTITY = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]


def _mat_mul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    """Multiply two 3x3 matrices."""
    return [
        [
            a[0][0] * b[0][0] + a[0][1] * b[1][0] + a[0][2] * b[2][0],
            a[0][0] * b[0][1] + a[0][1] * b[1][1] + a[0][2] * b[2][1],
            a[0][0] * b[0][2] + a[0][1] * b[1][2] + a[0][2] * b[2][2],
        ],
        [
            a[1][0] * b[0][0] + a[1][1] * b[1][0] + a[1][2] * b[2][0],
            a[1][0] * b[0][1] + a[1][1] * b[1][1] + a[1][2] * b[2][1],
            a[1][0] * b[0][2] + a[1][1] * b[1][2] + a[1][2] * b[2][2],
        ],
        [0.0, 0.0, 1.0],
    ]


def _compute_bbox(
    node_transform: list[list[float]],
    node_size: dict,
    parent_abs_transform: list[list[float]],
) -> dict:
    """Compute absoluteBoundingBox from local transform + parent absolute transform."""
    abs_t = _mat_mul(parent_abs_transform, node_transform)
    w = node_size.get("x", 0)
    h = node_size.get("y", 0)

    # Transform the 4 corners through the absolute transform
    corners = [(0, 0), (w, 0), (w, h), (0, h)]
    abs_corners = [
        (abs_t[0][0] * cx + abs_t[0][1] * cy + abs_t[0][2],
         abs_t[1][0] * cx + abs_t[1][1] * cy + abs_t[1][2])
        for cx, cy in corners
    ]

    xs = [c[0] for c in abs_corners]
    ys = [c[1] for c in abs_corners]
    return {
        "x": min(xs),
        "y": min(ys),
        "width": max(xs) - min(xs),
        "height": max(ys) - min(ys),
    }


# ---------------------------------------------------------------------------
# Node conversion: .fig node → REST API node
# ---------------------------------------------------------------------------


def _convert_fills(fig_node: dict) -> list[dict]:
    """Convert fillPaints → fills (same structure, just rename)."""
    return fig_node.get("fillPaints", [])


def _convert_strokes(fig_node: dict) -> list[dict]:
    """Convert strokePaints → strokes."""
    return fig_node.get("strokePaints", [])


def _convert_text_style(fig_node: dict) -> dict:
    """Build REST-style 'style' dict from .fig text properties."""
    style: dict = {}
    font_name = fig_node.get("fontName", {})
    if font_name:
        style["fontFamily"] = font_name.get("family", "")
        style["fontPostScriptName"] = font_name.get("postscript", "")
        style["fontWeight"] = _style_to_weight(font_name.get("style", ""))

    if "fontSize" in fig_node:
        style["fontSize"] = fig_node["fontSize"]

    ls = fig_node.get("letterSpacing")
    if ls and isinstance(ls, dict):
        val = ls.get("value", 0)
        if ls.get("units") == "PERCENT" and "fontSize" in fig_node:
            val = fig_node["fontSize"] * val / 100
        style["letterSpacing"] = val

    lh = fig_node.get("lineHeight")
    if lh and isinstance(lh, dict):
        val = lh.get("value", 0)
        units = lh.get("units", "PIXELS")
        if units == "PERCENT" and "fontSize" in fig_node:
            val = fig_node["fontSize"] * val / 100
        elif units == "RAW" and "fontSize" in fig_node:
            val = fig_node["fontSize"] * val
        style["lineHeightPx"] = val

    return style


def _guid_to_id(guid: tuple) -> str:
    """Convert GUID tuple (sessionID, localID) to Figma-style node ID string."""
    if isinstance(guid, tuple) and len(guid) == 2:
        return f"{guid[0]}:{guid[1]}"
    return str(guid)


def _convert_node(
    fig_node: dict,
    children_of: dict[tuple, list[dict]],
    parent_abs_transform: list[list[float]],
) -> dict:
    """Convert a single .fig node to REST API format, recursively."""
    guid = fig_node.get("guid", (0, 0))
    fig_type = fig_node.get("type", "FRAME")
    rest_type = _TYPE_MAP.get(fig_type, fig_type)

    # Transform / bounding box
    node_transform = fig_node.get("transform", _IDENTITY)
    if not isinstance(node_transform, list):
        node_transform = _IDENTITY
    node_size = fig_node.get("size", {"x": 0, "y": 0})

    abs_transform = _mat_mul(parent_abs_transform, node_transform)
    bbox = _compute_bbox(node_transform, node_size, parent_abs_transform)

    rest_node: dict = {
        "id": _guid_to_id(guid),
        "name": fig_node.get("name", ""),
        "type": rest_type,
        "visible": fig_node.get("visible", True),
        "absoluteBoundingBox": bbox,
    }

    # Fills and strokes
    fills = _convert_fills(fig_node)
    if fills:
        rest_node["fills"] = fills
    strokes = _convert_strokes(fig_node)
    if strokes:
        rest_node["strokes"] = strokes
    if "strokeWeight" in fig_node:
        rest_node["strokeWeight"] = fig_node["strokeWeight"]

    # Background color (for FRAME / CANVAS)
    if fills:
        for f in fills:
            if f.get("type") == "SOLID" and f.get("visible", True):
                rest_node["backgroundColor"] = f.get("color", {})
                break

    # Corner radius
    if "cornerRadius" in fig_node:
        rest_node["cornerRadius"] = fig_node["cornerRadius"]
    for key in (
        "rectangleTopLeftCornerRadius",
        "rectangleTopRightCornerRadius",
        "rectangleBottomLeftCornerRadius",
        "rectangleBottomRightCornerRadius",
    ):
        if key in fig_node:
            rest_node[key] = fig_node[key]

    # Text properties
    if fig_type == "TEXT":
        text_data = fig_node.get("textData", {})
        rest_node["characters"] = text_data.get("characters", "")
        rest_node["style"] = _convert_text_style(fig_node)

    # Auto-layout → layoutMode
    stack_mode = fig_node.get("stackMode")
    if stack_mode:
        rest_node["layoutMode"] = stack_mode
        if "stackSpacing" in fig_node:
            rest_node["itemSpacing"] = fig_node["stackSpacing"]
        for fig_key, rest_key in (
            ("stackHorizontalPadding", "paddingLeft"),
            ("stackVerticalPadding", "paddingTop"),
            ("stackPaddingRight", "paddingRight"),
            ("stackPaddingBottom", "paddingBottom"),
        ):
            if fig_key in fig_node:
                rest_node[rest_key] = fig_node[fig_key]

    # Opacity / blend
    if "opacity" in fig_node:
        rest_node["opacity"] = fig_node["opacity"]

    # Effects (shadows, blurs)
    if "effects" in fig_node:
        rest_node["effects"] = fig_node["effects"]

    # Recursively convert children
    child_nodes = children_of.get(guid, [])
    if child_nodes:
        rest_node["children"] = [
            _convert_node(child, children_of, abs_transform)
            for child in child_nodes
            if child.get("visible", True)
        ]

    return rest_node


# ---------------------------------------------------------------------------
# Tree reconstruction from flat nodeChanges
# ---------------------------------------------------------------------------


def _build_tree(fig_data: dict) -> tuple[dict, dict[tuple, list[dict]]]:
    """Build parent→children mapping from flat nodeChanges array.

    Returns (root_node, children_of_mapping).
    """
    node_changes = fig_data.get("nodeChanges", [])

    id_map: dict[tuple, dict] = {}
    children_of: dict[tuple, list[dict]] = {}
    root = None

    for node in node_changes:
        guid = node.get("guid")
        if guid is None:
            continue
        id_map[guid] = node

        parent_index = node.get("parentIndex")
        if parent_index:
            parent_guid = parent_index.get("guid")
            if parent_guid is not None:
                children_of.setdefault(parent_guid, []).append(node)
        elif root is None:
            root = node

    # Sort children by position string for correct ordering
    for children in children_of.values():
        children.sort(key=lambda n: n.get("parentIndex", {}).get("position", ""))

    if root is None and node_changes:
        root = node_changes[0]

    return root, children_of


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_fig_file(fig_path: str | Path) -> dict:
    """Parse a .fig file and return a dict matching FigmaClient.get_file() format.

    The returned dict has the structure:
    {
        "name": "File Name",
        "lastModified": "",
        "version": "",
        "document": {
            "id": "0:0",
            "name": "Document",
            "type": "DOCUMENT",
            "children": [  # pages (CANVAS nodes)
                {
                    "id": "...",
                    "name": "Page Name",
                    "type": "CANVAS",
                    "children": [  # top-level frames (screens)
                        ...
                    ]
                }
            ]
        }
    }
    """
    fig_path = Path(fig_path)
    if not fig_path.exists():
        raise FileNotFoundError(f"File not found: {fig_path}")

    type_converters = {
        "GUID": lambda x: (x["sessionID"], x["localID"]),
        "Matrix": lambda m: [
            [m["m00"], m["m01"], m["m02"]],
            [m["m10"], m["m11"], m["m12"]],
            [0.0, 0.0, 1.0],
        ],
    }

    # Open and detect ZIP vs raw binary
    raw = fig_path.read_bytes()
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            with zf.open("canvas.fig") as canvas:
                fig_data = _kiwi_decode(canvas, type_converters)
    else:
        fig_data = _kiwi_decode(io.BytesIO(raw), type_converters)

    # Build tree
    root, children_of = _build_tree(fig_data)
    if root is None:
        raise ValueError("No root node found in .fig file")

    # Convert to REST API format
    document = _convert_node(root, children_of, _IDENTITY)

    # Filter out internal-only pages (component pages)
    if "children" in document:
        document["children"] = [
            page for page in document["children"]
            if not _is_internal_page(page, children_of, root)
        ]

    return {
        "name": fig_path.stem,
        "lastModified": "",
        "version": "",
        "document": document,
    }


def _is_internal_page(page_node: dict, children_of: dict, root: dict) -> bool:
    """Check if a CANVAS page is internal (components page)."""
    # In the original .fig data, internal pages have internalOnly=True
    # We need to check the original node data
    page_id = page_node.get("id", "")
    # Parse back the GUID
    parts = page_id.split(":")
    if len(parts) == 2:
        try:
            guid = (int(parts[0]), int(parts[1]))
            root_guid = root.get("guid", (0, 0))
            for child in children_of.get(root_guid, []):
                if child.get("guid") == guid:
                    return bool(child.get("internalOnly", False))
        except (ValueError, TypeError):
            pass
    return False
