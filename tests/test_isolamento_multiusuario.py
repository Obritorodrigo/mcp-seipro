"""Isolamento e cache dos clientes SEI por sessão OAuth (modo HTTP).

Em `stateless_http` o SDK recria o lifespan a cada request, então a cache de
clientes vive no módulo (`srv._cache_clientes`). Estes testes garantem que:
- duas sessões nunca compartilham cliente;
- a mesma sessão reaproveita o cliente (um login no SEI, não um por tool);
- credenciais diferentes na mesma sessão geram outro cliente;
- clientes ociosos expiram e o tamanho da cache é limitado.
"""

from types import SimpleNamespace

import pytest

import mcp_seipro.server as srv


class ClienteFicticio:
    def __init__(self, **kwargs):
        self.credenciais = kwargs
        self.unidade_ativa = None
        self.fechado = False

    async def close(self):
        self.fechado = True


def _creds(usuario: str) -> dict:
    return {
        "sei_url": "https://sei.exemplo.gov.br",
        "sei_usuario": usuario,
        "sei_senha": f"senha_ficticia_{usuario}",
        "sei_orgao": "1",
    }


@pytest.fixture
def ambiente(monkeypatch):
    monkeypatch.setattr(srv, "_http_mode", True)
    monkeypatch.setattr(srv, "SEIClient", ClienteFicticio)
    monkeypatch.setattr(srv, "SEIWebClient", ClienteFicticio)
    monkeypatch.setattr(srv, "_cache_clientes", {})

    sessoes = {
        "A": ("sid-usuario-A", _creds("usuario_A")),
        "B": ("sid-usuario-B", _creds("usuario_B")),
    }
    atual = {"usuario": "A"}
    monkeypatch.setattr(srv, "_credenciais_sessao", lambda: sessoes[atual["usuario"]])

    # O lifespan HTTP não carrega clientes; o contexto só precisa existir.
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={"sei": None, "sei_web": None})
    )
    return atual, sessoes, ctx


def test_clientes_rest_sao_independentes(ambiente):
    atual, _, ctx = ambiente

    atual["usuario"] = "A"
    cliente_a = srv._get_client(ctx)
    atual["usuario"] = "B"
    cliente_b = srv._get_client(ctx)

    assert cliente_a is not cliente_b
    assert cliente_a.credenciais["sei_usuario"] == "usuario_A"
    assert cliente_b.credenciais["sei_usuario"] == "usuario_B"


def test_mesma_sessao_reaproveita_cliente_entre_requests(ambiente):
    atual, _, ctx = ambiente
    atual["usuario"] = "A"

    primeiro = srv._get_client(ctx)
    # Request seguinte: lifespan novo (como no stateless_http), mesmo token.
    ctx_novo = SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={"sei": None, "sei_web": None})
    )
    assert srv._get_client(ctx_novo) is primeiro


def test_unidades_ativas_sao_independentes(ambiente):
    atual, _, ctx = ambiente

    atual["usuario"] = "A"
    cliente_a = srv._get_client(ctx)
    cliente_a.unidade_ativa = "SEJUC/DEPLAF"
    atual["usuario"] = "B"
    cliente_b = srv._get_client(ctx)
    cliente_b.unidade_ativa = "SEJUC/DESIPE"

    assert cliente_a.unidade_ativa == "SEJUC/DEPLAF"
    assert cliente_b.unidade_ativa == "SEJUC/DESIPE"


def test_rest_e_web_nao_se_misturam(ambiente):
    atual, _, ctx = ambiente
    atual["usuario"] = "A"

    rest = srv._get_client(ctx)
    web = srv._get_web_client(ctx)

    assert rest is not web
    assert srv._get_web_client(ctx) is web


def test_clientes_web_sao_independentes(ambiente):
    atual, _, ctx = ambiente

    atual["usuario"] = "A"
    cliente_a = srv._get_web_client(ctx)
    atual["usuario"] = "B"
    cliente_b = srv._get_web_client(ctx)

    assert cliente_a is not cliente_b
    assert cliente_a.credenciais["sei_usuario"] == "usuario_A"
    assert cliente_b.credenciais["sei_usuario"] == "usuario_B"


def test_credenciais_novas_na_mesma_sessao_geram_outro_cliente(ambiente):
    atual, sessoes, ctx = ambiente
    atual["usuario"] = "A"
    antigo = srv._get_client(ctx)

    sid, creds = sessoes["A"]
    sessoes["A"] = (sid, {**creds, "sei_senha": "senha_trocada"})

    novo = srv._get_client(ctx)
    assert novo is not antigo
    assert novo.credenciais["sei_senha"] == "senha_trocada"


def test_sessao_invalida_nao_obtem_cliente(ambiente, monkeypatch):
    _, _, ctx = ambiente

    def revogada():
        raise ValueError("Token invalido ou expirado. Reconecte o MCP.")

    monkeypatch.setattr(srv, "_credenciais_sessao", revogada)
    with pytest.raises(ValueError):
        srv._get_client(ctx)
    assert srv._cache_clientes == {}


def test_cliente_ocioso_expira(ambiente, monkeypatch):
    atual, _, ctx = ambiente
    atual["usuario"] = "A"
    relogio = {"t": 1000.0}
    monkeypatch.setattr("time.monotonic", lambda: relogio["t"])
    monkeypatch.setattr(srv, "_fechar_depois", lambda c: None)

    primeiro = srv._get_client(ctx)
    relogio["t"] += srv._CACHE_TTL + 1
    assert srv._get_client(ctx) is not primeiro


def test_cache_respeita_tamanho_maximo(ambiente, monkeypatch):
    _, sessoes, ctx = ambiente
    monkeypatch.setattr(srv, "_CACHE_MAX", 2)
    despejados = []
    monkeypatch.setattr(srv, "_fechar_depois", despejados.append)
    relogio = {"t": 0.0}

    def agora():
        relogio["t"] += 1
        return relogio["t"]

    monkeypatch.setattr("time.monotonic", agora)

    clientes = []
    for i in range(4):
        sessoes[f"U{i}"] = (f"sid-{i}", _creds(f"u{i}"))
        monkeypatch.setattr(srv, "_credenciais_sessao", lambda i=i: sessoes[f"U{i}"])
        clientes.append(srv._get_client(ctx))

    assert len(srv._cache_clientes) <= 2
    # Os mais antigos saem primeiro e são agendados para fechar.
    assert clientes[0] in despejados
