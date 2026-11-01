"""sei_converter_documento: Despacho → Ofício com os modelos reais de sei.rr.gov.br.

Reproduz a conversão feita à mão em 01/11/2026 (Despacho 1220 → Ofício 63).
"""

import html
import json
from types import SimpleNamespace

import pytest

import mcp_seipro.server as srv

CTX = SimpleNamespace()
E = html.escape  # o SEI devolve o HTML das seções escapado


def sec(ident, conteudo, principal="N", dinamica="N", leitura="N"):
    return {"idSecaoModelo": ident, "conteudo": E(conteudo), "PrincipalSecaoDocumento": principal,
            "DinamicaSecaoDocumento": dinamica, "somenteLeitura": leitura}


DESPACHO = {"secoes": [
    sec("461", "<img src='brasao'>", dinamica="S", leitura="S"),
    sec("462", "<p>Despacho 1220/2026/SEJUC/DEPLAF</p>", leitura="S"),
    sec("463", "<p>Boa Vista/RR, 31 de outubro de 2026.</p>", dinamica="S", leitura="S"),
    sec("464", '<p class="Texto_Alinhado_Esquerda">Ao Senhor,</p><p><strong>CHARLES DE OLIVEIRA PARENTE</strong></p>'
               "<p>Secretário de Estado de Licitação e Contratação</p><p>SELC/RR</p>"),
    sec("465", "<p><strong>Assunto: Resposta ao Ofício-Circular nº 24/2026</strong></p>"),
    sec("467", "", leitura="S"),
    sec("468", '<p class="Texto_Justificado_Recuo_Primeira_Linha"> </p>'
               '<p class="Texto_Justificado_Recuo_Primeira_Linha">Senhor Secretário,</p>'
               '<p class="Texto_Justificado_Recuo_Primeira_Linha">Em atenção ao Ofício-Circular...</p>',
        principal="S"),
    sec("470", "<hr/><table>rodapé</table>", dinamica="S", leitura="S"),
]}

OFICIO = {"secoes": [
    sec("3157", "<img src='brasao'>", dinamica="S", leitura="S"),
    sec("3158", "<p><strong>ofício Nº 63/2026/SEJUC/DEPLAF</strong></p>", leitura="S"),
    sec("3159", "<p>Boa Vista - RR, 31 de outubro de 2026.</p>", dinamica="S", leitura="S"),
    sec("3160", "<p>@tratamento_destinatario@</p><p><strong>@nome_destinatario_maiusculas@</strong></p>"
                "<p>@cargo_destinatario@</p>"),
    sec("3161", "<p><br /><strong>Assunto: Resposta Ã  SELC</strong></p>"),
    sec("3162", '<p class="Texto_Justificado_Recuo_Primeira_Linha"><br />@vocativo_destinatario@,</p>'),
    sec("3163", "", leitura="S"),
    sec("3164", '<p class="Texto_Alinhado_Esquerda">Digite aqui o conteúdo do ofício ....</p>', principal="S"),
    sec("3167", "<p>Av. Getúlio Vargas 8120 - São Vicente - Boa Vista/RR</p>"),
    sec("3166", "<hr/><table>rodapé</table>", dinamica="S", leitura="S"),
]}

META_ORIGEM = {
    "idDocumento": "27113292", "nomeDocumento": "Despacho 1220 (24846922)", "protocolo": "27102756",
    "descricao": "Resposta à SELC - ARPs nº 074/2025 e 075/2025", "nivelAcesso": "1",
    "idHipoteseLegal": "1", "assuntos": [{"id": "1243"}], "interessados": [], "destinatarios": [],
}


class Cliente:
    def __init__(self):
        self.criados, self.metadados = [], []
        self._cache = {}

    def _cache_get(self, k):
        return self._cache.get(k)

    def _cache_set(self, k, v):
        self._cache[k] = v

    async def consultar_documento_interno(self, doc_id):
        return META_ORIGEM

    async def pesquisar_tipos_documento(self, filtro="", favoritos="", aplicabilidade="", limit=50, start=0):
        tipos = [{"id": "290", "nome": "Ofício"}, {"id": "292", "nome": "Ofício-Circular"},
                 {"id": "291", "nome": "Despacho"}]
        if filtro:
            return {"tipos": [t for t in tipos if filtro in t["nome"]]}
        return {"tipos": tipos[start * limit:(start + 1) * limit]}

    async def listar_secao_documento(self, doc_id):
        return DESPACHO if doc_id == "27113292" else OFICIO

    async def criar_documento_interno(self, **kw):
        self.criados.append(kw)
        return {"idDocumento": "27113319", "protocoloDocumentoFormatado": "24846946"}

    async def copiar_metadados_documento_interno(self, origem, id_destino, descricao=""):
        self.metadados.append((origem, id_destino, descricao))
        return {}


