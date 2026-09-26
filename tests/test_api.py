import unittest

from api.index import normalize_vercel_api_path


class VercelApiRewriteTests(unittest.TestCase):
    def test_rewritten_routes_are_restored(self):
        for route in ("overview", "config", "workflow", "submit-clip"):
            with self.subTest(route=route):
                self.assertEqual(
                    normalize_vercel_api_path(f"/api?route={route}"),
                    f"/api/{route}",
                )

    def test_unknown_or_direct_routes_are_unchanged(self):
        self.assertEqual(normalize_vercel_api_path("/api?route=other"), "/api?route=other")
        self.assertEqual(normalize_vercel_api_path("/api/config"), "/api/config")
        self.assertEqual(normalize_vercel_api_path("/?route=config"), "/api/config")


if __name__ == "__main__":
    unittest.main()
