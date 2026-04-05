"""Shopify API rate limit handling.

ShopifyAPI + pyactiveresource have no built-in retry on HTTP 429.
When Shopify's leaky bucket is exhausted, pyactiveresource raises a
generic ``ClientError`` for 429 responses. This module provides a
decorator and helper that catch 429s and retry with exponential backoff.

Shopify REST Admin API rate limit:
  - Bucket size: 40 requests
  - Leak rate:   2 requests/second
  - Response:    HTTP 429 with ``Retry-After`` header (seconds)
"""

import functools
import logging
import time

from pyactiveresource.connection import ClientError

logger = logging.getLogger(__name__)

# Defaults
MAX_RETRIES = 5
BASE_DELAY = 2.0  # seconds
MAX_DELAY = 30.0  # seconds


def _is_rate_limit_error(exc: ClientError) -> bool:
	"""Check if a ClientError is a 429 Too Many Requests."""
	response = getattr(exc, "response", None)
	if response is not None:
		code = getattr(response, "code", None) or getattr(response, "status_code", None)
		if code == 429:
			return True
	# Fallback: inspect the string representation
	return "429" in str(exc)


def _get_retry_after(exc: ClientError) -> float | None:
	"""Extract Retry-After header value from the error response, if present."""
	response = getattr(exc, "response", None)
	if response is None:
		return None

	# pyactiveresource wraps urllib HTTPError — headers may be on .response or .response.headers
	headers = getattr(response, "headers", None) or {}
	if hasattr(headers, "get"):
		retry_after = headers.get("Retry-After") or headers.get("retry-after")
		if retry_after:
			try:
				return float(retry_after)
			except (ValueError, TypeError):
				pass
	return None


def retry_on_rate_limit(max_retries=MAX_RETRIES, base_delay=BASE_DELAY, max_delay=MAX_DELAY):
	"""Decorator that retries a function on Shopify 429 rate limit errors.

	Uses exponential backoff, respecting the Retry-After header when available.

	Usage::

	    @retry_on_rate_limit()
	    def fetch_orders():
	        return Order.find(limit=50)
	"""

	def decorator(func):
		@functools.wraps(func)
		def wrapper(*args, **kwargs):
			return call_with_rate_limit_retry(
				func, args, kwargs, max_retries=max_retries, base_delay=base_delay, max_delay=max_delay
			)

		return wrapper

	return decorator


def call_with_rate_limit_retry(
	func, args=(), kwargs=None, max_retries=MAX_RETRIES, base_delay=BASE_DELAY, max_delay=MAX_DELAY
):
	"""Call ``func(*args, **kwargs)`` with retry on 429 errors.

	This is the non-decorator form, useful when you can't decorate the
	target function (e.g. ``Order.find`` from the Shopify SDK).

	Args:
	    func: Callable to invoke.
	    args: Positional arguments.
	    kwargs: Keyword arguments.
	    max_retries: Maximum number of retries (default 5).
	    base_delay: Initial backoff delay in seconds (default 2.0).
	    max_delay: Maximum backoff delay in seconds (default 30.0).

	Returns:
	    The return value of ``func``.

	Raises:
	    The last ``ClientError`` if all retries are exhausted, or any
	    non-rate-limit exception immediately.
	"""
	if kwargs is None:
		kwargs = {}

	last_exc = None
	for attempt in range(max_retries + 1):
		try:
			return func(*args, **kwargs)
		except ClientError as exc:
			if not _is_rate_limit_error(exc):
				raise  # Not a 429 — propagate immediately

			last_exc = exc
			if attempt >= max_retries:
				logger.error(
					"Shopify rate limit: exhausted %d retries for %s",
					max_retries,
					func.__name__ if hasattr(func, "__name__") else str(func),
				)
				raise

			# Determine wait time
			retry_after = _get_retry_after(exc)
			if retry_after and retry_after > 0:
				wait = min(retry_after + 0.5, max_delay)  # small buffer
			else:
				wait = min(base_delay * (2**attempt), max_delay)

			logger.warning(
				"Shopify rate limit hit (429) on attempt %d/%d for %s — sleeping %.1fs",
				attempt + 1,
				max_retries + 1,
				func.__name__ if hasattr(func, "__name__") else str(func),
				wait,
			)
			time.sleep(wait)

	# Should not reach here, but just in case
	if last_exc:
		raise last_exc
