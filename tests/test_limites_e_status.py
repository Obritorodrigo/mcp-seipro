"""Limite de uso por usuário e página /status."""

import json

import pytest

import mcp_seipro.server as srv
from mcp_seipro import limites


def test_limite_de_chamadas_por_usuario(monkeypatch):
    monkeypatch.setenv("SEI_LIMITE_POR_MINUTO", "3")
    for _ in range(3):
        assert limites.registrar("A", escrita=False, agora=100.0) is None
    msg = limites.registrar("A", escrita=False, agora=101.0)
    assert "3 chamadas por minuto" in msg and "Nada foi enviado" in msg
    # outro usuário tem a própria cota
    assert limites.registrar("B", escrita=False, agora=101.0) is None
    # passada a janela, libera de novo
    assert limites.registrar("A", escrita=False, agora=161.0) is None


def test_limite_de_escritas_separado(monkeypatch):
    monkeypatch.setenv("SEI_LIMITE_ESCRITAS_POR_MINUTO", "2")
    assert limites.registrar("A", escrita=True, agora=0.0) is None
    assert limites.registrar("A", escrita=True, agora=1.0) is None
    assert "alterações por minuto" in limites.registrar("A", escrita=True, agora=2.0)
    assert limites.registrar("A", escrita=False, agora=2.0) is None  # leitura segue liberada


def test_bloqueio_nao_consome_cota(monkeypatch):
    monkeypatch.setenv("SEI_LIMITE_POR_MINUTO", "1")
    assert limites.registrar("A", False, agora=0.0) is None
    for t in range(1, 50):
        assert limites.registrar("A", False, agora=float(t)) is not None
    assert limites.registrar("A", False, agora=61.0) is None


def test_zero_desliga(monkeypatch):
    monkeypatch.setenv("SEI_LIMITE_POR_MINUTO", "0")
    monkeypatch.setenv("SEI_LIMITE_ESCRITAS_POR_MINUTO", "0")
    for t in range(500):
        assert limites.registrar("A", True, agora=float(t) / 100) is None


@pytest.mark.asyncio
async def test_tool_bloqueada_nao_chama_o_sei_e_audita(monkeypatch, tmp_path):
    destino = tmp_path / "aud.jsonl"
    monkeypatch.setenv("SEI_AUDITORIA_LOG", str(destino))
    monkeypatch.setenv("SEI_LIMITE_ESCRITAS_POR_MINUTO", "1")
    monkeypatch.setattr(srv, "_autor_auditoria", lambda: {"usuario": "u"})
    chamadas = []

    class C:
        async def alterar_bloco_interno(self, id_bloco, descricao):
            chamadas.append(id_bloco)
            return {"mensagem": "ok"}

    monkeypatch.setattr(srv, "_get_client", lambda ctx: C())
    fn = srv.mcp._tool_manager._tools["sei_alterar_bloco_interno"].fn
    await fn(id_bloco="1", descricao="a", ctx=None)
    r = json.loads(await fn(id_bloco="2", descricao="b", ctx=None))

    assert chamadas == ["1"]
    assert r["erro_origem"] == "limite_de_uso"
    linhas = [json.loads(l) for l in destino.read_text().splitlines()]
    assert [l["ok"] for l in linhas] == [True, "bloqueado_limite"]


def test_todas_as_tools_tem_limite():
    tools = srv.mcp._tool_manager._tools
    assert all(getattr(t.fn, "_limitada", False) for t in tools.values())
    # a marca de auditoria sobrevive ao wrapper externo
    assert tools["sei_assinar_documento"].fn._auditada
    assert not tools["sei_ler_documento"].fn._auditada


@pytest.mark.asyncio
async def test_status_sem_dados_de_usuario(monkeypatch):
    async def sei_ok():
        return {"host": "sei.rr.gov.br", "acessivel": True, "http": 200, "ms": 120}

    monkeypatch.setattr(srv, "_status_sei", sei_ok)
    monkeypatch.setattr(srv, "_cache_clientes", {
        ("rest", "sid-secreto-1", "h"): [0, object()],
        ("web", "sid-secreto-1", "h"): [0, object()],
        ("rest", "sid-secreto-2", "h"): [0, object()],
    })
    limites.registrar("x", False)
    st = await srv._pagina_status()
    assert st["servico"] == "ok"
    assert st["sessoes_ativas"] == 2
    assert st["chamadas_ultimos_5_min"] == 1
    assert st["sei"]["acessivel"] is True
    assert "sid-secreto" not in json.dumps(st)


@pytest.mark.asyncio
async def test_status_sei_tem_cache(monkeypatch):
    monkeypatch.setenv("SEI_URL", "https://sei.exemplo.gov.br/api/v2")
    monkeypatch.setattr(srv, "_STATUS_SEI_CACHE", {})
    pedidos = []

    class Cli:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            pedidos.append(url)
            return type("R", (), {"status_code": 200})()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", Cli)
    a = await srv._status_sei()
    b = await srv._status_sei()
    assert pedidos == ["https://sei.exemplo.gov.br/api/v2/orgao/listar"]
    assert a == b and a["acessivel"] is True and a["host"] == "sei.exemplo.gov.br"
