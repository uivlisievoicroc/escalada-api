"""
Test suite for podium.py endpoint
Run: poetry run pytest tests/test_podium.py -v --cov=escalada.api.podium
"""
import unittest
import asyncio
import os
import tempfile
import json
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient
from fastapi import FastAPI


class PodiumSecurityTest(unittest.TestCase):
    """Test path traversal protection"""

    def setUp(self):
        """Create FastAPI test client"""
        from escalada.api.podium import router
        self.app = FastAPI()
        self.app.include_router(router)
        self.client = TestClient(self.app)

    def test_path_traversal_protection_dotdot(self):
        """Test that ../ in category name is sanitized"""
        response = self.client.get("/podium/../../../etc/passwd")
        # Path traversal should be blocked (either 400 or 404)
        self.assertIn(response.status_code, [400, 404])

    def test_path_traversal_protection_absolute_path(self):
        """Test that absolute paths are rejected"""
        response = self.client.get("/podium//etc/passwd")
        self.assertIn(response.status_code, [400, 404])

    def test_safe_category_name_missing_file(self):
        """Test normal category names with missing file"""
        response = self.client.get("/podium/NonExistent")
        self.assertEqual(response.status_code, 404)
        self.assertIn("inexistent", response.json()["detail"])


class PodiumEndpointTest(unittest.TestCase):
    """Test podium data retrieval"""

    def setUp(self):
        """Create FastAPI test client"""
        from escalada.api.podium import router
        self.app = FastAPI()
        self.app.include_router(router)
        self.client = TestClient(self.app)

    def test_missing_category_file(self):
        """Test handling of missing category file"""
        response = self.client.get("/podium/NonExistent")
        self.assertEqual(response.status_code, 404)

    def test_podium_with_valid_data(self):
        """Test podium correctly returns top 3"""
        with patch('pathlib.Path.exists', return_value=True):
            with patch('pandas.read_excel') as mock_read:
                import pandas as pd
                mock_data = pd.DataFrame({
                    'Rank': [1, 2, 3, 4, 5],
                    'Nume': ['Alice', 'Bob', 'Charlie', 'David', 'Eve'],
                    'Total': [200, 180, 160, 140, 120]
                })
                mock_read.return_value = mock_data
                
                response = self.client.get("/podium/Tineri")
                
                self.assertEqual(response.status_code, 200)
                result = response.json()
                self.assertLessEqual(len(result), 3)
                self.assertGreater(len(result), 0)

    def test_podium_with_special_characters_in_name(self):
        """Test podium handles special characters in names"""
        with patch('pathlib.Path.exists', return_value=True):
            with patch('pandas.read_excel') as mock_read:
                import pandas as pd
                mock_data = pd.DataFrame({
                    'Rank': [1, 2, 3],
                    'Nume': ['Ångela Öberg', 'François Müller', 'José García'],
                    'Total': [200, 180, 160]
                })
                mock_read.return_value = mock_data
                
                response = self.client.get("/podium/Tineri")
                self.assertEqual(response.status_code, 200)
                result = response.json()
                self.assertEqual(len(result), 3)

    def test_podium_response_has_colors(self):
        """Test podium response includes medal colors"""
        with patch('pathlib.Path.exists', return_value=True):
            with patch('pandas.read_excel') as mock_read:
                import pandas as pd
                mock_data = pd.DataFrame({
                    'Rank': [1, 2, 3],
                    'Nume': ['Alice', 'Bob', 'Charlie'],
                    'Total': [200, 180, 160]
                })
                mock_read.return_value = mock_data
                
                response = self.client.get("/podium/Tineri")
                result = response.json()
                
                self.assertEqual(len(result), 3)
                self.assertIn('color', result[0])
                self.assertIn('name', result[0])
                colors = [r['color'] for r in result]
                self.assertIn('#ffd700', colors)
                self.assertIn('#c0c0c0', colors)
                self.assertIn('#cd7f32', colors)


class PodiumRankingCalculationTest(unittest.TestCase):
    """Test ranking data accuracy"""

    def setUp(self):
        """Create FastAPI test client"""
        from escalada.api.podium import router
        self.app = FastAPI()
        self.app.include_router(router)
        self.client = TestClient(self.app)

    def test_ranking_order_descending(self):
        """Test podium ranks highest scores first"""
        with patch('pathlib.Path.exists', return_value=True):
            with patch('pandas.read_excel') as mock_read:
                import pandas as pd
                mock_data = pd.DataFrame({
                    'Rank': [1, 2, 3],
                    'Nume': ['Alice', 'Bob', 'Charlie'],
                    'Total': [200, 180, 160]
                })
                mock_read.return_value = mock_data
                
                response = self.client.get("/podium/Tineri")
                result = response.json()
                
                self.assertEqual(len(result), 3)
                self.assertIn('Alice', result[0]['name'])

    def test_ranking_with_ties(self):
        """Test podium handles tied scores correctly"""
        with patch('pathlib.Path.exists', return_value=True):
            with patch('pandas.read_excel') as mock_read:
                import pandas as pd
                mock_data = pd.DataFrame({
                    'Rank': [1, 1, 3],
                    'Nume': ['Alice', 'Bob', 'Charlie'],
                    'Total': [200, 200, 180]
                })
                mock_read.return_value = mock_data
                
                response = self.client.get("/podium/Tineri")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.json()), 3)

    def test_ranking_with_float_scores(self):
        """Test podium handles float scores correctly"""
        with patch('pathlib.Path.exists', return_value=True):
            with patch('pandas.read_excel') as mock_read:
                import pandas as pd
                mock_data = pd.DataFrame({
                    'Rank': [1, 2, 3],
                    'Nume': ['Alice', 'Bob', 'Charlie'],
                    'Total': [200.5, 180.3, 160.7]
                })
                mock_read.return_value = mock_data
                
                response = self.client.get("/podium/Tineri")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.json()), 3)


if __name__ == "__main__":
    unittest.main()
