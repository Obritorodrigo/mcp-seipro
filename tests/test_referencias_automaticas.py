"""Números de processo e de documento citados no texto viram links do SEI.

Caso real: no Ofício 64/2026/SEJUC/DEPLAF (sei.rr.gov.br) o "Processo SEI nº
26101.018680/2026.11" ficou como texto e os "ep." tinham classe errada.
"""

import json
from types import SimpleNamespace

import pytest

import mcp_seipro.server as srv


class Cliente:
    def __init__(self):
        self.consultas = []

    async def consultar_processo(self, numero):
        self.consultas.append(("P", numero))
        if numero == "26101.018680/2026.11":
            return {"IdProcedimento": "26972759", "ProtocoloProcedimentoFormatado": "26101.018680/2026.11"}
        raise Exception("Processo não encontrado")

    async def consultar_documento_interno_formatado(self, numero):
        self.consultas.append(("D", numero))
        ids = {"24837023": "27102769", "24846801": "27113158"}
        if numero in ids:
            return {"idDocumento": ids[numero]}
        raise Exception(f"Documento {numero} não encontrado.")


async def _vincular(texto, cliente=None):
    c = cliente or Cliente()
    v, n = [], []
    return await srv._vincular_referencias(c, texto, {}, v, n), v, n, c


@pytest.mark.asyncio
async def test_processo_e_documentos_viram_links():
    html, v, n, _ = await _vincular(
        "<p>Em atenção ao Ofício-Circular, ep. 24837023, e ao Despacho, SEI nº 24846801, "
        "no Processo SEI nº 26101.018680/2026.11.</p>")
    assert 'id="lnkSei27102769" style="text-indent:0;">24837023</a>' in html
    assert 'id="lnkSei27113158" style="text-indent:0;">24846801</a>' in html
    assert 'id="lnkSei26972759" style="text-indent:0;">26101.018680/2026.11</a>' in html
    assert {x["tipo"] for x in v} == {"processo", "documento"} and len(v) == 3
    assert n == []


@pytest.mark.asyncio
async def test_processo_digitado_com_hifen_usa_o_formato_do_sei():
    html, v, _, _ = await _vincular("<p>Processo 26101.018680/2026-11.</p>")
    assert '>26101.018680/2026.11</a>' in html  # texto do link = formato real
    assert v[0]["id"] == "26972759"


@pytest.mark.asyncio
async def test_links_existentes_nao_sao_tocados():
    original = ('<p>ep. <span contenteditable="false"><a class="ancoraSei" id="lnkSei27102769">'
                '24837023</a></span> e processo <a href="x">26101.018680/2026.11</a></p>')
    html, v, _, c = await _vincular(original)
    assert html == original and v == [] and c.consultas == []


@pytest.mark.asyncio
async def test_numero_solto_nao_vira_link():
    # Sem "ep."/"SEI nº", um número qualquer (CNPJ, valor, quantidade) não é tocado.
    html, v, _, c = await _vincular("<p>CNPJ 21776066 e 24837023 unidades.</p>")
    assert "lnkSei" not in html and c.consultas == []


@pytest.mark.asyncio
async def test_nao_encontrado_fica_como_texto_e_e_informado():
    html, v, n, _ = await _vincular("<p>ep. 99999999 e Processo 11111.111111/2026-11</p>")
    assert "lnkSei" not in html
    assert set(n) == {"99999999", "11111.111111/2026-11"}


@pytest.mark.asyncio
async def test_atributos_nao_sao_alterados():
    original = '<p title="ep. 24837023">texto</p>'
    html, v, _, _ = await _vincular(original)
    assert html == original and v == []


@pytest.mark.asyncio
async def test_mesmo_numero_consultado_uma_vez():
    _, v, _, c = await _vincular("<p>ep. 24837023; ep. 24837023; Processo 26101.018680/2026.11 e 26101.018680/2026.11</p>")
    assert len(v) == 4
    assert c.consultas.count(("D", "24837023")) == 1
    assert c.consultas.count(("P", "26101.018680/2026.11")) == 1


@pytest.mark.asyncio
async def test_gerar_referencia_de_processo(monkeypatch):
    monkeypatch.setattr(srv, "_get_client", lambda ctx: Cliente())
    r = json.loads(await srv.sei_gerar_referencia(numero_sei="26101.018680/2026-11", ctx=SimpleNamespace()))
    assert r["id_procedimento"] == "26972759"
    assert r["numero_processo"] == "26101.018680/2026.11"
    assert 'lnkSei26972759' in r["html"]
