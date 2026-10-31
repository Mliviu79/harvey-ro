"""External integrations: Instantly, email discovery, calendar.

Submodules are intentionally NOT imported here so that optional heavy
dependencies (dnspython, aiosmtplib) are only loaded by the
agents that actually use them.
"""

__all__ = ["instantly", "email_finder", "calendar"]
