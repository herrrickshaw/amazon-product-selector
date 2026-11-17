"""Tests must never reach a real API: block outbound sockets for every test.

A test that forgets to stub a fetch should fail here instead of quietly sending
a request (and a placeholder key) to OpenWeb Ninja or Nexscope. The attempt is
recorded and asserted at teardown because the fetch code catches and logs
network errors, so raising alone would be swallowed.
"""
import socket

import pytest


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    attempts = []

    def guard(*args, **kwargs):
        attempts.append(args[:2])
        raise OSError("network access attempted during a test")
    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(socket, "getaddrinfo", guard)
    yield
    assert not attempts, f"test tried to open a network connection: {attempts}"
