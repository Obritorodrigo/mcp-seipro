"""Testes de integração com sessões e respostas fictícias, sem rede."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sgp2rr_integration import (
    AdaptadorSGP2RR, ClienteAutenticado, integrar_sgp2rr,
    _url_consulta_permitida, MAX_PDF_BYTES,
)

PESQUISA = (
    "## Formulários\n[F1] GET pesquisa_resultadoVULGO.php name=form1\n"
    "  - busca1 (select) opções: nome=NOME; mae=MÃE; pai=PAI; cpf=CPF\n"
    "  - busca2 (text)\n  - busca3 (select)\n"
)
CODIGO = (
    "## Formulários\n[F1] GET Ficha_Menu.php name=form1\n"
    "  - id_cad_preso (text) '' = ''\n"
)


class Resposta:
    def __init__(self, body=b"%PDF-1.7\nconteudo ficticio", mime="application/pdf",
                 status=200, length=None):
        self.body = body
        self.status_code = status
        self.headers = {"Content-Type": mime}
        if length is not None:
            self.headers["Content-Length"] = str(length)
        self.closed = False
    def iter_content(self, chunk_size):
        for pos in range(0, len(self.body), chunk_size):
            yield self.body[pos:pos + chunk_size]
    def close(self):
        self.closed = True


class SessaoFicticia:
    def __init__(self, resposta=None):
        self.resposta = resposta or Resposta()
        self.visitas = []
    def get(self, url, **kw):
        self.visitas.append((url, kw))
        return self.resposta


class UsuarioFicticio:
    def __init__(self, tag):
        self.tag = tag
        self.calls = []
        self.session = SessaoFicticia()

    def abrir(self, caminho):
        self.calls.append(("abrir", caminho))
        if caminho == "areas/unidades/pesquisa.php":
            return PESQUISA
        if caminho == "areas/unidades/pesquisa_Iframe%20CODIGO.php":
            return CODIGO
        if caminho.startswith("areas/unidades/pesquisa_resultadoVULGO.php?"):
            return "1 REEDUCANDO(S) CADASTRADO(S)\nID 77\nCADASTRO FICTICIO\n"
        if caminho == "areas/unidades/Ficha_Menu.php?id_cad_preso=77":
            return ("## Links\n[1] (quadro) ficha → "
                    "areas/unidades/Ficha_Preso_index.php?id_cad_preso=77\n")
        if caminho == "areas/unidades/Ficha_Preso_index.php?id_cad_preso=77":
            return "## Texto\nCódigo\n77\nNome\nCADASTRO FICTICIO\n"
        raise AssertionError("Navegação inesperada")

    def enviar(self, number, fields, button):
        self.calls.append(("enviar", number, dict(fields), button))
        if button == "Abrir Ficha":
            return (
                "## Links\n[1] (quadro) ficha → "
                "areas/unidades/Ficha_Preso_index.php?id_cad_preso=77\n"
            )
        if button == "Pesquisar":
            return "1 REEDUCANDO(S) CADASTRADO(S)\nID 77\nCADASTRO FICTICIO\n"
        raise AssertionError("Formulário inesperado")

    def cliente(self):
        return ClienteAutenticado(
            abrir=self.abrir, sessao_http=self.session
        )


class MCPFicticio:
    def __init__(self):
        self.tools = {}
    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.user = UsuarioFicticio("U1")
        self.adapter = AdaptadorSGP2RR(self.user.cliente())

    def test_paths_permitted(self):
        valid = (
            "areas/unidades/pesquisa.php",
            "areas/unidades/pesquisa_Iframe%20CODIGO.php",
            "areas/unidades/Ficha_Preso_index.php?id_cad_preso=77",
            "areas/unidades/HistCar_LER.php?pageNum_rsHistcar=1&id_cad_preso=77",
            "areas/impressoes/UND_CertidaoCarceraria.php?id_cad_preso=77",
        )
        for path in valid:
            with self.subTest(path=path):
                _url_consulta_permitida(path)

    def test_paths_denied(self):
        blocked = (
            "https://attacker.example/exfil",
            "//attacker.example/file",
            "../admin",
            "areas/unidades/HistCar_EXC_conf.php?id_his_carc=77",
            "areas/unidades/HistCar_CAD.php?id_cad_preso=77",
            "areas/unidades/Ficha_Preso_index.php?url=https://attacker.test",
            "areas/unidades/Ficha_Preso_index.php?id_cad_preso=77&id_cad_preso=88",
            "areas/unidades/HistCar_LER.php?code=77",
            "areas/unidades/HistCar_LER.php?id_cad_preso=77%250A",
        )
        for path in blocked:
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    _url_consulta_permitida(path)

    def test_only_get_forms(self):
        with self.assertRaises(PermissionError):
            self.adapter.enviar_formulario(1, {"id": "5"}, "Gravar")
        self.adapter.abrir_pagina("areas/unidades/pesquisa.php")
        with self.assertRaises(PermissionError):
            self.adapter.enviar_formulario(
                1, {"busca1": "nome", "busca2": "x", "busca3": "", "extra": "1"},
                "Pesquisar",
            )
        text = self.adapter.enviar_formulario(
            1, {"busca1": "nome", "busca2": "NOME FICTICIO", "busca3": ""},
            "Pesquisar",
        )
        self.assertIn("1 REEDUCANDO", text)
        self.assertNotIn("enviar", [entry[0] for entry in self.user.calls])
        with self.assertRaises(PermissionError):
            self.adapter.enviar_formulario(
                1, {"busca1": "nome", "busca2": "NOME FICTICIO", "busca3": ""},
                "Pesquisar",
            )

    def test_pdf_is_downloaded_from_fixed_host_with_no_redirects(self):
        content, mime = self.adapter.baixar_anexo(
            "fotos/presos_doc/0123456789abcdef.pdf"
        )
        self.assertEqual(mime, "application/pdf")
        self.assertTrue(content.startswith(b"%PDF-"))
        url, kw = self.user.session.visitas[0]
        self.assertEqual(
            url,
            "https://www.canaime.com.br/sgp2rr/fotos/presos_doc/0123456789abcdef.pdf",
        )
        self.assertIs(kw["allow_redirects"], False)
        self.assertTrue(kw["stream"])
        self.assertTrue(self.user.session.resposta.closed)

    def test_pdf_rejects_invalid_format_and_size(self):
        with self.assertRaises(ValueError):
            self.adapter.baixar_anexo("https://elsewhere.test/f.pdf")
        self.user.session.resposta = Resposta(body=b"NOTPDF")
        with self.assertRaises(ValueError):
            self.adapter.baixar_anexo("fotos/presos_doc/0123456789abcdef.pdf")
        self.user.session.resposta = Resposta(length=MAX_PDF_BYTES + 1)
        with self.assertRaises(ValueError):
            self.adapter.baixar_anexo("fotos/presos_doc/0123456789abcdef.pdf")
        self.user.session.resposta = Resposta(status=302, mime="text/html")
        with self.assertRaises(RuntimeError):
            self.adapter.baixar_anexo("fotos/presos_doc/0123456789abcdef.pdf")

    def test_pdf_no_session_fails_closed(self):
        empty = ClienteAutenticado(self.user.abrir)
        with self.assertRaises(RuntimeError):
            AdaptadorSGP2RR(empty).baixar_anexo(
                "fotos/presos_doc/0123456789abcdef.pdf"
            )

    def test_registration_uses_client_of_each_invocation(self):
        user1 = UsuarioFicticio("U1")
        user2 = UsuarioFicticio("U2")
        current = [user1]
        mcp = MCPFicticio()
        integrar_sgp2rr(mcp, lambda: current[0].cliente())
        self.assertEqual(len(mcp.tools), 8)

        output = mcp.tools["sgp_pesquisar_cadastro"](
            "mae", "NOME FICTICIO", "CPP"
        )
        self.assertEqual(output["resultados"][0]["codigo"], 77)
        self.assertGreater(len(user1.calls), 0)
        self.assertEqual(len(user2.calls), 0)

        current[0] = user2
        result = mcp.tools["sgp_pesquisar_cadastro"]("codigo", "77")
        self.assertEqual(result["resultados"][0]["codigo"], 77)
        self.assertGreater(len(user2.calls), 0)

    def test_invalid_client_denied(self):
        mcp = MCPFicticio()
        integrar_sgp2rr(mcp, lambda: None)
        with self.assertRaises(RuntimeError):
            mcp.tools["sgp_pesquisar_cadastro"]("nome", "PESSOA FICTICIA")


if __name__ == "__main__":
    unittest.main()
