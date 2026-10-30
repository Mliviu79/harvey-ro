"""Which failures are worth trying again: the network, a timeout, a server
error, being asked to slow down, or the database busy with another
writer for longer than its wait. Shared by the search code (which only
marks a failure) and the job worker (which retries it), and imports
nothing of Harvey's."""

import asyncio
import sqlite3

import httpx


class Transient(Exception):
    """A passing problem: the job is tried again later."""


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, Transient):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    if isinstance(exc, sqlite3.OperationalError):
        text = str(exc).lower()
        return "locked" in text or "busy" in text
    return isinstance(exc, (httpx.TransportError, asyncio.TimeoutError, ConnectionError))
