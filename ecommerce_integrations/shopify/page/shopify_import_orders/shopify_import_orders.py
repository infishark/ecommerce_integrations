import time

import frappe
from frappe.utils import cstr, get_datetime
from shopify.resources import Order

from ecommerce_integrations.shopify.connection import temp_shopify_session
from ecommerce_integrations.shopify.constants import ORDER_ID_FIELD
from ecommerce_integrations.shopify.order import sync_sales_order
from ecommerce_integrations.shopify.rate_limit import call_with_rate_limit_retry
from ecommerce_integrations.shopify.utils import create_shopify_log

# constants
SYNC_JOB_NAME = "shopify.job.sync.all.orders"
REALTIME_KEY = "shopify.key.sync.all.orders"
SYNC_PROGRESS_KEY = "shopify_sync_all_progress"


@frappe.whitelist()
def get_shopify_orders(from_=None, created_at_min=None, created_at_max=None):
	shopify_orders = fetch_all_orders(from_, created_at_min, created_at_max)
	return shopify_orders


def fetch_all_orders(from_=None, created_at_min=None, created_at_max=None):
	collection = _fetch_orders_from_shopify(
		from_=from_, created_at_min=created_at_min, created_at_max=created_at_max
	)

	orders = []
	for order in collection:
		d = order.to_dict()
		d["synced"] = is_order_synced(order.id)
		orders.append(d)

	next_url = None
	if collection.has_next_page():
		next_url = collection.next_page_url

	prev_url = None
	if collection.has_previous_page():
		prev_url = collection.previous_page_url

	return {
		"orders": orders,
		"nextUrl": next_url,
		"prevUrl": prev_url,
	}


@temp_shopify_session
def _fetch_orders_from_shopify(from_=None, created_at_min=None, created_at_max=None, limit=20):
	if from_:
		collection = call_with_rate_limit_retry(Order.find, kwargs={"from_": from_})
	else:
		kwargs = {"limit": limit, "status": "any"}
		if created_at_min:
			kwargs["created_at_min"] = get_datetime(created_at_min).astimezone().isoformat()
		if created_at_max:
			kwargs["created_at_max"] = get_datetime(created_at_max).astimezone().isoformat()
		collection = call_with_rate_limit_retry(Order.find, kwargs=kwargs)

	return collection


@frappe.whitelist()
def get_order_count():
	synced_orders = frappe.db.sql(
		f"""SELECT COUNT(*) FROM `tabSales Order`
		WHERE `{ORDER_ID_FIELD}` IS NOT NULL AND `{ORDER_ID_FIELD}` != ''""",
	)[0][0]

	total_sales_orders = frappe.db.count("Sales Order")

	shopify_count = get_shopify_order_count()

	return {
		"shopifyCount": shopify_count,
		"syncedCount": synced_orders,
		"erpnextCount": total_sales_orders,
	}


@temp_shopify_session
def get_shopify_order_count():
	return call_with_rate_limit_retry(Order.count, kwargs={"status": "any"})


@frappe.whitelist()
def sync_order(order_id):
	"""Fetch order from Shopify and enqueue sync as a background job.

	sync_sales_order must run in a background job because it calls
	frappe.set_user("Administrator") and create_log calls frappe.db.commit(),
	which together corrupt the browser session when run synchronously.
	"""
	try:
		order_dict = _fetch_order_from_shopify(order_id)

		log = create_shopify_log(
			status="Queued",
			method="ecommerce_integrations.shopify.order.sync_sales_order",
			request_data=order_dict,
			make_new=True,
		)

		frappe.enqueue(
			method="ecommerce_integrations.shopify.order.sync_sales_order",
			queue="short",
			timeout=300,
			is_async=True,
			payload=order_dict,
			request_id=log.name,
		)
		return True
	except Exception:
		frappe.db.rollback()
		return False


@temp_shopify_session
def _fetch_order_from_shopify(order_id):
	order = Order.find(order_id)
	return order.to_dict()


def is_order_synced(order_id):
	return bool(
		frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: cstr(order_id)})
	)


@frappe.whitelist()
def is_sync_running():
	"""Check if a sync-all operation is currently in progress."""
	return bool(frappe.cache.get_value(SYNC_PROGRESS_KEY))


