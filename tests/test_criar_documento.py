"""sei_criar_documento: tipo pelo nome e criação já restrita."""

import json
from types import SimpleNamespace

import pytest

import mcp_seipro.server as srv

CTX = SimpleNamespace()
HIPOTESES = [{"id": "1", "nome": "Informação Pessoal", "baselegal": "Art. 31 da Lei nº 12.527/2011"},
             {"id": "17", "nome": "Sigilo de Informação.", "baselegal": "Art. 24 do Decreto Nº 20.477/2016"}]


class Cliente:
    def __init__(self):
        self.criados, self.alterados = [], []
        self._cache = {}

    def _cache_get(self, k):
        return self._cache.get(k)

    def _cache_set(self, k, v):
        self._cache[k] = v

    async def pesquisar_tipos_documento(self, filtro="", favoritos="", aplicabilidade="", limit=50, start=0):
        tipos = [{"id": "290", "nome": "Ofício"}, {"id": "292", "nome": "Ofício-Circular"},
                 {"id": "291", "nome": "Despacho"}]
        if filtro:
            return {"tipos": [t for t in tipos if filtro in t["nome"]]}
        return {"tipos": tipos[start * limit:(start + 1) * limit]}

    async def pesquisar_hipoteses_legais(self, filtro="", limit=50, start=0, nivel_acesso=""):
        return {"hipoteses": HIPOTESES}

    async def criar_documento_interno(self, **kw):
        self.criados.append(kw)
        return {"idDocumento": "27113999", "protocoloDocumentoFormatado": "24847999"}

    async def alterar_documento_interno(self, **kw):
        self.alterados.append(kw)
        return {}


@pytest.fixture
def cliente(monkeypatch):
    c = Cliente()

    async def resolver_proc(client, ref):
        return "27102756"
    monkeypatch.setattr(srv, "_get_client", lambda ctx: c)
    monkeypatch.setattr(srv, "_resolver_processo", resolver_proc)
    return c


async def _criar(**kw):
    return json.loads(await srv.sei_criar_documento(processo="35101.001300/2026.08", ctx=CTX, **kw))


@pytest.mark.asyncio
async def test_tipo_pelo_nome(cliente):
    r = await _criar(id_serie="oficio")
    assert cliente.criados[0]["id_serie"] == "290"
    assert r["tipo"] == "Ofício" and r["nivel_acesso_descricao"] == "Público"
    assert cliente.alterados == []


@pytest.mark.asyncio
async def test_restrito_sem_hipotese_nao_cria(cliente):
    r = await _criar(id_serie="291", nivel_acesso="restrito")
    assert "hipótese legal" in r["error"]
    assert cliente.criados == [] and cliente.alterados == []


@pytest.mark.asyncio
async def test_restrito_com_hipotese_cria_e_restringe(cliente):
    r = await _criar(id_serie="despacho", nivel_acesso="restrito", hipotese_legal="informacao pessoal")
    assert cliente.criados[0]["nivel_acesso"] == "0"  # a criação não aceita hipótese
    [alt] = cliente.alterados
    assert alt == {"id_documento": "27113999", "nivel_acesso": "1", "id_hipotese_legal": "1"}
    assert r["nivel_acesso_descricao"] == "Restrito"


@pytest.mark.asyncio
async def test_falha_ao_restringir_avisa(cliente):
    async def falha(**kw):
        raise Exception("Erro do SEI")
    cliente.alterar_documento_interno = falha
    r = await _criar(id_serie="291", nivel_acesso="1", hipotese_legal="1")
    assert "criado PÚBLICO" in r["_aviso"]
