"""Сверка неоплаченных счетов через GetState.

Уведомление от банка может не дойти: ученица закрыла вкладку, сайт был
недоступен, обработчик упал. Деньги при этом списаны. Поэтому раз в час
спрашиваем у Т-Банка статус по свежим неоплаченным счетам.
"""

import frappe
from frappe.utils import add_days, nowdate

from rca_payments.rca_payments.doctype.tbank_settings.tbank_settings import (
	SUCCESS_STATUSES,
	make_token,
)
from rca_payments.tbank import mark_paid

BATCH_SIZE = 25
LOOKBACK_DAYS = 3


def reconcile_pending_payments():
	settings = frappe.get_cached_doc("TBank Settings", "TBank")
	if not settings.enabled:
		return

	password = settings.get_password("password", raise_exception=False)
	if not password:
		return

	pending = frappe.get_all(
		"LMS Payment",
		filters={
			"payment_received": 0,
			"order_id": ["is", "set"],
			"creation": [">", add_days(nowdate(), -LOOKBACK_DAYS)],
		},
		fields=["name"],
		limit=BATCH_SIZE,
		order_by="creation desc",
	)

	for row in pending:
		try:
			check_payment(row.name, settings, password)
		except Exception:
			frappe.log_error(
				title="TBank: сверка счёта упала",
				message=frappe.get_traceback(with_context=True),
			)

	frappe.db.commit()


def check_payment(payment_name, settings, password):
	payment = frappe.get_doc("LMS Payment", payment_name)

	payload = {"TerminalKey": settings.terminal_key, "OrderId": payment.order_id}
	payload["Token"] = make_token(payload, password)

	response = settings.api_request("GetState", payload)
	if not response.get("Success"):
		return
	if str(response.get("Status") or "") not in SUCCESS_STATUSES:
		return

	payment.reload()
	if payment.payment_received:
		return

	mark_paid(payment, response)
