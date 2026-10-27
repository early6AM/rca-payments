"""Правила срока доступа: 90 дней, включая день оплаты.

Frappe здесь не нужен — обычный unittest, без bench.
"""

import unittest
from datetime import date, datetime

from rca_payments.access_rules import (
	ACCESS_DAYS,
	access_until_from,
	days_left,
	is_expired,
	should_warn,
)


class TestAccessUntil(unittest.TestCase):
	def test_ninety_days_including_payment_day(self):
		# Оплата 27.10.2026: доступ 27.10 – 24.01.2027, это ровно 90 дней.
		self.assertEqual(access_until_from("2026-10-27"), date(2027, 1, 24))

	def test_always_exactly_access_days(self):
		for start in [
			"2026-01-01",
			"2026-01-31",
			"2026-02-28",
			"2026-03-01",
			"2026-05-31",
			"2026-10-27",
			"2026-12-31",
			"2027-02-28",
			"2028-02-29",
			"2028-12-31",
		]:
			with self.subTest(start=start):
				until = access_until_from(start)
				start_date = datetime.strptime(start, "%Y-%m-%d").date()
				self.assertEqual((until - start_date).days + 1, ACCESS_DAYS)

	def test_accepts_date_and_datetime(self):
		self.assertEqual(access_until_from(date(2026, 10, 27)), date(2027, 1, 24))
		self.assertEqual(
			access_until_from(datetime(2026, 10, 27, 23, 59, 59)), date(2027, 1, 24)
		)


class TestAccessWindow(unittest.TestCase):
	def test_days_left(self):
		self.assertEqual(days_left("2027-01-24", "2026-10-27"), 89)
		self.assertEqual(days_left("2027-01-24", "2027-01-24"), 0)
		self.assertEqual(days_left("2027-01-24", "2027-01-25"), -1)

	def test_expired_only_after_last_day(self):
		self.assertFalse(is_expired("2027-01-24", "2027-01-24"))
		self.assertTrue(is_expired("2027-01-24", "2027-01-25"))

	def test_warn_window(self):
		self.assertTrue(should_warn("2027-01-24", "2027-01-17", 7))
		self.assertTrue(should_warn("2027-01-24", "2027-01-24", 7))
		self.assertFalse(should_warn("2027-01-24", "2027-01-16", 7))
		self.assertFalse(should_warn("2027-01-24", "2027-01-25", 7))


if __name__ == "__main__":
	unittest.main()
