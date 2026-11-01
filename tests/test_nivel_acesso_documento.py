"""Mudar documento entre público, restrito e sigiloso.

Restrito/sigiloso exigem hipótese legal. Sem ela (ou com nome ambíguo) a tool
não altera nada e devolve as hipóteses válidas para o usuário escolher.
"""

import json
from types import SimpleNamespace

import pytest

import mcp_seipro.server as srv

CTX = SimpleNamespace()
HIPOTESES = [
    {"id": "34", "nome": "Informação Pessoal", "baselegal": "Art. 31 da Lei nº 12.527/2011"},
    {"id": "12", "nome": "Controle Interno", "baselegal": "Art. 26, § 3º, da Lei nº 10.180/2001"},
    {"id": "15", "nome": "Documento Preparatório", "baselegal": "Art. 7º, § 3º, da Lei nº 12.527/2011"},
]


class Cliente:
    def __init__(self, nivel_atual="0"):
        self.nivel_atual = nivel_atual
        self.alteracoes = []
        self.pesquisas = []

    async def consultar_documento_interno(self, doc_id):
        return {"nivelAcesso": self.nivel_atual}

    async def consultar_documento_externo(self, doc_id):
        return {"nivelAcesso": self.nivel_atual}

    async def pesquisar_hipoteses_legais(self, filtro="", limit=50, start=0, nivel_acesso=""):
        self.pesquisas.append(nivel_acesso)
        return {"hipoteses": HIPOTESES}

    async def alterar_documento_interno(self, **kw):
        self.alteracoes.append(("I", kw))
        return {"idDocumento": kw["id_documento"]}

    async def alterar_documento_externo(self, **kw):
        self.alteracoes.append(("X", kw))
        return {"idDocumento": kw["id_documento"]}


@pytest.fixture
def usar(monkeypatch):
    async def resolver(client, ref):
        return "27113158", "I"

    monkeypatch.setattr(srv, "_resolver_documento", resolver)

    def _usar(c):
        monkeypatch.setattr(srv, "_get_client", lambda ctx: c)
        return c
    return _usar


async def _alterar(**kw):
    return json.loads(await srv.sei_alterar_documento_interno(id_documento="24846801", ctx=CTX, **kw))


@pytest.mark.asyncio
async def test_restrito_sem_hipotese_lista_opcoes_e_nao_altera(usar):
    c = usar(Cliente())
    r = await _alterar(nivel_acesso="restrito")
    assert "hipótese legal" in r["error"]
    assert [h["id"] for h in r["hipoteses_disponiveis"]] == ["34", "12", "15"]
    assert c.alteracoes == []
    assert c.pesquisas == ["1"]  # só hipóteses válidas para restrito


@pytest.mark.asyncio
async def test_restrito_com_hipotese_pelo_nome_sem_acento(usar):
    c = usar(Cliente())
    r = await _alterar(nivel_acesso="restrito", hipotese_legal="informacao pessoal")
    [(tipo, kw)] = c.alteracoes
    assert tipo == "I"
    assert kw["nivel_acesso"] == "1" and kw["id_hipotese_legal"] == "34"
    assert r["nivel_acesso_descricao"] == "Restrito"


@pytest.mark.asyncio
async def test_restrito_com_id_da_hipotese_nao_pesquisa(usar):
    c = usar(Cliente())
    await _alterar(nivel_acesso="1", hipotese_legal="12")
    assert c.pesquisas == []
    assert c.alteracoes[0][1]["id_hipotese_legal"] == "12"


@pytest.mark.asyncio
async def test_hipotese_ambigua_nao_altera(usar):
    c = usar(Cliente())
    r = await _alterar(nivel_acesso="restrito", hipotese_legal="lei 12.527")
    assert "Mais de uma" in r["error"]
    assert {h["id"] for h in r["hipoteses_disponiveis"]} == {"34", "15"}
    assert c.alteracoes == []


@pytest.mark.asyncio
async def test_tornar_publico_nao_exige_hipotese(usar):
    c = usar(Cliente(nivel_atual="1"))
    r = await _alterar(nivel_acesso="público")
    [(_, kw)] = c.alteracoes
    assert kw["nivel_acesso"] == "0" and kw["id_hipotese_legal"] == ""
    assert c.pesquisas == []
    assert r["nivel_acesso_descricao"] == "Público"


@pytest.mark.asyncio
async def test_trocar_so_a_hipotese_de_documento_ja_restrito(usar):
    c = usar(Cliente(nivel_atual="1"))
    await _alterar(hipotese_legal="controle interno")
    assert c.alteracoes[0][1]["id_hipotese_legal"] == "12"
    assert c.alteracoes[0][1]["nivel_acesso"] == "1"


@pytest.mark.asyncio
async def test_nivel_invalido(usar):
    usar(Cliente())
    r = await _alterar(nivel_acesso="secreto")
    assert "inválido" in r["error"]


@pytest.mark.asyncio
async def test_documento_externo_usa_o_mesmo_fluxo(usar):
    c = usar(Cliente())
    await srv.sei_alterar_documento_externo(
        id_documento="1", nivel_acesso="restrito", hipotese_legal="preparatorio", ctx=CTX)
    [(tipo, kw)] = c.alteracoes
    assert tipo == "X" and kw["id_hipotese_legal"] == "15"
