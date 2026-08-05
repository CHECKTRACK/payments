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

		# Reject a stale/expired payment link before creating any real Stripe Subscription
		# - unlike the one-time-charge path (create_charge_on_stripe), this had no
		# expiration check at all. sns_fca cancels the Payment Request (status ->
		# "Cancelled") ~10 minutes after a membership signup/onsite-sell payment link is
		# created if it hasn't been paid yet.
		if payment_request_doc.status in ("Paid", "Cancelled"):
			frappe.log_error("Payment Link Expired", f"Payment Link Expired {payment_request_doc.name}")
			return stripe_settings.finalize_request()

		if payment_request_doc.is_a_subscription:
			# Pull all subscription plan details for this request
			stripe_settings.payment_plans = frappe.get_all(
				"Subscription Plan Detail",
				filters={"parent": payment_request_doc.name},
				fields=["plan", "qty"]
			)

		else:
			stripe_settings.payment_plans = []

		return create_subscription_on_stripe(stripe_settings)

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


def _safe_detach_payment_method(payment_method_id):
	"""Best-effort detach for a card Stripe may have already auto-attached to the customer
	as a side effect of confirming a SetupIntent, even though we're about to reject it.
	Swallows errors since the card may genuinely not be attached (e.g. the SetupIntent
	itself never reached "succeeded") - this is cleanup insurance, not the primary control."""
	try:
		stripe.PaymentMethod.detach(payment_method_id)
	except Exception:
		frappe.log_error("Detach Rejected Card", frappe.get_traceback())


