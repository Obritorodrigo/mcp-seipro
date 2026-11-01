"""Erro claro ao editar documento que está em bloco de assinatura disponibilizado.

Caso real: Ofício 64/2026/SEJUC/DEPLAF no bloco "SECRETÁRIO DAGOBERTO" (441032).
"""

import json
from types import SimpleNamespace

import pytest

import mcp_seipro.server as srv


class Cliente:
    async def listar_secao_documento(self, doc_id):
        raise Exception("Erro ao alterar seção do documento 27113336: Documento disponibilizado em bloco de assinatura.")

    async def listar_blocos_documento(self, doc_id):
        return {"idDocumento": doc_id, "blocos": ["441032"]}

    async def pesquisar_blocos_assinatura(self, filtro="", limit=50, start=0, estado="", id_bloco=""):
        assert id_bloco == "441032"
        return {"blocos": [{"id": "441032", "atributos": {
            "descricao": "SECRETÁRIO DAGOBERTO", "estado": "D", "siglaUnidade": "SEJUC/DEPLAF",
            "unidades": [{"unidade": "SEJUC/GAB"}, {"unidade": "SEJUC/GAB/ADJ"}], "numeroDocumentos": "1"}}]}


@pytest.fixture
def ambiente(monkeypatch):
    async def resolver(client, ref):
        return "27113336", "I"
    monkeypatch.setattr(srv, "_get_client", lambda ctx: Cliente())
    monkeypatch.setattr(srv, "_resolver_documento", resolver)


@pytest.mark.asyncio
async def test_editar_secao_explica_o_bloco(ambiente):
    r = json.loads(await srv.sei_editar_secao(
        id_documento="24846961", secoes=[{"idSecaoModelo": "3164", "conteudo": "<p>x</p>"}],
        ctx=SimpleNamespace()))
    assert r["erro_origem"] == "documento_em_bloco_de_assinatura"
    [b] = r["blocos"]
    assert b["id"] == "441032" and b["descricao"] == "SECRETÁRIO DAGOBERTO"
    assert b["estado_descricao"] == "Disponibilizado"
    assert b["disponibilizado_para"] == ["SEJUC/GAB", "SEJUC/GAB/ADJ"]
    assert "sei_cancelar_disponibilizacao_bloco" in r["como_editar"]
    assert "Confirme com o usuário" in r["como_editar"]


@pytest.mark.asyncio
async def test_outro_erro_continua_como_antes(monkeypatch):
    class Outro:
        async def listar_secao_documento(self, doc_id):
            raise Exception("Acesso negado")

    async def resolver(client, ref):
        return "1", "I"
    monkeypatch.setattr(srv, "_get_client", lambda ctx: Outro())
    monkeypatch.setattr(srv, "_resolver_documento", resolver)
    r = json.loads(await srv.sei_editar_secao(
        id_documento="1", secoes=[{"idSecaoModelo": "1", "conteudo": "x"}], ctx=SimpleNamespace()))
    assert r["error"] == "Acesso negado" and "blocos" not in r
