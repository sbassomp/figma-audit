"""Tests for data models."""

from figma_audit.models import Bounds, FigmaElement, FigmaManifest, FigmaScreen, FileMeta


class TestFigmaScreen:
    def test_minimal(self):
        screen = FigmaScreen(id="1:2", name="Test", page="Page 1", width=390, height=844)
        assert screen.id == "1:2"
        assert screen.elements == []
        assert screen.image_path is None

    def test_with_elements(self):
        el = FigmaElement(
            type="TEXT",
            content="Hello",
            font_family="Outfit",
            font_size=16,
            font_weight=400,
            color="#FFFFFF",
            bounds=Bounds(x=0, y=0, w=100, h=20),
        )
        screen = FigmaScreen(
            id="1:2", name="Test", page="Page 1", width=390, height=844, elements=[el]
        )
        assert len(screen.elements) == 1
        assert screen.elements[0].content == "Hello"


class TestFigmaManifest:
    def test_serialization(self):
        manifest = FigmaManifest(
            file_key="abc123",
            file_name="My File",
            screens=[
                FigmaScreen(id="1:2", name="Screen 1", page="Page", width=390, height=844)
            ],
        )
        data = manifest.model_dump()
        assert data["file_key"] == "abc123"
        assert len(data["screens"]) == 1

        # Round-trip
        manifest2 = FigmaManifest(**data)
        assert manifest2.file_key == manifest.file_key
        assert manifest2.screens[0].name == "Screen 1"


class TestFileMeta:
    def test_all_fields(self):
        meta = FileMeta(
            file_key="abc",
            file_name="Test",
            last_modified="2026-01-01T00:00:00Z",
            version="123",
            downloaded_at="2026-01-01T00:00:01Z",
        )
        data = meta.model_dump()
        assert data["file_key"] == "abc"
        assert data["last_modified"] == "2026-01-01T00:00:00Z"
