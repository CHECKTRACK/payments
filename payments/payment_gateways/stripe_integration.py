# Copyright (c) 2018, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import stripe
import frappe
from datetime import datetime, timedelta
import pytz
from frappe import _
from frappe.integrations.utils import create_request_log


def create_stripe_subscription(gateway_controller, data):
	stripe_settings = frappe.get_doc("Stripe Settings", gateway_controller)
	stripe_settings.data = frappe._dict(data)

	stripe.api_key = stripe_settings.get_password(fieldname="secret_key", raise_exception=False)
	stripe.default_http_client = stripe.http_client.RequestsClient()
	stripe.api_version = "2025-06-30.basil"

	try:
		stripe_settings.integration_request = create_request_log(stripe_settings.data, "Host", "Stripe")

		# Fetch Subscription Plans from Subscription Doctype instead of Payment Plan
		payment_request_doc = frappe.get_doc("Payment Request", stripe_settings.data.reference_docname)

		if payment_request_doc.is_a_subscription:
			# Pull all subscription plan details for this request
			stripe_settings.payment_plans = frappe.get_all(
				"Subscription Plan Detail",
				filters={"parent": payment_request_doc.name},
				fields=["plan", "qty"]
			)

			sales_invoice = frappe.get_doc(payment_request_doc.reference_doctype, payment_request_doc.reference_name)
			registration_fee_item = None
			item_price_id = None
			# Step 2: Loop through its items to find "Registration Fee"
			for item in sales_invoice.items:
				if item.item_name == "Registration Fee" or item.item_code == "Registration Fee":
					registration_fee_item = item
					break
			
			if registration_fee_item:
				item_price_id = frappe.db.get_value(
					"Item Price",
					{"item_code": registration_fee_item.item_code, "price_list": "Standard Selling"},
					"reference"
				)

		else:
			stripe_settings.payment_plans = []

		return create_subscription_on_stripe(stripe_settings,item_price_id)

	except Exception:
		stripe_settings.log_error("Unable to create Stripe subscription")
		return {
			"redirect_to": frappe.redirect_to_message(
				_("Server Error"),
				_(
					"It seems that there is an issue with the server's stripe configuration. "
					"In case of failure, the amount will get refunded to your account."
				),
			),
			"status": 401,
		}


def create_subscription_on_stripe(stripe_settings,item_price_id):
	items = []
	for payment_plan in stripe_settings.payment_plans:
		plan = frappe.db.get_value("Subscription Plan", payment_plan.plan, "product_price_id")
		items.append({"price": plan, "quantity": payment_plan.qty})

	if item_price_id:
		items.append({"price": item_price_id, "quantity": 1})

	try:
		payer_email = stripe_settings.data.payer_email
		payer_name = stripe_settings.data.payer_name
		token_id = stripe_settings.data.stripe_token_id
		
		existing_customers = stripe.Customer.list(email=payer_email, limit=1)
		
		if existing_customers.data and len(existing_customers.data) > 0:
			customer = existing_customers.data[0]
		else:
			customer = stripe.Customer.create(
				source=token_id,
				description=payer_name,
				email=payer_email,
			)
		tz = pytz.timezone("America/Los_Angeles")
		now = datetime.now(tz)
		# backdate_start_date = tz.localize(datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0))
		backdate_start_date = int(datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=tz).timestamp())
		next_anchor = datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=tz)
		if next_anchor <= now:
			next_anchor += timedelta(days=1)
		billing_cycle_anchor = int(next_anchor.timestamp())

		subscription = stripe.Subscription.create(
			customer=customer,
			items=items,
			billing_mode={"type" : "flexible"},
			off_session=True,
			payment_behavior="error_if_incomplete",
			proration_behavior="none"
		)

		if subscription.status == "active":
			stripe_settings.integration_request.db_set("status", "Completed", update_modified=False)
			stripe_settings.flags.status_changed_to = "Completed"

		else:
			stripe_settings.integration_request.db_set("status", "Failed", update_modified=False)
			frappe.log_error(f"Stripe Subscription ID {subscription.id}: Payment failed")
	except Exception:
		stripe_settings.integration_request.db_set("status", "Failed", update_modified=False)
		stripe_settings.log_error("Unable to create Stripe subscription")

	return stripe_settings.finalize_request()