@pytest.fixture
def ambiente(monkeypatch):
    c = Cliente()
    enviados = []

    async def resolver(client, ref):
        return "27113292", "I"

    async def ativar(client, doc_id):
        return None

    async def editar(id_documento, secoes, ctx=None, **kw):
        enviados.append((id_documento, secoes))
        return json.dumps({"resultado": []})

    monkeypatch.setattr(srv, "_get_client", lambda ctx: c)
    monkeypatch.setattr(srv, "_resolver_documento", resolver)
    monkeypatch.setattr(srv, "_ativar_unidade_geradora", ativar)
    monkeypatch.setattr(srv, "sei_editar_secao", editar)
    return c, enviados


@pytest.mark.asyncio
async def test_despacho_para_oficio(ambiente):
    c, enviados = ambiente
    r = json.loads(await srv.sei_converter_documento(
        id_documento="24846922", tipo_destino="oficio", ctx=CTX))

    assert c.criados[0]["id_serie"] == "290"  # "oficio" casa exato com "Ofício", não "Ofício-Circular"
    assert c.criados[0]["id_procedimento"] == "27102756"
    assert c.criados[0]["descricao"] == META_ORIGEM["descricao"]
    assert r["novo_documento"] == {"id": "27113319", "numero_sei": "24846946", "tipo": "Ofício"}

    [(destino, secoes)] = enviados
    assert destino == "27113319"
    por_id = {s["idSecaoModelo"]: s["conteudo"] for s in secoes}
    assert "CHARLES DE OLIVEIRA PARENTE" in por_id["3160"]          # destinatário
    assert "Resposta ao Ofício-Circular" in por_id["3161"]           # assunto
    assert "Senhor Secretário," in por_id["3162"]                    # vocativo separado do corpo
    assert "Em atenção ao Ofício-Circular" in por_id["3164"]         # corpo
    assert "Senhor Secretário," not in por_id["3164"]
    assert "3167" not in por_id                                      # endereço do modelo mantido
    assert not any(k in por_id for k in ("3157", "3158", "3159", "3166"))  # dinâmicas/leitura
    assert set(r["secoes_copiadas"]) == {"destinatario", "assunto", "vocativo", "principal"}

    # metadados (nível restrito, hipótese, assuntos) copiados da origem
    [(origem, id_destino, descricao)] = c.metadados
    assert origem is META_ORIGEM and id_destino == "27113319"
    assert r["metadados_copiados"] is True
    assert "não foi excluído" in r["_aviso"]


@pytest.mark.asyncio
async def test_marcadores_sem_conteudo_sao_limpos(ambiente, monkeypatch):
    c, enviados = ambiente
    so_corpo = {"secoes": [sec("468", "<p>Texto do despacho sem vocativo.</p>", principal="S")]}

    async def listar(doc_id):
        return so_corpo if doc_id == "27113292" else OFICIO

    monkeypatch.setattr(c, "listar_secao_documento", listar)
    r = json.loads(await srv.sei_converter_documento(id_documento="1", tipo_destino="290", ctx=CTX))
    [(_, secoes)] = enviados
    por_id = {s["idSecaoModelo"]: s["conteudo"] for s in secoes}
    assert "@" not in por_id["3160"] and "@" not in por_id["3162"]   # marcadores limpos
    assert "Texto do despacho" in por_id["3164"]
    assert set(r["marcadores_limpos"]) == {"3160", "3162"}


@pytest.mark.asyncio
async def test_tipo_por_parte_unica_do_nome(ambiente):
    c, _ = ambiente
    await srv.sei_converter_documento(id_documento="1", tipo_destino="circular", ctx=CTX)
    assert c.criados[0]["id_serie"] == "292"  # só "Ofício-Circular" contém "circular"


@pytest.mark.asyncio
async def test_tipo_ambiguo_nao_cria_nada(ambiente):
    c, enviados = ambiente
    r = json.loads(await srv.sei_converter_documento(id_documento="1", tipo_destino="ofi", ctx=CTX))
    assert "ambíguo" in r["error"]  # Ofício e Ofício-Circular
    assert c.criados == [] and enviados == []


def test_papeis_das_secoes():
    papel = lambda conteudo, principal="N": srv._papel_secao(  # noqa: E731
        {"PrincipalSecaoDocumento": principal}, conteudo)
    assert papel("<p>@tratamento_destinatario@</p>") == "destinatario"
    assert papel("<p>À Senhora</p><p>FULANA</p>") == "destinatario"
    assert papel("<p><strong>Assunto: x</strong></p>") == "assunto"
    assert papel("<p>@vocativo_destinatario@,</p>") == "vocativo"
    assert papel("<p>Senhora Diretora,</p>") == "vocativo"
    assert papel("<p>Av. Getúlio Vargas</p>") == "outra"
    assert papel("<p>qualquer</p>", principal="S") == "principal"
