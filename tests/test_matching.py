import unittest
import numpy as np
from app.ai.matcher import cosine_similarity, update_passenger_embedding
from app.models import PassengerIdentity

class TestIdentityMatching(unittest.TestCase):
    def test_cosine_similarity_identical(self):
        v1 = [1.0, 0.0, 0.0]
        v2 = [1.0, 0.0, 0.0]
        sim = cosine_similarity(v1, v2)
        self.assertAlmostEqual(sim, 1.0, places=4)

    def test_cosine_similarity_orthogonal(self):
        v1 = [1.0, 0.0, 0.0]
        v2 = [0.0, 1.0, 0.0]
        sim = cosine_similarity(v1, v2)
        self.assertAlmostEqual(sim, 0.0, places=4)

    def test_cosine_similarity_opposite(self):
        v1 = [1.0, 0.0, 0.0]
        v2 = [-1.0, 0.0, 0.0]
        sim = cosine_similarity(v1, v2)
        self.assertAlmostEqual(sim, -1.0, places=4)

    def test_cosine_similarity_high_correlation(self):
        # 512-D vectors with slight noise (simulating angle/lighting change)
        np.random.seed(42)
        base = np.random.randn(512)
        base = base / np.linalg.norm(base)
        
        # Add slight Gaussian noise
        noisy = base + np.random.normal(0, 0.005, 512)
        noisy = noisy / np.linalg.norm(noisy)

        sim = cosine_similarity(base.tolist(), noisy.tolist())
        # Should be a very confident match (> 0.90)
        self.assertGreater(sim, 0.90)

    def test_update_passenger_embedding(self):
        v1 = [1.0, 0.0, 0.0]
        v2 = [0.0, 1.0, 0.0]
        p = PassengerIdentity(
            journey_id=1,
            person_id="P001",
            representative_embedding_json=None
        )
        # Initialize
        update_passenger_embedding(p, v1)
        self.assertIsNotNone(p.representative_embedding_json)

        # Smooth update with v2
        update_passenger_embedding(p, v2, alpha=0.5)
        import json
        updated = json.loads(p.representative_embedding_json)
        self.assertEqual(len(updated), 3)
        self.assertAlmostEqual(updated[0], updated[1], places=4)

if __name__ == "__main__":
    unittest.main()
