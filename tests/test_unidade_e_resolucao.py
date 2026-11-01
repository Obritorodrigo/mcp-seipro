"""Unidade estável por sessão e resolução segura de número SEI.

Visto ao vivo em sei.rr.gov.br (01/11/2026): a "unidade atual" do SEI é a
última usada pelo usuário em QUALQUER sessão; outra sessão da mesma pessoa
voltou-a ao Controle Interno no meio de uma operação. Sem acesso ao despacho
24847106, o resolvedor tratou o número como id interno e acertou um Empenho de
outra secretaria (nada foi gravado porque as seções não batiam).
"""

import httpx
import pytest

import mcp_seipro.server as srv
from mcp_seipro.sei_client import SEIClient


def _cliente(rotas, chamadas):
    def responder(request):
        chamadas.append(request)
        caminho = request.url.path.split("/api/v2", 1)[-1]
        return httpx.Response(200, json=rotas.get((request.method, caminho), {"sucesso": True, "data": {}}))

    c = SEIClient(sei_url="https://sei.exemplo.gov.br/api/v2", sei_usuario="u",
                  sei_senha="s", sei_orgao="16", permitir_arquivo_local=False)
    c._token = "t"
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(responder))
    return c


@pytest.mark.asyncio
async def test_unidade_escolhida_vai_em_toda_requisicao():
    chamadas = []
    c = _cliente({("POST", "/usuario/alterar/unidade"): {"sucesso": True, "mensagem": "ok"}}, chamadas)
    try:
        await c._request("GET", "/versao")
        await c.trocar_unidade("110001335")
        await c._request("GET", "/versao")
        await c._request("POST", "/documento/secao/alterar", data={"x": "1"})
    finally:
        await c.close()
    assert "unidade" not in chamadas[0].headers            # antes de escolher: padrão do SEI
    assert chamadas[2].headers["unidade"] == "110001335"
    assert chamadas[3].headers["unidade"] == "110001335"


@pytest.mark.asyncio
async def test_login_novo_nao_derruba_a_unidade_escolhida(monkeypatch):
    c = SEIClient(sei_url="https://sei.exemplo.gov.br/api/v2", sei_usuario="u",
                  sei_senha="s", sei_orgao="16", permitir_arquivo_local=False)

    def responder(request):
        return httpx.Response(200, json={"sucesso": True, "data": {
            "token": "novo", "loginData": {"IdUsuario": 1, "IdUnidadeAtual": 110005930}}})

    c._client = httpx.AsyncClient(transport=httpx.MockTransport(responder))
    c._unidade_escolhida = "110001335"
    try:
        await c.autenticar()
    finally:
        await c.close()
    assert c._unidade_ativa == "110001335"
    assert (await c._get_headers())["unidade"] == "110001335"


class Resolvedor:
    def __init__(self, erro_formatado, visualizar=None):
        self.erro_formatado = erro_formatado
        self.visualizou = []
        self.visualizar = visualizar

    async def consultar_documento_interno_formatado(self, numero):
        raise Exception(self.erro_formatado)

    async def pesquisar_processos(self, **kw):
        return {"processos": []}

    async def visualizar_documento_interno(self, ref):
        self.visualizou.append(ref)
        return self.visualizar or "<html>outro documento qualquer</html>"


@pytest.mark.asyncio
async def test_numero_existente_sem_acesso_nao_vira_id_interno():
    c = Resolvedor("Erro ao consultar: Acesso ao documento 27113502 não autorizado.")
    with pytest.raises(ValueError, match="existe, mas a unidade atual não tem acesso"):
        await srv._resolver_documento(c, "24847106")
    assert c.visualizou == []  # nunca tentou o número como id interno


@pytest.mark.asyncio
async def test_numero_inexistente_ainda_aceita_id_interno():
    # Quem informa o id interno (ex.: 27113502) continua sendo atendido.
    c = Resolvedor("Erro ao consultar: Documento 27113502 não encontrado.")
    doc_id, tipo = await srv._resolver_documento(c, "27113502")
    assert (doc_id, tipo) == ("27113502", "I")
    assert c.visualizou == ["27113502"]
