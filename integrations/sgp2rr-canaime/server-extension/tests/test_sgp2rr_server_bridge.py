"""Testes de ponte/patch em servidor sintético, sem rede nem dados reais."""
from contextlib import contextmanager
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sgp2rr_server_bridge import registrar_sgp2rr_no_servidor
from prepare_integration import preparar, MARKER, FILES

TEST_SOURCE = (
    "from types import SimpleNamespace\n"
    "class S: pass\n"
    "mcp = SimpleNamespace(tool=lambda: lambda f: f)\n"
    "def atual(): pass\n"
    "def renderizar(page): pass\n"
    + "".join(
        "@mcp.tool()\n"
        "def " + name + "():\n"
        "    return 'ok'\n"
        for name in (
            "sgp_abrir_pagina", "sgp_seguir_link", "sgp_ver_parte",
            "sgp_enviar_formulario", "sgp_status"
        )
    )
    + "\nif __name__ == '__main__':\n    pass\n"
)


class MCPFake:
    def __init__(self):
        self.tools = {}
    def tool(self):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


class FakeResponse:
    status_code = 200
    headers = {"Content-Type": "application/pdf"}
    def iter_bytes(self, chunk_size):
        yield b"%PDF-1.7\nficticio"


class FakeHTTP:
    def __init__(self):
        self.events = []
    @contextmanager
    def stream(self, method, url, **kw):
        self.events.append((method, kw))
        yield FakeResponse()


class FakeSession:
    def __init__(self, label):
        import threading
        self.label = label
        self.logado = True
        self.lock = threading.Lock()
        self.client = FakeHTTP()
        self.paths = []
    def _url(self, path):
        return path
    def carregar(self, method, path):
        self.paths.append((method, path))
        if "pesquisa_resultadoVULGO.php" in path:
            return "1 REEDUCANDO(S) CADASTRADO(S)\nID 55\nPESSOA FICTICIA\n"
        if path.endswith("pesquisa.php"):
            return (
                "## Formulários\n[F1] GET pesquisa_resultadoVULGO.php name=form1\n"
                " - busca1 (select) opções: mae=MÃE; nome=NOME\n"
            )
        raise AssertionError("Unrecognized fake path")


class BridgeTests(unittest.TestCase):
    def test_bridge_reuses_current_oauth_session_and_lock(self):
        sessions = [FakeSession("A"), FakeSession("B")]
        index = [0]
        calls = []
        def resolver():
            calls.append(index[0])
            return sessions[index[0]]
        mcp = MCPFake()
        registrar_sgp2rr_no_servidor(mcp, resolver, lambda x: x)
        self.assertEqual(len(mcp.tools), 8)
        self.assertEqual(calls, [])  # não resolve token na inicialização
        a = mcp.tools["sgp_pesquisar_cadastro"]("mae", "NOME FICTICIO")
        self.assertEqual(a["resultados"][0]["codigo"], 55)
        self.assertEqual(len(sessions[0].paths), 2)
        self.assertEqual(len(sessions[1].paths), 0)
        index[0] = 1
        b = mcp.tools["sgp_pesquisar_cadastro"]("nome", "PESSOA FICTICIA")
        self.assertEqual(b["resultados"][0]["codigo"], 55)
        self.assertEqual(len(sessions[1].paths), 2)
        self.assertEqual(calls, [0, 1])

    def test_fails_if_session_is_not_authenticated(self):
        session = FakeSession("A")
        session.logado = False
        mcp = MCPFake()
        registrar_sgp2rr_no_servidor(mcp, lambda: session, lambda x: x)
        with self.assertRaises(PermissionError):
            mcp.tools["sgp_pesquisar_cadastro"]("nome", "PESSOA FICTICIA")

    def test_stage_isolated_from_original(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            production_dir = tmp / "production"
            production_dir.mkdir()
            src = production_dir / "sgp2rr_mcp.py"
            src.write_text(TEST_SOURCE, encoding="utf-8")
            stage = tmp / "staging"
            patched = preparar(src, stage, Path(__file__).resolve().parents[1])
            self.assertIn(MARKER, patched.read_text(encoding="utf-8"))
            self.assertNotIn(MARKER, src.read_text(encoding="utf-8"))
            for module in FILES:
                self.assertTrue((stage / module).is_file())

    def test_no_overwrite_on_repeated_stage(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            production_dir = tmp / "production"
            production_dir.mkdir()
            src = production_dir / "sgp2rr_mcp.py"
            src.write_text(TEST_SOURCE, encoding="utf-8")
            stage = tmp / "staging"
            preparar(src, stage, Path(__file__).resolve().parents[1])
            with self.assertRaises(FileExistsError):
                preparar(src, stage, Path(__file__).resolve().parents[1])


if __name__ == "__main__":
    unittest.main()
