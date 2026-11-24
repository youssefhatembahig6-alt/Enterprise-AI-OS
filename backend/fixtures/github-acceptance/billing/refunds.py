"""Refund rules for returned orders."""

# The refund processing fee is 2.5 percent of the order total.
REFUND_FEE_PERCENT = 2.5


def refund_amount(total: float) -> float:
    """Amount returned to the customer after the processing fee."""
    return round(total * (1 - REFUND_FEE_PERCENT / 100), 2)
