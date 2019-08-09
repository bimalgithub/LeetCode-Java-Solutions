from fastapi import HTTPException
from typing import Any, Tuple, List, Optional
import uuid

from app.commons.context.app_context import AppContext
from app.commons.context.req_context import ReqContext
from app.commons.types import CountryCode
from app.commons.providers.stripe_models import (
    CapturePaymentIntent,
    CreatePaymentIntent,
)
from app.commons.utils.types import PaymentProvider
from app.payin.core.cart_payment.model import (
    CartPayment,
    PaymentIntent,
    PgpPaymentIntent,
)
from app.payin.core.cart_payment.types import (
    CaptureMethod,
    ConfirmationMethod,
    IntentStatus,
)

from app.payin.repository.cart_payment_repo import CartPaymentRepository


class CartPaymentInterface:
    def __init__(
        self,
        app_context: AppContext,
        req_context: ReqContext,
        payment_repo: CartPaymentRepository,
    ):
        self.app_context = app_context
        self.req_context = req_context
        self.payment_repo = payment_repo

    async def _find_existing(
        self, payer_id: str, idempotency_key: str
    ) -> Tuple[Optional[CartPayment], Optional[PaymentIntent]]:
        payment_intent = await self.payment_repo.get_payment_intent_for_idempotency_key(
            idempotency_key
        )

        if not payment_intent:
            return (None, None)

        cart_payment = await self.payment_repo.get_cart_payment_by_id(
            payment_intent.cart_payment_id
        )

        return (cart_payment, payment_intent)

    def _transform_method_for_stripe(self, method_name: str) -> str:
        if method_name == "auto":
            return "automatic"
        return method_name

    def _get_provider_capture_method(
        self, pgp_payment_intent: PgpPaymentIntent
    ) -> CreatePaymentIntent.CaptureMethod:
        target_method = self._transform_method_for_stripe(
            pgp_payment_intent.capture_method
        )
        return CreatePaymentIntent.CaptureMethod(target_method)

    def _get_provider_confirmation_method(
        self, pgp_payment_intent: PgpPaymentIntent
    ) -> CreatePaymentIntent.ConfirmationMethod:
        target_method = self._transform_method_for_stripe(
            pgp_payment_intent.confirmation_method
        )
        return CreatePaymentIntent.ConfirmationMethod(target_method)

    def _get_provider_future_usage(self, payment_intent: PaymentIntent):
        if payment_intent.capture_method == CaptureMethod.AUTO:
            return CreatePaymentIntent.SetupFutureUsage.on_session

        return CreatePaymentIntent.SetupFutureUsage.off_session

    async def _create_provider_payment(
        self, payment_intent: PaymentIntent, pgp_payment_intent: PgpPaymentIntent
    ):
        # Call to stripe payment intent API
        assert pgp_payment_intent.idempotency_key
        try:
            intent_request = CreatePaymentIntent(
                amount=pgp_payment_intent.amount,
                currency=pgp_payment_intent.currency,
                application_fee_amount=pgp_payment_intent.application_fee_amount,
                capture_method=self._get_provider_capture_method(pgp_payment_intent),
                confirm=True,
                confirmation_method=self._get_provider_confirmation_method(
                    pgp_payment_intent
                ),
                on_behalf_of=pgp_payment_intent.payout_account_id,
                setup_future_usage=self._get_provider_future_usage(payment_intent),
                payment_method=pgp_payment_intent.payment_method_resource_id,
                statement_descriptor=payment_intent.statement_descriptor,
            )

            response = await self.app_context.stripe.create_payment_intent(
                country=CountryCode(payment_intent.country),
                request=intent_request,
                idempotency_key=pgp_payment_intent.idempotency_key,
            )
            # self.req_context.log.info("Provider response: ")
            # self.req_context.log.info(response)
            return response
        except Exception as e:
            # TODO Add better error handling here
            self.req_context.log.error(f"Error invoking provider: {e}")
            raise HTTPException(status_code=500, detail="Internal error")

    # req_context, payin_repos, pgp_intent_id, provider_payment_response
    async def _update_pgp_intent_from_provider(
        self,
        connection,
        pgp_intent_id: uuid.UUID,
        status: IntentStatus,
        provider_payment_response: Any,
    ) -> None:
        await self.payment_repo.update_pgp_payment_intent(
            connection=connection,
            id=pgp_intent_id,
            status=status,
            provider_intent_id=provider_payment_response,
        )

    async def _submit_payment_to_provider(
        self, payment_intent: PaymentIntent, pgp_payment_intent: PgpPaymentIntent
    ):
        # Call out to provider to create the payment intent in their system.  The payment_intent
        # instance includes the idempotency_key, which is passed to provider to ensure records already
        # sumitted are actually processed only once.
        provider_payment_response = await self._create_provider_payment(
            payment_intent=payment_intent, pgp_payment_intent=pgp_payment_intent
        )

        connection, transaction = await (
            self.payment_repo.get_payment_database_connection_and_transaction()
        )

        try:
            # Update the records we created to reflect that the provider has been invoked.
            # Cannot gather calls here because of shared connection/transaction
            await self.payment_repo.update_payment_intent_status(
                connection, payment_intent.id, IntentStatus.REQUIRES_CAPTURE
            )
            await self._update_pgp_intent_from_provider(
                connection,
                pgp_payment_intent.id,
                IntentStatus.REQUIRES_CAPTURE,
                provider_payment_response,
            )

            await transaction.commit()
        except Exception as e:
            await transaction.rollback()
            raise e
        finally:
            await self.payment_repo.close_payment_database_connection(connection)

    def _populate_cart_payment_for_response(
        self,
        cart_payment: CartPayment,
        payment_intent: PaymentIntent,
        pgp_payment_intent: PgpPaymentIntent,
    ):
        """
        Populate fields within a CartPayment instance to be suitable for an API response body.
        Since CartPayment is a view on top of several models, it is necessary to synthesize info
        into a CartPayment instance from associated models.

        Arguments:
            cart_payment {CartPayment} -- The CartPayment instance to update.
            payment_intent {PaymentIntent} -- An associated PaymentIntent.
            pgp_payment_intent {PgpPaymentIntent} -- An associated PgpPaymentIntent.
        """
        cart_payment.capture_method = payment_intent.capture_method
        cart_payment.payer_statement_description = payment_intent.statement_descriptor
        cart_payment.payment_method_id = pgp_payment_intent.payment_method_resource_id

    async def submit_new_payment(
        self,
        request_cart_payment: CartPayment,
        idempotency_key: str,
        country: str,
        currency: str,
        client_description: str,
    ):
        # Create a new cart payment, with associated models
        self.req_context.log.debug(
            f"Submitting new payment for payer ${request_cart_payment.payer_id}"
        )

        # Required as inputs to creation API, but optional in model
        assert request_cart_payment.capture_method
        assert request_cart_payment.payment_method_id

        connection, transaction = (
            await self.payment_repo.get_payment_database_connection_and_transaction()
        )

        try:
            # Create CartPayment
            cart_payment: CartPayment = await self.payment_repo.insert_cart_payment(
                connection=connection,
                id=uuid.uuid4(),
                payer_id=request_cart_payment.payer_id,
                type=request_cart_payment.cart_metadata.type,
                client_description=request_cart_payment.client_description,
                reference_id=request_cart_payment.cart_metadata.reference_id,
                reference_ct_id=request_cart_payment.cart_metadata.ct_reference_id,
                legacy_consumer_id=request_cart_payment.legacy_payment.consumer_id
                if request_cart_payment.legacy_payment
                else None,
                amount_original=request_cart_payment.amount,
                amount_total=request_cart_payment.amount,
            )

            # Create PaymentIntent
            assert cart_payment.id
            payment_intent: PaymentIntent = await self.payment_repo.insert_payment_intent(
                connection=connection,
                id=uuid.uuid4(),
                cart_payment_id=cart_payment.id,
                idempotency_key=idempotency_key,
                amount_initiated=request_cart_payment.amount,
                amount=request_cart_payment.amount,
                application_fee_amount=getattr(
                    request_cart_payment.split_payment, "application_fee_amount", None
                ),
                country=country,
                currency=currency,
                capture_method=request_cart_payment.capture_method,
                confirmation_method=ConfirmationMethod.MANUAL,
                status=IntentStatus.INIT,
                statement_descriptor=request_cart_payment.payer_statement_description,
            )

            # Create PgpPaymentIntent
            pgp_payment_intent: PgpPaymentIntent = await self.payment_repo.insert_pgp_payment_intent(
                connection=connection,
                id=uuid.uuid4(),
                payment_intent_id=payment_intent.id,
                idempotency_key=idempotency_key,
                provider=PaymentProvider.STRIPE.value,
                payment_method_resource_id=request_cart_payment.payment_method_id,
                currency=currency,
                amount=request_cart_payment.amount,
                application_fee_amount=getattr(
                    request_cart_payment.split_payment, "application_fee_amount", None
                ),
                payout_account_id=getattr(
                    request_cart_payment.split_payment, "payout_account_id", None
                ),
                capture_method=request_cart_payment.capture_method,
                confirmation_method=ConfirmationMethod.MANUAL,
                status=IntentStatus.INIT,
                statement_descriptor=request_cart_payment.payer_statement_description,
            )

            # Commit and close transaction.  Close transaction prior to external call.
            await transaction.commit()
        except Exception as e:
            await transaction.rollback()
            raise e
        finally:
            await self.payment_repo.close_payment_database_connection(connection)

        await self._submit_payment_to_provider(payment_intent, pgp_payment_intent)
        self._populate_cart_payment_for_response(
            cart_payment, payment_intent, pgp_payment_intent
        )
        return cart_payment

    def _get_cart_payment_submission_pgp_intent(
        self, pgp_intents: List[PgpPaymentIntent]
    ) -> PgpPaymentIntent:
        # Since cart_payment/payment_intent/pgp_payment_intent are first created in one transaction,
        # we will have at least one.  Find the first one, since this is an attempt to recreate the
        # cart_payment.
        return pgp_intents[0]

    def _is_payment_intent_submitted(self, payment_intent: PaymentIntent):
        return payment_intent.status != IntentStatus.INIT

    def _is_intent_processed(self, payment_intent: PaymentIntent):
        return payment_intent.status in [IntentStatus.SUCCEEDED, IntentStatus.FAILED]

    def _get_intent_status_from_provider_status(
        self, provider_status: str
    ) -> IntentStatus:
        return IntentStatus(provider_status)

    def _is_pgp_payment_intent_submitted(self, pgp_payment_intent: PgpPaymentIntent):
        return pgp_payment_intent.status != IntentStatus.INIT

    async def resubmit_existing_payment(
        self, cart_payment: CartPayment, payment_intent: PaymentIntent
    ) -> CartPayment:
        """
        Resubmit an existing cart payment.  Intended to be used for second calls to create a cart payment for the same idempotency key.

        Arguments:
            cart_payment {CartPayment} -- The CartPayment instance to resubmit.
            payment_intent {PaymentIntent} -- The associated PaymentIntent instance for the cart payment.

        Returns:
            CartPayment -- The (resubmitted) CartPayment.
        """
        assert cart_payment.id == payment_intent.cart_payment_id
        # Handle creation attempts of the same cart_payment/payment_intent
        if self._is_payment_intent_submitted(payment_intent):
            # Already submitted, nothing left to do.
            return cart_payment

        # Check state of call to provider/PgpPaymentIntent: resubmit if necessary
        pgp_intents = await self.payment_repo.find_pgp_payment_intents(
            payment_intent.id
        )

        pgp_intent = self._get_cart_payment_submission_pgp_intent(pgp_intents)
        if self._is_pgp_payment_intent_submitted(pgp_intent):
            return cart_payment

        self.req_context.log.info(
            f"Attempting resubmission of payment to provider for cart_payment {cart_payment.id}, payment_intent {payment_intent.id}, pgp_payment_intent {pgp_intent.id if pgp_intent else 'None'}"
        )
        await self._submit_payment_to_provider(payment_intent, pgp_intent)
        self._populate_cart_payment_for_response(
            cart_payment, payment_intent, pgp_intent
        )
        return cart_payment

    async def _capture_payment_with_provider(
        self, payment_intent: PaymentIntent, pgp_payment_intent: PgpPaymentIntent
    ) -> str:
        # Call to stripe payment intent API
        try:
            intent_request = CapturePaymentIntent(sid=pgp_payment_intent.resource_id)

            self.req_context.log.info(
                f"Capturing payment intent: {payment_intent.country}, key: {pgp_payment_intent.idempotency_key}"
            )
            response = await self.app_context.stripe.capture_payment_intent(
                country=CountryCode(payment_intent.country),
                request=intent_request,
                idempotency_key=str(uuid.uuid4()),  # TODO handle idempotency key
            )
            self.req_context.log.info("Provider response: ")
            self.req_context.log.info(response)
            return response
        except Exception as e:
            # TODO Add better error handling here
            self.req_context.log.error(f"Error invoking provider: {e}")
            print(dir(e))
            print(e.__traceback__)
            raise HTTPException(status_code=500, detail="Internal error")

    async def capture_payment(self, payment_intent: PaymentIntent) -> None:
        """Capture a payment intent.

        Arguments:
            payment_intent {PaymentIntent} -- The PaymentIntent to capture.

        Raises:
            e: Raises an exception if database operations fail.

        Returns:
            None
        """
        self.req_context.log.info(
            f"Capture attempt for payment_intent {payment_intent.id}"
        )
        # TODO scheduling of this logic
        # TODO concurrency control to ensure we are capturing something once (though underlying provider call will be idempotent)
        # Check state of intent.  If already captured (successfully or not), stop.  Clients are expected to
        if self._is_intent_processed(payment_intent):
            self.req_context.log.info(
                f"Payment intent not eligible for capturing, in state {payment_intent.status}"
            )
            return

        # Find the PgpPaymentIntent to capture
        pgp_intents = await self.payment_repo.find_pgp_payment_intents(
            payment_intent.id
        )
        # TODO work through case where there may be multiples
        pgp_payment_intent = pgp_intents[0]

        # Call to provider to capture, with idempotency key
        provider_status = await self._capture_payment_with_provider(
            payment_intent, pgp_payment_intent
        )
        new_status = self._get_intent_status_from_provider_status(provider_status)
        self.req_context.log.info(
            f"Updating intent {payment_intent.id}, pgp intent {pgp_payment_intent.id} to status {new_status}"
        )

        # Update state
        connection, transaction = await (
            self.payment_repo.get_payment_database_connection_and_transaction()
        )

        try:
            await self.payment_repo.update_payment_intent_status(
                connection, payment_intent.id, new_status
            )
            await self._update_pgp_intent_from_provider(
                connection,
                pgp_payment_intent.id,
                new_status,
                pgp_payment_intent.resource_id,
            )

            await transaction.commit()
        except Exception as e:
            await transaction.rollback()
            raise e
        finally:
            await self.payment_repo.close_payment_database_connection(connection)


