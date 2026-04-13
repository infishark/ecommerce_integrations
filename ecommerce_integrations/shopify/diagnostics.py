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


# ════════════════════════════════════════════════════════════════════
#  Mass cancellation for currency revert
# ════════════════════════════════════════════════════════════════════

@frappe.whitelist()
def cancel_shopify_documents(dry_run="1"):
	"""Cancel all Shopify-synced documents so they can be re-synced with
	correct currency.

	Cancellation order: Payment Entry → Sales Invoice → Delivery Note → Sales Order.

	Args:
		dry_run: "1" (default) to only report what WOULD be cancelled.
		         "0" to actually cancel.

	Call via:
		/api/method/ecommerce_integrations.shopify.diagnostics.cancel_shopify_documents
		/api/method/ecommerce_integrations.shopify.diagnostics.cancel_shopify_documents?dry_run=0
	"""
	frappe.only_for("System Manager")
	dry_run = dry_run != "0"

	report = {"dry_run": dry_run}

	# ── 1. Find all Shopify-synced Sales Orders (non-cancelled) ───
	shopify_sos = frappe.db.sql(
		f"""SELECT name, docstatus FROM `tabSales Order`
		WHERE `{ORDER_ID_FIELD}` IS NOT NULL AND `{ORDER_ID_FIELD}` != ''
			AND docstatus != 2""",
		as_dict=True,
	)
	so_names = [r["name"] for r in shopify_sos]
	report["sales_orders_to_cancel"] = len(so_names)

	if not so_names:
		report["message"] = "No Shopify Sales Orders to cancel."
		return report

	# ── 2. Find linked Sales Invoices (non-cancelled) ─────────────
	shopify_sis = frappe.db.sql(
		f"""SELECT name, docstatus FROM `tabSales Invoice`
		WHERE `{ORDER_ID_FIELD}` IS NOT NULL AND `{ORDER_ID_FIELD}` != ''
			AND docstatus != 2""",
		as_dict=True,
	)
	si_names = [r["name"] for r in shopify_sis]
	report["sales_invoices_to_cancel"] = len(si_names)

	# ── 3. Find linked Payment Entries (non-cancelled) ────────────
	pe_names = []
	if si_names:
		pe_rows = frappe.db.sql(
			"""SELECT DISTINCT pe.name, pe.docstatus
			FROM `tabPayment Entry` pe
			INNER JOIN `tabPayment Entry Reference` per ON per.parent = pe.name
			WHERE per.reference_doctype = 'Sales Invoice'
				AND per.reference_name IN %s
				AND pe.docstatus != 2""",
			(si_names,),
			as_dict=True,
		)
		pe_names = [r["name"] for r in pe_rows]
	report["payment_entries_to_cancel"] = len(pe_names)

	# ── 4. Find linked Delivery Notes (non-cancelled) ────────────
	shopify_dns = frappe.db.sql(
		f"""SELECT name, docstatus FROM `tabDelivery Note`
		WHERE `{ORDER_ID_FIELD}` IS NOT NULL AND `{ORDER_ID_FIELD}` != ''
			AND docstatus != 2""",
		as_dict=True,
	)
	dn_names = [r["name"] for r in shopify_dns]
	report["delivery_notes_to_cancel"] = len(dn_names)

	report["total_documents"] = (
		len(pe_names) + len(si_names) + len(dn_names) + len(so_names)
	)

	if dry_run:
		report["message"] = (
			"DRY RUN — no changes made. "
			"Call with ?dry_run=0 to execute cancellation as a background job."
		)
		return report

	# ── REAL EXECUTION — enqueue as background job ────────────────
	frappe.enqueue(
		_run_cancellation,
		queue="long",
		timeout=7200,  # 2 hours
		pe_names=pe_names,
		si_names=si_names,
		dn_names=dn_names,
		so_names=so_names,
	)

	report["message"] = (
		"Cancellation job enqueued. This will take 30-60 minutes. "
		"Check progress at: /api/method/ecommerce_integrations.shopify.diagnostics.cancel_progress"
	)
	return report


CANCEL_PROGRESS_KEY = "shopify_cancel_progress"


@frappe.whitelist()
def cancel_progress():
	"""Check the progress of a running cancellation job."""
	frappe.only_for("System Manager")
	progress = frappe.cache.get_value(CANCEL_PROGRESS_KEY)
	if not progress:
		return {"status": "no job running or completed"}
	return progress


def _run_cancellation(pe_names, si_names, dn_names, so_names):
	"""Background job: cancel all Shopify documents in dependency order."""
	progress = {
		"status": "running",
		"phase": "starting",
		"cancelled": {"pe": 0, "si": 0, "dn": 0, "so": 0},
		"errors": [],
		"total": len(pe_names) + len(si_names) + len(dn_names) + len(so_names),
	}
	frappe.cache.set_value(CANCEL_PROGRESS_KEY, progress, expires_in_sec=14400)

	def _cancel_doc(doctype, name):
		try:
			doc = frappe.get_doc(doctype, name)
			if doc.docstatus == 1:
				doc.flags.ignore_permissions = True
				doc.cancel()
			return True
		except Exception as e:
			if len(progress["errors"]) < 50:
				progress["errors"].append({"doctype": doctype, "name": name, "error": str(e)[:200]})
			frappe.db.rollback()
			return False

	def _cancel_batch(doctype, names, key):
		progress["phase"] = f"Cancelling {doctype}s ({len(names)})"
		frappe.cache.set_value(CANCEL_PROGRESS_KEY, progress, expires_in_sec=14400)

		for i, name in enumerate(names):
			if _cancel_doc(doctype, name):
				progress["cancelled"][key] += 1
			if (i + 1) % 20 == 0:
				frappe.db.commit()
				frappe.cache.set_value(CANCEL_PROGRESS_KEY, progress, expires_in_sec=14400)
		frappe.db.commit()

	_cancel_batch("Payment Entry", pe_names, "pe")
	_cancel_batch("Sales Invoice", si_names, "si")
	_cancel_batch("Delivery Note", dn_names, "dn")
	_cancel_batch("Sales Order", so_names, "so")

	progress["status"] = "completed"
	progress["phase"] = "done"
	frappe.cache.set_value(CANCEL_PROGRESS_KEY, progress, expires_in_sec=14400)
