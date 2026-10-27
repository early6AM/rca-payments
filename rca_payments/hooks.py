app_name = "rca_payments"
app_title = "RCA Payments"
app_publisher = "RCA Yachts"
app_description = "Эквайринг Т-Банка (T-Bank acquiring) для Frappe Learning"
app_email = "hello@rca.yachts"
app_license = "mit"

required_apps = ["payments"]

after_install = "rca_payments.setup.after_install"

# Уведомление Т-Банка приходит на /api/method/rca_payments.tbank.notify.
# Раз в час сверяем неоплаченные счета через GetState: уведомление может не
# дойти, если ученица закрыла вкладку или упал обработчик.
scheduler_events = {
    "hourly": ["rca_payments.tasks.reconcile_pending_payments"],
    # Срок доступа: за неделю — сделка менеджеру, по истечении — закрытие.
    "daily": ["rca_payments.access.daily"],
}

# Срок доступа ставится в момент записи на курс: и после оплаты, и при
# ручной выдаче. Свои обработчики lms не перетираем — Frappe складывает
# списки методов из всех приложений.
doc_events = {
    "LMS Enrollment": {
        "before_insert": "rca_payments.access.stamp_enrollment",
    },
}

# Фронтенд LMS — собранный бандл без точки расширения, поэтому поле
# «доступ до …» вставляет наш скрипт в HTML страниц /lms.
after_request = ["rca_payments.access.inject_notice"]
