import unittest

from fizzbuzz import fizzbuzz


class FizzBuzzTest(unittest.TestCase):
    def test_values(self) -> None:
        self.assertEqual(fizzbuzz(1), "1")
        self.assertEqual(fizzbuzz(3), "fizz")
        self.assertEqual(fizzbuzz(5), "buzz")
        self.assertEqual(fizzbuzz(15), "fizzbuzz")


if __name__ == "__main__":
    unittest.main()
