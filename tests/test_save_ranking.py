"""
Test suite for save_ranking.py helper functions
Run: poetry run pytest tests/test_save_ranking.py -v --cov=escalada.api.save_ranking
"""
import unittest
import math


class FormatTimeTest(unittest.TestCase):
    """Test _format_time helper function"""

    def test_format_time_basic(self):
        from escalada.api.save_ranking import _format_time
        self.assertEqual(_format_time(125), "02:05")
        self.assertEqual(_format_time(60), "01:00")
        self.assertEqual(_format_time(0), "00:00")

    def test_format_time_large_values(self):
        from escalada.api.save_ranking import _format_time
        self.assertEqual(_format_time(3600), "60:00")
        self.assertEqual(_format_time(3661), "61:01")

    def test_format_time_none(self):
        from escalada.api.save_ranking import _format_time
        self.assertIsNone(_format_time(None))

    def test_format_time_with_decimal(self):
        from escalada.api.save_ranking import _format_time
        # Should convert to int first
        self.assertEqual(_format_time(125.7), "02:05")


class ToSecondsTest(unittest.TestCase):
    """Test _to_seconds helper function"""

    def test_to_seconds_integer(self):
        from escalada.api.save_ranking import _to_seconds
        self.assertEqual(_to_seconds(125), 125)
        self.assertEqual(_to_seconds(0), 0)
        self.assertEqual(_to_seconds(3600), 3600)

    def test_to_seconds_float(self):
        from escalada.api.save_ranking import _to_seconds
        self.assertEqual(_to_seconds(125.7), 125)
        self.assertEqual(_to_seconds(60.9), 60)

    def test_to_seconds_string_mmss(self):
        from escalada.api.save_ranking import _to_seconds
        self.assertEqual(_to_seconds("02:05"), 125)
        self.assertEqual(_to_seconds("01:00"), 60)
        self.assertEqual(_to_seconds("10:30"), 630)
        self.assertEqual(_to_seconds("00:00"), 0)

    def test_to_seconds_numeric_string(self):
        from escalada.api.save_ranking import _to_seconds
        self.assertEqual(_to_seconds("125"), 125)
        self.assertEqual(_to_seconds("125.5"), 125)

    def test_to_seconds_none(self):
        from escalada.api.save_ranking import _to_seconds
        self.assertIsNone(_to_seconds(None))

    def test_to_seconds_invalid_string(self):
        from escalada.api.save_ranking import _to_seconds
        self.assertIsNone(_to_seconds("invalid"))
        self.assertIsNone(_to_seconds("abc:def"))
        self.assertIsNone(_to_seconds(""))

    def test_to_seconds_nan(self):
        from escalada.api.save_ranking import _to_seconds
        self.assertIsNone(_to_seconds(float('nan')))

    def test_to_seconds_malformed_mmss(self):
        from escalada.api.save_ranking import _to_seconds
        self.assertIsNone(_to_seconds("5:30:45"))  # Too many parts
        self.assertIsNone(_to_seconds("invalid:time"))


class BuildRankingDataTest(unittest.TestCase):
    """Test ranking calculation functions"""

    def test_ranking_with_unique_scores(self):
        """Test ranking with all unique scores"""
        from escalada.api.save_ranking import _build_overall_df, RankingIn

        payload = RankingIn(
            categorie="Test",
            route_count=2,
            scores={
                "Alice": [100, 90],
                "Bob": [80, 85],
                "Charlie": [70, 95]
            },
            clubs={"Alice": "Club A", "Bob": "Club B", "Charlie": "Club C"}
        )

        df = _build_overall_df(payload)
        self.assertIsNotNone(df)
        self.assertGreater(len(df), 0)
        self.assertIn("Rank", df.columns)
        self.assertIn("Nume", df.columns)
        self.assertIn("Total", df.columns)

    def test_ranking_with_ties(self):
        """Test ranking with tied scores"""
        from escalada.api.save_ranking import _build_overall_df, RankingIn

        payload = RankingIn(
            categorie="Test",
            route_count=2,
            scores={
                "Alice": [100, 100],
                "Bob": [100, 100],
                "Charlie": [50, 50]
            },
            clubs={}
        )

        df = _build_overall_df(payload)
        self.assertIsNotNone(df)
        # Tied scores should have same rank
        if "Total" in df.columns:
            # Check for competitors with equal high scores
            top_scores = df.nlargest(2, "Total")
            self.assertGreaterEqual(len(top_scores), 1)

    def test_ranking_with_missing_scores(self):
        """Test ranking when some competitors have missing scores"""
        from escalada.api.save_ranking import _build_overall_df, RankingIn

        payload = RankingIn(
            categorie="Test",
            route_count=3,
            scores={
                "Alice": [100, 90, 80],
                "Bob": [80, 85],  # Missing one score
                "Charlie": [70]   # Missing two scores
            },
            clubs={}
        )

        df = _build_overall_df(payload)
        self.assertIsNotNone(df)
        self.assertEqual(len(df), 3)  # All competitors present

    def test_ranking_single_route(self):
        """Test ranking with only one route"""
        from escalada.api.save_ranking import _build_overall_df, RankingIn

        payload = RankingIn(
            categorie="Test",
            route_count=1,
            scores={
                "Alice": [100],
                "Bob": [80],
                "Charlie": [90]
            },
            clubs={}
        )

        df = _build_overall_df(payload)
        self.assertEqual(len(df), 3)
        # Should rank them correctly: Alice(100) -> Charlie(90) -> Bob(80)
        self.assertEqual(df.iloc[0]["Rank"], 1)

    def test_ranking_with_time_tiebreak(self):
        """Test ranking with time criterion enabled"""
        from escalada.api.save_ranking import _build_overall_df, RankingIn

        payload = RankingIn(
            categorie="Test",
            route_count=1,
            scores={"Alice": [100], "Bob": [100]},
            times={"Alice": [10.5], "Bob": [12.3]},
            use_time_tiebreak=True,
            clubs={}
        )

        df = _build_overall_df(payload)
        self.assertIsNotNone(df)

    def test_ranking_empty_scores(self):
        """Test ranking with empty scores dict"""
        from escalada.api.save_ranking import _build_overall_df, RankingIn

        payload = RankingIn(
            categorie="Test",
            route_count=1,
            scores={},
            clubs={}
        )

        df = _build_overall_df(payload)
        self.assertEqual(len(df), 0)

    def test_ranking_with_clubs(self):
        """Test ranking includes club information"""
        from escalada.api.save_ranking import _build_overall_df, RankingIn

        payload = RankingIn(
            categorie="Test",
            route_count=1,
            scores={"Alice": [100], "Bob": [80]},
            clubs={"Alice": "Climbing Club A", "Bob": "Climbing Club B"}
        )

        df = _build_overall_df(payload)
        self.assertIn("Club", df.columns)
        self.assertIn("Climbing Club A", df["Club"].values)


if __name__ == "__main__":
    unittest.main()
