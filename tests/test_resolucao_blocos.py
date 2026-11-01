"""Tools de bloco/assinatura resolvem número SEI → id interno antes de chamar o wssei.

Visto ao vivo em 01/11/2026: sei_listar_blocos_documento("24846961") devolveu o
documento 22734049 (outro), porque o número SEI foi usado como id interno.
"""

import json
from types import SimpleNamespace

import pytest

import mcp_seipro.server as srv

CTX = SimpleNamespace()
MAPA = {"24846961": "27113336", "24846801": "27113158"}


class C:
    def __init__(self):
        self.chamadas = []

    def __getattr__(self, nome):
        async def rota(*args, **kw):
            self.chamadas.append((nome, args, kw))
            return {"ok": True}
        return rota


@pytest.fixture
def cliente(monkeypatch):
    c = C()

    async def resolver(client, ref):
        return MAPA[ref], "I"

    monkeypatch.setattr(srv, "_get_client", lambda ctx: c)
    monkeypatch.setattr(srv, "_resolver_documento", resolver)
    return c


@pytest.mark.asyncio
async def test_listar_blocos_e_assinaturas(cliente):
    await srv.sei_listar_blocos_documento(id_documento="24846961", ctx=CTX)
    await srv.sei_listar_assinaturas(id_documento="24846961", ctx=CTX)
    assert [c[1] for c in cliente.chamadas] == [("27113336",), ("27113336",)]


@pytest.mark.asyncio
async def test_incluir_retirar_e_anotar_em_bloco(cliente):
    await srv.sei_incluir_documento_bloco_assinatura(id_bloco="441032", documentos="24846961, 24846801", ctx=CTX)
    await srv.sei_retirar_documentos_bloco_assinatura(id_bloco="441032", documentos="24846961", ctx=CTX)
    await srv.sei_anotar_documento_bloco_assinatura(id_bloco="441032", documento="24846961", descricao="x", ctx=CTX)
    assert cliente.chamadas[0][1] == ("441032", "27113336,27113158")
    assert cliente.chamadas[1][1] == ("441032", "27113336")
    assert cliente.chamadas[2][1] == ("441032", "27113336", "x")


@pytest.mark.asyncio
async def test_numero_sem_acesso_nao_vira_outro_documento(monkeypatch):
    c = C()

    async def resolver(client, ref):
        raise ValueError("Documento 24847106 existe, mas a unidade atual não tem acesso")

    monkeypatch.setattr(srv, "_get_client", lambda ctx: c)
    monkeypatch.setattr(srv, "_resolver_documento", resolver)
    r = json.loads(await srv.sei_incluir_documento_bloco_assinatura(id_bloco="1", documentos="24847106", ctx=CTX))
    assert "error" in r and c.chamadas == []
