"""Эквайринг Т-Банка: настройки терминала и создание платежа."""

import json

import frappe
import requests
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, get_url

from rca_payments.tbank_token import make_token

DEFAULT_API_URL = "https://securepay.tinkoff.ru/v2/"
REQUEST_TIMEOUT = 20
SUCCESS_STATUSES = {"CONFIRMED", "AUTHORIZED"}


def get_notification_url():
	"""Адрес, который нужно указать в личном кабинете Т-Бизнеса."""
	return get_url("/api/method/rca_payments.tbank.notify")


class TBankSettings(Document):
	def validate(self):
		if self.enabled:
			if not self.terminal_key:
				frappe.throw(_("Укажите TerminalKey из личного кабинета Т-Бизнеса."))
			if not self.get_password("password", raise_exception=False):
				frappe.throw(_("Укажите пароль терминала (это не пароль от личного кабинета)."))

		self.api_url = (self.api_url or DEFAULT_API_URL).rstrip("/") + "/"
		self.notification_url = get_notification_url()

	# --- интерфейс платёжного шлюза Frappe ---------------------------------

	def get_payment_url(self, **kwargs):
		"""Создаёт платёж в Т-Банке и возвращает ссылку на платёжную форму.

		LMS открывает возвращённый адрес в браузере ученицы
		(frontend/src/pages/Billing.vue: window.location.href = data).
		"""
		if not self.enabled:
			frappe.throw(_("Приём платежей Т-Банка выключен в настройках."))

		amount = flt(kwargs.get("amount"))
		if amount <= 0:
			frappe.throw(_("Сумма платежа должна быть больше нуля."))

		payment_name = kwargs.get("payment")
		order_id = str(payment_name or frappe.generate_hash(length=16))

		payload = {
			"TerminalKey": self.terminal_key,
			"Amount": int(round(amount * 100)),
			"OrderId": order_id,
			"Description": self.get_description(kwargs),
			"NotificationURL": self.notification_url or get_notification_url(),
		}

		payer_email = kwargs.get("payer_email")
		if payer_email:
			payload["DATA"] = {"Email": payer_email}

		success_url = self.success_url or kwargs.get("redirect_to")
		if success_url:
			payload["SuccessURL"] = get_url(success_url)
		if self.fail_url:
			payload["FailURL"] = get_url(self.fail_url)

		if self.send_receipt:
			payload["Receipt"] = self.build_receipt(amount, payer_email)

		payload["Token"] = make_token(payload, self.get_password("password"))

		response = self.api_request("Init", payload)
		self.remember(response)

		if not response.get("Success"):
			frappe.log_error(
				title="TBank: Init отклонён",
				message=json.dumps(response, ensure_ascii=False, indent=2),
			)
			frappe.throw(
				_("Т-Банк отклонил создание платежа: {0}").format(
					response.get("Message") or response.get("Details") or response.get("ErrorCode")
				)
			)

		payment_url = response.get("PaymentURL")
		if not payment_url:
			frappe.throw(_("Т-Банк не вернул ссылку на платёжную форму."))

		self.remember_payment_ids(payment_name, order_id, response)
		return payment_url

	@frappe.whitelist()
	def check_credentials(self):
		"""Кнопка в настройках: показать сырой ответ Т-Банка на наш запрос."""
		frappe.only_for("System Manager")
		payload = {"TerminalKey": self.terminal_key, "PaymentId": "0"}
		payload["Token"] = make_token(payload, self.get_password("password"))
		response = self.api_request("GetState", payload)
		self.remember(response)
		return json.dumps(response, ensure_ascii=False, indent=2)

	# --- вспомогательное ---------------------------------------------------

	@staticmethod
	def get_description(kwargs):
		return str(kwargs.get("title") or _("Оплата обучения"))[:140]

	def build_receipt(self, amount, payer_email):
		"""Данные чека 54-ФЗ.

		Нужны только если у терминала НЕТ своей онлайн-кассы: иначе чек
		формирует сам Т-Банк и поле Receipt передавать не надо.
		"""
		kopecks = int(round(amount * 100))
		return {
			"Email": payer_email or "no-reply@rca.yachts",
			"Taxation": self.taxation or "usn_income",
			"Items": [
				{
					"Name": (self.receipt_item_name or "Оплата обучения")[:128],
					"Price": kopecks,
					"Quantity": 1,
					"Amount": kopecks,
					"Tax": self.vat or "none",
					"PaymentMethod": "full_payment",
					"PaymentObject": "service",
				}
			],
		}

	def api_request(self, method, payload):
		url = (self.api_url or DEFAULT_API_URL) + method
		try:
			response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
		except requests.RequestException as exception:
			frappe.log_error(title="TBank: запрос не ушёл", message=f"{url}\n{exception}")
			frappe.throw(_("Не удалось связаться с Т-Банком: {0}").format(exception))

		try:
			return response.json()
		except ValueError:
			frappe.log_error(
				title="TBank: неожиданный ответ",
				message=f"{url}\nHTTP {response.status_code}\n{response.text[:2000]}",
			)
			frappe.throw(_("Т-Банк вернул неожиданный ответ (HTTP {0}).").format(response.status_code))

	def remember(self, response):
		"""Сохраняем последний ответ банка — иначе отладка вслепую."""
		try:
			frappe.db.set_value(
				"TBank Settings",
				"TBank",
				"last_status",
				json.dumps(response, ensure_ascii=False, indent=2)[:2000],
				update_modified=False,
			)
		except Exception:
			pass

	def remember_payment_ids(self, payment_name, order_id, response):
		"""Пишем OrderId и PaymentId в счёт LMS: по ним придёт уведомление."""
		if not payment_name or not frappe.db.exists("LMS Payment", payment_name):
			return
		frappe.db.set_value(
			"LMS Payment",
			payment_name,
			{"order_id": order_id, "payment_id": str(response.get("PaymentId") or "")},
			update_modified=False,
		)