"""Срок доступа к курсу: 90 дней от оплаты, потом закрытие.

Штатного срока в Frappe Learning нет: запись на курс бессрочная. Поэтому
срок живёт в своём поле `LMS Enrollment.rca_access_until` (последний день
доступа), а закрытие — удаление записи на курс: это единственная точка,
через которую LMS пускает к урокам, файлам и тестам, поэтому закрывается
всё сразу и нет риска забыть какой-то эндпоинт.

Порядок:
1. Запись на курс создаётся (оплата или ручная выдача) — ставим срок.
2. За N дней до конца создаём сделку в CRM и задачу менеджеру.
3. В день после последнего дня доступа запись удаляется, факт пишется
   в журнал `RCA Access Closure` (по нему доступ можно вернуть).

Режим `enforce` в RCA Access Settings выключен по умолчанию: задача только
пишет в лог, кого бы закрыла. Так прод проверяется без последствий.
"""

import os

import frappe
from frappe import _
from frappe.utils import getdate, now, nowdate

from rca_payments.access_rules import (
	ACCESS_DAYS,
	WARN_DAYS_BEFORE,
	access_until_from,
	days_left,
	is_expired,
	should_warn,
)

ACCESS_FIELD = "rca_access_until"
DEAL_FIELD = "rca_expiry_deal"
SETTINGS = "RCA Access Settings"
CLOSURE = "RCA Access Closure"
DEAL_STATUS = "Заканчивается доступ"
SCRIPT_PATH = ("public", "js", "access_notice.js")

_script_cache = {}


# --------------------------------------------------------------------------
# Настройки
# --------------------------------------------------------------------------


def get_settings():
	return frappe.get_cached_doc(SETTINGS)


def access_days():
	try:
		value = get_settings().access_days
	except Exception:
		value = None
	return int(value or ACCESS_DAYS)


def warn_days():
	try:
		value = get_settings().warn_days_before
	except Exception:
		value = None
	return int(value if value is not None else WARN_DAYS_BEFORE)


def is_enforced():
	"""Выключено — задача только пишет, кого бы закрыла. Включается в настройках."""
	try:
		return bool(get_settings().enforce)
	except Exception:
		return False


# --------------------------------------------------------------------------
# Срок при выдаче доступа
# --------------------------------------------------------------------------


def stamp_enrollment(doc, method=None):
	"""Ставит срок доступа новой записи на курс.

	Дата отсчёта — сегодняшний день сайта: для оплаты это день, когда
	пришло уведомление банка и выдался доступ, для ручной выдачи — день
	выдачи. Уже заполненное поле не перетираем: ручной срок главнее.
	"""
	if not frappe.get_meta("LMS Enrollment").has_field(ACCESS_FIELD):
		return
	if doc.get(ACCESS_FIELD):
		return
	doc.set(ACCESS_FIELD, access_until_from(nowdate()))


def enrollment_until(enrollment):
	"""Срок доступа записи на курс или None, если он не задан (бессрочно)."""
	if not frappe.get_meta("LMS Enrollment").has_field(ACCESS_FIELD):
		return None
	return frappe.db.get_value("LMS Enrollment", enrollment, ACCESS_FIELD)


# --------------------------------------------------------------------------
# Ежедневная задача
# --------------------------------------------------------------------------


def daily():
	"""Предупреждения менеджеру и закрытие доступа."""
	if not frappe.get_meta("LMS Enrollment").has_field(ACCESS_FIELD):
		# Поле ещё не создано (патч не применён) — молчим, а не падаем.
		return

	warn_expiring()
	close_expired()
	frappe.db.commit()


