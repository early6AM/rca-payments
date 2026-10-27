"""Расчёт срока доступа к курсу.

Frappe здесь не нужен — поэтому правила живут отдельно и проверяются
обычным unittest, без bench. Всё, что требует базы, — в rca_payments.access.

Срок — 90 календарных дней, считая день оплаты первым днём. Значит
последний день доступа = дата оплаты + 89 дней, а закрытие происходит
на следующий день. Для ученицы это ровно 90 дней с доступом.
"""

from datetime import date, datetime, timedelta

ACCESS_DAYS = 90
WARN_DAYS_BEFORE = 7


def to_date(value):
	"""Дата из date/datetime/строки ISO. Строка с временем обрезается до дня."""
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def access_until_from(start):
	"""Последний день доступа для оплаты (или выдачи) в день start."""
	return to_date(start) + timedelta(days=ACCESS_DAYS - 1)


def days_left(until, today):
	"""Сколько дней доступа осталось: 0 — последний день, -1 — уже закрыт."""
	return (to_date(until) - to_date(today)).days


def is_expired(until, today):
	"""Доступ закрыт: последний день уже прошёл."""
	return to_date(until) < to_date(today)


def should_warn(until, today, warn_days=WARN_DAYS_BEFORE):
	"""Пора предупреждать менеджера: до конца доступа осталось warn_days дней."""
	return 0 <= days_left(until, today) <= warn_days
