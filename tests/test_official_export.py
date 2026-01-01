import json
import unittest
import zipfile
from io import BytesIO


class OfficialExportZipTest(unittest.TestCase):
    def test_build_official_results_zip_contains_expected_files(self):
        from escalada.api.official_export import build_official_results_zip

        snapshot = {
            "boxId": 7,
            "competitionId": 3,
            "categorie": "U13F",
            "routesCount": 2,
            "timeCriterionEnabled": True,
            "scores": {"Ana": [3.5, 4.0], "Bob": [3.5, 3.0]},
            "times": {"Ana": [12.34, 11.0], "Bob": [13.0, 10.0]},
        }

        zip_bytes = build_official_results_zip(snapshot)
        self.assertIsInstance(zip_bytes, (bytes, bytearray))
        self.assertGreater(len(zip_bytes), 200)

        zf = zipfile.ZipFile(BytesIO(zip_bytes))
        names = set(zf.namelist())

        expected = {
            "U13F/overall.xlsx",
            "U13F/overall.pdf",
            "U13F/route_1.xlsx",
            "U13F/route_1.pdf",
            "U13F/route_2.xlsx",
            "U13F/route_2.pdf",
            "U13F/metadata.json",
        }
        self.assertTrue(expected.issubset(names))

        meta = json.loads(zf.read("U13F/metadata.json").decode("utf-8"))
        self.assertEqual(meta["boxId"], 7)
        self.assertEqual(meta["competitionId"], 3)
        self.assertEqual(meta["categorie"], "U13F")
        self.assertEqual(meta["routesCount"], 2)
        self.assertTrue(meta["timeCriterionEnabled"])
        self.assertIn("exportedAt", meta)

    def test_build_official_results_zip_requires_scores(self):
        from escalada.api.official_export import build_official_results_zip

        snapshot = {"boxId": 1, "competitionId": 1, "categorie": "U13F", "routesCount": 1}
        with self.assertRaises(ValueError):
            build_official_results_zip(snapshot)

    def test_build_official_results_zip_requires_routes_count(self):
        from escalada.api.official_export import build_official_results_zip

        snapshot = {
            "boxId": 1,
            "competitionId": 1,
            "categorie": "U13F",
            "scores": {"Ana": []},
            "times": {"Ana": []},
        }
        with self.assertRaises(ValueError):
            build_official_results_zip(snapshot)


if __name__ == "__main__":
    unittest.main()
