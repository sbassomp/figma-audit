"""Tests for the .fig file parser and REST API converter."""

from figma_audit.utils.fig_parser import (
    _IDENTITY,
    _TYPE_MAP,
    _build_tree,
    _compute_bbox,
    _convert_node,
    _mat_mul,
    _style_to_weight,
)


class TestFontWeight:
    def test_regular(self):
        assert _style_to_weight("Regular") == 400

    def test_bold(self):
        assert _style_to_weight("Bold") == 700

    def test_semibold(self):
        assert _style_to_weight("SemiBold") == 600

    def test_light(self):
        assert _style_to_weight("Light") == 300

    def test_bold_italic(self):
        assert _style_to_weight("Bold Italic") == 700

    def test_thin(self):
        assert _style_to_weight("Thin") == 100

    def test_black(self):
        assert _style_to_weight("Black") == 900

    def test_medium(self):
        assert _style_to_weight("Medium") == 500

    def test_unknown_defaults_400(self):
        assert _style_to_weight("FancyCustom") == 400

    def test_empty_string(self):
        assert _style_to_weight("") == 400

    def test_extra_bold(self):
        assert _style_to_weight("ExtraBold") == 800


class TestTypeMapping:
    def test_rounded_rectangle(self):
        assert _TYPE_MAP["ROUNDED_RECTANGLE"] == "RECTANGLE"

    def test_symbol(self):
        assert _TYPE_MAP["SYMBOL"] == "COMPONENT"

    def test_frame_not_remapped(self):
        assert "FRAME" not in _TYPE_MAP


class TestMatrixMul:
    def test_identity(self):
        result = _mat_mul(_IDENTITY, _IDENTITY)
        assert result == _IDENTITY

    def test_translation(self):
        translate = [[1, 0, 100], [0, 1, 200], [0, 0, 1]]
        result = _mat_mul(_IDENTITY, translate)
        assert result[0][2] == 100
        assert result[1][2] == 200

    def test_chained_translations(self):
        t1 = [[1, 0, 10], [0, 1, 20], [0, 0, 1]]
        t2 = [[1, 0, 30], [0, 1, 40], [0, 0, 1]]
        result = _mat_mul(t1, t2)
        assert result[0][2] == 40  # 10 + 30
        assert result[1][2] == 60  # 20 + 40


class TestBoundingBox:
    def test_no_rotation(self):
        transform = [[1, 0, 50], [0, 1, 100], [0, 0, 1]]
        size = {"x": 390, "y": 844}
        bbox = _compute_bbox(transform, size, _IDENTITY)
        assert bbox["x"] == 50
        assert bbox["y"] == 100
        assert bbox["width"] == 390
        assert bbox["height"] == 844

    def test_with_parent_offset(self):
        parent = [[1, 0, 10], [0, 1, 20], [0, 0, 1]]
        node = [[1, 0, 5], [0, 1, 5], [0, 0, 1]]
        size = {"x": 100, "y": 50}
        bbox = _compute_bbox(node, size, parent)
        assert bbox["x"] == 15  # 10 + 5
        assert bbox["y"] == 25  # 20 + 5
        assert bbox["width"] == 100
        assert bbox["height"] == 50

    def test_90_degree_rotation(self):
        # 90 degree clockwise rotation
        transform = [[0, 1, 0], [-1, 0, 0], [0, 0, 1]]
        size = {"x": 100, "y": 50}
        bbox = _compute_bbox(transform, size, _IDENTITY)
        # Rotated: width and height swap
        assert abs(bbox["width"] - 50) < 0.01
        assert abs(bbox["height"] - 100) < 0.01

    def test_zero_size(self):
        transform = [[1, 0, 10], [0, 1, 20], [0, 0, 1]]
        size = {"x": 0, "y": 0}
        bbox = _compute_bbox(transform, size, _IDENTITY)
        assert bbox["width"] == 0
        assert bbox["height"] == 0


class TestFillsConversion:
    def test_solid_fill_passthrough(self):
        fig_node = {
            "guid": (1, 100),
            "type": "RECTANGLE",
            "name": "bg",
            "fillPaints": [
                {
                    "type": "SOLID",
                    "visible": True,
                    "color": {"r": 0.2, "g": 0.5, "b": 1.0, "a": 1.0},
                }
            ],
            "transform": _IDENTITY,
            "size": {"x": 100, "y": 50},
        }
        children_of = {}
        rest = _convert_node(fig_node, children_of, _IDENTITY)
        assert rest["fills"][0]["color"]["r"] == 0.2
        assert rest["fills"][0]["color"]["b"] == 1.0
        # Should also set backgroundColor
        assert rest["backgroundColor"]["r"] == 0.2

    def test_no_fills(self):
        fig_node = {
            "guid": (1, 101),
            "type": "FRAME",
            "name": "empty",
            "transform": _IDENTITY,
            "size": {"x": 100, "y": 100},
        }
        rest = _convert_node(fig_node, {}, _IDENTITY)
        assert "fills" not in rest


