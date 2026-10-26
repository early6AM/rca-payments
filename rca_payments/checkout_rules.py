"""Чистая логика оформления заказа — без frappe, чтобы её можно было тестировать.

Здесь только то, что не требует базы: проверка почты, разбор согласия,
цена курса, поиск свежего неоплаченного счёта, сборка полей документов.
Всё, что требует Frappe (get_doc, db, шлюз), живёт в rca_payments.checkout.
"""

import re
from datetime import datetime, timedelta

MAX_EMAIL = 254
PENDING_MINUTES = 60

# Статусы CRM Deal. Строки дублируют записи CRM Deal Status, которые заводит
# миграция CRM: опечатка здесь — это «сделка застряла в старом статусе».
STATUS_NEW = "Заявка оставлена"
STATUS_PENDING = "Ожидает оплаты"
STATUS_DECLINED = "Отказ"
STATUS_PAID = "Оплачено"

_TRUE_VALUES = {"1", "true", "yes", "on", "да"}

_EMAIL_RE = re.compile(
	r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
	r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
	r"[A-Za-z]{2,63}$"
)
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_TAGS_RE = re.compile(r"<[^>]+>")


class CheckoutError(Exception):
	"""Отказ с текстом для человека и машинным кодом для лендинга."""

	def __init__(self, code, message):
		super().__init__(message)
		self.code = code
		self.message = message


# --- строки и почта -------------------------------------------------------


def clean_text(value, limit=140):
	"""Схлопывает пробелы и обрезает длину: в CRM не должно быть простыней."""
	if value is None:
		return ""
	text = " ".join(str(value).split())
	return text[:limit]


def strip_tags(value):
	"""Убирает HTML из сообщений frappe.throw — в них бывают <br> и <b>."""
	return " ".join(_TAGS_RE.sub(" ", str(value or "")).split())


def normalize_email(value):
	return clean_text(value, MAX_EMAIL).lower()


def validate_email(value):
	"""Возвращает нормализованный адрес или отказывает с понятным текстом."""
	email = normalize_email(value)
	if not email:
		raise CheckoutError("invalid_email", "Укажите адрес почты — на него придёт доступ к курсу.")
	if len(email) > MAX_EMAIL or not _EMAIL_RE.match(email):
		raise CheckoutError(
			"invalid_email",
			"Похоже, в адресе почты опечатка. Проверьте его — на этот адрес придёт доступ к курсу.",
		)
	return email


def is_truthy(value):
	"""Галочка согласия приходит из формы строкой: 'on', 'true', '1'."""
	if value is True:
		return True
	if value is None or value is False:
		return False
	if isinstance(value, (int, float)):
		return value == 1
	return str(value).strip().lower() in _TRUE_VALUES


def normalize_telegram_id(value):
	"""Telegram ID — только цифры; мусор не сохраняем."""
	text = clean_text(value, 32)
	return text if text.isdigit() else ""


def normalize_telegram_username(value):
	"""@durov и durov — один и тот же ник, храним без собаки."""
	text = clean_text(value, 64).lstrip("@")
	return text if _USERNAME_RE.match(text) else ""


# --- курс и сумма ---------------------------------------------------------


def course_field(course, fieldname):
	"""Достаёт поле из dict, frappe._dict или документа — без import frappe."""
	if course is None:
		return None
	if hasattr(course, "get"):
		return course.get(fieldname)
	return getattr(course, fieldname, None)


def _as_float(value):
	try:
		return float(value or 0)
	except (TypeError, ValueError):
		return 0.0


def assert_course_payable(course):
	"""Платить онлайн можно только за платный и опубликованный курс."""
	if not is_truthy(course_field(course, "paid_course")):
		raise CheckoutError(
			"course_not_payable",
			"Этот курс не продаётся онлайн. Напишите нам — подскажем, как записаться.",
		)
	if not is_truthy(course_field(course, "published")):
		raise CheckoutError(
			"course_not_published",
			"Курс снят с публикации. Обновите страницу или напишите нам.",
		)
	return True


def course_amount(course):
	"""Сумма берётся только из курса — единственный доверенный источник цены."""
	assert_course_payable(course)
	amount = _as_float(course_field(course, "course_price"))
	if amount <= 0:
		raise CheckoutError(
			"course_without_price",
			"У курса не указана цена, оплатить его онлайн нельзя. Напишите нам — оформим вручную.",
		)
	return round(amount, 2)


# --- защита от дублей -----------------------------------------------------


def parse_datetime(value):
	"""Приводит creation из базы (строка или datetime) к datetime без зоны."""
	if value is None:
		return None
	if isinstance(value, datetime):
		moment = value
	elif isinstance(value, str):
		try:
			moment = datetime.fromisoformat(value.strip())
		except ValueError:
			return None
	else:
		return None
	if moment.tzinfo is not None:
		# Сравниваем с наивным now() из Frappe: сдвиг зоны здесь не важен,
		# потому что оба времени приходят из одной базы.
		moment = moment.replace(tzinfo=None)
	return moment


def find_reusable_payment(rows, now, max_age_minutes=PENDING_MINUTES):
	"""Самый свежий неоплаченный счёт не старше max_age_minutes или None.

	Повторная заявка в течение часа не должна плодить счета: банк получит
	тот же OrderId (имя счёта), а ученица — ту же платёжную ссылку.
	"""
	moment_now = parse_datetime(now)
	if moment_now is None:
		raise ValueError("now должен быть datetime или строкой даты")
	cutoff = moment_now - timedelta(minutes=max_age_minutes)

	best, best_created = None, None
	for row in rows or []:
		if is_truthy(course_field(row, "payment_received")):
			continue
		created = parse_datetime(course_field(row, "creation"))
		if created is None or created < cutoff:
			continue
		if best_created is None or created > best_created:
			best, best_created = row, created
	return best


# --- поля документов ------------------------------------------------------


def build_deal_fields(data, consent_at, ip=""):
	"""Поля CRM Deal из заявки. Согласие фиксируем с временем и IP."""
	return {
		"status": STATUS_NEW,
		"first_name": data["name"],
		"email": data["email"],
		"rca_telegram_id": data.get("telegram_id") or "",
		"rca_telegram_username": data.get("telegram_username") or "",
		"rca_utm_source": data.get("utm_source") or "",
		"rca_utm_medium": data.get("utm_medium") or "",
		"rca_utm_campaign": data.get("utm_campaign") or "",
		"rca_consent_at": consent_at,
		"rca_consent_version": data.get("consent_version") or "",
		"rca_consent_ip": clean_text(ip, 45),
	}


def build_payment_fields(member, billing_name, course, amount, source, currency="RUB"):
	"""Поля LMS Payment. Суммы из запроса тут нет и быть не может."""
	return {
		"member": member,
		"billing_name": billing_name,
		"source": source,
		"payment_for_document_type": "LMS Course",
		"payment_for_document": course,
		"amount": amount,
		"amount_with_gst": amount,
		"currency": currency,
		"payment_received": 0,
	}
