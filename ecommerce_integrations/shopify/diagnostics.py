"""Diagnostic endpoints for the Shopify integration.

These are read-only audit endpoints used to inspect the state of synced
documents on Frappe Cloud (where shell access isn't available). They
return JSON for analysis.

Call via: /api/method/ecommerce_integrations.shopify.diagnostics.<name>
"""

import json

import frappe

from ecommerce_integrations.shopify.constants import ORDER_ID_FIELD, ORDER_NUMBER_FIELD


@frappe.whitelist()
def audit_shopify_sales_orders():
	"""Audit all Sales Orders synced from Shopify.

	Returns aggregate counts, currency distribution, and a sample of
	links so we can decide whether a mass revert/re-sync is safe.
	"""
	frappe.only_for("System Manager")

	report = {}

	# ── 1. Aggregate counts ───────────────────────────────────────
	report["counts"] = {
		"total_shopify_sales_orders": frappe.db.count(
			"Sales Order", {ORDER_ID_FIELD: ["is", "set"]}
		),
		"submitted": frappe.db.count(
			"Sales Order", {ORDER_ID_FIELD: ["is", "set"], "docstatus": 1}
		),
		"draft": frappe.db.count(
			"Sales Order", {ORDER_ID_FIELD: ["is", "set"], "docstatus": 0}
		),
		"cancelled": frappe.db.count(
			"Sales Order", {ORDER_ID_FIELD: ["is", "set"], "docstatus": 2}
		),
		"linked_sales_invoices": frappe.db.sql(
			f"""SELECT COUNT(DISTINCT si.name)
			FROM `tabSales Invoice` si
			WHERE si.`{ORDER_ID_FIELD}` IS NOT NULL AND si.`{ORDER_ID_FIELD}` != ''"""
		)[0][0],
		"linked_payment_entries": frappe.db.sql(
			"""SELECT COUNT(DISTINCT pe.name)
			FROM `tabPayment Entry` pe
			INNER JOIN `tabPayment Entry Reference` per ON per.parent = pe.name
			INNER JOIN `tabSales Invoice` si ON si.name = per.reference_name
			WHERE per.reference_doctype = 'Sales Invoice'
				AND si.`{order_id_field}` IS NOT NULL
				AND si.`{order_id_field}` != ''""".format(order_id_field=ORDER_ID_FIELD)
		)[0][0],
		"linked_delivery_notes": frappe.db.sql(
			f"""SELECT COUNT(DISTINCT dn.name)
			FROM `tabDelivery Note` dn
			WHERE dn.`{ORDER_ID_FIELD}` IS NOT NULL AND dn.`{ORDER_ID_FIELD}` != ''"""
		)[0][0],
	}

	# ── 2. Currency distribution on existing SOs ──────────────────
	report["currency_distribution"] = {
		row.currency: row.count
		for row in frappe.db.sql(
			f"""SELECT currency, COUNT(*) as count
			FROM `tabSales Order`
			WHERE `{ORDER_ID_FIELD}` IS NOT NULL AND `{ORDER_ID_FIELD}` != ''
			GROUP BY currency""",
			as_dict=True,
		)
	}

	# ── 3. Sample SO + the original Shopify currency from log ────
	sample_so = frappe.db.sql(
		f"""SELECT name, `{ORDER_ID_FIELD}` as order_id, `{ORDER_NUMBER_FIELD}` as order_number,
				currency, conversion_rate, grand_total, base_grand_total, customer, docstatus
			FROM `tabSales Order`
			WHERE `{ORDER_ID_FIELD}` IS NOT NULL AND `{ORDER_ID_FIELD}` != ''
			ORDER BY creation DESC
			LIMIT 5""",
		as_dict=True,
	)
	report["sample_sales_orders"] = sample_so

	# Try to find the matching Integration Log to get the original currency
	for so in sample_so:
		log = frappe.db.sql(
			"""SELECT name, request_data
			FROM `tabEcommerce Integration Log`
			WHERE method = 'ecommerce_integrations.shopify.order.sync_sales_order'
				AND request_data LIKE %s
			ORDER BY creation DESC
			LIMIT 1""",
			(f'%"id": {so["order_id"]}%',),
			as_dict=True,
		)
		if log:
			try:
				data = json.loads(log[0]["request_data"])
				so["shopify_currency"] = data.get("currency")
				so["shopify_total"] = data.get("total_price")
			except Exception:
				so["shopify_currency"] = "PARSE_ERROR"

	# ── 4. Check for non-standard links to a sample SO ────────────
	if sample_so:
		sample_name = sample_so[0]["name"]
		report["sample_so_links"] = _find_all_links_to_so(sample_name)

	# ── 5. Check Customer.accounts table for currency entries ─────
	report["customer_currency_accounts"] = frappe.db.sql(
		"""SELECT DISTINCT account_currency, COUNT(*) as count
		FROM `tabParty Account`
		WHERE parenttype = 'Customer'
		GROUP BY account_currency""",
		as_dict=True,
	)

	return report


