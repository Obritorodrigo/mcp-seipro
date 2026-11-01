"""Correções de consulta vistas ao vivo em sei.rr.gov.br (SEI 5.0.5 / wssei 3.0.4).

- listar_unidades devolvia as 6.232 unidades do sistema (faltava `usuario`);
- listar_unidades_processo devolvia só {"unidades": "21"} (a rota conta
  processos relacionados);
- verificar_acesso respondia "Processo não encontrado" para tudo (a rota do
  wssei não lê o protocolo da URL);
- listar_secoes devolvia 750 KB (brasão em base64 na seção dinâmica);
- pesquisar_tipos_documento("Oficio") não achava "Ofício";
- não havia tool para metadados de documento interno.
"""

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

import mcp_seipro.server as srv
from mcp_seipro.sei_client import SEIClient

CTX = SimpleNamespace()


def _cliente(rotas: dict, chamadas: list | None = None) -> SEIClient:
    """SEIClient com transporte falso: rotas = {(método, caminho): payload|callable}."""
    def responder(request: httpx.Request) -> httpx.Response:
        caminho = request.url.path.split("/api/v2", 1)[-1]
        if chamadas is not None:
            chamadas.append(request)
        alvo = rotas.get((request.method, caminho))
        if callable(alvo):
            alvo = alvo(request)
        if alvo is None:
            return httpx.Response(404, json={"sucesso": False, "mensagem": "rota"})
        return httpx.Response(200, json=alvo)

    c = SEIClient(sei_url="https://sei.exemplo.gov.br/api/v2", sei_usuario="u",
                  sei_senha="s", sei_orgao="16", permitir_arquivo_local=False)
    c._token = "t"
    c._id_usuario = "4242"
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(responder))
    return c


# --- listar_unidades ---------------------------------------------------------

@pytest.mark.asyncio
async def test_listar_unidades_envia_usuario_e_repoe_a_atual():
    chamadas = []
    c = _cliente({("GET", "/usuario/unidades"): {"sucesso": True, "data": [
        {"id": "110001336", "sigla": "SEJUC/DESIPE", "nome": "Desipe"},
    ]}}, chamadas)
    c._unidade_ativa = "110001335"
    c._cache_set("unidade:110001335", {"sigla": "SEJUC/DEPLAF", "nome": "Deplaf"})
    try:
        unidades = await c.listar_unidades_usuario()
    finally:
        await c.close()

    assert parse_qs(urlparse(str(chamadas[0].url)).query)["usuario"] == ["4242"]
    assert unidades[0] == {"id": "110001335", "sigla": "SEJUC/DEPLAF", "nome": "Deplaf", "atual": True}
    assert unidades[1]["atual"] is False


@pytest.mark.asyncio
async def test_trocar_unidade_invalida_cache_de_unidades():
    c = _cliente({("POST", "/usuario/alterar/unidade"): {"sucesso": True, "mensagem": "ok"}})
    c._cache_set("unidades_usuario", [{"id": "1"}])
    try:
        await c.trocar_unidade("2")
    finally:
        await c.close()
    assert c._cache_get("unidades_usuario") is None
    assert c._unidade_ativa == "2"


@pytest.mark.asyncio
async def test_tool_listar_unidades_filtra_sem_acento(monkeypatch):
    class C:
        async def listar_unidades_usuario(self):
            return [
                {"id": "1", "sigla": "SEJUC/DEPLAF", "nome": "Departamento de Planejamento", "atual": True},
                {"id": "2", "sigla": "SEJUC/DESIPE", "nome": "Departamento do Sistema Penitenciário"},
            ]
    monkeypatch.setattr(srv, "_get_client", lambda ctx: C())
    r = json.loads(await srv.sei_listar_unidades(filtro="penitenciario", ctx=CTX))
    assert [u["id"] for u in r["unidades"]] == ["2"]


# --- listar_unidades_processo -------------------------------------------------

@pytest.mark.asyncio
async def test_unidades_processo_vem_de_dados_abertura():
    c = _cliente({("GET", "/processo/27102756"): {"sucesso": True, "data": {"atributos": {
        "dadosAbertura": {
            "info": "Processo aberto nas unidades:",
            "unidades": [{"id": 110001335, "nome": "SEJUC/DEPLAF"},
                         {"id": 110005688, "nome": "SELC/SEC/COPLAC"}],
            "lista": [{"sigla": "SEJUC/DEPLAF (atribuído a Fulano)"},
                      {"sigla": "SELC/SEC/COPLAC"}],
        }}}}})
    try:
        r = await c.listar_unidades_processo("27102756")
    finally:
        await c.close()

    assert r["total"] == 2
    assert r["unidades"][0] == {"id": "110001335", "sigla": "SEJUC/DEPLAF", "atribuido_a": "Fulano"}
    assert r["unidades"][1] == {"id": "110005688", "sigla": "SELC/SEC/COPLAC"}


# --- verificar_acesso ---------------------------------------------------------

