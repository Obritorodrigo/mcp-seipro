"""Bloqueia conexões de rede durante os testes automatizados."""

import socket
import pytest


@pytest.fixture(autouse=True)
def bloquear_rede(monkeypatch):
    def negar_conexao(*args, **kwargs):
        raise RuntimeError(
            "Conexao de rede bloqueada durante os testes"
        )

    monkeypatch.setattr(socket.socket, "connect", negar_conexao)
    monkeypatch.setattr(socket, "create_connection", negar_conexao)


@pytest.fixture(autouse=True)
def banco_revogacoes_temporario(monkeypatch, tmp_path):
    """
    Cada teste usa um SQLite exclusivo.
    Nunca escreve no banco de produção.
    """
    from mcp_seipro import revogacoes

    caminho = tmp_path / "revogacoes.sqlite3"
    monkeypatch.setattr(revogacoes, "DB_PATH", caminho)

    assert str(caminho).startswith(str(tmp_path))


@pytest.fixture(autouse=True)
def auditoria_temporaria(monkeypatch, tmp_path):
    """A auditoria de cada teste vai para um arquivo temporário."""
    monkeypatch.setenv("SEI_AUDITORIA_LOG", str(tmp_path / "auditoria.jsonl"))


@pytest.fixture(autouse=True)
def limites_zerados():
    """Cada teste começa sem chamadas contadas no limitador de uso."""
    from mcp_seipro import limites

    limites.limpar()
    yield
    limites.limpar()
