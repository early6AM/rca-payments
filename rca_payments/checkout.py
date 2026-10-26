"""Оформление заказа с лендинга: гость → User + CRM Deal + счёт → ссылка Т-Банка.

Эндпоинт гостевой, его зовёт форма на лендинге. Поэтому frappe.session.user
здесь бесполезен: ученицу находим или заводим по email, а сумму берём только
из LMS Course.course_price — цену из запроса принимать нельзя, её подменят
в консоли браузера.

Ответ всегда 200 и всегда JSON: лендингу нужно показать текст отказа, а не
страницу ошибки Frappe. Успех — {"ok": true, "payment_url": ..., "payment": ...},
отказ — {"ok": false, "error": "<код>", "message": "<текст для человека>"}.
"""

import frappe
import frappe.rate_limiter
from frappe.utils import add_to_date, now

from rca_payments.checkout_rules import (
	PENDING_MINUTES,
	STATUS_NEW,
	STATUS_PENDING,
	CheckoutError,
	build_deal_fields,
	build_payment_fields,
	clean_text,
	course_amount,
	find_reusable_payment,
	is_truthy,
	normalize_telegram_id,
	normalize_telegram_username,
	strip_tags,
	validate_email,
)

GATEWAY = "TBank"
SOURCE = "Website"
CURRENCY = "RUB"
COURSE_DOCTYPE = "LMS Course"
REDIRECT_TO = "/mini-course/thanks"
STUDENT_ROLE = "LMS Student"

# 10 заявок в час с одного IP: живой человек столько не отправит, а перебор
# адресов и создание пачки пользователей — обрубит.
RATE_LIMIT = 10
RATE_WINDOW = 60 * 60


@frappe.whitelist(allow_guest=True, methods=["POST"])
@frappe.rate_limiter.rate_limit(limit=RATE_LIMIT, seconds=RATE_WINDOW)
def checkout():
	"""Создаёт заявку и счёт, возвращает ссылку на оплату."""
	try:
		return place_order()
	except CheckoutError as exc:
		return fail(exc.code, exc.message)
	except Exception:
		# Счёт мог уже уйти в банк — поэтому не откатываем транзакцию,
		# а разбираемся по Error Log: деньги важнее чистоты базы.
		frappe.log_error(
			title="Checkout: оформление заказа упало",
			message=frappe.get_traceback(with_context=True),
		)
		return fail(
			"internal",
			"Не удалось оформить заказ. Попробуйте ещё раз через минуту — "
			"или напишите нам, мы поможем.",
		)


def place_order():
	data = read_request()
	course = load_course(data["course"])
	amount = course_amount(course)
	member = ensure_member(data["email"], data["name"])

	if already_enrolled(member, data["course"]):
		return {
			"ok": True,
			"already_enrolled": True,
			"payment_url": None,
			"payment": None,
			"message": "Вы уже записаны на этот курс — доступ ждёт в личном кабинете.",
		}

	# Свежий неоплаченный счёт переиспользуем: повторная заявка не должна
	# плодить записи и новые OrderId в банке.
	payment = find_recent_payment(member, data["course"])
	reused = bool(payment)
	deal = find_open_deal(data["email"]) if reused else None
	if not deal:
		deal = create_deal(data)
	if not payment:
		payment = create_payment(member, data, amount)

	try:
		payment_url = init_payment(payment, data, course, amount)
	except frappe.ValidationError as exc:
		if not reused:
			drop_messages()
			raise CheckoutError("gateway", gateway_message(exc)) from exc
		# Банк не принял повторный Init по тому же OrderId — заводим новый счёт.
		drop_messages()
		frappe.log_error(
			title="Checkout: банк не принял повторный Init",
			message=f"Счёт {payment} переиспользован, но банк отказал: {strip_tags(exc)}\n"
			+ frappe.get_traceback(with_context=True),
		)
		payment = create_payment(member, data, amount)
		try:
			payment_url = init_payment(payment, data, course, amount)
		except frappe.ValidationError as second:
			drop_messages()
			raise CheckoutError("gateway", gateway_message(second)) from second

	set_deal_status(deal, STATUS_PENDING)
	frappe.db.commit()

	return {
		"ok": True,
		"already_enrolled": False,
		"payment_url": payment_url,
		"payment": payment,
	}


# --- вход -----------------------------------------------------------------


