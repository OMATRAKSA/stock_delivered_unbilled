import frappe
from frappe import _
from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
	repost_sl_entries, 
	repost_gl_entries, 
	notify_error_to_stock_managers, 
	_get_directly_dependent_vouchers,
	in_configured_timeslot
)
from frappe.utils import cint, get_link_to_form, get_weekday, getdate, now, nowtime
from erpnext.accounts.general_ledger import toggle_debit_credit_if_negative
from erpnext.accounts.utils import get_future_stock_vouchers, repost_gle_for_stock_vouchers, _delete_accounting_ledger_entries
from erpnext.stock.stock_ledger import (
	get_affected_transactions,
	get_items_to_be_repost,
	repost_future_sle,
)

from rq.timeouts import JobTimeoutException
from frappe.exceptions import QueryDeadlockError, QueryTimeoutError

RecoverableErrors = (JobTimeoutException, QueryDeadlockError, QueryTimeoutError)


def repost_invoice_entries():
	"""
	Pick and repost the invoices listed in Repost Sales Invoice List.
	"""

	riv_entries = frappe.db.get_all(
		"Repost Sales Invoice",
		filters={"completed": 0},
		fields=["repost_item_valuation", "affected_sales_invoice", "name"],
	)

	for row in riv_entries:
		voucher_obj = frappe.get_doc("Sales Invoice", row.affected_sales_invoice)
		expected_gle = toggle_debit_credit_if_negative(voucher_obj.get_gl_entries())
		_delete_accounting_ledger_entries("Sales Invoice", row.affected_sales_invoice)
		voucher_obj.make_gl_entries(gl_entries=expected_gle, from_repost=True)
		frappe.db.commit()
		frappe.db.set_value('Repost Sales Invoice', row.name, 'completed', 1)
		
	return

def validate_expense_accoount(self,method):
	stock_delivered_but_not_billed_account = None
	stock_delivered_but_not_billed_account = frappe.db.get_value("Company", self.company, "stock_delivered_but_not_billed")


	disable_sdbnb_in_sr = frappe.db.get_value("Company", self.company, "disable_sdbnb_in_sr")
	default_expense_account = frappe.db.get_value("Company", self.company, "default_expense_account")

	if stock_delivered_but_not_billed_account:
		if disable_sdbnb_in_sr and self.is_return:
			if default_expense_account:
				for item in self.items:
					item.expense_account = default_expense_account
		else:
			for item in self.items:
				if item.get("si_detail"):
					# Invoiced first, delivered later: the invoice is already booked,
					# so the cost goes straight to cost of sales (nothing to park).
					item.expense_account = get_invoice_expense_account(item) or default_expense_account
					if not item.expense_account:
						frappe.throw(
							_("Row #{0}: no Cost of Goods Sold account on invoice {1} for item {2}.").format(
								item.idx, item.against_sales_invoice, item.item_code
							)
						)
				else:
					item.expense_account = stock_delivered_but_not_billed_account

	if not self.is_return:
		validate_invoiced_first(self)


def get_invoice_expense_account(item):
	account = frappe.db.get_value("Sales Invoice Item", item.si_detail, "expense_account")
	if account and frappe.get_cached_value("Account", account, "report_type") == "Profit and Loss":
		return account
	return None


def validate_invoiced_first(self):
	"""An order line that was invoiced before delivery must be delivered from that
	invoice, or its cost is parked in Stock Delivered But Not Billed for ever."""
	for item in self.items:
		if item.get("si_detail") or not item.get("so_detail"):
			continue
		invoice = frappe.db.sql(
			"""
			select sii.parent
			from `tabSales Invoice Item` sii
			join `tabSales Invoice` si on si.name = sii.parent
			where sii.so_detail = %s and si.docstatus = 1 and si.is_return = 0
			  and si.update_stock = 0 and ifnull(sii.dn_detail, '') = ''
			limit 1
			""",
			item.so_detail,
		)
		if invoice:
			frappe.throw(
				_(
					"Row #{0}: item {1} of {2} was already invoiced on {3}. "
					"Create the Delivery Note from the invoice instead: open {3} and click Create → Delivery."
				).format(
					item.idx,
					frappe.bold(item.item_code),
					item.against_sales_order,
					get_link_to_form("Sales Invoice", invoice[0][0]),
				),
				title=_("Deliver from the invoice"),
			)