@frappe.whitelist()
def import_all_orders(created_at_min=None, created_at_max=None):
	frappe.enqueue(
		queue_sync_all_orders,
		queue="long",
		job_name=SYNC_JOB_NAME,
		key=REALTIME_KEY,
		created_at_min=created_at_min,
		created_at_max=created_at_max,
	)


def queue_sync_all_orders(created_at_min=None, created_at_max=None, **kwargs):
	start_time = time.time()

	publish("Fetching orders from Shopify...")

	# Batch-fetch all synced Shopify order IDs in a single query
	synced_ids = set(
		frappe.db.sql_list(
			f"""SELECT `{ORDER_ID_FIELD}` FROM `tabSales Order`
			WHERE `{ORDER_ID_FIELD}` IS NOT NULL AND `{ORDER_ID_FIELD}` != ''"""
		)
	)
	publish(f"Found {len(synced_ids)} already-synced order IDs in ERPNext.")

	# Fetch from Shopify in large batches (250 = Shopify API max)
	orders_to_sync = []
	skipped = 0
	page_num = 1
	collection = _fetch_orders_from_shopify(
		created_at_min=created_at_min,
		created_at_max=created_at_max,
		limit=250,
	)

	_fetching = True
	while _fetching:
		page_count = 0
		for order in collection:
			order_dict = order.to_dict()
			page_count += 1
			if str(order_dict.get("id")) in synced_ids:
				skipped += 1
				continue
			orders_to_sync.append(order_dict)

		publish(
			f"Page {page_num}: fetched {page_count} orders "
			f"({len(orders_to_sync)} to sync, {skipped} skipped so far)"
		)

		if collection.has_next_page():
			page_num += 1
			collection = _fetch_orders_from_shopify(from_=collection.next_page_url)
		else:
			_fetching = False

	total = len(orders_to_sync)
	if total == 0:
		msg = "No new orders to sync."
		if skipped:
			msg = f"All {skipped} orders already synced."
		publish(msg, done=True)
		return True

	if skipped:
		publish(f"Skipped {skipped} already-synced orders.")

	# Mark sync as running (auto-expires in 8 hours as a safety net)
	frappe.cache.set_value(
		SYNC_PROGRESS_KEY,
		{"total": total, "done": 0, "start_time": start_time},
		expires_in_sec=28800,
	)

	publish(f"Syncing {total} orders...", dispatched=total)

	synced = 0
	errors = 0

	for idx, order_dict in enumerate(orders_to_sync):
		order_name = order_dict.get("name", order_dict.get("id"))
		order_id = cstr(order_dict.get("id"))

		# sync_sales_order handles its own error logging and calls
		# create_shopify_log internally, which does frappe.db.commit()
		# on both success and error paths. We don't wrap in try/except
		# or savepoints because those conflict with the commits inside
		# create_log.
		try:
			sync_sales_order(order_dict)
		except Exception:
			# sync_sales_order catches all exceptions internally, so this
			# only fires if create_shopify_log itself throws (meta-failure).
			pass

		# Force an explicit commit after every order — belt and suspenders
		# in case create_log's internal commit didn't persist.
		try:
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()

		# Verify the order actually persisted by checking the DB.
		# This catches silent rollbacks, failed submits, and any other
		# scenario where sync_sales_order reported success but the SO
		# didn't actually land.
		if frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: order_id}):
			synced += 1
			publish(f"Synced Order {order_name} ({synced}/{total})", synced=True)
		else:
			errors += 1
			publish(f"Failed Order {order_name} — not found in ERPNext ({errors} errors)", error=True)

	elapsed = time.time() - start_time
	publish(f"Done in {elapsed:.1f}s — {synced} synced, {errors} errors", done=True)
	frappe.cache.delete_value(SYNC_PROGRESS_KEY)
	return True


def publish(message, synced=False, error=False, done=False, dispatched=0, br=True):
	frappe.publish_realtime(
		REALTIME_KEY,
		{
			"synced": synced,
			"error": error,
			"message": message + ("<br /><br />" if br else ""),
			"done": done,
			"dispatched": dispatched,
		},
	)