def read_request():
	form = frappe.form_dict or {}
	data = {
		"name": clean_text(form.get("name"), 140),
		"email": validate_email(form.get("email")),
		"course": clean_text(form.get("course"), 140),
		"telegram_id": normalize_telegram_id(form.get("telegram_id")),
		"telegram_username": normalize_telegram_username(form.get("telegram_username")),
		"utm_source": clean_text(form.get("utm_source"), 100),
		"utm_medium": clean_text(form.get("utm_medium"), 100),
		"utm_campaign": clean_text(form.get("utm_campaign"), 100),
		"consent_version": clean_text(form.get("consent_version"), 40) or "не указана",
		"consent": is_truthy(form.get("consent")),
	}

	if not data["name"]:
		raise CheckoutError("invalid_name", "Укажите имя — так мы подпишем письмо и доступ.")
	if not data["course"]:
		raise CheckoutError("invalid_course", "Не выбран курс. Обновите страницу и попробуйте снова.")
	if not data["consent"]:
		raise CheckoutError(
			"no_consent",
			"Без согласия на обработку персональных данных мы не можем оформить заказ. "
			"Поставьте галочку — и попробуйте снова.",
		)
	return data


def load_course(course):
	course_doc = frappe.db.get_value(
		COURSE_DOCTYPE,
		course,
		["name", "title", "course_price", "paid_course", "published"],
		as_dict=True,
	)
	if not course_doc:
		raise CheckoutError(
			"course_not_found",
			"Такой курс не найден. Обновите страницу и попробуйте снова.",
		)
	return course_doc


# --- ученица --------------------------------------------------------------


def ensure_member(email, name):
	"""Находит User по email или заводит нового с ролью LMS Student.

	Существующего пользователя не трогаем: он мог сам сменить имя, пароль
	и настройки — перезапись из гостевой формы была бы захватом аккаунта.
	"""
	existing = frappe.db.get_value("User", email, ["name", "enabled"], as_dict=True)
	if existing:
		if not existing.enabled:
			raise CheckoutError(
				"user_disabled",
				"Доступ к этому адресу почты закрыт. Напишите нам — разберёмся.",
			)
		return existing.name

	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": name,
			"full_name": name,
			"enabled": 1,
			"user_type": "Website User",
			# Frappe отправит письмо со ссылкой на установку пароля при insert.
			"send_welcome_email": 1,
		}
	)
	try:
		user.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		# Параллельная заявка успела завести того же пользователя — это не ошибка.
		return email

	try:
		user.add_roles(STUDENT_ROLE)
	except Exception:
		frappe.log_error(
			title="Checkout: не удалось выдать роль LMS Student",
			message=frappe.get_traceback(with_context=True),
		)
	return user.name


def already_enrolled(member, course):
	return bool(frappe.db.exists("LMS Enrollment", {"member": member, "course": course}))


# --- CRM ------------------------------------------------------------------


def crm_available():
	try:
		return bool(frappe.db.exists("DocType", "CRM Deal"))
	except Exception:
		return False


def find_open_deal(email):
	"""Незакрытая сделка этого человека за последний час — чтобы не дублировать."""
	if not email or not crm_available():
		return None
	try:
		rows = frappe.get_all(
			"CRM Deal",
			filters={
				"email": email,
				"status": ["in", [STATUS_NEW, STATUS_PENDING]],
				"creation": [">", add_to_date(now(), minutes=-PENDING_MINUTES)],
			},
			pluck="name",
			order_by="creation desc",
			limit=1,
		)
	except Exception:
		frappe.log_error(
			title="Checkout: не удалось найти сделку",
			message=frappe.get_traceback(with_context=True),
		)
		return None
	return rows[0] if rows else None


def create_deal(data):
	"""Сделка в CRM — витрина заявки. Без CRM заказ всё равно оформляется."""
	if not crm_available():
		frappe.log_error(
			title="Checkout: CRM Deal недоступна",
			message="Доктайп CRM Deal не найден — заявка создана без сделки.",
		)
		return None

	# Поля rca_* заводит миграция CRM: если её ещё не применили, заявка
	# не должна падать из-за отсутствующего поля.
	meta = frappe.get_meta("CRM Deal")
	values = {
		field: value
		for field, value in build_deal_fields(data, now(), request_ip()).items()
		if meta.has_field(field)
	}

	try:
		deal = frappe.get_doc({"doctype": "CRM Deal", **values})
		deal.insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(
			title="Checkout: сделка не создалась",
			message=frappe.get_traceback(with_context=True),
		)
		return None
	return deal.name


