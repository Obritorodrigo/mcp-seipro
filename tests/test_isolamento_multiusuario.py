"""Testes de isolamento dos clientes por sessão OAuth."""

from types import SimpleNamespace

import pytest
import mcp_seipro.server as srv


class ClienteFicticio:
    def __init__(self, **kwargs):
        self.credenciais = kwargs
        self.unidade_ativa = None
        self.fechado = False

    async def close(self):
        self.fechado = True


@pytest.fixture
def ambiente(monkeypatch):
    monkeypatch.setattr(srv, "_http_mode", True)
    monkeypatch.setattr(srv, "SEIClient", ClienteFicticio)
    monkeypatch.setattr(srv, "SEIWebClient", ClienteFicticio)

    sessoes = {
        "A": (
            "sid-usuario-A",
            {
                "sei_url": "https://sei.exemplo.gov.br",
                "sei_usuario": "usuario_A",
                "sei_senha": "senha_ficticia_A",
                "sei_orgao": "1",
            },
        ),
        "B": (
            "sid-usuario-B",
            {
                "sei_url": "https://sei.exemplo.gov.br",
                "sei_usuario": "usuario_B",
                "sei_senha": "senha_ficticia_B",
                "sei_orgao": "1",
            },
        ),
    }

    atual = {"usuario": "A"}

    monkeypatch.setattr(
        srv,
        "_credenciais_sessao",
        lambda: sessoes[atual["usuario"]],
    )

    contexto = SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context={
                "sei": {},
                "sei_web": {},
            }
        )
    )

    return atual, contexto


def test_clientes_rest_sao_independentes(ambiente):
    atual, ctx = ambiente

    atual["usuario"] = "A"
    cliente_a = srv._get_client(ctx)

    atual["usuario"] = "B"
    cliente_b = srv._get_client(ctx)

    assert cliente_a is not cliente_b
    assert cliente_a.credenciais["sei_usuario"] == "usuario_A"
    assert cliente_b.credenciais["sei_usuario"] == "usuario_B"

    atual["usuario"] = "A"
    assert srv._get_client(ctx) is cliente_a


def test_unidades_ativas_sao_independentes(ambiente):
    atual, ctx = ambiente

    atual["usuario"] = "A"
    cliente_a = srv._get_client(ctx)
    cliente_a.unidade_ativa = "SEJUC/DEPLAF"

    atual["usuario"] = "B"
    cliente_b = srv._get_client(ctx)
    cliente_b.unidade_ativa = "SEJUC/DESIPE"

    assert cliente_a.unidade_ativa == "SEJUC/DEPLAF"
    assert cliente_b.unidade_ativa == "SEJUC/DESIPE"


def test_clientes_web_sao_independentes(ambiente):
    atual, ctx = ambiente

    atual["usuario"] = "A"
    cliente_a = srv._get_web_client(ctx)

    atual["usuario"] = "B"
    cliente_b = srv._get_web_client(ctx)

    assert cliente_a is not cliente_b
    assert cliente_a.credenciais["sei_usuario"] == "usuario_A"
    assert cliente_b.credenciais["sei_usuario"] == "usuario_B"


def test_reutiliza_cliente_web_da_mesma_sessao(ambiente):
    atual, ctx = ambiente

    atual["usuario"] = "A"
    primeiro = srv._get_web_client(ctx)
    segundo = srv._get_web_client(ctx)

    assert primeiro is segundo
