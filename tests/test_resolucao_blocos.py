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


# --- renomear marcador ------------------------------------------------------------

class ClienteMarcadores:
    def __init__(self):
        self.alterados = []
        self.lista = [
            {"id": "41058", "nome": "#6f0b0b", "idCor": "1"},
            {"id": "21996", "nome": "AGUARDANDO RETORNO", "idCor": "7"},
            {"id": "28055", "nome": "AGUARDANDO RETORNO #090601", "idCor": "2"},
        ]

    async def pesquisar_marcadores(self, limit=50, **kw):
        return {"marcadores": self.lista}

    async def alterar_marcador(self, id_marcador, nome, id_cor):
        self.alterados.append((id_marcador, nome, id_cor))
        return {}


@pytest.mark.asyncio
async def test_renomeia_mantendo_cor(monkeypatch):
    c = ClienteMarcadores()
    monkeypatch.setattr(srv, "_get_client", lambda ctx: c)
    r = json.loads(await srv.sei_alterar_marcador(marcador="#6f0b0b", novo_nome="PRIORIDADE", ctx=CTX))
    assert c.alterados == [("41058", "PRIORIDADE", "1")]
    assert r["antes"]["nome"] == "#6f0b0b"


@pytest.mark.asyncio
async def test_nao_duplica_nome(monkeypatch):
    c = ClienteMarcadores()
    monkeypatch.setattr(srv, "_get_client", lambda ctx: c)
    r = json.loads(await srv.sei_alterar_marcador(marcador="28055", novo_nome="aguardando retorno", ctx=CTX))
    assert "Já existe" in r["error"] and c.alterados == []


@pytest.mark.asyncio
async def test_marcador_inexistente(monkeypatch):
    c = ClienteMarcadores()
    monkeypatch.setattr(srv, "_get_client", lambda ctx: c)
    r = json.loads(await srv.sei_alterar_marcador(marcador="xyz", novo_nome="a", ctx=CTX))
    assert "não encontrado" in r["error"] and len(r["marcadores_disponiveis"]) == 3
