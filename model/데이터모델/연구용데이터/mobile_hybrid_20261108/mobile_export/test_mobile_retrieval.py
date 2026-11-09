from __future__ import annotations

import unittest

import numpy as np

from mobile_retrieval import mobile_l2_indices


class MobileRetrievalTest(unittest.TestCase):
    def test_ties_use_ascending_train_index(self) -> None:
        train = np.array([[1.0, 0.0], [-1.0, 0.0], [0.0, 1.0], [0.0, -1.0]])
        query = np.array([[0.0, 0.0]])
        np.testing.assert_array_equal(mobile_l2_indices(train, query, 3), [[0, 1, 2]])

    def test_float32_storage_precedes_float64_accumulation(self) -> None:
        train = np.array([[16_777_217.0], [16_777_216.0]], dtype=np.float64)
        query = np.array([[16_777_216.0]], dtype=np.float64)
        # Both train values become the same float32 value; row index breaks the tie.
        np.testing.assert_array_equal(mobile_l2_indices(train, query, 2), [[0, 1]])

    def test_rejects_invalid_k(self) -> None:
        with self.assertRaises(ValueError):
            mobile_l2_indices(np.zeros((2, 1)), np.zeros((1, 1)), 0)


if __name__ == "__main__":
    unittest.main()
