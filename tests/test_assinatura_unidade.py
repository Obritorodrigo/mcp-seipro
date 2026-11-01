"""Fluxo de `sei_assinar_documento`: identidade do assinante e unidade ativa."""

import json
from types import SimpleNamespace

import pytest

import mcp_seipro.server as srv


class ClienteAssinatura:
    """SEIClient mínimo para o fluxo de assinatura."""

    def __init__(self, unidade_doc="110001335", unidades_usuario=("110001335",),
                 id_usuario="99", id_orgao="16"):
        self._usuario = "10924687738"
        self._senha = "ficticia"
        self._orgao = "16"
        self._id_usuario = id_usuario
        self._id_orgao_usuario = id_orgao
        self._unidade_doc = unidade_doc
        self._unidades_usuario = unidades_usuario
        self.trocas = []
        self.assinaturas = []

    async def _get_headers(self):
        return {"token": "t"}

    async def autenticar(self):
        return "t"

    async def consultar_documento_interno(self, doc_id):
        return {"protocolo": "27102756"}

    async def listar_documentos_todos(self, id_procedimento):
        return {"documentos": [
            {"id": "1", "atributos": {"idUnidade": "1"}},
            {"id": "27113158", "atributos": {"idUnidade": self._unidade_doc}},
        ]}

    async def listar_unidades_usuario(self):
        return [{"id": u} for u in self._unidades_usuario]

    async def trocar_unidade(self, id_unidade):
        self.trocas.append(id_unidade)
        return {}

    async def assinar_documento(self, **kwargs):
        self.assinaturas.append(kwargs)
        return {"mensagem": "Documento assinado com sucesso."}


@pytest.fixture
def com_cliente(monkeypatch):
    def usar(cliente):
        monkeypatch.setattr(srv, "_get_client", lambda ctx: cliente)

        async def resolver(client, ref):
            return ref, "I"

        monkeypatch.setattr(srv, "_resolver_documento", resolver)
        return cliente
    return usar


async def _assinar(**kw):
    return json.loads(await srv.sei_assinar_documento(
        id_documento="27113158", cargo="Diretor", ctx=SimpleNamespace(), **kw
    ))


@pytest.mark.asyncio
async def test_ativa_unidade_geradora_quando_usuario_tem_acesso(com_cliente):
    c = com_cliente(ClienteAssinatura())
    resposta = await _assinar()

    assert c.trocas == ["110001335"]
    assert resposta["_unidade_ativada"] == "110001335"
    enviado = c.assinaturas[0]
    assert enviado["id_usuario"] == "99"
    assert enviado["orgao"] == "16"
    assert enviado["cargo"] == "Diretor"


@pytest.mark.asyncio
async def test_documento_de_outra_unidade_nao_bloqueia(com_cliente):
    # Ex.: documento recebido para assinatura de outra unidade. Antes da
    # correção a tool recusava; agora não troca a unidade e deixa o SEI decidir.
    c = com_cliente(ClienteAssinatura(unidade_doc="110005688"))
    resposta = await _assinar()

    assert c.trocas == []
    assert len(c.assinaturas) == 1
    assert "_unidade_ativada" not in resposta


@pytest.mark.asyncio
async def test_falha_ao_consultar_arvore_nao_bloqueia(com_cliente):
    c = ClienteAssinatura()

    async def falha(_):
        raise RuntimeError("502 Bad Gateway")

    c.listar_documentos_todos = falha
    com_cliente(c)
    await _assinar()

    assert c.trocas == []
    assert len(c.assinaturas) == 1


@pytest.mark.asyncio
async def test_sem_id_usuario_nao_envia_assinatura(com_cliente):
    c = com_cliente(ClienteAssinatura(id_usuario=None))
    resposta = await _assinar()

    assert "IdUsuario" in resposta["error"]
    assert c.assinaturas == []


@pytest.mark.asyncio
async def test_orgao_explicito_tem_precedencia(com_cliente):
    c = com_cliente(ClienteAssinatura())
    await _assinar(orgao="7")
    assert c.assinaturas[0]["orgao"] == "7"
