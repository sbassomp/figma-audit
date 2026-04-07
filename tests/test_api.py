"""Tests for the FastAPI application."""

import os
import tempfile

from fastapi.testclient import TestClient

from figma_audit.api.app import create_app


def _make_client() -> TestClient:
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app = create_app(db_path=db_path)
    return TestClient(app)


class TestHealth:
    def test_health(self):
        client = _make_client()
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestProjects:
    def test_create_and_list(self):
        client = _make_client()

        # Create
        r = client.post("/api/projects", json={"name": "My App", "app_url": "https://example.com"})
        assert r.status_code == 201
        data = r.json()
        assert data["slug"] == "my-app"

        # List
        r = client.get("/api/projects")
        assert r.status_code == 200
        projects = r.json()
        assert len(projects) == 1
        assert projects[0]["name"] == "My App"

    def test_get_project(self):
        client = _make_client()
        client.post("/api/projects", json={"name": "Test Project"})

        r = client.get("/api/projects/test-project")
        assert r.status_code == 200
        assert r.json()["name"] == "Test Project"
        assert r.json()["runs"] == []

    def test_update_project(self):
        client = _make_client()
        client.post("/api/projects", json={"name": "Test"})

        r = client.put("/api/projects/test", json={"app_url": "https://updated.com"})
        assert r.status_code == 200

        r = client.get("/api/projects/test")
        assert r.json()["app_url"] == "https://updated.com"

    def test_delete_project(self):
        client = _make_client()
        client.post("/api/projects", json={"name": "ToDelete"})

        r = client.delete("/api/projects/todelete")
        assert r.status_code == 204

        r = client.get("/api/projects/todelete")
        assert r.status_code == 404

    def test_duplicate_slug(self):
        client = _make_client()
        client.post("/api/projects", json={"name": "Test"})
        r = client.post("/api/projects", json={"name": "Test"})
        assert r.status_code == 409

    def test_not_found(self):
        client = _make_client()
        r = client.get("/api/projects/nonexistent")
        assert r.status_code == 404


class TestRuns:
    def test_create_run(self):
        client = _make_client()
        client.post("/api/projects", json={"name": "Test", "output_dir": "/tmp/test-output"})

        r = client.post("/api/projects/test/runs", json={})
        assert r.status_code == 201
        assert r.json()["status"] == "pending"

    def test_list_runs(self):
        client = _make_client()
        client.post("/api/projects", json={"name": "Test", "output_dir": "/tmp/test-output"})
        client.post("/api/projects/test/runs", json={})

        r = client.get("/api/projects/test/runs")
        assert r.status_code == 200
        assert len(r.json()) >= 1


class TestScreens:
    def test_list_empty(self):
        client = _make_client()
        client.post("/api/projects", json={"name": "Test"})

        r = client.get("/api/projects/test/screens")
        assert r.status_code == 200
        assert r.json() == []


class TestDiscrepancies:
    def test_list_requires_run(self):
        client = _make_client()
        client.post("/api/projects", json={"name": "Test"})

        r = client.get("/api/projects/test/runs/999/discrepancies")
        assert r.status_code == 404
