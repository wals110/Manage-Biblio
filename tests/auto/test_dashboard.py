#!/usr/bin/env python3
"""Tests basiques du dashboard — routes et data.py."""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)


class TestDashboardRoutes(unittest.TestCase):
    """Vérifie que toutes les routes retournent 200."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        from dashboard.app import app
        cls.app = app
        cls.client = TestClient(app)

    def test_overview(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)

    def test_tests_page(self):
        r = self.client.get("/tests")
        self.assertEqual(r.status_code, 200)

    def test_rapports_page(self):
        r = self.client.get("/rapports")
        self.assertEqual(r.status_code, 200)

    def test_rapports_classify(self):
        r = self.client.get("/rapports?type=classify")
        self.assertEqual(r.status_code, 200)

    def test_rapports_rename(self):
        r = self.client.get("/rapports?type=rename")
        self.assertEqual(r.status_code, 200)

    def test_comparer_page(self):
        r = self.client.get("/comparer")
        self.assertEqual(r.status_code, 200)

    def test_metriques_page(self):
        r = self.client.get("/metriques")
        self.assertEqual(r.status_code, 200)

    def test_historique_page(self):
        r = self.client.get("/historique")
        self.assertEqual(r.status_code, 200)

    def test_suggestions_page(self):
        r = self.client.get("/suggestions")
        self.assertEqual(r.status_code, 200)

    # ─── Overview cockpit refactor 2026-06-05 ─────────────────────

    def test_overview_default_profile(self):
        """GET / sans param → 200 et le selector affiche un profil
        sélectionné (peu importe lequel, dépend de l'environnement)."""
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'selected', r.content)
        self.assertIn(b'filter-select', r.content)

    def test_overview_explicit_profile(self):
        r = self.client.get("/?profile=test")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"test", r.content)

    def test_overview_unknown_profile_falls_back(self):
        """?profile=foo → fallback transparent (pas de 404)."""
        r = self.client.get("/?profile=foo-does-not-exist")
        self.assertEqual(r.status_code, 200)

    def test_overview_refresh_param_returns_200(self):
        r = self.client.get("/?profile=default&refresh=1")
        self.assertEqual(r.status_code, 200)

    def test_overview_contains_global_cost_banner(self):
        r = self.client.get("/")
        self.assertIn(b"Co", r.content)  # "Coût" présent (UTF-8 ou HTML entity)
        self.assertIn(b"dash-global-cost", r.content)

    def test_overview_contains_kpi_grid(self):
        r = self.client.get("/")
        self.assertIn(b"dash-kpi-big", r.content)

    def test_overview_no_longer_contains_test_kpis(self):
        """Régression : Overview ne doit plus afficher Pass/Fail/Skip."""
        r = self.client.get("/")
        # L'ancienne page contenait "Heatmap par phase"
        self.assertNotIn(b"Heatmap par phase", r.content)
        # Et "Release Gate"
        self.assertNotIn(b"Release Gate", r.content)

    def test_admin_page(self):
        r = self.client.get("/admin")
        self.assertEqual(r.status_code, 200)

    def test_api_status(self):
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("running", data)
        self.assertFalse(data["running"])

    def test_tests_with_filters(self):
        """Filtres ne cassent pas la page."""
        r = self.client.get("/tests?phase=0")
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/tests?status=pass")
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/tests?phase=&status=")
        self.assertEqual(r.status_code, 200)

    def test_static_css(self):
        r = self.client.get("/static/style.css")
        self.assertEqual(r.status_code, 200)

    def test_static_js_common(self):
        r = self.client.get("/static/js/common.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn("copyCmd", r.text)

    def test_static_js_tests(self):
        r = self.client.get("/static/js/tests.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn("updateSeriesStatus", r.text)

    def test_static_js_viewer(self):
        r = self.client.get("/static/js/viewer.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn("loadThumbnail", r.text)

    def test_viewer_page(self):
        r = self.client.get("/viewer")
        self.assertEqual(r.status_code, 200)

    def test_viewer_page_with_profile(self):
        r = self.client.get("/viewer?profile=test")
        self.assertEqual(r.status_code, 200)

    def test_viewer_files_endpoint(self):
        r = self.client.get("/api/viewer/files?profile=test")
        self.assertEqual(r.status_code, 200)

    def test_viewer_page_with_both_profiles(self):
        r = self.client.get("/viewer?source_profile=default&dest_profile=test")
        self.assertEqual(r.status_code, 200)

    def test_copy_endpoint_rejects_default_destination(self):
        r = self.client.post(
            "/api/viewer/copy?source_profile=default&dest_profile=default&filenames=any.pdf"
        )
        self.assertEqual(r.status_code, 400)

    def test_copy_endpoint_no_filenames(self):
        r = self.client.post(
            "/api/viewer/copy?source_profile=default&dest_profile=test&filenames="
        )
        self.assertEqual(r.status_code, 400)

    def test_clear_destination_rejects_default(self):
        r = self.client.post("/api/viewer/clear-destination?profile=default")
        self.assertEqual(r.status_code, 400)

    def test_sse_events_endpoint_registered(self):
        """SSE endpoint /api/events is registered in the app routes."""
        routes = [r.path for r in self.app.routes if hasattr(r, 'path')]
        self.assertIn("/api/events", routes)


class TestDashboardData(unittest.TestCase):
    """Vérifie que data.py ne crashe pas sur des données vides/absentes."""

    def test_get_project_root(self):
        from dashboard.data import get_project_root
        root = get_project_root()
        self.assertTrue(root.exists())

    def test_get_tests_yaml(self):
        from dashboard.data import get_tests_yaml
        result = get_tests_yaml()
        # Peut être None si tests.yaml n'existe pas, mais ne doit pas crasher
        if result is not None:
            self.assertIn("phases", result)

    def test_get_latest_report_no_crash(self):
        from dashboard.data import get_latest_report
        # Ne doit pas crasher, même sans rapports
        get_latest_report()

    def test_get_all_reports_no_crash(self):
        from dashboard.data import get_all_reports
        result = get_all_reports()
        self.assertIsInstance(result, list)

    def test_get_available_runs_no_crash(self):
        from dashboard.data import get_available_runs
        result = get_available_runs()
        self.assertIsInstance(result, list)

    def test_get_run_from_db_none(self):
        from dashboard.data import get_run_from_db
        # Ne doit pas crasher sur un run inexistant
        result = get_run_from_db("inexistant_run_id")
        self.assertIsNone(result)

    def test_get_run_from_db_latest(self):
        from dashboard.data import get_run_from_db
        # Ne doit pas crasher même si DB vide
        get_run_from_db(None)

    def test_get_csv_files_empty(self):
        from dashboard.data import get_csv_files
        result = get_csv_files("classify")
        self.assertIsInstance(result, list)

    def test_get_csv_files_all_types(self):
        from dashboard.data import get_csv_files
        for report_type in ("classify", "rename", "refine", "process", "all"):
            result = get_csv_files(report_type)
            self.assertIsInstance(result, list)

    def test_get_csv_files_invalid_type(self):
        from dashboard.data import get_csv_files
        result = get_csv_files("invalid_type")
        self.assertEqual(result, [])

    def test_load_csv_missing_file(self):
        from dashboard.data import load_csv
        headers, rows = load_csv("/nonexistent/file.csv")
        self.assertEqual(headers, [])
        self.assertEqual(rows, [])

    def test_compute_csv_stats_empty(self):
        from dashboard.data import compute_csv_stats
        stats = compute_csv_stats([], "classify")
        self.assertEqual(stats["total"], 0)

    def test_get_history_data_no_crash(self):
        from dashboard.data import get_history_data
        result = get_history_data()
        self.assertIsInstance(result, list)

    def test_get_suggestions_no_crash(self):
        from dashboard.data import get_suggestions
        result = get_suggestions()
        self.assertIsInstance(result, list)

    def test_get_llm_config(self):
        from dashboard.data import get_llm_config
        result = get_llm_config()
        if result is not None:
            self.assertIn("provider", result)
            self.assertIn("model", result)

    def test_get_merged_test_view_none(self):
        from dashboard.data import get_merged_test_view
        result = get_merged_test_view(None, None)
        self.assertIsNone(result)

    def test_get_run_csv_files_no_crash(self):
        from dashboard.data import get_run_csv_files
        result = get_run_csv_files(None)
        self.assertIsInstance(result, dict)
        self.assertIn("classify", result)
        self.assertIn("rename", result)

    def test_compare_runs_empty(self):
        from dashboard.data import compare_runs
        empty_run = {"phases": []}
        result = compare_runs(empty_run, empty_run)
        self.assertEqual(result["stats"]["unchanged"], 0)
        self.assertEqual(result["series"], [])

    def test_resolve_variables(self):
        from dashboard.data import _resolve_variables
        result = _resolve_variables("${A}/inbox", {"A": "/path"})
        self.assertEqual(result, "/path/inbox")

    def test_resolve_variables_chained(self):
        from dashboard.data import _resolve_variables
        result = _resolve_variables("${B}", {"A": "/root", "B": "${A}/sub"})
        self.assertEqual(result, "/root/sub")


if __name__ == "__main__":
    unittest.main()
