"""Уведомления Т-Банка об оплате.

Т-Банк ждёт HTTP 200 и тело ровно `OK`, иначе повторяет уведомление.
Поэтому любой отказ по формальным причинам (чужая подпись, неизвестный
OrderId) тоже отвечает OK и пишет причину в Error Log — повторять такое
бессмысленно. Потерянные уведомления добирает rca_payments.tasks.
"""

import json

import frappe
from frappe.utils import flt

from rca_payments.tbank_token import verify_token

PAID_STATUSES = {"CONFIRMED", "AUTHORIZED"}


def ok():
	"""HTTP 200 с телом ровно OK — ответ, который ждёт Т-Банк."""
	frappe.local.response["type"] = "txt"
	frappe.local.response["doctype"] = "ok"
	frappe.local.response["result"] = "OK"
	return "OK"


def get_payload():
	payload = frappe.request.get_json(silent=True) if frappe.request else None
	if not payload:
		payload = dict(frappe.form_dict or {})
	return payload or {}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def notify():
	"""Приём уведомления Т-Банка о смене статуса платежа."""
	payload = get_payload()

	try:
		settings = frappe.get_cached_doc("TBank Settings", "TBank")
		password = settings.get_password("password", raise_exception=False)
		if not password:
			reject("пароль терминала не задан", payload)
			return ok()

		if not verify_token(payload, password):
			reject("неверная подпись Token", payload)
			return ok()

		order_id = str(payload.get("OrderId") or "")
		payment = get_payment(order_id)
		if not payment:
			reject(f"платёж {order_id or '(без OrderId)'} не найден", payload)
			return ok()

		status = str(payload.get("Status") or "")
		if not payload.get("Success") or status not in PAID_STATUSES:
			note_status(payment, status, payload)
			return ok()

		if payment.payment_received:
			# Уведомление могло прийти дважды — второй раз ничего не делаем.
			return ok()

		if not amount_matches(payment, payload):
			reject(
				"сумма в уведомлении не совпала со счётом "
				f"{payment.name}: ожидали {payment.amount}, получили "
				f"{int(payload.get('Amount') or 0) / 100}",
				payload,
			)
			return ok()

		mark_paid(payment, payload)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(
			title="TBank: обработка уведомления упала",
			message=frappe.get_traceback(with_context=True),
		)

	return ok()


def get_payment(order_id):
	"""Счёт LMS ищем по OrderId, которым мы сделали имя счёта."""
	if not order_id:
		return None
	if frappe.db.exists("LMS Payment", order_id):
		return frappe.get_doc("LMS Payment", order_id)

	name = frappe.db.get_value("LMS Payment", {"order_id": order_id}, "name")
	return frappe.get_doc("LMS Payment", name) if name else None


def amount_matches(payment, payload):
	expected = flt(payment.amount_with_gst or payment.amount)
	return int(payload.get("Amount") or 0) == int(round(expected * 100))


def mark_paid(payment, payload):
	"""Отмечаем счёт оплаченным и выдаём доступ ученице."""
	frappe.db.set_value(
		"LMS Payment",
		payment.name,
		{"payment_received": 1, "payment_id": str(payload.get("PaymentId") or "")},
		update_modified=False,
	)

	member = payment.member
	if not member:
		frappe.log_error(
			title="TBank: у счёта нет ученицы",
			message=f"LMS Payment {payment.name} без поля member",
		)
		return

	# complete_enrollment создаёт LMS Enrollment от имени frappe.session.user,
	# а уведомление приходит гостем — без подмены доступ выдали бы гостю.
	session_user = frappe.session.user
	frappe.set_user(member)
	try:
		from lms.lms.utils import complete_enrollment

		complete_enrollment(
			payment.name,
			payment.payment_for_document_type,
			payment.payment_for_document,
		)
	finally:
		frappe.set_user(session_user)

	frappe.db.commit()


def reject(reason, payload):
	frappe.log_error(
		title=f"TBank: уведомление отклонено — {reason}",
		message=json.dumps(payload, ensure_ascii=False, indent=2)[:3000],
	)


def note_status(payment, status, payload):
	frappe.db.set_value(
		"LMS Payment",
		payment.name,
		"payment_id",
		str(payload.get("PaymentId") or ""),
		update_modified=False,
	)
	frappe.log_error(
		title=f"TBank: платёж {payment.name} в статусе {status or 'неизвестном'}",
		message=json.dumps(payload, ensure_ascii=False, indent=2)[:3000],
	)