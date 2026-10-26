"""Чистая логика оформления заказа: почта, согласие, сумма, дубли.

Frappe здесь не нужен — поэтому тесты идут обычным unittest, без bench.
Всё, что требует базы (get_doc, db), живёт в rca_payments.checkout и тут
не покрывается.
"""

import unittest
from collections import namedtuple
from datetime import datetime, timedelta

from rca_payments.checkout_rules import (
	STATUS_NEW,
	CheckoutError,
	build_deal_fields,
	build_payment_fields,
	course_amount,
	find_reusable_payment,
	is_truthy,
	normalize_telegram_id,
	normalize_telegram_username,
	strip_tags,
	validate_email,
)

NOW = datetime(2026, 10, 25, 12, 0, 0)


class TestValidateEmail(unittest.TestCase):
	def test_valid_email_is_lowercased_and_trimmed(self):
		self.assertEqual(validate_email("  Anna.Petrova@Example.RU "), "anna.petrova@example.ru")

	def test_plus_addressing_is_allowed(self):
		self.assertEqual(validate_email("anna+course@example.ru"), "anna+course@example.ru")

	def test_rejects_broken_addresses(self):
		for broken in [
			"",
			"   ",
			"anna",
			"anna@",
			"@example.ru",
			"anna@example",
			"anna@@example.ru",
			"anna petrova@example.ru",
			"anna@exa mple.ru",
			"anna@-example.ru",
			"anna@example..ru",
			".anna@example.ru",
			"anna.@example.ru",
			"anna@example.r",
		]:
			with self.subTest(broken=broken), self.assertRaises(CheckoutError) as caught:
				validate_email(broken)
			self.assertEqual(caught.exception.code, "invalid_email")

	def test_rejects_absurdly_long_address(self):
		long_email = "a" * 250 + "@example.ru"
		with self.assertRaises(CheckoutError):
			validate_email(long_email)


class TestIsTruthy(unittest.TestCase):
	def test_consent_checkbox_comes_as_string(self):
		for value in ["on", "1", "true", "TRUE", " yes ", "да", 1, True]:
			with self.subTest(value=value):
				self.assertTrue(is_truthy(value))

	def test_everything_else_is_refusal(self):
		for value in [None, False, "", "0", "false", "нет", "off", 0]:
			with self.subTest(value=value):
				self.assertFalse(is_truthy(value))


class TestCourseAmount(unittest.TestCase):
	COURSE = {
		"name": "k0tk11jeri",
		"title": "Вводный мини-курс в профессию",
		"course_price": 3900.0,
		"paid_course": 1,
		"published": 1,
	}

	def test_amount_comes_from_course(self):
		self.assertEqual(course_amount(self.COURSE), 3900.0)

	def test_price_sent_in_request_is_ignored(self):
		tampered = dict(self.COURSE, amount=1, course_price="3900")
		self.assertEqual(course_amount(tampered), 3900.0)

	def test_rounds_to_kopecks(self):
		self.assertEqual(course_amount(dict(self.COURSE, course_price=3900.456)), 3900.46)

	def test_free_course_is_not_sellable(self):
		for price in [0, None, "", "не число"]:
			with self.subTest(price=price), self.assertRaises(CheckoutError) as caught:
				course_amount(dict(self.COURSE, course_price=price))
			self.assertEqual(caught.exception.code, "course_without_price")

	def test_unpaid_course_is_refused(self):
		with self.assertRaises(CheckoutError) as caught:
			course_amount(dict(self.COURSE, paid_course=0))
		self.assertEqual(caught.exception.code, "course_not_payable")

	def test_unpublished_course_is_refused(self):
		with self.assertRaises(CheckoutError) as caught:
			course_amount(dict(self.COURSE, published=0))
		self.assertEqual(caught.exception.code, "course_not_published")


