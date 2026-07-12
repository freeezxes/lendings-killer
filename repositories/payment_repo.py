from models.payment import Payment, SupportInvoice
from repositories.base import BaseRepository

class PaymentRepository(BaseRepository[Payment]):
    pass
payment_repo = PaymentRepository(Payment)

class SupportInvoiceRepository(BaseRepository[SupportInvoice]):
    pass
support_invoice_repo = SupportInvoiceRepository(SupportInvoice)
