import stripe
import frappe
from frappe import _
from frappe.integrations.utils import create_request_log

@frappe.whitelist(allow_guest=True)
def stripe_cancel_subscription(subscription_id):
    """
    Cancel an active Stripe subscription from ERPNext Subscription Doc
    """
    # Get Stripe Settings
    frappe.log_error(f"before Status check", "Stripe Debug")
    if subscription_id:
        subscription_doc = frappe.get_doc("Subscription", subscription_id)
        frappe.log_error(f"Status check done", "Stripe Debug")
        stripe_settings = frappe.get_doc("Stripe Settings", "Stripe")
        stripe.api_key = stripe_settings.get_password(fieldname="secret_key", raise_exception=False)
        stripe.default_http_client = stripe.http_client.RequestsClient()

        try:
            frappe.log_error(f"in try block", "Stripe Debug")
            # Log the cancellation request
            stripe_settings.integration_request = create_request_log(
                {"subscription_name": subscription_doc.name},
                "Host",
                "Stripe"
            )

            # Fetch the Stripe subscription ID from the ERPNext subscription doc
            stripe_subscription_id = subscription_doc.custom_stripe_subscription_id
            if not stripe_subscription_id:
                frappe.throw(_("No Stripe Subscription ID found for this subscription"))

            frappe.log_error(f"Attempting Stripe cancellation for {stripe_subscription_id}", "Stripe Debug")

            # Cancel the subscription in Stripe
            stripe.Subscription.modify(stripe_subscription_id, cancel_at_period_end=True)

            # Update Integration Log
            stripe_settings.integration_request.db_set("status", "Completed", update_modified=False)
            subscription_doc.db_set("cancelation_date", subscription_doc.current_invoice_start)
            frappe.response.message = {
                "success": True,
                "message": "Auto Pay cancelled successfully"
            }
            return frappe.response.message

        except Exception as e:
            stripe_settings.integration_request.db_set("status", "Failed", update_modified=False)
            frappe.log_error(f"Unable to cancel Stripe subscription: {str(e)}", _("Stripe Subscription Cancel"))
