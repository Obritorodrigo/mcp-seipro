"""Unidade de trabalho lembrada entre sessões.

Em sei.rr.gov.br o login caía sempre no Controle Interno (SEJUC/DEPLAF/CI) e
era preciso trocar para a SEJUC/DEPLAF a cada conversa.
"""

import json
import os
import stat
from types import SimpleNamespace

import httpx
import pytest

import mcp_seipro.server as srv
from mcp_seipro import preferencias
from mcp_seipro.sei_client import SEIClient

URL = "https://sei.exemplo.gov.br/api/v2"


def _cliente(responder, usuario="10924687738"):
    c = SEIClient(sei_url=URL, sei_usuario=usuario, sei_senha="s", sei_orgao="16",
                  permitir_arquivo_local=False)
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(responder))
    return c


def _login(unidade_login="110005930"):
    return {"sucesso": True, "data": {"token": "t", "loginData": {
        "IdUsuario": 99, "IdOrgao": 16, "IdUnidadeAtual": unidade_login}}}


@pytest.mark.asyncio
async def test_sessao_nova_comeca_na_unidade_lembrada():
    chave = preferencias.chave_usuario(URL, "16", "10924687738")
    preferencias.salvar_unidade(chave, "110001335")
    pedidos = []

    def responder(req):
        pedidos.append(req)
        if req.url.path.endswith("/autenticar"):
            return httpx.Response(200, json=_login())
        return httpx.Response(200, json={"sucesso": True, "mensagem": "Unidade alterada com sucesso!"})

    c = _cliente(responder)
    try:
        await c.autenticar()
        headers = await c._get_headers()
    finally:
        await c.close()

    troca = [p for p in pedidos if p.url.path.endswith("/usuario/alterar/unidade")]
    assert len(troca) == 1 and b"unidade=110001335" in troca[0].content
    assert c._unidade_ativa == "110001335"
    assert headers["unidade"] == "110001335"


@pytest.mark.asyncio
async def test_unidade_recusada_e_esquecida():
    chave = preferencias.chave_usuario(URL, "16", "10924687738")
    preferencias.salvar_unidade(chave, "123")

    def responder(req):
        if req.url.path.endswith("/autenticar"):
            return httpx.Response(200, json=_login())
        return httpx.Response(200, json={"sucesso": False, "mensagem": "Unidade não autorizada"})

    c = _cliente(responder)
    try:
        await c.autenticar()
    finally:
        await c.close()
    assert c._unidade_ativa == "110005930"
    assert "unidade" not in await c._get_headers()
    assert preferencias.unidade(chave) is None


@pytest.mark.asyncio
async def test_sem_preferencia_nao_troca_nada():
    pedidos = []

    def responder(req):
        pedidos.append(req)
        return httpx.Response(200, json=_login())

    c = _cliente(responder)
    try:
        await c.autenticar()
    finally:
        await c.close()
    assert [p.url.path.rsplit("/", 1)[-1] for p in pedidos] == ["autenticar"]


def test_preferencia_e_por_usuario_e_arquivo_protegido():
    a = preferencias.chave_usuario(URL, "16", "usuarioA")
    b = preferencias.chave_usuario(URL, "16", "usuarioB")
    preferencias.salvar_unidade(a, "1")
    assert preferencias.unidade(a) == "1" and preferencias.unidade(b) is None
    assert "usuarioA" not in preferencias.caminho().read_text()
    assert stat.S_IMODE(os.stat(preferencias.caminho()).st_mode) == 0o600


class ClienteTroca:
    def __init__(self):
        self.trocas, self.lembradas = [], []

    async def listar_unidades_usuario(self):
        return [{"id": "110001335", "sigla": "SEJUC/DEPLAF", "nome": "Departamento de Planejamento"},
                {"id": "110005930", "sigla": "SEJUC/DEPLAF/CI", "nome": "Controle Interno"},
                {"id": "110001336", "sigla": "SEJUC/DEPLAF/DIADM", "nome": "Divisão de Administração"}]

    async def trocar_unidade(self, id_unidade):
        self.trocas.append(id_unidade)
        return {"mensagem": "Unidade alterada com sucesso!"}

    def lembrar_unidade(self, id_unidade):
        self.lembradas.append(id_unidade)


@pytest.mark.asyncio
async def test_trocar_por_sigla_exata_e_lembrar(monkeypatch):
    c = ClienteTroca()
    monkeypatch.setattr(srv, "_get_client", lambda ctx: c)
    r = json.loads(await srv.sei_trocar_unidade(id_unidade="sejuc/deplaf", ctx=SimpleNamespace()))
    assert c.trocas == ["110001335"] and c.lembradas == ["110001335"]
    assert r["sigla"] == "SEJUC/DEPLAF" and r["lembrada"] is True


@pytest.mark.asyncio
async def test_sigla_ambigua_nao_troca(monkeypatch):
    c = ClienteTroca()
    monkeypatch.setattr(srv, "_get_client", lambda ctx: c)
    # "deplaf/" casa com SEJUC/DEPLAF/CI e SEJUC/DEPLAF/DIADM
    r = json.loads(await srv.sei_trocar_unidade(id_unidade="deplaf/", ctx=SimpleNamespace()))
    assert "error" in r and c.trocas == [] and c.lembradas == []