def warn_expiring():
	"""За N дней до конца — сделка в CRM и задача менеджеру, по одной на доступ."""
	today = nowdate()
	rows = frappe.get_all(
		"LMS Enrollment",
		filters={
			ACCESS_FIELD: ["is", "set"],
			DEAL_FIELD: ["is", "not set"],
		},
		fields=["name", "member", "course", ACCESS_FIELD],
	)
	for row in rows:
		if not should_warn(row.get(ACCESS_FIELD), today, warn_days()):
			continue
		try:
			deal = create_renewal_deal(row)
			if deal:
				frappe.db.set_value("LMS Enrollment", row.name, DEAL_FIELD, deal, update_modified=False)
				frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title="Срок доступа: сделка не создалась",
				message=f"Запись на курс {row.name} ({row.member})\n{frappe.get_traceback(with_context=True)}",
			)


def close_expired():
	"""Удаляет записи на курс, у которых срок доступа прошёл."""
	today = nowdate()
	enforced = is_enforced()
	rows = frappe.get_all(
		"LMS Enrollment",
		filters={ACCESS_FIELD: ["<", today]},
		fields=["name", "member", "course", "payment", ACCESS_FIELD],
	)

	for row in rows:
		if not enforced:
			frappe.logger("rca_access").info(
				"dry-run: закрыл бы доступ %s (%s, курс %s, срок %s)",
				row.member,
				row.name,
				row.course,
				row.get(ACCESS_FIELD),
			)
			continue

		try:
			close_enrollment(row)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title="Срок доступа: доступ не закрылся",
				message=f"Запись на курс {row.name} ({row.member})\n{frappe.get_traceback(with_context=True)}",
			)


def close_enrollment(row):
	"""Пишет факт в журнал и удаляет запись на курс."""
	course_title = frappe.db.get_value("LMS Course", row.course, "title") or row.course
	member_name = frappe.db.get_value("User", row.member, "full_name") or row.member

	frappe.get_doc(
		{
			"doctype": CLOSURE,
			"member": row.member,
			"member_name": member_name,
			"course": row.course,
			"course_title": course_title,
			"access_until": row.get(ACCESS_FIELD),
			"closed_on": now(),
			"payment": row.get("payment"),
			"enrollment": row.name,
			"note": "Доступ закрыт автоматически по сроку",
		}
	).insert(ignore_permissions=True)

	frappe.delete_doc("LMS Enrollment", row.name, ignore_permissions=True, force=True)


# --------------------------------------------------------------------------
# Сделка менеджеру
# --------------------------------------------------------------------------


def create_renewal_deal(row):
	"""Сделка «продление» на владельца из настроек. Возвращает имя сделки."""
	if not frappe.db.exists("DocType", "CRM Deal"):
		return None

	settings = get_settings()
	owner = settings.manager or "Administrator"
	status = settings.renewal_status if frappe.db.exists("CRM Deal Status", settings.renewal_status) else None
	if not status:
		frappe.log_error(
			title="Срок доступа: нет статуса сделки",
			message=f"В CRM Deal Status нет «{settings.renewal_status}» — сделка не создана.",
		)
		return None

	member = row.member
	meta = frappe.get_meta("CRM Deal")
	person = frappe.db.get_value("User", member, ["full_name", "first_name"], as_dict=True) or frappe._dict()
	name = person.get("full_name") or person.get("first_name") or member

	values = {
		"doctype": "CRM Deal",
		"status": status,
		"deal_owner": owner,
		"first_name": name,
		"email": member,
		"contacts": [{"full_name": name, "email": member, "is_primary": 1}],
		"expected_closure_date": row.get(ACCESS_FIELD),
		"source": settings.lead_source or "",
	}
	# Telegram и метки берём из прежней сделки этого человека: почта у LMS
	# есть всегда, а мессенджер — только в CRM.
	previous = frappe.get_all(
		"CRM Deal",
		filters={"email": member},
		fields=["rca_telegram_id", "rca_telegram_username", "rca_utm_source", "rca_utm_medium", "rca_utm_campaign"],
		order_by="creation desc",
		limit=1,
	)
	if previous:
		for field in (
			"rca_telegram_id",
			"rca_telegram_username",
			"rca_utm_source",
			"rca_utm_medium",
			"rca_utm_campaign",
		):
			values[field] = previous[0].get(field) or ""

	values = {field: value for field, value in values.items() if field == "doctype" or meta.has_field(field)}

	deal = frappe.get_doc(values)
	deal.flags.ignore_permissions = True
	deal.insert(ignore_permissions=True)

	create_manager_task(deal.name, row, owner)
	return deal.name


