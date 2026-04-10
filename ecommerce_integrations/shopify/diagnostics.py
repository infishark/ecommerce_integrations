"""Diagnostic endpoints for the Shopify integration.

These are read-only audit endpoints used to inspect the state of synced
documents on Frappe Cloud (where shell access isn't available). They
return JSON for analysis.

Call via: /api/method/ecommerce_integrations.shopify.diagnostics.<name>
"""

import json
import traceback

import frappe

from ecommerce_integrations.shopify.constants import ORDER_ID_FIELD, ORDER_NUMBER_FIELD


def _safe(section_name, fn):
	"""Run a diagnostic section and capture exceptions as part of the report."""
	try:
		return fn()
	except Exception as e:
		return {
			"error": str(e),
			"traceback": traceback.format_exc(),
		}


@frappe.whitelist()
def audit_shopify_sales_orders():
	"""Audit all Sales Orders synced from Shopify."""
	frappe.only_for("System Manager")

	report = {}

	# ── 1. Aggregate counts via raw SQL ───────────────────────────
	def _counts():
		return {
			"total_shopify_sales_orders": frappe.db.sql(
				f"""SELECT COUNT(*) FROM `tabSales Order`
				WHERE `{ORDER_ID_FIELD}` IS NOT NULL AND `{ORDER_ID_FIELD}` != ''"""
			)[0][0],
			"submitted": frappe.db.sql(
				f"""SELECT COUNT(*) FROM `tabSales Order`
				WHERE `{ORDER_ID_FIELD}` IS NOT NULL AND `{ORDER_ID_FIELD}` != ''
				AND docstatus = 1"""
			)[0][0],
			"draft": frappe.db.sql(
				f"""SELECT COUNT(*) FROM `tabSales Order`
				WHERE `{ORDER_ID_FIELD}` IS NOT NULL AND `{ORDER_ID_FIELD}` != ''
				AND docstatus = 0"""
			)[0][0],
			"cancelled": frappe.db.sql(
				f"""SELECT COUNT(*) FROM `tabSales Order`
				WHERE `{ORDER_ID_FIELD}` IS NOT NULL AND `{ORDER_ID_FIELD}` != ''
				AND docstatus = 2"""
			)[0][0],
			"linked_sales_invoices": frappe.db.sql(
				f"""SELECT COUNT(*) FROM `tabSales Invoice`
				WHERE `{ORDER_ID_FIELD}` IS NOT NULL AND `{ORDER_ID_FIELD}` != ''"""
			)[0][0],
			"linked_delivery_notes": frappe.db.sql(
				f"""SELECT COUNT(*) FROM `tabDelivery Note`
				WHERE `{ORDER_ID_FIELD}` IS NOT NULL AND `{ORDER_ID_FIELD}` != ''"""
			)[0][0],
		}

	report["counts"] = _safe("counts", _counts)

	# ── 2. Payment Entries linked to Shopify Sales Invoices ───────
	def _payment_entries():
		return frappe.db.sql(
			f"""SELECT COUNT(DISTINCT pe.name)
			FROM `tabPayment Entry` pe
			INNER JOIN `tabPayment Entry Reference` per ON per.parent = pe.name
			INNER JOIN `tabSales Invoice` si ON si.name = per.reference_name
			WHERE per.reference_doctype = 'Sales Invoice'
				AND si.`{ORDER_ID_FIELD}` IS NOT NULL
				AND si.`{ORDER_ID_FIELD}` != ''"""
		)[0][0]

	report["linked_payment_entries"] = _safe("payment_entries", _payment_entries)

	# ── 3. Currency distribution on existing SOs ──────────────────
	def _currency_dist():
		rows = frappe.db.sql(
			f"""SELECT currency, COUNT(*) as count
			FROM `tabSales Order`
			WHERE `{ORDER_ID_FIELD}` IS NOT NULL AND `{ORDER_ID_FIELD}` != ''
			GROUP BY currency""",
			as_dict=True,
		)
		return {row["currency"]: row["count"] for row in rows}

	report["so_currency_distribution"] = _safe("so_currency", _currency_dist)

	def _si_currency_dist():
		rows = frappe.db.sql(
			f"""SELECT currency, COUNT(*) as count
			FROM `tabSales Invoice`
			WHERE `{ORDER_ID_FIELD}` IS NOT NULL AND `{ORDER_ID_FIELD}` != ''
			GROUP BY currency""",
			as_dict=True,
		)
		return {row["currency"]: row["count"] for row in rows}

	report["si_currency_distribution"] = _safe("si_currency", _si_currency_dist)

	# ── 4. Sample SOs + original Shopify currency from logs ──────
	def _samples():
		sample = frappe.db.sql(
			f"""SELECT name, `{ORDER_ID_FIELD}` as order_id,
				`{ORDER_NUMBER_FIELD}` as order_number,
				currency, conversion_rate, grand_total, base_grand_total,
				customer, docstatus
			FROM `tabSales Order`
			WHERE `{ORDER_ID_FIELD}` IS NOT NULL AND `{ORDER_ID_FIELD}` != ''
			ORDER BY creation DESC
			LIMIT 5""",
			as_dict=True,
		)
		for so in sample:
			try:
				log = frappe.db.sql(
					"""SELECT request_data FROM `tabEcommerce Integration Log`
					WHERE method = 'ecommerce_integrations.shopify.order.sync_sales_order'
						AND request_data LIKE %s
					ORDER BY creation DESC
					LIMIT 1""",
					(f'%"id": {so["order_id"]}%',),
				)
				if log and log[0][0]:
					data = json.loads(log[0][0])
					so["shopify_currency"] = data.get("currency")
					so["shopify_total_price"] = data.get("total_price")
				else:
					so["shopify_currency"] = "NO_LOG_FOUND"
			except Exception as e:
				so["shopify_currency"] = f"ERROR: {e}"
		return sample

	report["sample_sales_orders"] = _safe("samples", _samples)

	# ── 5. Non-standard links for one sample SO ───────────────────
	def _find_links():
		samples = report.get("sample_sales_orders") or []
		if not samples or not isinstance(samples, list) or "error" in samples[0]:
			return {"skipped": "no sample SO available"}
		sample_name = samples[0]["name"]
		return _find_all_links_to_so(sample_name)

	report["sample_so_links"] = _safe("links", _find_links)

	# ── 6. Company default receivable account currency ───────────
	def _company_receivable():
		rows = frappe.db.sql(
			"""SELECT name, default_currency, default_receivable_account
			FROM `tabCompany`""",
			as_dict=True,
		)
		for row in rows:
			if row.get("default_receivable_account"):
				row["receivable_currency"] = frappe.db.get_value(
					"Account", row["default_receivable_account"], "account_currency"
				)
		return rows

	report["companies"] = _safe("companies", _company_receivable)

	return report


