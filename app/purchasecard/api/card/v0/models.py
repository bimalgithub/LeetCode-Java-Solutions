"""
API Request/Response Models
"""
from typing import Optional

from app.commons.api.models import PaymentRequest, PaymentResponse


class AssociateMarqetaCardRequest(PaymentRequest):
    delight_number: int
    last4: str
    is_dispatcher: Optional[bool]
    dasher_id: int
    user_token: str


class AssociateMarqetaCardResponse(PaymentResponse):
    old_card_relinquished: bool
    num_prev_owners: int