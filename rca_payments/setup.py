"""Создание настроек и записи шлюза при установке приложения."""

import frappe


def after_install():
	ensure_gateway()
	ensure_access()


def ensure_access():
	"""Поля срока доступа и настройки — те же шаги, что в патче миграции."""
	from rca_payments.patches.v1_0.add_access_fields import (
		ensure_deal_status,
		ensure_fields,
		ensure_settings,
	)

	ensure_fields()
	ensure_deal_status()
	ensure_settings()


def ensure_gateway():
	"""Создаёт TBank Settings и Payment Gateway, чтобы шлюз появился в LMS."""
	if not frappe.db.exists("TBank Settings", "TBank"):
		# ignore_mandatory: TerminalKey и пароль обязательны в форме, но
		# заполнять их должен администратор, а не установщик.
		frappe.get_doc(
			{
				"doctype": "TBank Settings",
				"gateway_name": "TBank",
				"enabled": 0,
				"api_url": "https://securepay.tinkoff.ru/v2/",
				"receipt_item_name": "Оплата обучения",
				"taxation": "usn_income",
				"vat": "none",
			}
		).insert(ignore_permissions=True, ignore_mandatory=True)

	if not frappe.db.exists("Payment Gateway", "TBank"):
		frappe.get_doc(
			{
				"doctype": "Payment Gateway",
				"gateway": "TBank",
				"gateway_settings": "TBank Settings",
				"gateway_controller": "TBank",
			}
		).insert(ignore_permissions=True)

	# Не перетираем уже выбранный шлюз на сайте.
	if not frappe.db.get_single_value("LMS Settings", "payment_gateway"):
		frappe.db.set_single_value("LMS Settings", "payment_gateway", "TBank")
