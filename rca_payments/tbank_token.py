"""Подпись запросов и уведомлений Т-Банка.

Модуль намеренно не зависит от frappe: алгоритм подписи — единственное место,
где ошибка ломает всё молча, и его нужно уметь прогнать тестами отдельно.
"""

import hashlib
import hmac


def make_token(payload, password):
	"""SHA-256 подпись Т-Банка.

	Берём корневые скалярные поля, добавляем Password, сортируем по имени поля,
	склеиваем значения и считаем SHA-256. Вложенные объекты (Receipt, DATA)
	в подпись не входят — это требование Т-Банка.
	"""
	parts = []
	for key, value in payload.items():
		if key == "Token" or value is None:
			continue
		if isinstance(value, (dict, list, tuple)):
			continue
		if isinstance(value, bool):
			value = "true" if value else "false"
		parts.append((key, str(value)))

	parts.append(("Password", password))
	parts.sort(key=lambda item: item[0])
	return hashlib.sha256("".join(value for _, value in parts).encode("utf-8")).hexdigest()


def verify_token(payload, password):
	"""Проверка подписи входящего уведомления."""
	received = payload.get("Token") or ""
	if not received:
		return False
	return hmac.compare_digest(str(received).lower(), make_token(payload, password).lower())