def create_manager_task(deal, row, owner):
	"""Задача менеджеру: написать ученице до окончания доступа."""
	if not frappe.db.exists("DocType", "CRM Task"):
		return

	until = row.get(ACCESS_FIELD)
	course_title = frappe.db.get_value("LMS Course", row.course, "title") or row.course
	description = (
		f"Доступ к курсу «{course_title}» открыт до {frappe.utils.format_date(until)}.\n"
		f"Написать ученице и предупредить, что доступ закрывается, — "
		f"предложить продление."
	)

	task = frappe.get_doc(
		{
			"doctype": "CRM Task",
			"title": _("Написать ученице об окончании доступа"),
			"description": description,
			"due_date": nowdate(),
			"assigned_to": owner,
			"reference_doctype": "CRM Deal",
			"reference_docname": deal,
		}
	)
	task.flags.ignore_permissions = True
	task.insert(ignore_permissions=True)


# --------------------------------------------------------------------------
# Что видит ученица
# --------------------------------------------------------------------------


@frappe.whitelist()
def my_access(course=None):
	"""Срок доступа текущей ученицы к курсу. Пустой ответ — показывать нечего."""
	user = frappe.session.user
	if not course or user in (None, "", "Guest"):
		return {}

	if not frappe.get_meta("LMS Enrollment").has_field(ACCESS_FIELD):
		return {}

	until = frappe.db.get_value("LMS Enrollment", {"member": user, "course": course}, ACCESS_FIELD)
	if not until:
		return {}

	return {
		"course": course,
		"ends_on": str(getdate(until)),
		"days_left": days_left(until, nowdate()),
		"expired": is_expired(until, nowdate()),
		"warn_days": warn_days(),
	}


# --------------------------------------------------------------------------
# Поле с датой на странице курса
# --------------------------------------------------------------------------


def inject_notice(response=None, request=None):
	"""Дописывает скрипт поля «доступ до …» на страницы LMS.

	Фронтенд LMS — собранный бандл без точки расширения, поэтому поле
	рисует наш скрипт: он спрашивает срок у my_access и вставляет строку
	под названием курса. Файлы LMS не правим, откат — снять этот хук.
	"""
	try:
		if response is None or request is None:
			return
		if not (request.path or "").startswith("/lms"):
			return
		if frappe.session.user in (None, "", "Guest"):
			return
		if getattr(response, "direct_passthrough", False):
			return
		if response.status_code != 200:
			return
		if "text/html" not in (response.headers.get("Content-Type") or ""):
			return

		html = response.get_data(as_text=True)
		if "data-rca-access-notice" in html:
			return
		script = notice_script()
		if not script:
			return

		tag = f'<script data-rca-access-notice="1">{script}</script>'
		if "</body>" in html:
			html = html.replace("</body>", tag + "\n</body>", 1)
		else:
			html = html + tag
		response.set_data(html)
	except Exception:
		frappe.log_error(
			title="Срок доступа: скрипт не вставился",
			message=frappe.get_traceback(with_context=True),
		)


def notice_script():
	"""Содержимое скрипта, прочитанное один раз и обновляемое по mtime."""
	path = frappe.get_app_path("rca_payments", *SCRIPT_PATH)
	try:
		mtime = os.path.getmtime(path)
	except OSError:
		return ""
	cached = _script_cache.get(path)
	if cached and cached[0] == mtime:
		return cached[1]
	with open(path, encoding="utf-8") as handle:
		text = handle.read()
	_script_cache[path] = (mtime, text)
	return text