class TestFindReusablePayment(unittest.TestCase):
	@staticmethod
	def row(name, minutes_ago, received=0):
		return {
			"name": name,
			"creation": (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S.%f"),
			"payment_received": received,
		}

	def test_fresh_unpaid_payment_is_reused(self):
		row = self.row("ACC-PAY-1", minutes_ago=5)
		self.assertEqual(find_reusable_payment([row], NOW), row)

	def test_paid_payment_is_skipped(self):
		self.assertIsNone(find_reusable_payment([self.row("ACC-PAY-1", 5, received=1)], NOW))

	def test_payment_older_than_an_hour_is_skipped(self):
		self.assertIsNone(find_reusable_payment([self.row("ACC-PAY-1", 61)], NOW))

	def test_newest_of_several_is_chosen(self):
		rows = [self.row("old", 40), self.row("new", 3), self.row("mid", 20)]
		self.assertEqual(find_reusable_payment(rows, NOW)["name"], "new")

	def test_empty_and_broken_rows_give_nothing(self):
		self.assertIsNone(find_reusable_payment([], NOW))
		self.assertIsNone(find_reusable_payment(None, NOW))
		self.assertIsNone(find_reusable_payment([{"name": "x", "creation": "вчера"}], NOW))

	def test_accepts_now_as_string_and_rows_as_objects(self):
		Row = namedtuple("Row", "name creation payment_received")
		row = Row("ACC-PAY-9", "2026-10-25 11:30:00", 0)
		found = find_reusable_payment([row], "2026-10-25 12:00:00")
		self.assertEqual(found.name, "ACC-PAY-9")

	def test_window_is_configurable(self):
		row = self.row("ACC-PAY-1", minutes_ago=90)
		self.assertIsNone(find_reusable_payment([row], NOW))
		self.assertEqual(find_reusable_payment([row], NOW, max_age_minutes=120), row)


class TestDocumentFields(unittest.TestCase):
	def test_payment_amount_is_the_course_amount(self):
		fields = build_payment_fields(
			member="anna@example.ru",
			billing_name="Анна",
			course="k0tk11jeri",
			amount=3900.0,
			source="Website",
		)
		self.assertEqual(fields["amount"], 3900.0)
		self.assertEqual(fields["amount_with_gst"], 3900.0)
		self.assertEqual(fields["currency"], "RUB")
		self.assertEqual(fields["payment_for_document_type"], "LMS Course")
		self.assertEqual(fields["payment_for_document"], "k0tk11jeri")
		self.assertEqual(fields["payment_received"], 0)

	def test_deal_fields_carry_consent_and_utm(self):
		data = {
			"name": "Анна",
			"email": "anna@example.ru",
			"telegram_id": "123456789",
			"telegram_username": "anna_p",
			"utm_source": "instagram",
			"utm_medium": "reels",
			"utm_campaign": "mini-course",
			"consent_version": "1.0",
		}
		fields = build_deal_fields(data, "2026-10-25 12:00:00", ip=" 10.0.0.7 ")
		self.assertEqual(fields["status"], STATUS_NEW)
		self.assertEqual(fields["first_name"], "Анна")
		self.assertEqual(fields["email"], "anna@example.ru")
		self.assertEqual(fields["rca_consent_at"], "2026-10-25 12:00:00")
		self.assertEqual(fields["rca_consent_version"], "1.0")
		self.assertEqual(fields["rca_consent_ip"], "10.0.0.7")
		self.assertEqual(fields["rca_utm_campaign"], "mini-course")

	def test_missing_optional_fields_become_empty_strings(self):
		fields = build_deal_fields({"name": "Анна", "email": "anna@example.ru"}, "2026-10-25 12:00:00")
		self.assertEqual(fields["rca_telegram_id"], "")
		self.assertEqual(fields["rca_utm_source"], "")
		self.assertEqual(fields["rca_consent_ip"], "")


class TestTelegramAndText(unittest.TestCase):
	def test_telegram_id_keeps_digits_only(self):
		self.assertEqual(normalize_telegram_id(" 123456789 "), "123456789")
		self.assertEqual(normalize_telegram_id("@anna"), "")

	def test_telegram_username_drops_the_at_sign(self):
		self.assertEqual(normalize_telegram_username("@anna_p"), "anna_p")
		self.assertEqual(normalize_telegram_username("anna_p"), "anna_p")
		self.assertEqual(normalize_telegram_username("anna p"), "")
		self.assertEqual(normalize_telegram_username(""), "")

	def test_strip_tags_cleans_frappe_messages(self):
		self.assertEqual(strip_tags("Сумма <b>больше</b><br>нуля"), "Сумма больше нуля")


if __name__ == "__main__":
	unittest.main()
