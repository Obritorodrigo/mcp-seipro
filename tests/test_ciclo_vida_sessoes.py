"""Ciclo de vida dos clientes SEI em modo HTTP.

No `stateless_http` o SDK entra e sai do lifespan a CADA request. Se o lifespan
fechasse os clientes, a cache por sessão seria inútil (um login no SEI por
tool). Os clientes só são fechados ao serem despejados da cache, após carência.
"""

import asyncio

import pytest

import mcp_seipro.server as srv


class ClienteFicticio:
    def __init__(self):
        self.fechamentos = 0

    async def close(self):
        self.fechamentos += 1


@pytest.mark.asyncio
async def test_lifespan_http_nao_fecha_clientes_da_cache(monkeypatch):
    monkeypatch.setattr(srv, "_http_mode", True)
    cliente = ClienteFicticio()
    monkeypatch.setattr(srv, "_cache_clientes", {("rest", "sid-A", "h"): [0.0, cliente]})

    async with srv.lifespan(srv.mcp) as estado:
        assert estado == {"sei": None, "sei_web": None}

    assert cliente.fechamentos == 0
    assert ("rest", "sid-A", "h") in srv._cache_clientes


@pytest.mark.asyncio
async def test_cliente_despejado_fecha_apos_carencia(monkeypatch):
    monkeypatch.setattr(srv, "_CACHE_GRACA_FECHAR", 0.01)
    cliente = ClienteFicticio()

    srv._fechar_depois(cliente)
    assert cliente.fechamentos == 0  # não fecha na hora: pode estar em uso

    await asyncio.sleep(0.05)
    assert cliente.fechamentos == 1


def test_fechar_depois_sem_loop_nao_falha():
    srv._fechar_depois(ClienteFicticio())  # fora de loop: só ignora
