import stripe
import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from datetime import datetime, timedelta
from frappe.utils import getdate, nowdate, add_months

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

            start_date = getdate(subscription_doc.start_date)
            cancel_after_months = 4
            cancel_at_date = add_months(start_date, cancel_after_months)  # cancel after 4 months

            # If current date < cancel_at_date, we must charge remaining months
            today = getdate(nowdate())
            if today < cancel_at_date:
                remaining_months = (cancel_at_date.year - today.year) * 12 + (cancel_at_date.month - start_date.month)

                if remaining_months > 0:
                    # Get Stripe Customer from ERPNext subscription
                    customer_id = stripe.Subscription.retrieve(stripe_subscription_id).customer

                    # Retrieve plan price to charge remaining months
                    total_amount = 0
                    for plan in subscription_doc.plans:
                        price_id = frappe.db.get_value("Subscription Plan", plan.plan, "product_price_id")
                        # price_obj = stripe.Price.retrieve(price_id)
                        # amount_per_month = price_obj["unit_amount"] / 100  # convert cents to dollars
                        # plan_total = amount_per_month * remaining_months * plan.qty
                        # total_amount += plan_total

                        # Add item to pending Stripe invoice
                        price_obj = stripe.Price.retrieve(price_id)
                        amount_per_month = price_obj["unit_amount"]  # amount in cents

                        stripe.InvoiceItem.create(
                            customer=customer_id,
                            amount=int(amount_per_month * plan.qty * remaining_months),
                            currency=price_obj["currency"],
                            description=f"Early cancellation charge for {remaining_months} remaining month(s)"
                        )
                        # stripe.InvoiceItem.create(
                        #     customer=customer_id,
                        #     price=price_id,
                        #     quantity=plan.qty * remaining_months,
                        #     description=f"Early cancellation charge for {remaining_months} remaining month(s)"
                        # )

                    # Create & charge the one-time invoice on Stripe
                    invoice = stripe.Invoice.create(
                        customer=customer_id,
                        auto_advance=True, # finalize & charge automatically
                        pending_invoice_items_behavior="include"
                    )

                    frappe.log_error(f"Created Stripe invoice {invoice.id} for {remaining_months} months", "Stripe Early Cancellation")

                    for month_index in range(remaining_months):
                        invoice = create_subscription_invoice(subscription_doc.name)

                    # frappe.log_error(f"Created and Paid Sales Invoice {si.name}", "Stripe Early Cancellation")

                # Schedule Stripe cancellation at the 4th month mark
                # cancel_timestamp = int(datetime.combine(cancel_at_date, datetime.min.time()).timestamp())
                frappe.log_error(f"{cancel_at_date}", "Stripe Early Cancellation")
                # stripe.Subscription.modify(
                #     stripe_subscription_id,
                #     cancel_at=cancel_timestamp  # cancel automatically after 4 months
                # )

            else:
                # Cancel the subscription in Stripe
                frappe.log_error("Cancel the subscription in Stripe", "Stripe Early Cancellation")
                # stripe.Subscription.modify(stripe_subscription_id, cancel_at_period_end=True)

                # Update Integration Log
                # stripe_settings.integration_request.db_set("status", "Completed", update_modified=False)
                # subscription_doc.db_set("cancelation_date", subscription_doc.current_invoice_start)

            frappe.response.message = {
                "success": True,
                "message": "Auto Pay cancelled successfully"
            }
            return frappe.response.message

        except Exception as e:
            stripe_settings.integration_request.db_set("status", "Failed", update_modified=False)
            frappe.log_error(f"Unable to cancel Stripe subscription: {str(e)}", _("Stripe Subscription Cancel"))



def create_subscription_invoice(subscription_name, posting_date=None):
    """
    Generate a new Sales Invoice for a Subscription (Frappe Cloud compatible)
    """
    subscription = frappe.get_doc("Subscription", subscription_name)

    if subscription.status == "Cancelled":
        frappe.throw("Subscription is cancelled.")

    # Posting date logic
    if subscription.generate_invoice_at == "Beginning of the current subscription period":
        posting_date = subscription.current_invoice_start
    elif subscription.generate_invoice_at == "Days before the current subscription period":
        posting_date = posting_date or subscription.current_invoice_start
    else:
        posting_date = subscription.current_invoice_end

    # Create invoice doc
    invoice = frappe.get_doc({
        "doctype": "Sales Invoice",
        "company": subscription.company or frappe.db.get_single_value("Global Defaults", "default_company"),
        "customer": subscription.party,
        "posting_date": posting_date,
        "set_posting_time": 1,
        "cost_center": subscription.cost_center or frappe.db.get_value("Company", subscription.company, "cost_center"),
        "subscription": subscription.name,
        "from_date": subscription.current_invoice_start,
        "to_date": subscription.current_invoice_end,
        "currency": frappe.db.get_value("Subscription Plan", {"name": subscription.plans[0].plan}, "currency")
    })

    # Add items from plans
    for plan in subscription.plans:
        plan_doc = frappe.get_doc("Subscription Plan", plan.plan)
        rate = plan_doc.cost * plan.qty
        item = {
            "item_code": plan_doc.item,
            "qty": plan.qty,
            "uom": "Nos",
            "rate": rate,
            "income_account": f"Sales - S&S"
        }
        invoice.append("items", item)

    # Due date
    if subscription.days_until_due:
        due_date = frappe.utils.data.add_days(invoice.posting_date, subscription.days_until_due)
        invoice.append("payment_schedule", {"due_date": due_date, "invoice_portion": 100})

    invoice.flags.ignore_mandatory = True
    invoice.insert()
    if subscription.submit_invoice:
        invoice.submit()

    invoice_doc = frappe.get_doc("Sales Invoice", invoice.name)
    if invoice_doc.status not in ["Paid", "Cancelled"]:
        pe = frappe.get_doc({
            "doctype": "Payment Entry",
            "payment_type": "Receive",
            "posting_date": frappe.utils.nowdate(),
            "party_type": "Customer",
            "party": subscription.party,
            "paid_amount": invoice_doc.grand_total,
            "received_amount": invoice_doc.grand_total,
            "paid_from_account_currency": "USD",
            "paid_to_account_currency": "USD",
            "currency": "USD",
            "paid_from": "Debtors - S&S",       # <-- must set valid account
            "paid_to": "Cash - S&S",    
            "exchange_rate": 1,
            "references": [{
                "reference_doctype": "Sales Invoice",
                "reference_name": invoice_doc.name,  # comes from Payment Request
                "allocated_amount": invoice_doc.grand_total
            }]
        })
        pe.insert(ignore_permissions=True)
        pe.submit()

    next_start = add_months(subscription.current_invoice_start, 1)
    next_end = add_months(subscription.current_invoice_end, 1)

    subscription.db_set("current_invoice_start", next_start, update_modified=False)
    subscription.db_set("current_invoice_end", next_end, update_modified=False)

    return invoice.name