@pytest.mark.asyncio
async def test_verificar_acesso_contorna_rota_quebrada_do_wssei():
    c = _cliente({
        ("GET", "/processo/verifica/acesso/27102756"): {"sucesso": False, "mensagem": "Processo não encontrado!"},
        ("GET", "/processo/consultar/27102756"): {"sucesso": True, "data": {"nivelAcesso": "0"}},
    })
    try:
        r = await c.verificar_acesso("27102756")
    finally:
        await c.close()
    assert r["acesso"] is True
    assert r["nivelAcesso"] == "0"


@pytest.mark.asyncio
async def test_verificar_acesso_negado_no_contorno():
    c = _cliente({
        ("GET", "/processo/verifica/acesso/9"): {"sucesso": False, "mensagem": "Processo não encontrado!"},
        ("GET", "/processo/consultar/9"): {"sucesso": False, "mensagem": "Acesso negado"},
    })
    try:
        r = await c.verificar_acesso("9")
    finally:
        await c.close()
    assert r["acesso"] is False


@pytest.mark.asyncio
async def test_verificar_acesso_outro_erro_sobe():
    c = _cliente({("GET", "/processo/verifica/acesso/9"): {"sucesso": False, "mensagem": "Falha interna"}})
    try:
        with pytest.raises(Exception, match="Falha interna"):
            await c.verificar_acesso("9")
    finally:
        await c.close()


# --- número de processo -------------------------------------------------------

def test_variantes_protocolo():
    assert srv._variantes_protocolo("35101.001300/2026-08") == [
        "35101.001300/2026-08", "35101.001300/2026.08"]
    assert srv._variantes_protocolo("35101.001300/2026.08") == [
        "35101.001300/2026.08", "35101.001300/2026-08"]


@pytest.mark.asyncio
async def test_resolver_processo_tenta_o_outro_separador():
    class C:
        def __init__(self):
            self.tentativas = []

        async def consultar_processo(self, numero):
            self.tentativas.append(numero)
            if numero.endswith(".08"):
                return {"IdProcedimento": "27102756"}
            raise Exception("Processo não encontrado")

    c = C()
    assert await srv._resolver_processo(c, "35101.001300/2026-08") == "27102756"
    assert c.tentativas == ["35101.001300/2026-08", "35101.001300/2026.08"]


# --- listar_secoes ------------------------------------------------------------

@pytest.mark.asyncio
async def test_listar_secoes_omite_conteudo_dinamico(monkeypatch):
    brasao = "<img src='data:image/png;base64," + "A" * 750_000 + "'>"

    class C:
        async def listar_secao_documento(self, doc_id):
            return {"ultimaVersaoDocumento": 2, "secoes": [
                {"idSecaoModelo": "461", "DinamicaSecaoDocumento": "S", "conteudo": brasao},
                {"idSecaoModelo": "468", "DinamicaSecaoDocumento": "N", "conteudo": "<p>corpo</p>"},
            ]}

    async def resolver(client, ref):
        return ref, "I"

    async def identidade(client, doc_id):
        return {"id_documento": doc_id}

    monkeypatch.setattr(srv, "_get_client", lambda ctx: C())
    monkeypatch.setattr(srv, "_resolver_documento", resolver)
    monkeypatch.setattr(srv, "_identidade_documento", identidade)

    r = await srv.sei_listar_secoes(id_documento="1", ctx=CTX)
    assert len(r) < 5_000
    secoes = json.loads(r)["secoes"]
    assert secoes[0]["conteudo"] == "" and "750" in secoes[0]["conteudo_omitido"]
    assert secoes[1]["conteudo"] == "<p>corpo</p>"

    completo = json.loads(await srv.sei_listar_secoes(id_documento="1", incluir_dinamicas=True, ctx=CTX))
    assert completo["secoes"][0]["conteudo"] == brasao


# --- pesquisar_tipos_documento -----------------------------------------------

@pytest.mark.asyncio
async def test_tipos_documento_sem_acento(monkeypatch):
    todos = [{"id": "290", "nome": "Ofício"}, {"id": "292", "nome": "Ofício-Circular"},
             {"id": "348", "nome": "Relatório"}]

    class C:
        _cache = {}

        def _cache_get(self, k):
            return self._cache.get(k)

        def _cache_set(self, k, v):
            self._cache[k] = v

        async def pesquisar_tipos_documento(self, filtro="", favoritos="", aplicabilidade="", limit=50, start=0):
            if filtro:  # busca nativa: substring exata, sensível a acento
                return {"tipos": [t for t in todos if filtro in t["nome"]]}
            return {"tipos": todos[start * limit:(start + 1) * limit]}

    monkeypatch.setattr(srv, "_get_client", lambda ctx: C())
    r = json.loads(await srv.sei_pesquisar_tipos_documento(filtro="oficio circular", ctx=CTX))
    assert [t["id"] for t in r["tipos"]] == ["292"]
    r = json.loads(await srv.sei_pesquisar_tipos_documento(filtro="Oficio", ctx=CTX))
    assert {t["id"] for t in r["tipos"]} == {"290", "292"}