def create_subscription_on_stripe(stripe_settings):
	items = []
	item_one_time = []
	discount_items = []
	payment_request_doc = frappe.get_doc("Payment Request", stripe_settings.data.reference_docname)
	sales_invoice_doc = frappe.get_doc("Sales Invoice", payment_request_doc.reference_name)
	subscription_data = frappe.get_doc("Subscription", sales_invoice_doc.subscription)
	for payment_plan in stripe_settings.payment_plans:
		plan = frappe.db.get_value("Subscription Plan",payment_plan.plan,["product_price_id", "custom_product_coupons_id"],as_dict=True)
		# custom_product_coupons_id is a manually pre-created Stripe Coupon attached at
		# real-Subscription-creation time when a membership coupon code was actually used
		# (custom_membership_coupon_code, a Link to SS-Membership Coupon - not
		# custom_coupon_code, which is a Link to the older, unrelated Coupon Code doctype
		# and would fail Link validation if it ever held one of these codes) and the
		# resulting invoice already carries a Grand Total discount.
		if plan.custom_product_coupons_id and subscription_data.custom_membership_coupon_code and (
			sales_invoice_doc.apply_discount_on == "Grand Total" and sales_invoice_doc.discount_amount > 0
		):
			discount_items.append({"coupon": plan.custom_product_coupons_id})
		price_obj = stripe.Price.retrieve(plan.product_price_id)
		if price_obj["type"] == "recurring":
			items.append({"price": plan.product_price_id, "quantity": payment_plan.qty if payment_plan.qty > 0 else 1})
		elif price_obj["type"] == "one_time":
			item_one_time.append({"price": plan.product_price_id, "quantity": payment_plan.qty if payment_plan.qty > 0 else 1})


	try:
		if subscription_data.status != "Cancelled":
			payer_email = stripe_settings.data.payer_email
			payer_name = stripe_settings.data.payer_name
			token_id = stripe_settings.data.stripe_token_id
			
			# --- STEP 1: Get fingerprint of the incoming card from token ---
			token_card = stripe.Token.retrieve(token_id).card
			new_fingerprint = token_card.fingerprint

			# --- STEP 2: Find or create customer by email ---
			existing_customers = stripe.Customer.list(email=payer_email, limit=1)
			if existing_customers.data:
				customer = existing_customers.data[0]
			else:
				customer = stripe.Customer.create(
					description=payer_name,
					email=payer_email
				)

			# --- STEP 3: Check if this card already exists for the customer ---
			existing_pms = stripe.PaymentMethod.list(customer=customer.id, type="card")
			matched_pm = None

			for pm in existing_pms.data:
				card = pm.card
				if card.fingerprint == new_fingerprint:
					matched_pm = pm
					break

			# A fingerprint match only proves it's the same card *number* - Stripe never
			# persists raw CVC, only the check *result* from whenever this PaymentMethod was
			# first validated. If that result was "fail", reusing it blindly would trap a
			# customer who's retrying with a corrected CVC forever (the new token they just
			# submitted, which may carry the corrected CVC, would be silently discarded in
			# favor of the old, already-known-bad PaymentMethod object). Fall through to a
			# fresh SetupIntent in that case instead of short-circuiting here.
			if matched_pm and matched_pm.card.checks.cvc_check != "fail":
				# Card already exists and previously passed validation → set as default
				stripe.Customer.modify(
					customer.id,
					invoice_settings={"default_payment_method": matched_pm.id}
				)
				selected_card_id = matched_pm.id

			else:
				# --- STEP 4: Validate card BEFORE saving (IMPORTANT FIX) ---
				setup_intent = stripe.SetupIntent.create(
					customer=customer.id,
					payment_method_data={
						"type": "card",
						"card": {"token": token_id}
					},
					payment_method_types=["card"],            # ← forces card only
					confirm=True,
					automatic_payment_methods={"enabled": False}  # ← disable redirect methods
				)

				if setup_intent.status != "succeeded":
					_safe_detach_payment_method(setup_intent.payment_method)
					frappe.throw(_("Card validation failed. Please use another card."))

				# setup_intent.status == "succeeded" only proves the card authorized - it does
				# NOT prove the CVC matched. Some issuers/networks treat CVC as advisory and
				# still authorize on a mismatch, recording the real result in
				# card.checks.cvc_check instead - the stricter check only runs on a real
				# charge, which for a subscription means the *first invoice*, well after
				# this card has already been attached as the customer's default payment
				# method. Confirming a SetupIntent with a customer attached also auto-attaches
				# the PaymentMethod as a side effect of a *successful* confirmation,
				# independently of the explicit attach below - so a CVC failure caught here
				# must still be explicitly detached, or a "rejected" card ends up silently
				# saved as the customer's default anyway.
				pm_after = stripe.PaymentMethod.retrieve(setup_intent.payment_method)
				if pm_after.card.checks.cvc_check == "fail":
					_safe_detach_payment_method(setup_intent.payment_method)
					frappe.throw(_("Card validation failed. Please use another card."))

				# --- STEP 5: Validation succeeded → Now attach card ---
				payment_method_id = setup_intent.payment_method
				stripe.PaymentMethod.attach(
					payment_method_id,
					customer=customer.id,
				)
				stripe.Customer.modify(
					customer.id,
					invoice_settings={"default_payment_method": payment_method_id}
				)
				selected_card_id = payment_method_id

			tz = pytz.timezone("America/Los_Angeles")
			start_date = datetime(2025, 11, 8, 0, 0, 0, tzinfo=tz)

			# Get the current UTC time
			now = datetime.now(tz)

			# If today is before or on 8 Nov 2025 → delay start
			if now <= start_date:
				subscription = stripe.Subscription.create(
					customer=customer,
					discounts=discount_items,
					items=items,
					add_invoice_items=item_one_time,
					billing_mode={"type": "flexible"},
					off_session=True,
					payment_behavior="error_if_incomplete",
					proration_behavior="none",
					billing_cycle_anchor="1762588800",  # schedule start on 8 Nov
					metadata={
						"customer_id": customer.id
					}
				)
			else:
				# Start immediately
				subscription = stripe.Subscription.create(
					customer=customer,
					discounts=discount_items,
					items=items,
					add_invoice_items=item_one_time,
					billing_mode={"type": "flexible"},
					off_session=True,
					payment_behavior="error_if_incomplete",
					proration_behavior="none",
					metadata={
						"customer_id": customer.id
					}
				)

			if subscription.status == "active":
				stripe_settings.integration_request.db_set("status", "Completed", update_modified=False)
				stripe_settings.flags.status_changed_to = "Completed"

			else:
				stripe_settings.integration_request.db_set("status", "Failed", update_modified=False)
				frappe.log_error(f"Stripe Subscription ID {subscription.id}: Payment failed")
		else:
			stripe_settings.integration_request.db_set("status", "Failed", update_modified=False)
			frappe.log_error(f"Stripe Subscription ID {subscription_data.id}: Payment Link Expired")
			frappe.throw("Payment Link Expired")
	except Exception:
		stripe_settings.integration_request.db_set("status", "Failed", update_modified=False)
		stripe_settings.log_error("Unable to create Stripe subscription")

	return stripe_settings.finalize_request()