async def submit_payment(
    app_context: AppContext,
    req_context: ReqContext,
    payment_repo: CartPaymentRepository,
    request_cart_payment: CartPayment,
    idempotency_key: str,
    country: str,
    currency: str,
    client_description: str,
) -> CartPayment:
    """Submit a cart payment creation request.

    Arguments:
        app_context {AppContext} -- Application context.
        req_context {ReqContext} -- Request context.
        payment_repo {CartPaymentRepository} -- Repo for accessing CartPayment and associated models.
        request_cart_payment {CartPayment} -- CartPayment model containing request paramters provided by client.
        idempotency_key {str} -- Client specified value for ensuring idempotency.
        country {str} -- ISO country code.
        currency {str} -- Currency for cart payment request.
        client_description {str} -- Pass through value clients may associated with the cart payment.

    Returns:
        CartPayment -- A CartPayment model for the created payment.
    """
    # Check for resubmission by client
    cart_payment_interface = CartPaymentInterface(
        app_context, req_context, payment_repo
    )

    cart_payment, payment_intent = await cart_payment_interface._find_existing(
        request_cart_payment.payer_id, idempotency_key
    )
    if cart_payment:
        assert payment_intent
        return await cart_payment_interface.resubmit_existing_payment(
            cart_payment, payment_intent
        )

    return await cart_payment_interface.submit_new_payment(
        request_cart_payment, idempotency_key, country, currency, client_description
    )