# --- consultar_documento_interno ----------------------------------------------

@pytest.mark.asyncio
async def test_consultar_documento_interno(monkeypatch):
    class C:
        async def consultar_documento_interno(self, doc_id):
            return {
                "idDocumento": "27113158", "nomeDocumento": "Despacho 1219 (24846801)",
                "protocolo": "27102756", "idSerie": "291", "nomeSerie": "Despacho",
                "descricao": "Consolidação", "nivelAcesso": "1", "idHipoteseLegal": "34",
                "nomeHipoteseLegal": "Informação Pessoal", "dataElaboracao": "31/10/2026",
                "observacao": "Obs", "assuntos": [{"id": "1243", "codigoestruturadoformatado": "00.01",
                                                    "descricao": "Informes"}],
                "interessados": [{"id": "501", "nomeformatado": "Fulano (FUL)"}],
                "destinatarios": [],
            }

    async def resolver(client, ref):
        return "27113158", "I"

    monkeypatch.setattr(srv, "_get_client", lambda ctx: C())
    monkeypatch.setattr(srv, "_resolver_documento", resolver)
    r = json.loads(await srv.sei_consultar_documento_interno(id_documento="24846801", ctx=CTX))
    assert r["tipo"] == "Despacho"
    assert r["assuntos"] == [{"id": "1243", "nome": "Informes", "codigo": "00.01"}]
    assert r["interessados"][0]["nome"] == "Fulano (FUL)"
    assert r["nivel_acesso_descricao"] == "Restrito"
    assert "_aviso_acesso" in r


# --- correções após o teste ao vivo -------------------------------------------

@pytest.mark.asyncio
async def test_tipos_documento_converte_pagina_em_posicao():
    # /documento/tipo/pesquisar: `start` é posição 1-based (array_slice($a, $start-1)).
    chamadas = []
    c = _cliente({("GET", "/documento/tipo/pesquisar"): {"sucesso": True, "data": [], "total": 0}}, chamadas)
    try:
        await c.pesquisar_tipos_documento(limit=50, start=0)
        await c.pesquisar_tipos_documento(limit=50, start=2)
    finally:
        await c.close()
    starts = [parse_qs(urlparse(str(r.url)).query)["start"][0] for r in chamadas]
    assert starts == ["1", "101"]


@pytest.mark.asyncio
async def test_tipos_documento_sem_repeticao_quando_paginas_se_sobrepoem(monkeypatch):
    # Reproduz o visto ao vivo: páginas sobrepostas repetiam "Ofício-Circular" 5x.
    todos = [{"id": str(i), "nome": f"Tipo {i}"} for i in range(600)] + [
        {"id": "292", "nome": "Ofício-Circular"}, {"id": "1142", "nome": "Ofício-Circular Conjunto"}]

    class C:
        _cache = {}

        def _cache_get(self, k):
            return self._cache.get(k)

        def _cache_set(self, k, v):
            self._cache[k] = v

        async def pesquisar_tipos_documento(self, filtro="", favoritos="", aplicabilidade="", limit=50, start=0):
            if filtro:
                return {"tipos": [t for t in todos if filtro in t["nome"]]}
            pagina = todos[start * limit:(start + 1) * limit]
            # o mesmo tipo aparecendo em mais de uma página (visto ao vivo)
            return {"tipos": pagina + [{"id": "292", "nome": "Ofício-Circular"}]}

    monkeypatch.setattr(srv, "_get_client", lambda ctx: C())
    r = json.loads(await srv.sei_pesquisar_tipos_documento(filtro="oficio circular", ctx=CTX))
    assert [t["id"] for t in r["tipos"]] == ["292", "1142"]


@pytest.mark.asyncio
async def test_unidade_atual_ganha_sigla_e_nome_da_lista_geral(monkeypatch):
    import mcp_seipro.sei_client as mod
    monkeypatch.setattr(mod, "_UNIDADES_SISTEMA", {})

    def unidades(request):
        q = parse_qs(urlparse(str(request.url)).query)
        if "usuario" in q:  # lista do usuário: sem a unidade atual
            return {"sucesso": True, "data": [{"id": "110001335", "sigla": "SEJUC/DEPLAF", "nome": "Deplaf"}]}
        return {"sucesso": True, "data": [  # lista geral do órgão
            {"id": "110005930", "sigla": "SEJUC/DEPLAF/X", "nome": "Unidade X"},
            {"id": "110001335", "sigla": "SEJUC/DEPLAF", "nome": "Deplaf"}]}

    c = _cliente({("GET", "/usuario/unidades"): unidades})
    c._unidade_ativa = "110005930"
    try:
        lista = await c.listar_unidades_usuario()
    finally:
        await c.close()
    assert lista[0] == {"id": "110005930", "sigla": "SEJUC/DEPLAF/X", "nome": "Unidade X", "atual": True}