class TestTextConversion:
    def test_text_properties(self):
        fig_node = {
            "guid": (1, 200),
            "type": "TEXT",
            "name": "Title",
            "fontName": {"family": "Inter", "style": "SemiBold", "postscript": "Inter-SemiBold"},
            "fontSize": 24.0,
            "letterSpacing": {"value": 0.5, "units": "PIXELS"},
            "lineHeight": {"value": 32.0, "units": "PIXELS"},
            "textData": {"characters": "Hello World"},
            "fillPaints": [
                {"type": "SOLID", "visible": True, "color": {"r": 0, "g": 0, "b": 0, "a": 1}}
            ],
            "transform": _IDENTITY,
            "size": {"x": 200, "y": 30},
        }
        rest = _convert_node(fig_node, {}, _IDENTITY)
        assert rest["type"] == "TEXT"
        assert rest["characters"] == "Hello World"
        assert rest["style"]["fontFamily"] == "Inter"
        assert rest["style"]["fontWeight"] == 600
        assert rest["style"]["fontSize"] == 24.0
        assert rest["style"]["letterSpacing"] == 0.5
        assert rest["style"]["lineHeightPx"] == 32.0

    def test_missing_text_data(self):
        fig_node = {
            "guid": (1, 201),
            "type": "TEXT",
            "name": "Empty",
            "transform": _IDENTITY,
            "size": {"x": 100, "y": 20},
        }
        rest = _convert_node(fig_node, {}, _IDENTITY)
        assert rest["characters"] == ""


class TestTreeReconstruction:
    def _make_tree_data(self):
        """Build a minimal nodeChanges array: document -> canvas -> frame -> text."""
        return {
            "nodeChanges": [
                {"guid": (0, 0), "type": "DOCUMENT", "name": "Document"},
                {
                    "guid": (0, 1),
                    "type": "CANVAS",
                    "name": "Page 1",
                    "parentIndex": {"guid": (0, 0), "position": "a"},
                    "transform": _IDENTITY,
                    "size": {"x": 0, "y": 0},
                },
                {
                    "guid": (1, 10),
                    "type": "FRAME",
                    "name": "Login Screen",
                    "parentIndex": {"guid": (0, 1), "position": "a"},
                    "transform": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                    "size": {"x": 390, "y": 844},
                },
                {
                    "guid": (1, 20),
                    "type": "TEXT",
                    "name": "Title",
                    "parentIndex": {"guid": (1, 10), "position": "a"},
                    "fontName": {"family": "Outfit", "style": "Bold", "postscript": "Outfit-Bold"},
                    "fontSize": 32,
                    "textData": {"characters": "Login"},
                    "fillPaints": [
                        {
                            "type": "SOLID",
                            "visible": True,
                            "color": {"r": 1, "g": 1, "b": 1, "a": 1},
                        }
                    ],
                    "transform": [[1, 0, 20], [0, 1, 100], [0, 0, 1]],
                    "size": {"x": 350, "y": 40},
                },
            ]
        }

    def test_tree_structure(self):
        data = self._make_tree_data()
        root, children_of = _build_tree(data)
        assert root["type"] == "DOCUMENT"
        assert root["guid"] == (0, 0)
        # Document has one child (page)
        assert len(children_of[(0, 0)]) == 1
        # Page has one child (frame)
        assert len(children_of[(0, 1)]) == 1
        # Frame has one child (text)
        assert len(children_of[(1, 10)]) == 1

    def test_full_conversion(self):
        data = self._make_tree_data()
        root, children_of = _build_tree(data)
        document = _convert_node(root, children_of, _IDENTITY)

        assert document["type"] == "DOCUMENT"
        assert len(document["children"]) == 1

        page = document["children"][0]
        assert page["type"] == "CANVAS"
        assert page["name"] == "Page 1"
        assert len(page["children"]) == 1

        frame = page["children"][0]
        assert frame["type"] == "FRAME"
        assert frame["name"] == "Login Screen"
        assert frame["absoluteBoundingBox"]["width"] == 390
        assert frame["absoluteBoundingBox"]["height"] == 844

        text = frame["children"][0]
        assert text["type"] == "TEXT"
        assert text["characters"] == "Login"
        assert text["style"]["fontFamily"] == "Outfit"
        assert text["style"]["fontWeight"] == 700


class TestCornerRadius:
    def test_uniform(self):
        fig_node = {
            "guid": (1, 300),
            "type": "ROUNDED_RECTANGLE",
            "name": "Button",
            "cornerRadius": 12.0,
            "transform": _IDENTITY,
            "size": {"x": 200, "y": 48},
        }
        rest = _convert_node(fig_node, {}, _IDENTITY)
        assert rest["type"] == "RECTANGLE"
        assert rest["cornerRadius"] == 12.0

    def test_independent_corners(self):
        fig_node = {
            "guid": (1, 301),
            "type": "ROUNDED_RECTANGLE",
            "name": "Card",
            "rectangleTopLeftCornerRadius": 16,
            "rectangleTopRightCornerRadius": 16,
            "rectangleBottomLeftCornerRadius": 0,
            "rectangleBottomRightCornerRadius": 0,
            "transform": _IDENTITY,
            "size": {"x": 300, "y": 200},
        }
        rest = _convert_node(fig_node, {}, _IDENTITY)
        assert rest["rectangleTopLeftCornerRadius"] == 16
        assert rest["rectangleBottomLeftCornerRadius"] == 0
