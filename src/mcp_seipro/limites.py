"""Limite de uso por usuário (janela deslizante de 60 s, em memória).

Protege o SEI do órgão de uma conversa em loop: com vários usuários num mesmo
servidor, um agente descontrolado não pode derrubar o serviço para os demais.

- `SEI_LIMITE_POR_MINUTO` (padrão 60): chamadas de tool por usuário;
- `SEI_LIMITE_ESCRITAS_POR_MINUTO` (padrão 20): só as de escrita;
- `0` desliga o respectivo limite.

A chave é a sessão OAuth (sid) — usuários diferentes não dividem cota.
O estado é por processo e some num restart, o que é aceitável para um
limitador de rajada.
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque

JANELA = 60.0
_lock = threading.Lock()
_chamadas: dict[str, deque] = defaultdict(deque)
_escritas: dict[str, deque] = defaultdict(deque)
_todas: deque = deque()  # instantes de todas as chamadas (página de status)


def _limite(nome: str, padrao: int) -> int:
    try:
        return max(0, int(os.environ.get(nome, str(padrao))))
    except ValueError:
        return padrao


def limite_chamadas() -> int:
    return _limite("SEI_LIMITE_POR_MINUTO", 60)


def limite_escritas() -> int:
    return _limite("SEI_LIMITE_ESCRITAS_POR_MINUTO", 20)


def _podar(fila: deque, agora: float, janela: float = JANELA) -> None:
    while fila and agora - fila[0] > janela:
        fila.popleft()


def registrar(chave: str, escrita: bool, agora: float | None = None) -> str | None:
    """Conta a chamada. Devolve a mensagem de bloqueio, ou None se liberada.

    Chamada bloqueada não consome cota (senão o usuário nunca sairia do
    bloqueio enquanto o agente insistisse).
    """
    agora = time.monotonic() if agora is None else agora
    with _lock:
        fila = _chamadas[chave]
        _podar(fila, agora)
        fila_e = _escritas[chave]
        _podar(fila_e, agora)

        lim = limite_chamadas()
        if lim and len(fila) >= lim:
            espera = int(JANELA - (agora - fila[0])) + 1
            return (f"Limite de uso atingido: {lim} chamadas por minuto por usuário. "
                    f"Nada foi enviado ao SEI. Tente de novo em {espera} s.")
        lim_e = limite_escritas()
        if escrita and lim_e and len(fila_e) >= lim_e:
            espera = int(JANELA - (agora - fila_e[0])) + 1
            return (f"Limite de escrita atingido: {lim_e} alterações por minuto por usuário. "
                    f"Nada foi enviado ao SEI. Tente de novo em {espera} s.")

        fila.append(agora)
        if escrita:
            fila_e.append(agora)
        _todas.append(agora)
        _podar(_todas, agora, 300.0)
    return None


def chamadas_recentes(janela: float = 300.0) -> int:
    agora = time.monotonic()
    with _lock:
        _podar(_todas, agora, 300.0)
        return sum(1 for t in _todas if agora - t <= janela)


def limpar() -> None:
    """Zera o estado (testes)."""
    with _lock:
        _chamadas.clear()
        _escritas.clear()
        _todas.clear()
