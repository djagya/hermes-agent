"""Checkout grants: one confirmation, one submit, no card data, no silent reuse after change."""

from agent.payment_grant import (
    authorize,
    build_envelope,
    confirmation_text,
    consume_submit,
    release_submit,
    reset_for_tests,
)


def _envelope(**overrides):
    payload = dict(
        session_key="sess",
        task_id="task",
        handle="op:vault:card",
        card_label="N26 Sera Credit Card",
        merchant_origin="https://tickets.mahalla.berlin",
        currency="eur",
        amount="94.26",
        recipient="owner@example.com",
        items=[{"label": "General Admission", "count": 3}],
        lease_epoch=0,
    )
    payload.update(overrides)
    return build_envelope(**payload)


def setup_function():
    reset_for_tests()


def test_identical_open_grant_does_not_ask_again():
    asks = []

    def consent(message, description):
        asks.append(message)
        return "accept"

    envelope = _envelope()
    first = authorize(envelope, consent=consent, now=1_000)
    second = authorize(envelope, consent=consent, now=1_100)
    assert first.status == "accept" and first.reused is False
    assert second.status == "accept" and second.reused is True
    assert len(asks) == 1
    assert "94.26" in asks[0] and "EUR" in asks[0]
    assert "4111" not in asks[0]


def test_changed_amount_asks_again_and_decline_writes_no_grant():
    asks = []

    def consent(message, description):
        asks.append(message)
        return "decline"

    assert authorize(_envelope(), consent=consent, now=1_000).status == "decline"
    assert (
        authorize(_envelope(amount="10.00"), consent=consent, now=1_000).status
        == "decline"
    )
    assert len(asks) == 2


def test_submit_is_once_until_a_provable_miss_releases_it():
    envelope = _envelope()
    authorize(envelope, consent=lambda *_: "accept", now=1_000)
    assert consume_submit(envelope, now=1_000) is True
    assert (
        authorize(envelope, consent=lambda *_: "accept", now=1_100).status == "replay"
    )
    assert consume_submit(envelope, now=1_100) is False
    release_submit(envelope)
    assert authorize(envelope, consent=lambda *_: "accept", now=1_100).reused is True


def test_expired_grant_asks_again():
    asks = []
    envelope = _envelope()
    authorize(envelope, consent=lambda *_: "accept", now=1_000)
    again = authorize(
        envelope, consent=lambda *args: asks.append(args) or "accept", now=1_000 + 601
    )
    assert again.reused is False and len(asks) == 1


def test_confirmation_names_the_charge_and_not_a_card_number():
    message, description = confirmation_text(_envelope())
    assert "94.26 EUR" in message
    assert "3 × General Admission" in description
    assert "owner@example.com" in description
    assert "4111111111111111" not in message + description
