"""Cargo da assinatura por id/nome e busca de blocos de assinatura por nome."""

import json
from types import SimpleNamespace

import pytest

import mcp_seipro.server as srv

CTX = SimpleNamespace()

# Lista real de sei.rr.gov.br (SEJUC/DEPLAF)
CARGOS = [
    {"id": "7375", "nome": "Diretora em Exercício"},
    {"id": "7376", "nome": "Diretor do Departamento de Planejamento, Administração e Finanças"},
    {"id": "7377", "nome": "Diretora do Departamento de Planejamento, Administração e Finanças"},
    {"id": "7378", "nome": "Controle Interno do Departamento de Planejamento, Administração e Finanças"},
    {"id": "", "nome": "Policial Penal"},
]


class Resp:
    def __init__(self, dados):
        self._dados = dados

    def json(self):
        return self._dados


class ClienteCargos:
    def __init__(self, cargos=CARGOS, falha=False):
        self.cargos = cargos
        self.falha = falha

    async def _request(self, metodo, rota, **kw):
        if self.falha:
            raise RuntimeError("502")
        assert rota == "/assinante/listar"
        return Resp({"sucesso": True, "data": self.cargos})


@pytest.mark.asyncio
@pytest.mark.parametrize("pedido,esperado", [
    ("7376", "Diretor do Departamento de Planejamento, Administração e Finanças"),
    ("diretor do departamento de planejamento, administracao e financas",
     "Diretor do Departamento de Planejamento, Administração e Finanças"),
    ("Diretora do Departamento de Planejamento, Administração e Finanças",
     "Diretora do Departamento de Planejamento, Administração e Finanças"),
    ("controle interno", "Controle Interno do Departamento de Planejamento, Administração e Finanças"),
    ("policial penal", "Policial Penal"),  # cargo sem id na lista
])
async def test_resolve_cargo(pedido, esperado):
    nome, escolha = await srv._resolver_cargo(ClienteCargos(), pedido)
    assert escolha is None and nome == esperado


@pytest.mark.asyncio
async def test_cargo_ambiguo_nao_assina():
    # "diretor" casa com "Diretor…" e com "Diretora…" (substring).
    nome, escolha = await srv._resolver_cargo(ClienteCargos(), "diretor departamento")
    assert nome == ""
    r = json.loads(escolha)
    assert "Mais de um" in r["error"]
    assert {c["id"] for c in r["cargos_disponiveis"]} == {"7376", "7377"}


@pytest.mark.asyncio
async def test_cargo_inexistente_lista_todos():
    _, escolha = await srv._resolver_cargo(ClienteCargos(), "governador")
    r = json.loads(escolha)
    assert "Nenhum cargo" in r["error"] and len(r["cargos_disponiveis"]) == 5


@pytest.mark.asyncio
async def test_lista_indisponivel_mantem_texto_informado():
    nome, escolha = await srv._resolver_cargo(ClienteCargos(falha=True), "Diretor X")
    assert (nome, escolha) == ("Diretor X", None)


# --- assinatura usa o cargo resolvido ------------------------------------------

@pytest.mark.asyncio
async def test_assinar_documento_com_id_do_cargo(monkeypatch):
    enviados = []

    class C(ClienteCargos):
        _usuario, _senha, _orgao = "u", "s", "16"
        _id_usuario, _id_orgao_usuario = "99", "16"

        async def _get_headers(self):
            return {}

        async def assinar_documento(self, **kw):
            enviados.append(kw)
            return {"mensagem": "ok"}

    async def resolver(client, ref):
        return ref, "I"

    async def ativar(client, doc_id):
        return None

    monkeypatch.setattr(srv, "_get_client", lambda ctx: C())
    monkeypatch.setattr(srv, "_resolver_documento", resolver)
    monkeypatch.setattr(srv, "_ativar_unidade_geradora", ativar)
    await srv.sei_assinar_documento(id_documento="1", cargo="7376", ctx=CTX)
    assert enviados[0]["cargo"] == "Diretor do Departamento de Planejamento, Administração e Finanças"


# --- blocos de assinatura -------------------------------------------------------

BLOCOS_ASSINATURA = [
    {"id": "880", "atributos": {"idBloco": "880", "descricao": "Assinaturas – Diárias Novembro",
                                "estado": "D", "siglaUnidade": "SEJUC/DEPLAF",
                                "unidades": [{"idUnidade": "1", "unidade": "SEJUC/GAB"}],
                                "numeroDocumentos": "4"}},
    {"id": "879", "atributos": {"idBloco": "879", "descricao": "Contratos mobiliário ARP",
                                "estado": "A", "siglaUnidade": "SEJUC/DEPLAF",
                                "unidades": [], "numeroDocumentos": "2"}},
]


class ClienteBlocos:
    async def pesquisar_blocos_assinatura(self, filtro="", limit=50, start=0, estado=""):
        itens = BLOCOS_ASSINATURA
        if estado:
            itens = [b for b in itens if b["atributos"]["estado"] in estado.split(",")]
        if filtro:
            itens = [b for b in itens if filtro in b["atributos"]["descricao"]]
        return {"blocos": itens[start * limit:(start + 1) * limit]}


@pytest.mark.asyncio
async def test_bloco_assinatura_por_nome_sem_acento(monkeypatch):
    monkeypatch.setattr(srv, "_get_client", lambda ctx: ClienteBlocos())
    r = json.loads(await srv.sei_pesquisar_blocos_assinatura(filtro="diarias novembro", ctx=CTX))
    [b] = r["blocos"]
    assert b["id"] == "880"
    assert b["estado_descricao"] == "Disponibilizado"
    assert b["numero_documentos"] == "4"
    assert b["disponibilizado_para"] == ["SEJUC/GAB"]


@pytest.mark.asyncio
async def test_bloco_assinatura_por_estado(monkeypatch):
    monkeypatch.setattr(srv, "_get_client", lambda ctx: ClienteBlocos())
    r = json.loads(await srv.sei_pesquisar_blocos_assinatura(estado="a", ctx=CTX))
    assert [b["id"] for b in r["blocos"]] == ["879"]