def _find_all_links_to_so(so_name: str) -> dict:
	"""Find every document that references the given Sales Order."""
	links = {}

	queries = {
		"sales_invoice_items": (
			"""SELECT DISTINCT parent FROM `tabSales Invoice Item`
			WHERE sales_order = %s""",
			(so_name,),
		),
		"delivery_note_items": (
			"""SELECT DISTINCT parent FROM `tabDelivery Note Item`
			WHERE against_sales_order = %s""",
			(so_name,),
		),
		"material_requests": (
			"""SELECT DISTINCT parent FROM `tabMaterial Request Item`
			WHERE sales_order = %s""",
			(so_name,),
		),
		"work_orders": (
			"""SELECT name FROM `tabWork Order` WHERE sales_order = %s""",
			(so_name,),
		),
		"journal_entry_accounts": (
			"""SELECT DISTINCT parent FROM `tabJournal Entry Account`
			WHERE reference_type = 'Sales Order' AND reference_name = %s""",
			(so_name,),
		),
		"payment_entry_references": (
			"""SELECT DISTINCT parent FROM `tabPayment Entry Reference`
			WHERE reference_doctype = 'Sales Order' AND reference_name = %s""",
			(so_name,),
		),
		"dynamic_links": (
			"""SELECT DISTINCT parenttype, parent FROM `tabDynamic Link`
			WHERE link_doctype = 'Sales Order' AND link_name = %s""",
			(so_name,),
		),
	}

	for key, (sql, params) in queries.items():
		try:
			result = frappe.db.sql(sql, params, as_dict=True)
			if result:
				links[key] = result
		except Exception as e:
			links[f"{key}_error"] = str(e)

	# GL Entry count (just a number, not a list)
	try:
		gl_count = frappe.db.sql(
			"""SELECT COUNT(*) FROM `tabGL Entry`
			WHERE voucher_type = 'Sales Order' AND voucher_no = %s""",
			(so_name,),
		)[0][0]
		if gl_count:
			links["gl_entry_count"] = gl_count
	except Exception as e:
		links["gl_entry_count_error"] = str(e)

	return links
