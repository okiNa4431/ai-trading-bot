"""Automated tests must never access the live market API."""

import socket

import pytest
import yfinance as yf
from google import genai


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network access forbidden in automated tests")

    # curl_cffi may bypass Python sockets, so block the Yahoo entry point too.
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(yf, "Ticker", forbidden)
    monkeypatch.setattr(genai, "Client", forbidden)
