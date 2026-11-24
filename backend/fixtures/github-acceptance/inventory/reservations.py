"""Inventory reservations for open orders.

Reservations hold stock while a customer pays.
"""

RESERVATION_MINUTES = 45  # inventory reservations last 45 minutes before they expire


def reserve(sku: str, quantity: int) -> dict[str, int]:
    """Hold stock for an order."""
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    return {"quantity": quantity, "minutes": RESERVATION_MINUTES}
