"""Registro de auditoria das ações de escrita no SEI.

Com vários usuários num mesmo servidor, é preciso saber quem assinou, criou,
alterou ou enviou o quê, e quando. Cada chamada de tool de escrita vira uma
linha JSON em `SEI_AUDITORIA_LOG` (padrão `/var/lib/sei-pro/auditoria.jsonl`,
arquivo 0600):

    {"ts": "2026-11-01T03:40:12Z", "usuario": "10924687738", "sessao": "3f9a…",
     "tool": "sei_assinar_documento", "args": {"id_documento": "27113158",
     "cargo": "Diretor …"}, "ok": true, "resultado": {...}}

Não se grava CONTEÚDO: HTML de seções, arquivos e textos longos ficam de fora
ou truncados; senhas e tokens nunca entram. `SEI_AUDITORIA=0` desliga.
Rotação fica a cargo do logrotate do host.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Campos que carregam conteúdo ou segredo: nunca vão para o log.
_CAMPOS_OMITIDOS = {
    "secoes", "conteudo", "conteudo_html", "html", "texto",
    "arquivo_base64", "arquivo_bytes", "senha", "token", "sei_senha",
}
_MAX_TEXTO = 160
_MAX_RESULTADO = 8

_lock = threading.Lock()


def habilitada() -> bool:
    return os.environ.get("SEI_AUDITORIA", "1").strip().lower() not in ("0", "false", "nao", "não", "off")


def caminho() -> Path:
    return Path(os.environ.get("SEI_AUDITORIA_LOG", "/var/lib/sei-pro/auditoria.jsonl"))


def _resumir_valor(valor):
    if isinstance(valor, str):
        return valor if len(valor) <= _MAX_TEXTO else valor[:_MAX_TEXTO] + f"… ({len(valor)} caracteres)"
    if isinstance(valor, (int, float, bool)) or valor is None:
        return valor
    if isinstance(valor, (list, tuple)):
        return [_resumir_valor(v) for v in list(valor)[:20]]
    if isinstance(valor, dict):
        return {k: _resumir_valor(v) for k, v in list(valor.items())[:20]}
    return str(valor)[:_MAX_TEXTO]


def resumir_argumentos(args: dict) -> dict:
    saida = {}
    for chave, valor in (args or {}).items():
        if chave == "ctx" or valor in ("", None, [], {}):
            continue
        if chave in _CAMPOS_OMITIDOS:
            tamanho = len(valor) if hasattr(valor, "__len__") else None
            saida[chave] = f"<omitido{f': {tamanho} itens/caracteres' if tamanho is not None else ''}>"
            continue
        saida[chave] = _resumir_valor(valor)
    return saida


def resumir_resultado(retorno) -> tuple[bool | str, dict | str | None]:
    """(ok, resumo) a partir do retorno da tool (JSON em texto).

    `ok` é True, False ou "sem_alteracao" — quando a tool só devolveu opções
    para o usuário escolher (`cargos_disponiveis`, `hipoteses_disponiveis`).
    """
    dados = retorno
    if isinstance(retorno, str):
        try:
            dados = json.loads(retorno)
        except ValueError:
            return True, _resumir_valor(retorno)
    if isinstance(dados, dict):
        if "error" in dados:
            # Pedido de escolha (cargo, hipótese legal…): nada foi tentado no SEI.
            if any(k.endswith("_disponiveis") for k in dados):
                return "sem_alteracao", {"motivo": _resumir_valor(str(dados.get("error")))}
            return False, {"erro": _resumir_valor(str(dados.get("error")))}
        escalares = {
            k: _resumir_valor(v) for k, v in dados.items()
            if isinstance(v, (str, int, float, bool)) and not k.startswith("_")
        }
        return True, dict(list(escalares.items())[:_MAX_RESULTADO])
    return True, None


def registrar(evento: dict) -> None:
    """Acrescenta uma linha ao log. Nunca levanta exceção para a tool."""
    if not habilitada():
        return
    try:
        destino = caminho()
        destino.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        linha = json.dumps(
            {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **evento},
            ensure_ascii=False,
        ) + "\n"
        with _lock:
            fd = os.open(str(destino), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, linha.encode("utf-8"))
            finally:
                os.close(fd)
    except Exception:
        logger.warning("Falha ao gravar auditoria", exc_info=True)


def identificar(sid: str | None, usuario: str | None) -> dict:
    """Identidade do autor: login do SEI e um apelido curto da sessão OAuth."""
    ident = {"usuario": usuario or "?"}
    if sid:
        ident["sessao"] = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:12]
    return ident
