"""Tests for color conversion and comparison utilities."""

from figma_audit.utils.color import (
    color_distance,
    delta_e_2000,
    hex_to_rgb,
    rgb_to_lab,
    rgba_to_hex,
)


class TestRgbaToHex:
    def test_white(self):
        assert rgba_to_hex(1.0, 1.0, 1.0) == "#FFFFFF"

    def test_black(self):
        assert rgba_to_hex(0.0, 0.0, 0.0) == "#000000"

    def test_red(self):
        assert rgba_to_hex(1.0, 0.0, 0.0) == "#FF0000"

    def test_with_alpha(self):
        result = rgba_to_hex(1.0, 1.0, 1.0, 0.5)
        assert result == "#FFFFFF80"

    def test_figma_color(self):
        # Figma uses 0-1 floats
        result = rgba_to_hex(0.102, 0.102, 0.180)
        assert result == "#1A1A2E"

    def test_clamp(self):
        assert rgba_to_hex(1.5, -0.1, 0.5) == "#FF0080"


class TestHexToRgb:
    def test_white(self):
        assert hex_to_rgb("#FFFFFF") == (255, 255, 255)

    def test_black(self):
        assert hex_to_rgb("#000000") == (0, 0, 0)

    def test_shorthand(self):
        assert hex_to_rgb("#FFF") == (255, 255, 255)

    def test_no_hash(self):
        assert hex_to_rgb("FF0000") == (255, 0, 0)


class TestDeltaE:
    def test_identical_colors(self):
        lab = rgb_to_lab(128, 128, 128)
        assert delta_e_2000(lab, lab) == 0.0

    def test_similar_colors(self):
        lab1 = rgb_to_lab(255, 0, 0)
        lab2 = rgb_to_lab(250, 5, 5)
        de = delta_e_2000(lab1, lab2)
        assert de < 5.0  # Very similar

    def test_different_colors(self):
        lab1 = rgb_to_lab(255, 0, 0)
        lab2 = rgb_to_lab(0, 0, 255)
        de = delta_e_2000(lab1, lab2)
        assert de > 30  # Very different

    def test_black_white(self):
        lab1 = rgb_to_lab(0, 0, 0)
        lab2 = rgb_to_lab(255, 255, 255)
        de = delta_e_2000(lab1, lab2)
        assert de > 90  # Maximum difference


class TestColorDistance:
    def test_identical(self):
        assert color_distance("#FF0000", "#FF0000") == 0.0

    def test_similar(self):
        de = color_distance("#3A82F7", "#2563EB")
        assert 0 < de < 15  # Similar blues

    def test_very_different(self):
        de = color_distance("#FFFFFF", "#000000")
        assert de > 90

    def test_threshold_same(self):
        # deltaE < 3 = perceptually identical
        de = color_distance("#1A1A2E", "#1B1B2F")
        assert de < 3.0
