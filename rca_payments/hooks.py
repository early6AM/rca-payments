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
}
