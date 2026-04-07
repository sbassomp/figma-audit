"""Tests for htmx endpoints and web UI interactions."""

import os
import tempfile

from fastapi.testclient import TestClient
from sqlmodel import Session

from figma_audit.api.app import create_app
from figma_audit.db.engine import get_engine
from figma_audit.db.models import Discrepancy, Project, Run, Screen


def _setup_project(db_path: str) -> tuple[TestClient, int, int, int, int]:
    """Create a test project with a run, screen, and discrepancy."""
    app = create_app(db_path=db_path)
    engine = get_engine(db_path)
    with Session(engine) as s:
        p = Project(name="Test", slug="test", output_dir="/tmp")
        s.add(p)
        s.commit()
        s.refresh(p)
        pid = p.id

        r = Run(project_id=pid, status="completed")
        s.add(r)
        s.commit()
        s.refresh(r)
        rid = r.id

        sc = Screen(
            project_id=pid,
            figma_node_id="1:2",
            name="Test Screen",
            status="current",
        )
        s.add(sc)
        s.commit()
        s.refresh(sc)
        scid = sc.id

        d = Discrepancy(
            run_id=rid,
            screen_id=scid,
            page_id="home",
            route="/",
            category="COULEURS",
            severity="critical",
            description="Couleur differente",
            status="open",
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        did = d.id

    return TestClient(app), pid, rid, scid, did


class TestHtmxDiscrepancyStatus:
    def test_ignore_returns_html(self):
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        client, _, _, _, disc_id = _setup_project(db)

        r = client.post(f"/htmx/projects/test/discrepancies/{disc_id}/status/ignored")
        assert r.status_code == 200
        assert "badge-ignored" in r.text
        assert "IGNORED" in r.text.upper()

    def test_fix_returns_html(self):
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        client, _, _, _, disc_id = _setup_project(db)

        r = client.post(f"/htmx/projects/test/discrepancies/{disc_id}/status/fixed")
        assert r.status_code == 200
        assert "badge-fixed" in r.text

    def test_wontfix_returns_html(self):
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        client, _, _, _, disc_id = _setup_project(db)

        r = client.post(f"/htmx/projects/test/discrepancies/{disc_id}/status/wontfix")
        assert r.status_code == 200
        assert "badge-wontfix" in r.text

    def test_invalid_status(self):
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        client, _, _, _, disc_id = _setup_project(db)

        r = client.post(f"/htmx/projects/test/discrepancies/{disc_id}/status/invalid")
        assert r.status_code == 400

    def test_open_has_action_buttons(self):
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        client, _, _, _, disc_id = _setup_project(db)

        # First check it's open and has buttons
        r = client.post(f"/htmx/projects/test/discrepancies/{disc_id}/status/open")
        assert r.status_code == 200
        assert "Ignorer" in r.text
        assert "Corrige" in r.text

    def test_ignored_has_no_action_buttons(self):
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        client, _, _, _, disc_id = _setup_project(db)

        r = client.post(f"/htmx/projects/test/discrepancies/{disc_id}/status/ignored")
        assert "discrepancy-actions" not in r.text


class TestHtmxScreenStatus:
    def test_mark_obsolete(self):
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        client, _, _, sc_id, _ = _setup_project(db)

        r = client.post(f"/htmx/projects/test/screens/{sc_id}/status/obsolete")
        assert r.status_code == 200
        assert "badge-obsolete" in r.text
        assert "Restaurer" in r.text

    def test_restore_current(self):
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        client, _, _, sc_id, _ = _setup_project(db)

        # Mark obsolete first
        client.post(f"/htmx/projects/test/screens/{sc_id}/status/obsolete")
        # Restore
        r = client.post(f"/htmx/projects/test/screens/{sc_id}/status/current")
        assert r.status_code == 200
        assert "badge-current" in r.text

    def test_invalid_status(self):
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        client, _, _, sc_id, _ = _setup_project(db)

        r = client.post(f"/htmx/projects/test/screens/{sc_id}/status/banana")
        assert r.status_code == 400


class TestPathTraversal:
    def test_normal_path(self):
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app = create_app(db_path=db)
        engine = get_engine(db)

        # Create a temp dir with a file
        with tempfile.TemporaryDirectory() as tmpdir:
            (pathlib.Path(tmpdir) / "test.txt").write_text("hello")

            with Session(engine) as s:
                p = Project(name="X", slug="x", output_dir=tmpdir)
                s.add(p)
                s.commit()

            client = TestClient(app)
            r = client.get("/files/x/test.txt")
            assert r.status_code == 200
            assert r.text == "hello"

    def test_traversal_blocked(self):
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app = create_app(db_path=db)
        engine = get_engine(db)

        with tempfile.TemporaryDirectory() as tmpdir:
            with Session(engine) as s:
                p = Project(name="X", slug="x", output_dir=tmpdir)
                s.add(p)
                s.commit()

            client = TestClient(app)
            r = client.get("/files/x/../../etc/passwd")
            assert r.status_code in (403, 404)

    def test_traversal_encoded_blocked(self):
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app = create_app(db_path=db)
        engine = get_engine(db)

        with tempfile.TemporaryDirectory() as tmpdir:
            with Session(engine) as s:
                p = Project(name="X", slug="x", output_dir=tmpdir)
                s.add(p)
                s.commit()

            client = TestClient(app)
            r = client.get("/files/x/..%2F..%2Fetc%2Fpasswd")
            assert r.status_code in (403, 404)


class TestWebFilters:
    def test_severity_filter(self):
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        client, _, run_id, _, _ = _setup_project(db)

        r = client.get(f"/projects/test/runs/{run_id}?severity=critical")
        assert r.status_code == 200
        assert "Couleur differente" in r.text

    def test_status_filter_open(self):
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        client, _, run_id, _, _ = _setup_project(db)

        r = client.get(f"/projects/test/runs/{run_id}?status=open")
        assert r.status_code == 200
        assert "Couleur differente" in r.text

    def test_status_filter_ignored_empty(self):
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        client, _, run_id, _, _ = _setup_project(db)

        r = client.get(f"/projects/test/runs/{run_id}?status=ignored")
        assert r.status_code == 200
        assert "Couleur differente" not in r.text


import pathlib
