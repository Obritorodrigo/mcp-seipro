"""Bloqueio de rede externa durante testes locais."""

import socket
import pytest


@pytest.fixture(autouse=True)
def bloquear_rede_externa(monkeypatch):
    original = socket.socket.connect

    def connect(self, address):
        if isinstance(address, tuple):
            host = str(address[0])
            if host not in ("127.0.0.1", "::1", "localhost"):
                raise RuntimeError(
                    "Rede externa bloqueada durante os testes"
                )
        return original(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
