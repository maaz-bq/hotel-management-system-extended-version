# -*- coding: utf-8 -*-

from odoo import models
from odoo.addons.account.wizard.account_payment_register import (
    AccountPaymentRegister as AccountPaymentRegisterBase,
)


class AccountPaymentRegister(models.TransientModel):
    _inherit = "account.payment.register"

    def _create_payments(self):
        # Bypass hotel_management_system's broken template XML ID and call
        # the standard account implementation directly.
        payments = AccountPaymentRegisterBase._create_payments(self)
        if not payments:
            return payments

        invoice_ids = self.env.context.get("active_ids", [])
        if not invoice_ids:
            return payments

        invoices = self.env["account.move"].browse(invoice_ids).exists()
        if not invoices:
            return payments

        template = self.env.ref(
            "hotel_management_system.payment_confirmation_email_template",
            raise_if_not_found=False,
        )
        if not template:
            return payments

        for payment in payments:
            payment_moves = payment.move_id.line_ids.mapped("move_id")
            related_invoices = invoices.filtered(lambda inv: inv in payment_moves)
            if not related_invoices:
                continue

            source_orders = related_invoices.line_ids.sale_line_ids.order_id
            if source_orders:
                payment.sale_order_id = source_orders[0].id

            email_to = related_invoices[0].invoice_user_id.email
            if email_to:
                template.send_mail(
                    payment.id,
                    force_send=True,
                    email_values={"email_to": email_to},
                )

        return payments
