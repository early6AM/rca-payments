"""Поля срока доступа и настройки по умолчанию.

Патч, а не after_install: приложение на сайте уже стоит, а after_install
выполняется только при установке. bench migrate прогонит этот патч на
живом сайте, поэтому он должен быть идемпотентным и не трогать данные,
кроме пустого поля срока у существующих записей.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field
from frappe.utils import getdate

from rca_payments.access_rules import access_until_from

ACCESS_FIELD = "rca_access_until"
DEAL_FIELD = "rca_expiry_deal"
DEAL_STATUS = "Заканчивается доступ"


def execute():
	ensure_fields()
	ensure_deal_status()
	ensure_settings()
	backfill_existing()


def ensure_fields():
	"""Срок доступа и ссылка на сделку в записи на курс."""
	if not frappe.db.exists("DocType", "LMS Enrollment"):
		return

	if not frappe.get_meta("LMS Enrollment").has_field(ACCESS_FIELD):
		create_custom_field(
			"LMS Enrollment",
			{
				"fieldname": ACCESS_FIELD,
				"label": "Доступ до",
				"fieldtype": "Date",
				"insert_after": "member",
				"in_list_view": 1,
				"in_standard_filter": 1,
				"description": "Последний день доступа к курсу. Пусто — доступ бессрочный.",
			},
		)

	if not frappe.get_meta("LMS Enrollment").has_field(DEAL_FIELD):
		create_custom_field(
			"LMS Enrollment",
			{
				"fieldname": DEAL_FIELD,
				"label": "Сделка о продлении",
				"fieldtype": "Link",
				"options": "CRM Deal",
				"insert_after": ACCESS_FIELD,
				"read_only": 1,
				"description": "Заполняется автоматически за неделю до окончания доступа.",
			},
		)


def ensure_deal_status():
	"""Отдельный статус, чтобы продления не смешивались с первыми заявками."""
	if not frappe.db.exists("DocType", "CRM Deal Status"):
		return
	if frappe.db.exists("CRM Deal Status", DEAL_STATUS):
		return
	frappe.get_doc(
		{
			"doctype": "CRM Deal Status",
			"deal_status": DEAL_STATUS,
			"type": "Ongoing",
			"color": "orange",
			"position": 7,
		}
	).insert(ignore_permissions=True)


def ensure_settings():
	"""Настройки по умолчанию: 90 дней, 7 дней предупреждения, закрытие выключено."""
	if not frappe.db.exists("DocType", "RCA Access Settings"):
		return

	settings = frappe.get_single("RCA Access Settings")
	changed = False
	defaults = {
		"access_days": 90,
		"warn_days_before": 7,
		"renewal_status": DEAL_STATUS if frappe.db.exists("CRM Deal Status", DEAL_STATUS) else None,
		"lead_source": "Existing Customer" if frappe.db.exists("CRM Lead Source", "Existing Customer") else None,
	}
	for field, value in defaults.items():
		if value and not settings.get(field):
			settings.set(field, value)
			changed = True
	if changed:
		settings.flags.ignore_permissions = True
		settings.save(ignore_permissions=True)


def backfill_existing():
	"""Существующим записям на курс срок считается от даты записи.

	До этого патча поля не было и доступ был бессрочным; правило «90 дней»
	теперь общее, поэтому ставим срок и старым записям. Уже заполненное
	поле не трогаем.
	"""
	if not frappe.get_meta("LMS Enrollment").has_field(ACCESS_FIELD):
		return

	rows = frappe.get_all(
		"LMS Enrollment",
		filters={ACCESS_FIELD: ["is", "not set"]},
		fields=["name", "creation"],
	)
	for row in rows:
		frappe.db.set_value(
			"LMS Enrollment",
			row.name,
			ACCESS_FIELD,
			access_until_from(getdate(row.creation)),
			update_modified=False,
		)
