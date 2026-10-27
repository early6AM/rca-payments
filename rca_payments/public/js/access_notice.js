/* Поле «доступ открыт до …» на странице курса.
 *
 * Фронтенд LMS — собранный Vue-бандл без точки расширения, поэтому строку
 * рисуем снаружи: находим заголовок курса и вставляем под него маленькую
 * пилюлю. Срок спрашиваем у сервера (rca_payments.access.my_access), ничего
 * не считаем на клиенте — дата должна совпадать с той, по которой закроют
 * доступ.
 *
 * Вставляется через after_request в rca_payments/access.py, поэтому скрипт
 * работает на каждой странице /lms; на страницах не-курса он молчит.
 */
;(function () {
	var COURSE_RE = /\/lms\/courses\/([^\/?#]+)\/?$/
	var MARK = 'data-rca-access-notice'
	var cache = {}
	var timer = null

	function monthName(m) {
		return [
			'января',
			'февраля',
			'марта',
			'апреля',
			'мая',
			'июня',
			'июля',
			'августа',
			'сентября',
			'октября',
			'ноября',
			'декабря',
		][m]
	}

	function formatDate(iso) {
		var parts = String(iso).slice(0, 10).split('-')
		return Number(parts[2]) + ' ' + monthName(Number(parts[1]) - 1) + ' ' + parts[0]
	}

	function plural(n) {
		var mod10 = n % 10
		var mod100 = n % 100
		if (mod10 === 1 && mod100 !== 11) return 'день'
		if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return 'дня'
		return 'дней'
	}

	function removeNotice() {
		var existing = document.querySelectorAll('[' + MARK + ']')
		for (var i = 0; i < existing.length; i++) existing[i].remove()
	}

	function findHeading() {
		var app = document.getElementById('app') || document.body
		var headings = app.querySelectorAll('h1')
		for (var i = 0; i < headings.length; i++) {
			var text = (headings[i].textContent || '').trim()
			if (text) return headings[i]
		}
		return null
	}

	function buildNotice(data) {
		var el = document.createElement('div')
		el.setAttribute(MARK, '1')
		el.setAttribute('role', 'note')

		var closing = data.days_left <= data.warn_days
		el.textContent =
			'Доступ к курсу открыт до ' +
			formatDate(data.ends_on) +
			(data.days_left > 0 ? ' · осталось ' + data.days_left + ' ' + plural(data.days_left) : ' · последний день')

		el.style.cssText = [
			'display:inline-flex',
			'align-items:center',
			'gap:6px',
			'margin-top:10px',
			'padding:4px 12px',
			'border-radius:999px',
			'border:1px solid ' + (closing ? '#d97706' : 'rgba(128,128,128,.45)'),
			'color:' + (closing ? '#d97706' : 'inherit'),
			'opacity:' + (closing ? '1' : '.75'),
			'font-size:12px',
			'line-height:1.6',
			'font-weight:500',
			'white-space:nowrap',
		].join(';')

		return el
	}

	function place(data) {
		var heading = findHeading()
		if (!heading) return
		var existing = document.querySelector('[' + MARK + ']')
		if (existing) {
			if (existing.previousElementSibling === heading) return
			existing.remove()
		}
		heading.insertAdjacentElement('afterend', buildNotice(data))
	}

	function load(course) {
		if (cache[course] !== undefined) {
			if (cache[course]) place(cache[course])
			return
		}
		fetch('/api/method/rca_payments.access.my_access?course=' + encodeURIComponent(course), {
			credentials: 'same-origin',
			headers: { Accept: 'application/json' },
		})
			.then(function (response) {
				return response.ok ? response.json() : null
			})
			.then(function (payload) {
				var data = payload && payload.message
				if (!data || !data.ends_on || data.expired) {
					cache[course] = null
					return
				}
				cache[course] = data
				place(data)
			})
			.catch(function () {
				/* молчим: поле — подсказка, а не часть работы курса */
			})
	}

	function sync() {
		var match = location.pathname.match(COURSE_RE)
		if (!match) {
			removeNotice()
			return
		}
		load(match[1])
	}

	function schedule() {
		if (timer) clearTimeout(timer)
		timer = setTimeout(sync, 150)
	}

	function start() {
		sync()
		var observer = new MutationObserver(schedule)
		observer.observe(document.body, { childList: true, subtree: true })
		window.addEventListener('popstate', schedule)
		document.addEventListener('visibilitychange', schedule)
	}

	try {
		if (document.readyState === 'loading') {
			document.addEventListener('DOMContentLoaded', start)
		} else {
			start()
		}
	} catch (error) {
		/* поле не должно ломать страницу курса */
	}
})()
