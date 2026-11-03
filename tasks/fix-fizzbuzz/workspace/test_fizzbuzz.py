"""Unit tests the coding agent must pass without editing this file."""

import unittest

from fizzbuzz import fizzbuzz


class FizzBuzzTest(unittest.TestCase):
    """Checks the four fizzbuzz cases."""

    def test_values(self) -> None:
        """Accept plain numbers, fizz, buzz, and fizzbuzz."""
        self.assertEqual(fizzbuzz(1), "1")
        self.assertEqual(fizzbuzz(3), "fizz")
        self.assertEqual(fizzbuzz(5), "buzz")
        self.assertEqual(fizzbuzz(15), "fizzbuzz")


if __name__ == "__main__":
    unittest.main()
