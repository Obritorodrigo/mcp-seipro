"""Preferências por usuário do SEI que sobrevivem entre sessões.

Hoje guarda só a UNIDADE DE TRABALHO: o login do SEI cai na "última unidade
usada" do usuário em qualquer lugar (outra conversa, a tela do SEI), que muitas
vezes não é a de trabalho — em sei.rr.gov.br o usuário entrava sempre no
Controle Interno e precisava trocar para a SEJUC/DEPLAF a cada conversa.

- Arquivo JSON em `SEI_PREFERENCIAS_PATH` (padrão
  `/var/lib/sei-pro/preferencias.json`, 0600).
- Chave: sha256(url do SEI + órgão + login) — o login não fica em claro.
- Só `sei_trocar_unidade` (escolha explícita) grava; trocas automáticas da
  assinatura/alteração não mudam a preferência.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)
_lock = threading.Lock()


def caminho() -> Path:
    return Path(os.environ.get("SEI_PREFERENCIAS_PATH", "/var/lib/sei-pro/preferencias.json"))


def chave_usuario(base_url: str, orgao: str, usuario: str) -> str:
    bruto = f"{(base_url or '').rstrip('/').lower()}|{orgao or ''}|{(usuario or '').strip().lower()}"
    return hashlib.sha256(bruto.encode("utf-8")).hexdigest()


def _ler() -> dict:
    try:
        return json.loads(caminho().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning("Preferências ilegíveis; ignorando", exc_info=True)
        return {}


def unidade(chave: str) -> str | None:
    with _lock:
        valor = (_ler().get(chave) or {}).get("unidade")
    return str(valor) if valor else None


def salvar_unidade(chave: str, id_unidade: str | None) -> None:
    """Grava (ou remove, com None) a unidade preferida. Nunca levanta exceção."""
    try:
        with _lock:
            dados = _ler()
            if id_unidade:
                dados.setdefault(chave, {})["unidade"] = str(id_unidade)
            else:
                dados.get(chave, {}).pop("unidade", None)
                if chave in dados and not dados[chave]:
                    dados.pop(chave)
            destino = caminho()
            destino.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporario = destino.with_suffix(".tmp")
            fd = os.open(str(temporario), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(dados, f)
            os.replace(temporario, destino)
    except Exception:
        logger.warning("Falha ao gravar preferências", exc_info=True)