def _find_all_links_to_so(so_name: str) -> dict:
	"""Find every document that references the given Sales Order."""
	links = {}

	# Sales Invoice items linking to this SO
	links["sales_invoice_items"] = frappe.db.sql(
		"""SELECT DISTINCT parent FROM `tabSales Invoice Item` WHERE sales_order = %s""",
		(so_name,),
		as_dict=True,
	)

	# Delivery Note items linking to this SO
	links["delivery_note_items"] = frappe.db.sql(
		"""SELECT DISTINCT parent FROM `tabDelivery Note Item`
		WHERE against_sales_order = %s""",
		(so_name,),
		as_dict=True,
	)

	# Material Requests
	links["material_requests"] = frappe.db.sql(
		"""SELECT DISTINCT parent FROM `tabMaterial Request Item` WHERE sales_order = %s""",
		(so_name,),
		as_dict=True,
	)

	# Work Orders / Production
	links["work_orders"] = frappe.db.sql(
		"""SELECT name FROM `tabWork Order` WHERE sales_order = %s""",
		(so_name,),
		as_dict=True,
	)

	# Journal Entry references
	links["journal_entry_accounts"] = frappe.db.sql(
		"""SELECT DISTINCT parent FROM `tabJournal Entry Account`
		WHERE reference_type = 'Sales Order' AND reference_name = %s""",
		(so_name,),
		as_dict=True,
	)

	# Payment Entry direct references
	links["payment_entry_references"] = frappe.db.sql(
		"""SELECT DISTINCT parent FROM `tabPayment Entry Reference`
		WHERE reference_doctype = 'Sales Order' AND reference_name = %s""",
		(so_name,),
		as_dict=True,
	)

	# Stock Entry references (manufacturing)
	links["stock_entry_items"] = frappe.db.sql(
		"""SELECT DISTINCT parent FROM `tabStock Entry Detail`
		WHERE against_stock_entry IS NOT NULL AND against_stock_entry != ''
			AND parent IN (
				SELECT name FROM `tabStock Entry` WHERE work_order IN (
					SELECT name FROM `tabWork Order` WHERE sales_order = %s
				)
			)""",
		(so_name,),
		as_dict=True,
	)

	# Generic Dynamic Link
	links["dynamic_links"] = frappe.db.sql(
		"""SELECT DISTINCT parenttype, parent FROM `tabDynamic Link`
		WHERE link_doctype = 'Sales Order' AND link_name = %s""",
		(so_name,),
		as_dict=True,
	)

	# GL Entries (read-only audit, just count)
	links["gl_entry_count"] = frappe.db.sql(
		"""SELECT COUNT(*) FROM `tabGL Entry`
		WHERE voucher_type = 'Sales Order' AND voucher_no = %s""",
		(so_name,),
	)[0][0]

	# Filter out empty results so the report is concise
	return {k: v for k, v in links.items() if v}
