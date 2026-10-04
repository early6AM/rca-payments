frappe.ui.form.on("TBank Settings", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__("Проверить связь с Т-Банком"), () => {
			frm.call("check_credentials").then((r) => {
				if (!r || !r.message) return;
				frappe.msgprint({
					title: __("Ответ Т-Банка"),
					message: `<pre style="white-space: pre-wrap">${frappe.utils.escape_html(
						r.message
					)}</pre>`,
					wide: true,
				});
			});
		});
	},
});
