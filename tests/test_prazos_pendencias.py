"""sei_listar_prazos e sei_listar_pendencias (somente leitura)."""

import json
from datetime import date
from types import SimpleNamespace

import pytest

import mcp_seipro.server as srv

CTX = SimpleNamespace()
HOJE = date(2026, 11, 1)


def processo(id_proc, numero, *, retorno=None, marcador=None, lido="S", doc_novo="N", atribuido=None):
    att = {
        "idProcedimento": id_proc, "numero": numero, "tipoProcesso": "Expedientes", "descricao": "Desc",
        "status": {"retornoData": retorno or [], "processoAcessadoUnidade": lido, "documentoNovo": doc_novo},
        "marcador": marcador or [],
    }
    if atribuido:
        att["usuarioAtribuido"] = {"idUsuario": atribuido, "nome": "Rodrigo de Brito Silva"}
        att["dadosAbertura"] = {"unidades": [{"id": "110001335", "nome": "SEJUC/DEPLAF"}],
                                "lista": [{"sigla": "SEJUC/DEPLAF"}]}
    return {"id": id_proc, "atributos": att}


PROCESSOS = [
    processo("1", "35101.001300/2026.08", retorno={"dataProgramada": "04/11/2026", "unidade": "SELC/SEC/COPLAC"}),
    processo("2", "26101.018680/2026.11", marcador=[{"nome": "Prazo PGE", "texto": "responder até 30/10/2026"}],
             lido="N"),
    processo("3", "11111.000001/2026.01", retorno={"dataProgramada": "30/12/2026", "unidade": "X"}, doc_novo="S"),
    processo("4", "22222.000002/2026.02", atribuido="99"),
]


class Cliente:
    _unidade_ativa = "110001335"
    _id_usuario = "99"
    _nome_usuario = "Rodrigo de Brito Silva"
    _usuario = "10924687738"

    async def listar_processos(self, limit=50, start=0, **kw):
        return {"processos": PROCESSOS[start * limit:(start + 1) * limit]}

    async def pesquisar_blocos_assinatura(self, **kw):
        return {"blocos": [
            {"id": "441032", "atributos": {"descricao": "SECRETÁRIO DAGOBERTO", "estado": "D",
                                           "siglaUnidade": "SEJUC/DEPLAF", "unidades": [{"unidade": "SEJUC/GAB"}],
                                           "numeroDocumentos": "1"}},
            {"id": "10", "atributos": {"descricao": "vazio", "estado": "A", "siglaUnidade": "SEJUC/DEPLAF",
                                       "unidades": [], "numeroDocumentos": "0"}},
            {"id": "20", "atributos": {"descricao": "Para o Diretor assinar", "estado": "D",
                                       "siglaUnidade": "SEJUC/GAB", "unidades": [], "numeroDocumentos": "2"}},
        ]}

    async def listar_unidades_usuario(self):
        return [{"id": "110001335", "sigla": "SEJUC/DEPLAF", "atual": True}]

    async def listar_documentos_todos(self, id_proc):
        if id_proc != "1":
            return {"documentos": []}
        return {"documentos": [
            {"atributos": {"tipoDocumento": "I", "idUnidade": "110001335", "nomeComposto": "Ofício 64 (24846961)",
                           "protocoloFormatado": "24846961", "informacao": "Resposta à SELC",
                           "status": {"documentoAssinado": "N", "documentoCancelado": "N"}}},
            {"atributos": {"tipoDocumento": "I", "idUnidade": "110001335", "nomeComposto": "Despacho 1219",
                           "status": {"documentoAssinado": "S"}}},
            {"atributos": {"tipoDocumento": "I", "idUnidade": "999", "nomeComposto": "De outra unidade",
                           "status": {"documentoAssinado": "N"}}},
        ]}


@pytest.fixture(autouse=True)
def ambiente(monkeypatch):
    monkeypatch.setattr(srv, "_get_client", lambda ctx: Cliente())
    monkeypatch.setattr(srv, "_hoje", lambda: HOJE)


@pytest.mark.asyncio
async def test_prazos_ordenados_com_vencidos():
    r = json.loads(await srv.sei_listar_prazos(dias=15, ctx=CTX))
    assert [p["protocolo"] for p in r["prazos"]] == ["26101.018680/2026.11", "35101.001300/2026.08"]
    vencido, proximo = r["prazos"]
    assert vencido["situacao"] == "vencido há 2 dia(s)" and vencido["origem"].startswith("marcador")
    assert proximo["situacao"] == "vence em 3 dia(s)" and "SELC/SEC/COPLAC" in proximo["origem"]
    assert r["vencidos"] == 1


@pytest.mark.asyncio
async def test_prazos_sem_vencidos_e_horizonte():
    r = json.loads(await srv.sei_listar_prazos(dias=60, incluir_vencidos=False, ctx=CTX))
    assert [p["protocolo"] for p in r["prazos"]] == ["35101.001300/2026.08", "11111.000001/2026.01"]


@pytest.mark.asyncio
async def test_pendencias_reune_tudo():
    r = json.loads(await srv.sei_listar_pendencias(dias_prazo=7, ctx=CTX))
    assert r["resumo"] == {
        "prazos_ate_7_dias": 2, "nao_lidos": 1, "com_documento_novo": 1,
        "atribuidos_a_mim": 1, "blocos_de_assinatura": 2, "documentos_sem_assinatura": 1,
    }
    assert r["documentos_sem_assinatura"][0]["numero_sei"] == "24846961"
    origens = {b["id"]: b["origem"] for b in r["blocos_de_assinatura"]}
    assert origens == {"441032": "da unidade", "20": "recebido de outra unidade"}
    assert r["atribuidos_a_mim"][0]["protocolo"] == "22222.000002/2026.02"


@pytest.mark.asyncio
async def test_pendencias_sem_conferir_documentos():
    r = json.loads(await srv.sei_listar_pendencias(verificar_documentos=0, ctx=CTX))
    assert r["documentos_sem_assinatura"] == []


def test_ferramentas_sao_de_leitura():
    tools = srv.mcp._tool_manager._tools
    for nome in ("sei_listar_prazos", "sei_listar_pendencias"):
        assert tools[nome].annotations.readOnlyHint is True
        assert not tools[nome].fn._auditada