def set_deal_status(deal, status):
	"""Смена статуса сделки не должна ломать оплату — поэтому глушим ошибки."""
	if not deal:
		return
	try:
		if not frappe.db.exists("CRM Deal Status", status):
			frappe.log_error(
				title="Checkout: нет статуса сделки",
				message=f"В CRM Deal Status нет «{status}» — сделка {deal} осталась в прежнем статусе.",
			)
			return
		frappe.db.set_value("CRM Deal", deal, "status", status, update_modified=False)
	except Exception:
		frappe.log_error(
			title="Checkout: статус сделки не сменился",
			message=frappe.get_traceback(with_context=True),
		)


def request_ip():
	try:
		return clean_text(getattr(frappe.local, "request_ip", None), 45)
	except Exception:
		return ""


# --- счёт и банк ----------------------------------------------------------


def resolve_source():
	"""LMS Source обязателен на счёте; 'Website' заведён миграцией LMS."""
	if frappe.db.exists("LMS Source", SOURCE):
		return SOURCE
	fallback = frappe.get_all("LMS Source", pluck="name", limit=1)
	if fallback:
		frappe.log_error(
			title="Checkout: источник Website не найден",
			message=f"На счёте проставлен первый доступный источник: {fallback[0]}",
		)
		return fallback[0]
	raise CheckoutError(
		"config",
		"Приём оплаты временно недоступен. Напишите нам — оформим вручную.",
	)


def create_payment(member, data, amount):
	"""Счёт LMS. amount приходит из course_amount(), а не из запроса."""
	values = build_payment_fields(
		member=member,
		billing_name=data["name"],
		course=data["course"],
		amount=amount,
		source=resolve_source(),
		currency=CURRENCY,
	)
	payment = frappe.get_doc({"doctype": "LMS Payment", **values})
	# ignore_mandatory: address (Link Address) в гостевой форме не заполняется,
	# LMS требует его только для индийского GST-чека.
	payment.insert(ignore_permissions=True, ignore_mandatory=True)
	# Коммитим до похода в банк: если Init упадёт, счёт останется и следующий
	# заход переиспользует его, а не заведёт второй.
	frappe.db.commit()
	return payment.name


def find_recent_payment(member, course):
	"""Имя неоплаченного счёта этого человека на этот курс младше часа."""
	rows = frappe.get_all(
		"LMS Payment",
		filters={
			"member": member,
			"payment_for_document_type": COURSE_DOCTYPE,
			"payment_for_document": course,
			"payment_received": 0,
			"creation": [">", add_to_date(now(), minutes=-PENDING_MINUTES)],
		},
		fields=["name", "creation", "payment_received"],
		order_by="creation desc",
		limit=5,
	)
	row = find_reusable_payment(rows, now())
	return row["name"] if row else None


def init_payment(payment, data, course, amount):
	"""Отдаём счёт в банк и получаем ссылку на платёжную форму."""
	from lms.lms.payments import get_controller

	controller = get_controller(GATEWAY)
	title = course.get("title") or data["course"]
	return controller.get_payment_url(
		amount=amount,
		title=f"Оплата курса «{title}»",
		description=f"{data['name']}: оплата курса «{title}»",
		reference_doctype=COURSE_DOCTYPE,
		reference_docname=data["course"],
		payer_email=data["email"],
		payer_name=data["name"],
		currency=CURRENCY,
		payment_gateway=GATEWAY,
		redirect_to=REDIRECT_TO,
		payment=payment,
	)


# --- ответы ---------------------------------------------------------------


def gateway_message(exc):
	msg = getattr(exc, "msg", None)
	if isinstance(msg, (list, tuple)):
		msg = " ".join(str(item) for item in msg)
	text = strip_tags(msg or exc)
	if not text:
		return "Банк не смог создать платёж. Попробуйте ещё раз через минуту."
	return f"Не удалось открыть оплату: {text}"


def drop_messages():
	"""Пойманный frappe.throw оставил бы в ответе HTML-плашку — убираем."""
	try:
		frappe.clear_messages()
	except Exception:
		pass


def fail(code, message):
	"""Отказ — это 200 с ok=false: лендингу нужен текст, а не страница ошибки."""
	drop_messages()
	frappe.local.response["http_status_code"] = 200
	return {"ok": False, "error": code, "message": message}
