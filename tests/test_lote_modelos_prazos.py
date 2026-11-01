"""Assinatura em lote, modelos de criação, prazos no texto, marcar prazo e idade das minutas."""

import json
from datetime import date
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

import mcp_seipro.server as srv
from mcp_seipro.sei_client import SEIClient

CTX = SimpleNamespace()
HOJE = date(2026, 11, 1)


@pytest.fixture(autouse=True)
def hoje_fixo(monkeypatch):
    monkeypatch.setattr(srv, "_hoje", lambda: HOJE)


# --- prazos no texto ------------------------------------------------------------

def test_prazos_no_texto_formatos():
    texto = ("Solicita-se resposta até 04/11/2026, impreterivelmente. "
             "O relatório deve ser entregue até 15 de dezembro de 2026. "
             "Manifestação no prazo de 5 (cinco) dias úteis. Valor de 10/10 unidades.")
    achados = srv._prazos_no_texto(texto, data_documento=date(2026, 10, 30))
    datas = {a["data"]: a["tipo"] for a in achados}
    assert datas[date(2026, 11, 4)] == "data"
    assert datas[date(2026, 12, 15)] == "data por extenso"
    # 30/10 (sex) + 5 dias úteis = 06/11 (sex)
    assert datas[date(2026, 11, 6)].startswith("5 dias úteis")
    assert all("trecho" in a for a in achados)


def test_data_sem_palavra_de_prazo_nao_conta():
    assert srv._prazos_no_texto("Ofício de 31/10/2026 recebido em 01/11/2026.") == []


@pytest.mark.asyncio
async def test_buscar_prazos_no_texto(monkeypatch):
    class C:
        async def listar_documentos_todos(self, id_proc):
            return {"documentos": [
                {"id": "27102769", "atributos": {"tipoDocumento": "I", "protocoloFormatado": "24837023",
                                                 "nomeComposto": "Ofício-Circular 24 (24837023)", "status": {}}},
                {"id": "x", "atributos": {"tipoDocumento": "X", "status": {}}},
            ]}

        async def visualizar_documento_interno(self, doc_id):
            return "<p>Solicitamos o envio das informações até 04/11/2026.</p>"

        async def consultar_documento_interno(self, doc_id):
            return {"dataElaboracao": "28/10/2026"}

    async def resolver(client, ref):
        return "27102756"

    monkeypatch.setattr(srv, "_get_client", lambda ctx: C())
    monkeypatch.setattr(srv, "_resolver_processo", resolver)
    r = json.loads(await srv.sei_buscar_prazos_no_texto(processo="35101.001300/2026.08", ctx=CTX))
    [p] = r["prazos_encontrados"]
    assert p["prazo"] == "04/11/2026" and p["situacao"] == "vence em 3 dia(s)"
    assert p["numero_sei"] == "24837023" and "até 04/11/2026" in p["trecho"]


# --- marcar prazo ---------------------------------------------------------------

class ClienteMarcador:
    def __init__(self, marcadores):
        self.marcadores = marcadores
        self.marcados = []

    async def pesquisar_marcadores(self, limit=50, **kw):
        return {"marcadores": self.marcadores}

    async def marcar_processo(self, id_proc, id_marcador, texto):
        self.marcados.append((id_proc, id_marcador, texto))
        return {"mensagem": "ok"}


@pytest.fixture
def resolver_processo(monkeypatch):
    async def resolver(client, ref):
        return "27102756"
    monkeypatch.setattr(srv, "_resolver_processo", resolver)


@pytest.mark.asyncio
async def test_marcar_prazo_com_marcador_existente(monkeypatch, resolver_processo):
    c = ClienteMarcador([{"id": "55", "nome": "PRAZO"}, {"id": "56", "nome": "Urgente"}])
    monkeypatch.setattr(srv, "_get_client", lambda ctx: c)
    r = json.loads(await srv.sei_marcar_prazo(processo="p", data="04/11/2026",
                                              descricao="resposta à SELC", ctx=CTX))
    assert c.marcados == [("27102756", "55", "Prazo: 04/11/2026 – resposta à SELC")]
    assert r["situacao"] == "vence em 3 dia(s)"
    # o texto gravado é lido pelas pendências como prazo
    bruto = {"atributos": {"status": {}, "marcador": [{"nome": "PRAZO", "texto": r["texto"]}]}}
    assert srv._prazo_do_processo(bruto, HOJE)["prazo"] == "04/11/2026"


@pytest.mark.asyncio
async def test_sem_marcador_nao_cria_nada(monkeypatch, resolver_processo):
    c = ClienteMarcador([{"id": "56", "nome": "Urgente"}])
    monkeypatch.setattr(srv, "_get_client", lambda ctx: c)
    r = json.loads(await srv.sei_marcar_prazo(processo="p", data="04/11/2026", ctx=CTX))
    assert "Não há marcador" in r["error"] and c.marcados == []
    assert r["marcadores_disponiveis"] == [{"id": "56", "nome": "Urgente"}]


