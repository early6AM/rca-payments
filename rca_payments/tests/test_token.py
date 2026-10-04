"""Подпись Token — единственный алгоритм, который легко сломать молча."""

import hashlib
import unittest

from rca_payments.tbank_token import make_token, verify_token


class TestMakeToken(unittest.TestCase):
	def test_sorts_by_key_and_appends_password(self):
		payload = {"TerminalKey": "TinkoffBankTest", "Amount": 100000, "OrderId": "order-1"}
		expected = hashlib.sha256(
			("100000" + "order-1" + "secret" + "TinkoffBankTest").encode("utf-8")
		).hexdigest()
		self.assertEqual(make_token(payload, "secret"), expected)

	def test_nested_objects_are_excluded(self):
		flat = {"TerminalKey": "t", "Amount": 500}
		with_nested = {
			"TerminalKey": "t",
			"Amount": 500,
			"Receipt": {"Email": "a@b.c", "Items": [{"Name": "x"}]},
			"DATA": {"Email": "a@b.c"},
		}
		self.assertEqual(make_token(flat, "p"), make_token(with_nested, "p"))

	def test_booleans_are_lowercase(self):
		self.assertEqual(
			make_token({"Success": True}, "p"),
			make_token({"Success": "true"}, "p"),
		)

	def test_existing_token_is_ignored(self):
		self.assertEqual(
			make_token({"Amount": 1}, "p"),
			make_token({"Amount": 1, "Token": "что-то"}, "p"),
		)

	def test_verify_accepts_own_signature_and_rejects_other_password(self):
		payload = {"TerminalKey": "t", "OrderId": "o", "Success": True, "Status": "CONFIRMED"}
		payload["Token"] = make_token(payload, "secret")
		self.assertTrue(verify_token(payload, "secret"))
		self.assertFalse(verify_token(payload, "другой"))


if __name__ == "__main__":
	unittest.main()
