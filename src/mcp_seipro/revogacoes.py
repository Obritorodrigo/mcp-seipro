"""Persistência transacional de revogação e uso único de refresh OAuth.

O banco SQLite sobrevive a restart e é compartilhado entre processos (vários
workers no mesmo host). Caminho: `SEI_REVOGACOES_DB` (padrão
`/var/lib/sei-pro/revogacoes.sqlite3`). A pasta é criada se faltar; o usuário
do serviço precisa poder gravar nela.
"""

import hashlib
import os
import sqlite3
import time
import threading
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(
    os.environ.get("SEI_REVOGACOES_DB", "/var/lib/sei-pro/revogacoes.sqlite3")
)

# Caminhos cujo esquema já foi criado neste processo (evita CREATE TABLE a
# cada token aberto). Por caminho, para que os testes possam trocar DB_PATH.
_inicializados: set[str] = set()
_init_lock = threading.Lock()


def _hash(valor: str) -> str:
    return hashlib.sha256(valor.encode("utf-8")).hexdigest()


@contextmanager
def _conectar():
    caminho = Path(DB_PATH)
    chave = str(caminho)
    if chave not in _inicializados:
        with _init_lock:
            if chave not in _inicializados:
                caminho.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                # No banco novo, não deixar o umask do serviço criar arquivo
                # legível por outros.
                fd = os.open(chave, os.O_CREAT | os.O_RDWR, 0o600)
                os.close(fd)
    conn = sqlite3.connect(chave, timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        if chave not in _inicializados:
            conn.execute("""CREATE TABLE IF NOT EXISTS sessoes_revogadas (
                sid_hash TEXT PRIMARY KEY, expira_em REAL NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS refresh_utilizados (
                jti_hash TEXT PRIMARY KEY, sid_hash TEXT NOT NULL,
                usado_em REAL NOT NULL, expira_em REAL NOT NULL)""")
            conn.commit()
            _inicializados.add(chave)
        with conn:
            yield conn
    finally:
        conn.close()


def revogar(sid: str, expira_em: float) -> None:
    if not sid:
        raise ValueError("SID vazio")
    with _conectar() as conn:
        conn.execute("""INSERT INTO sessoes_revogadas (sid_hash, expira_em)
            VALUES (?, ?) ON CONFLICT(sid_hash) DO UPDATE SET
            expira_em=MAX(expira_em, excluded.expira_em)""",
            (_hash(sid), expira_em))


def esta_revogada(sid: str) -> bool:
    if not sid:
        return True
    with _conectar() as conn:
        row = conn.execute("""SELECT 1 FROM sessoes_revogadas
            WHERE sid_hash=? AND expira_em>?""",
            (_hash(sid), time.time())).fetchone()
    return row is not None


def usado_em_refresh(jti: str) -> float | None:
    if not jti:
        raise ValueError("JTI vazio")
    with _conectar() as conn:
        row = conn.execute("""SELECT usado_em FROM refresh_utilizados
            WHERE jti_hash=?""", (_hash(jti),)).fetchone()
    return float(row[0]) if row else None


def consumir_refresh(jti: str, sid: str, expira_em: float) -> bool:
    """True somente para o primeiro consumidor, inclusive entre processos.

    expira_em deve cobrir no mínimo a expiração do token; entradas antigas
    são mantidas até esse instante para barrar replay.
    """
    if not jti or not sid:
        raise ValueError("JTI ou SID vazio")
    with _conectar() as conn:
        cur = conn.execute("""INSERT OR IGNORE INTO refresh_utilizados
            (jti_hash, sid_hash, usado_em, expira_em) VALUES (?, ?, ?, ?)""",
            (_hash(jti), _hash(sid), time.time(), expira_em))
        return cur.rowcount == 1


def limpar_expiradas() -> int:
    with _conectar() as conn:
        now = time.time()
        a = conn.execute("DELETE FROM sessoes_revogadas WHERE expira_em<=?", (now,)).rowcount
        b = conn.execute("DELETE FROM refresh_utilizados WHERE expira_em<=?", (now,)).rowcount
    return a + b