@pytest.mark.asyncio
async def test_data_invalida(monkeypatch, resolver_processo):
    monkeypatch.setattr(srv, "_get_client", lambda ctx: ClienteMarcador([]))
    r = json.loads(await srv.sei_marcar_prazo(processo="p", data="2026-11-04", ctx=CTX))
    assert "dd/mm/aaaa" in r["error"]


# --- assinatura em lote -----------------------------------------------------------

@pytest.mark.asyncio
async def test_cliente_assina_em_lote_numa_chamada():
    enviados = []

    def responder(req):
        enviados.append(req)
        return httpx.Response(200, json={"sucesso": True, "mensagem": "Documentos assinados."})

    c = SEIClient(sei_url="https://sei.exemplo.gov.br/api/v2", sei_usuario="u", sei_senha="s",
                  sei_orgao="16", permitir_arquivo_local=False)
    c._token = "t"
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(responder))
    try:
        await c.assinar_documentos(["1", "2"], login="u", senha="s", cargo="Diretor", id_usuario="99")
    finally:
        await c.close()
    req = enviados[0]
    assert req.url.path.endswith("/documento/assinar/bloco")
    campos = {k: v[0] for k, v in parse_qs(req.content.decode("ascii")).items()}
    assert campos["arrDocumento"] == "1,2" and campos["usuario"] == "99"


@pytest.mark.asyncio
async def test_tool_assinar_documentos(monkeypatch):
    class C:
        _usuario, _senha, _orgao = "u", "s", "16"
        _id_usuario, _id_orgao_usuario = "99", "16"

        def __init__(self):
            self.lote = None

        async def _get_headers(self):
            return {}

        async def assinar_documentos(self, ids, **kw):
            self.lote = (ids, kw)
            return {"mensagem": "ok"}

        async def listar_assinaturas(self, doc_id):
            return [{"cargo": "Diretor do Departamento"}] if doc_id == "A" else []

    c = C()

    async def resolver(client, ref):
        return {"19136613": "A", "20453438": "B"}[ref], "I"

    async def ativar(client, doc_id):
        return "110001335"

    async def cargo(client, c_):
        return "Diretor do Departamento", None

    monkeypatch.setattr(srv, "_get_client", lambda ctx: c)
    monkeypatch.setattr(srv, "_resolver_documento", resolver)
    monkeypatch.setattr(srv, "_ativar_unidade_geradora", ativar)
    monkeypatch.setattr(srv, "_resolver_cargo", cargo)
    r = json.loads(await srv.sei_assinar_documentos(documentos="19136613, 20453438", cargo="7376", ctx=CTX))
    assert c.lote[0] == ["A", "B"]
    assert [d["assinado"] for d in r["documentos"]] == [True, False]


# --- modelos ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_modelo_processo_por_nome(monkeypatch):
    class C:
        async def pesquisar_tipos_processo(self, limit=50, **kw):
            return {"tipos": [{"id": "23", "nome": "Aquisição de Material Permanente"},
                              {"id": "24", "nome": "Aquisição de Material de Consumo"}]}

        async def modelo_processo(self, id_tipo):
            return {"assuntos": [{"id": "447"}], "nivelAcessoPermitido": {"publico": True}}

    monkeypatch.setattr(srv, "_get_client", lambda ctx: C())
    r = json.loads(await srv.sei_consultar_modelo_processo(tipo="material permanente", ctx=CTX))
    assert r["id_tipo_processo"] == "23" and r["modelo"]["assuntos"][0]["id"] == "447"
    r = json.loads(await srv.sei_consultar_modelo_processo(tipo="aquisicao", ctx=CTX))
    assert "ambíguo" in r["error"]


# --- idade das minutas --------------------------------------------------------------

@pytest.mark.asyncio
async def test_minutas_com_idade_mais_antigas_primeiro():
    class C:
        async def listar_documentos_todos(self, id_proc):
            return {"documentos": [
                {"id": "a", "atributos": {"tipoDocumento": "I", "idUnidade": "U", "nomeComposto": "Ofício 64",
                                          "status": {"documentoAssinado": "N"}}},
                {"id": "b", "atributos": {"tipoDocumento": "I", "idUnidade": "U", "nomeComposto": "Ofício 81",
                                          "status": {"documentoAssinado": "N"}}},
            ]}

        async def consultar_documento_interno(self, doc_id):
            return {"dataElaboracao": {"a": "31/10/2026", "b": "12/03/2024"}[doc_id]}

    itens = await srv._documentos_sem_assinatura(C(), [{"id": "1", "atributos": {"idProcedimento": "1"}}], "U", 5)
    assert [i["documento"] for i in itens] == ["Ofício 81", "Ofício 64"]
    assert itens[0]["dias_sem_assinatura"] == (HOJE - date(2024, 3, 12)).days
    assert itens[1]["criado_em"] == "31/10/2026" and "_id" not in itens[0]
