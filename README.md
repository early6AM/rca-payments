# rca_payments — эквайринг Т-Банка для Frappe Learning

Приложение Frappe, которое добавляет Т-Банк как платёжный шлюз в Frappe Learning.
Сделано под сайт `school.rca.yachts`.

## Что внутри

- `TBank Settings` — доктайп с настройками терминала (TerminalKey, пароль, чек 54-ФЗ).
- `get_payment_url()` — метод, которого ждёт Frappe: создаёт платёж через `POST /v2/Init`
  и возвращает `PaymentURL`, LMS открывает его в браузере ученицы.
- `POST /api/method/rca_payments.tbank.notify` — приём уведомлений банка.
  Проверяет подпись Token, отмечает счёт оплаченным и выдаёт доступ
  (`lms.lms.utils.complete_enrollment`), затем переводит сделку в CRM
  в статус «Оплачено».
- `POST /api/method/rca_payments.checkout` — гостевое оформление заказа с лендинга:
  находит или заводит пользователя, создаёт сделку в CRM и счёт в LMS,
  возвращает ссылку на платёжную форму Т-Банка.
- `reconcile_pending_payments()` — раз в час сверяет неоплаченные счета через
  `GetState`, если уведомление не дошло.

## Оформление заказа с лендинга

`POST /api/method/rca_payments.checkout`, `Content-Type: application/json`
(или обычная форма). Гостевой, не чаще 10 раз в час с одного IP.

Поля: `name`, `email`, `course` (name курса), `consent` (обязательно `true`),
`consent_version`; необязательные — `telegram_id`, `telegram_username`,
`utm_source`, `utm_medium`, `utm_campaign`.

Ответ — всегда HTTP 200 и JSON:

```json
{"message": {"ok": true, "already_enrolled": false,
             "payment_url": "https://securepay.tinkoff.ru/...", "payment": "ACC-PAY-2026-00001"}}
{"message": {"ok": false, "error": "invalid_email", "message": "Похоже, в адресе почты опечатка…"}}
```

Цена берётся **только** из `LMS Course.course_price`: сумму из запроса эндпоинт
не читает. Если у человека уже есть неоплаченный счёт на этот курс младше часа,
он переиспользуется, а не создаётся заново; если человек уже записан на курс,
счёт не создаётся вовсе и возвращается `already_enrolled: true`.

## Как это работает в Frappe

`Payment Gateway` — это запись из трёх полей: `gateway`, `gateway_settings`
(доктайп с кредами) и `gateway_controller` (конкретная запись). LMS зовёт
`get_payment_url()` у контроллера и ждёт строку-ссылку:

```
frontend/src/pages/Billing.vue → window.location.href = data
```

Форма настроек в админке LMS рисуется по метаданным доктайпа
(`lms/api.py: get_payment_gateway_details`), поэтому фронтенд править не нужно.

## Установка

Приложение вшивается в образ вместе с `payments` и `lms`:

```json
[
  {"url": "https://github.com/frappe/payments", "branch": "version-15"},
  {"url": "https://github.com/frappe/lms", "branch": "main"},
  {"url": "https://github.com/early6AM/rca-payments", "branch": "main"}
]
```

## Настройка в личном кабинете Т-Бизнеса

1. Подключить интернет-эквайринг, дождаться договора.
2. `Магазины` → нужный магазин → `Терминалы` → `Рабочий`: скопировать
   **TerminalKey** и **пароль терминала**. Пароль тестового и рабочего
   терминала разный.
3. Включить уведомления: URL `https://school.rca.yachts/api/method/rca_payments.tbank.notify`,
   метод POST. Отдельно для тестового и рабочего терминала.
4. Включить способы оплаты: карты, СБП, T-Pay, SberPay, Долями.
5. Решить вопрос с чеками 54-ФЗ: либо подключить онлайн-кассу в Т-Бизнесе
   (тогда `send_receipt` выключен, чек формирует банк), либо передавать чек
   в запросе (`send_receipt` включён + система налогообложения и НДС).
6. Возвраты — в кабинете, кнопкой по операции.

## Проверка

Тестовый терминал: включить тестовые платежи, оплатить тестовой картой,
убедиться, что счёт в LMS стал `payment_received = 1`, а ученица получила доступ.
Кнопка «Проверить связь с Т-Банком» в настройках показывает сырой ответ банка.
