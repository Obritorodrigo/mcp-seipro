"""MCP Server genérico para o SEI (Sistema Eletrônico de Informações)."""

import asyncio
import base64
import html
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Literal

from mcp.server.fastmcp import FastMCP, Context
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from mcp_seipro.sei_client import SEIClient, SEIAcessoNegado, SEICloudflareBlocked
from mcp_seipro.sei_web_client import SEIWebClient
from mcp_seipro.shaping import shape_processo_resumido
from mcp_seipro.html_utils import (
    html_to_text, html_to_markdown,
    pdf_to_text, pdf_to_markdown,
    normalizar_entidades_html, sanitize_iso8859, corrigir_classe_ancora_sei,
)
from mcp_seipro.sei_styles import (
    SEI_STYLES, STYLE_SHORTCUTS,
    html_referencia_sei, html_destinatario,
)
from mcp_seipro import access_control

logger = logging.getLogger(__name__)

MAX_BINARY_SIZE = 10 * 1024 * 1024  # 10 MB

# Detecta modo HTTP (Railway injeta PORT)
_http_mode = bool(os.environ.get("PORT"))
_http_port = int(os.environ.get("PORT", 8000))
# Atrás de proxy reverso na mesma máquina, 127.0.0.1. Em container, use HOST=0.0.0.0.
_http_host = os.environ.get("HOST", "127.0.0.1")


@asynccontextmanager
async def lifespan(server: FastMCP):
    if _http_mode:
        # Modo HTTP stateless: o SDK recria este contexto a CADA request, então
        # ele não serve de cache. Os clientes por sessão OAuth vivem em
        # `_cache_clientes` (nível de módulo) — ver `_obter_cliente_sessao`.
        yield {"sei": None, "sei_web": None}
    else:
        # Modo stdio: clients com credenciais das env vars
        client = SEIClient()
        web_client = SEIWebClient()
        try:
            yield {"sei": client, "sei_web": web_client}
        finally:
            await client.close()
            await web_client.close()


def _exigir_url_permitida(creds: dict) -> None:
    """Revalida a URL do token contra SEI_ALLOWED_HOSTS atual.

    A validação completa (com DNS) acontece no /login; aqui é a checagem barata
    que pega tokens emitidos antes de o operador restringir a allowlist.
    """
    from mcp_seipro.seguranca import validar_url_sei_sem_rede

    erro = validar_url_sei_sem_rede(creds.get("sei_url", ""))
    if erro:
        raise ValueError(f"{erro} Reconecte o MCP.")


def _credenciais_sessao():
    """Valida o token e identifica a sessão OAuth."""
    from mcp.server.auth.middleware.auth_context import get_access_token
    from mcp_seipro.auth import _abrir_token

    access = get_access_token()
    if not access:
        raise ValueError("Autenticacao necessaria. Reconecte o MCP.")

    payload = _abrir_token(access.token)
    if not payload or payload.get("typ") != "access":
        raise ValueError("Token invalido ou expirado. Reconecte o MCP.")

    sid = payload.get("sid")
    creds = payload.get("sei")

    if not sid or not isinstance(creds, dict):
        raise ValueError("Sessao OAuth invalida.")

    _exigir_url_permitida(creds)
    return sid, creds


# ---------------------------------------------------------------------------
# Cache de clientes por sessão OAuth (modo HTTP)
# ---------------------------------------------------------------------------
# Sem cache, cada chamada de tool criaria um SEIClient novo e faria um login
# novo no SEI. A chave combina tipo + sid + hash das credenciais: duas sessões
# nunca compartilham cliente, e uma sessão cujas credenciais mudem ganha outro.
# Revogação continua valendo: `_credenciais_sessao` reabre o token a cada
# chamada, então uma sessão revogada nem chega a consultar o cache.

_CACHE_TTL = float(os.environ.get("SEI_CLIENT_CACHE_TTL", "600"))
_CACHE_MAX = int(os.environ.get("SEI_CLIENT_CACHE_MAX", "200"))
_CACHE_GRACA_FECHAR = 120.0  # s antes de fechar um cliente despejado

# chave -> [ultimo_uso (monotonic), cliente]
_cache_clientes: dict[tuple[str, str, str], list] = {}


def _hash_credenciais(creds: dict) -> str:
    import hashlib

    bruto = json.dumps(creds, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(bruto).hexdigest()


def _fechar_depois(cliente) -> None:
    """Fecha o cliente despejado após uma carência.

    A carência evita fechar um cliente que uma request concorrente ainda está
    usando (ex.: despejo por limite de tamanho sob carga).
    """
    fechar = getattr(cliente, "close", None)
    if fechar is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    def _agendar():
        async def _fechar():
            try:
                await fechar()
            except Exception:
                logger.debug("Erro ao fechar cliente SEI despejado", exc_info=True)
        loop.create_task(_fechar())

    loop.call_later(_CACHE_GRACA_FECHAR, _agendar)


def _podar_cache(agora: float) -> None:
    for chave, (ultimo_uso, cliente) in list(_cache_clientes.items()):
        if agora - ultimo_uso > _CACHE_TTL:
            del _cache_clientes[chave]
            _fechar_depois(cliente)
    excesso = len(_cache_clientes) - (_CACHE_MAX - 1)
    if excesso > 0:
        mais_antigos = sorted(_cache_clientes.items(), key=lambda kv: kv[1][0])[:excesso]
        for chave, (_, cliente) in mais_antigos:
            del _cache_clientes[chave]
            _fechar_depois(cliente)


def _obter_cliente_sessao(tipo: str, fabrica):
    """Devolve o cliente (`tipo` = 'rest' | 'web') da sessão OAuth corrente."""
    import time as _time

    sid, creds = _credenciais_sessao()
    chave = (tipo, sid, _hash_credenciais(creds))
    agora = _time.monotonic()

    entrada = _cache_clientes.get(chave)
    if entrada is not None and agora - entrada[0] > _CACHE_TTL:
        entrada = None  # expirado: _podar_cache o fecha logo abaixo
    if entrada is None:
        entrada = [agora, fabrica(creds)]
    else:
        entrada[0] = agora
    _podar_cache(agora)  # antes de reinserir: o atual nunca é despejado
    _cache_clientes[chave] = entrada
    return entrada[1]


def _get_client(ctx: Context) -> SEIClient:
    """Retorna o cliente REST: das env vars (stdio) ou da sessão OAuth (HTTP)."""
    if not _http_mode:
        client = ctx.request_context.lifespan_context.get("sei")
        if client is None:
            raise ValueError("SEIClient nao configurado. Verifique as variaveis de ambiente.")
        return client
    return _obter_cliente_sessao(
        "rest", lambda creds: SEIClient(**creds, permitir_arquivo_local=False)
    )


def _web_scraper_enabled() -> bool:
    """Indica se o scraper web (SEIWebClient) deve ser usado nas tools híbridas.

    Padrão: DESLIGADO. Desde a migração SEI 5 da ANTAQ, o login web virou SSO
    Microsoft (Entra ID) e o scraper httpx não consegue mais logar — por isso as
    tools de listagem rodam por REST por padrão. Quem quiser reativar o scraper
    (ex.: após configurar persistência de sessão) liga SEI_WEB_SCRAPER=1.
    """
    return os.environ.get("SEI_WEB_SCRAPER", "").strip().lower() in ("1", "true", "yes", "on")


def _get_web_client(ctx: Context) -> SEIWebClient:
    """Retorna o cliente web (scraper): stdio ou isolado por sessão OAuth."""
    if not _http_mode:
        client = ctx.request_context.lifespan_context.get("sei_web")
        if client is None:
            raise ValueError("SEIWebClient nao configurado.")
        return client
    return _obter_cliente_sessao("web", lambda creds: SEIWebClient(**creds))


_http_kwargs = {}
if _http_mode:
    from pydantic import AnyHttpUrl
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
    from mcp_seipro.auth import SEIProOAuthProvider

    _base_url = os.environ.get("BASE_URL", f"http://localhost:{_http_port}")
    _provider = SEIProOAuthProvider()

    _http_kwargs = {
        "host": _http_host,
        "port": _http_port,
        "stateless_http": True,
        "transport_security": TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
        "auth": AuthSettings(
            issuer_url=AnyHttpUrl(_base_url),
            resource_server_url=AnyHttpUrl(f"{_base_url}/mcp"),
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
        ),
        "auth_server_provider": _provider,
    }

mcp = FastMCP(
    "sei",
    **_http_kwargs,
    instructions=(
        "MCP Server para o SEI (Sistema Eletrônico de Informações). "
        "Permite gerenciar processos, documentos, tramitação e assinatura. "
        "ASSINATURA: sei_assinar_documento usa a sessão já autenticada no servidor "
        "e não recebe login nem senha — só o id do documento e o cargo. Chamada "
        "sem cargo devolve a lista de cargos disponíveis para o usuário escolher. "
        "Fluxo típico: sei_trocar_unidade → sei_listar_processos → "
        "sei_consultar_processo (obter IdProcedimento) → sei_arvore_processo → "
        "sei_ler_documento. Para criar docs: sei_pesquisar_tipos_documento → "
        "sei_criar_documento → sei_listar_secoes → sei_editar_secao. "
        "Ao gerar HTML para documentos, use as classes CSS padronizadas do SEI. "
        "DESPACHOS: Texto_Alinhado_Esquerda com âncora SEI no destinatário "
        "(<span class='ancoraSei interessadoSeiPro' data-id='ID_UNIDADE'>SIGLA - Nome</span>) "
        "para vincular à unidade na tramitação. "
        "Texto_Justificado+<strong> (assunto), "
        "Paragrafo_Numerado_Nivel1 (corpo, autonumera 1. 2. 3.), "
        "Texto_Justificado_Recuo_Primeira_Linha (fecho), "
        "Texto_Centralizado_Maiusculas (signatário), Texto_Centralizado (cargo). "
        "NOTAS TÉCNICAS e PARECERES: Item_Nivel1/2/3/4 para títulos de seção "
        "(equivalem a H1/H2/H3/H4 ou #/##/###/####, autonumeram 1. 1.1. 1.1.1.), "
        "Paragrafo_Numerado_Nivel1 para parágrafos do corpo, "
        "Item_Alinea_Letra para alíneas (autonumera a, b, c — NUNCA escrever a) b) no texto), "
        "Item_Inciso_Romano para incisos (autonumera I, II, III — NUNCA escrever I - II - no texto). "
        "REGRA: toda numeração/enumeração deve usar as classes CSS, nunca texto manual. "
        "Use sei_estilos para consultar todos os estilos disponíveis. "
        "Ao citar documentos SEI no texto, use sei_gerar_referencia para "
        "gerar hiperlinks dinâmicos (<a class='ancoraSei'>) que o SEI "
        "renderiza como links clicáveis na interface web. "
        "IMPORTANTE: Quando o usuário mencionar 'SEI XXXX', 'SEI nº XXXX' ou "
        "'número SEI XXXX', use sei_ler_documento diretamente com o número — "
        "a tool resolve automaticamente o id interno via pesquisa Solr. "
        "Para buscar sem ler, use sei_buscar_documento. "
        "Quando o usuário pedir para ver documentos/árvore de um processo, "
        "use sei_arvore_processo e apresente como tabela markdown. Use emojis "
        "para tipo de documento: 📄 = Interno (HTML), 📎 = Externo (PDF). "
        "Colunas: #, 📄/📎, Tipo do Documento, Protocolo, Unidade, Tamanho, "
        "✍️ Assinado, 🚫 Cancelado, 👁 Visualizar, 🔒 Bloqueado. "
        "Use ✅ para sim e · para não. Se houver múltiplos volumes "
        "(campo total_volumes > 1), separe visualmente por volume. "
        "VERSÃO: Todos os endpoints funcionam com mod-wssei 2.0.0+ (SEI 4.0.x+), "
        "exceto sei_listar_relacionamentos que requer mod-wssei 3.0.2+ (SEI 5.0.x). "
        "Compatibilidade: SEI 4.0.x→mod-wssei 2.0.x | SEI 4.1.1→2.2.0 | SEI 5.0.x→3.0.x. "
        "Se um endpoint falhar com erro inesperado (404, método não encontrado), "
        "use sei_versao para verificar a versão e informe ao usuário qual versão "
        "do SEI/mod-wssei é necessária. Pergunte a versão do SEI ao usuário caso precise."
    ),
    lifespan=lifespan,
)


class _ConsentimentoRestrito(BaseModel):
    """Schema de elicitInput para consentimento de acesso a documento restrito."""
    autorizo_acesso: bool = Field(
        default=False,
        description=(
            "Marque para autorizar a leitura do conteúdo restrito. Ao marcar, "
            "você declara ciência dos riscos de LGPD/LAI/sigilo e assume "
            "responsabilidade pelo compartilhamento da informação fora do SEI."
        ),
    )


_ELICIT_TIMEOUT_S = float(os.environ.get("SEI_ELICIT_TIMEOUT_S", "30"))


def _cliente_suporta_elicit(ctx: Context) -> bool:
    """Verifica via MCP capabilities se o cliente declarou suporte a elicit."""
    if ctx is None:
        return False
    try:
        caps = ctx.request_context.session.client_params.capabilities  # type: ignore[attr-defined]
    except Exception:
        return False
    return getattr(caps, "elicitation", None) is not None


async def _solicitar_consentimento_via_elicit(
    ctx: Context,
    nivel: str,
    rotulo: str,
    hipotese: str | None,
    alvo: dict,
) -> str:
    """Solicita consentimento ao usuário via MCP elicitInput.

    Retorna:
      - "aceitou": usuário marcou autorizo_acesso=True
      - "recusou": usuário rejeitou ou desmarcou
      - "nao_suportado": cliente MCP não implementa elicitInput, ou não
        respondeu dentro de SEI_ELICIT_TIMEOUT_S — cair no fallback JSON
    """
    if not _cliente_suporta_elicit(ctx):
        return "nao_suportado"

    riscos_txt = "\n".join(f"• {r}" for r in access_control.riscos_padrao())
    hl_txt = f"\nHipótese legal: {hipotese}" if hipotese else ""
    alvo_txt = ""
    if alvo.get("tipo") == "documento":
        alvo_txt = f"\nDocumento: id {alvo.get('id')} (tipo {alvo.get('tipo_documento','?')})"
    elif alvo.get("tipo") == "processo":
        alvo_txt = f"\nProcesso: {alvo.get('protocolo')}"

    message = (
        f"⚠ Documento/processo classificado como {rotulo} no SEI.{hl_txt}{alvo_txt}\n\n"
        f"Riscos:\n{riscos_txt}\n\n"
        "Marque a opção abaixo para autorizar a leitura do conteúdo bruto. "
        "Se não autorizar, o MCP retornará apenas um aviso ao modelo."
    )

    try:
        result = await asyncio.wait_for(
            ctx.elicit(message=message, schema=_ConsentimentoRestrito),
            timeout=_ELICIT_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"elicit timeout após {_ELICIT_TIMEOUT_S}s — cliente não respondeu, "
            "caindo no fallback JSON"
        )
        return "nao_suportado"
    except Exception as e:
        logger.debug(f"elicit falhou ({type(e).__name__}: {e}) — fallback JSON")
        return "nao_suportado"

    if result.action == "accept" and result.data and result.data.autorizo_acesso:
        return "aceitou"
    return "recusou"


async def _aplicar_gate_documento(
    ctx: Context,
    client: SEIClient,
    id_documento: str,
    tipo_documento: str,
    confirmou: bool,
) -> tuple[str, dict | None, str]:
    """Resolve metadados e aplica o gate de acesso para um documento.

    Retorna (acao, payload, erro):
      - acao="liberar": prossiga; payload é o disclaimer acompanhante (ou None
        se público)
      - acao="bloquear": retorne payload (JSON de bloqueio) ao caller
      - acao="recusou": retorne payload (JSON de recusa) ao caller
      - acao="erro": retorne erro (string) ao caller
    """
    try:
        if tipo_documento == "X":
            meta = await client.consultar_documento_externo(id_documento)
        else:
            meta = await client.consultar_documento_interno(id_documento)
    except Exception as e:
        msg = str(e)
        low = msg.lower()
        if "não autorizado" in low or "nao autorizado" in low:
            return ("erro", None, (
                f"SEI retornou 'não autorizado' para o id {id_documento!r}. "
                "Verifique se você passou o id INTERNO do documento (ex.: 3149544) "
                "e não o número SEI / protocoloFormatado (ex.: 2867926). "
                "Se tiver apenas o número SEI, use sei_buscar_documento ou "
                "sei_ler_documento (que faz auto-resolução)."
            ))
        return ("erro", None, f"Falha ao consultar metadados: {msg}")

    nivel, hipotese = access_control.extrair_nivel(meta)
    alvo = {"tipo": "documento", "id": str(id_documento), "tipo_documento": tipo_documento}

    if not access_control.precisa_disclaimer(nivel):
        return ("liberar", None, "")

    if confirmou or access_control.env_permite_restritos():
        return (
            "liberar",
            access_control.construir_disclaimer_acompanhante(nivel, hipotese, alvo),
            "",
        )

    rotulo = access_control.ROTULOS.get(nivel, "Restrito")
    consent = await _solicitar_consentimento_via_elicit(ctx, nivel, rotulo, hipotese, alvo)

    if consent == "aceitou":
        return (
            "liberar",
            access_control.construir_disclaimer_acompanhante(nivel, hipotese, alvo),
            "",
        )
    if consent == "recusou":
        return (
            "recusou",
            {
                "tipo_resposta": "consentimento_recusado",
                "mensagem_para_usuario_humano": (
                    f"Acesso ao conteúdo {rotulo.lower()} NÃO foi autorizado pelo "
                    "usuário humano. Nenhum conteúdo bruto foi entregue ao modelo."
                ),
                "instrucao_para_modelo": (
                    "O usuário humano recusou expressamente o acesso ao conteúdo "
                    "restrito via MCP elicitInput. NÃO tente caminhos alternativos "
                    "(troca de unidade, outras tools de leitura, IDs alternativos). "
                    "Confirme ao usuário que a recusa foi registrada e ofereça "
                    "ações que não dependam do conteúdo bruto."
                ),
                "alvo": alvo,
                "nivel_acesso": nivel,
            },
            "",
        )
    return (
        "bloquear",
        access_control.construir_aviso_bloqueio(nivel, hipotese, alvo),
        "",
    )


async def _resolver_processo(client: SEIClient, referencia: str) -> str:
    """Resolve uma referência de processo para o IdProcedimento.

    Aceita:
    - IdProcedimento numérico (ex: "683589") — usa direto
    - Protocolo formatado (ex: "50300.018905/2018-67") — consulta na API

    Retorna o IdProcedimento (str).
    """
    referencia = referencia.strip()
    # Se contém ponto ou barra, é protocolo formatado
    if "." in referencia or "/" in referencia:
        ultimo_erro = None
        for candidato in _variantes_protocolo(referencia):
            try:
                proc = await client.consultar_processo(candidato)
            except Exception as e:  # formato não reconhecido pelo órgão
                ultimo_erro = e
                continue
            id_proc = str((proc or {}).get("IdProcedimento") or "")
            if id_proc:
                return id_proc
        if ultimo_erro:
            raise ultimo_erro
        raise ValueError(f"Processo {referencia} não encontrado.")
    return referencia


def _variantes_protocolo(referencia: str) -> list[str]:
    """O número como digitado e com o separador do dígito trocado.

    Cada órgão formata o dígito verificador de um jeito: ANTAQ usa
    `50300.018905/2018-67`, Roraima `35101.001300/2026.08`. Quem digita
    costuma usar o formato de outro órgão; a consulta do SEI só acha o exato.
    """
    variantes = [referencia]
    if "/" in referencia:
        prefixo, sufixo = referencia.rsplit("/", 1)
        for de, para in (("-", "."), (".", "-")):
            if de in sufixo:
                ano, digito = sufixo.rsplit(de, 1)
                variantes.append(f"{prefixo}/{ano}{para}{digito}")
                break
    return variantes


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _classificar_erro(msg: str) -> dict:
    """Rotula a origem de um erro para que o agente não trate 403 do WAF como 403 de login.

    Os dois chegam como "403" e pedem ações OPOSTAS: o da borda não melhora com
    outra credencial; o do SEI não melhora com transporte/bypass. Os marcadores
    abaixo vêm das mensagens que o próprio sei_client escreve.
    """
    m = msg.lower()
    if "regra de waf" in m or "bloqueio de waf" in m or "managed rule" in m:
        return {
            "erro_origem": "cloudflare_waf",
            "erro_acao": "O corpo do POST casou uma managed rule na borda. Repetir "
                         "com outra credencial ou outro transporte não muda nada; "
                         "reduza/simplifique o conteúdo enviado (dry_run ajuda a "
                         "inspecionar) ou peça exceção de WAF ao órgão.",
        }
    if "desafio do cloudflare" in m or "managed challenge" in m or "pela borda" in m:
        return {
            "erro_origem": "cloudflare_borda",
            "erro_acao": "A requisição não chegou ao SEI. Não é erro de credencial. "
                         "Use SEI_TRANSPORT=browser (Playwright), cookie "
                         "SEI_CF_CLEARANCE ou regra de bypass no WAF do órgão.",
        }
    if "autenticação/permissão" in m or "não autorizado" in m or "nao autorizado" in m:
        return {
            "erro_origem": "sei_acesso",
            "erro_acao": "O SEI recebeu a requisição e recusou. Confira a unidade "
                         "atual (sei_trocar_unidade) e as permissões do usuário "
                         "sobre o protocolo — transporte/bypass não têm efeito aqui.",
        }
    return {}


def _error(msg: str) -> str:
    return json.dumps({"error": msg, **_classificar_erro(msg)}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tools de unidade e usuário
# ---------------------------------------------------------------------------


@mcp.tool()
async def sei_listar_unidades(filtro: str = "", ctx: Context = None) -> str:
    """Lista as unidades às quais o usuário autenticado tem acesso no SEI.

    Retorna id, sigla, nome e `atual` (unidade ativa da sessão). Use o id
    para trocar de unidade com sei_trocar_unidade.

    - filtro: parte da sigla ou do nome (ignora acento e maiúsculas)
    """
    try:
        client = _get_client(ctx)
        unidades = await client.listar_unidades_usuario()
        termos = _normalizar_busca(filtro).split()
        if termos:
            unidades = [
                u for u in unidades
                if all(t in _normalizar_busca(f"{u.get('sigla', '')} {u.get('nome', '')}") for t in termos)
            ]
        return _json({"total": len(unidades), "unidades": unidades})
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_trocar_unidade(id_unidade: str, ctx: Context = None) -> str:
    """Troca a unidade ativa do usuário no SEI e a lembra para as próximas sessões.

    Aceita o id da unidade (ex.: "110001335") ou a sigla (ex.: "SEJUC/DEPLAF",
    sem diferenciar acento/maiúsculas). A unidade escolhida vira a unidade de
    trabalho deste usuário: as próximas conversas já começam nela. Use
    sei_listar_unidades para ver as disponíveis.
    """
    try:
        client = _get_client(ctx)
        alvo = str(id_unidade).strip()
        sigla = None
        if not alvo.isdigit():
            unidades = await client.listar_unidades_usuario()
            procurado = _normalizar_busca(alvo)
            exatas = [u for u in unidades if _normalizar_busca(str(u.get("sigla") or "")) == procurado]
            if len(exatas) != 1:
                parecidas = [u for u in unidades
                             if procurado in _normalizar_busca(f"{u.get('sigla', '')} {u.get('nome', '')}")]
                if len(parecidas) == 1:
                    exatas = parecidas
                else:
                    return _json({
                        "error": (f"Unidade {alvo!r} não encontrada entre as suas." if not parecidas
                                  else f"Mais de uma unidade corresponde a {alvo!r}."),
                        "unidades_disponiveis": parecidas or unidades,
                        "dica": "Nada foi alterado. Informe o `id` ou a sigla exata.",
                    })
            alvo, sigla = str(exatas[0].get("id")), exatas[0].get("sigla")
        result = await client.trocar_unidade(alvo)
        client.lembrar_unidade(alvo)
        if isinstance(result, dict):
            result["id_unidade"] = alvo
            if sigla:
                result["sigla"] = sigla
            result["lembrada"] = True
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_pesquisar_unidades(
    filtro: str = "",
    limit: int = 50,
    pagina: int = 0,
    ctx: Context = None,
) -> str:
    """Pesquisa unidades disponíveis no SEI por nome ou sigla.

    Útil para encontrar o ID de uma unidade destino ao tramitar processos.
    Paginação: pagina=0 é a primeira página, pagina=1 a segunda, etc.
    """
    try:
        client = _get_client(ctx)
        result = await client.pesquisar_unidades(filtro=filtro, limit=limit, start=pagina)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_listar_usuarios(
    filtro: str = "",
    apenas_unidade: bool = True,
    ctx: Context = None,
) -> str:
    """Lista usuários no SEI, com filtro por nome ou sigla.

    - apenas_unidade=true (padrão): só usuários com permissão na unidade
      atual — ideal para atribuição de processos
    - apenas_unidade=false: todos os usuários do órgão

    Use o campo id_usuario retornado para sei_atribuir_processo.
    """
    try:
        client = _get_client(ctx)
        result = await client.listar_usuarios(filtro=filtro, apenas_unidade=apenas_unidade)
        return _json(result)
    except Exception as e:
        return _error(str(e))


# ---------------------------------------------------------------------------
# Tools de leitura
# ---------------------------------------------------------------------------


@mcp.tool()
async def sei_consultar_processo(protocolo_formatado: str, ctx: Context) -> str:
    """Consulta um processo SEI pelo número de protocolo formatado.

    Exemplo de protocolo: 50300.000123/2025-00

    Por padrão usa só a REST mod-wssei (campos estruturados). Com
    SEI_WEB_SCRAPER=1, roda também o scraper web em paralelo (asyncio.gather)
    para anexar documentos[]/relacionados[] — inativo desde o SSO Microsoft.

    Campos da REST (`/processo/consultar` + `/processo/consultar/{id}`):
    - IdProcedimento, ProtocoloProcedimentoFormatado, NomeTipoProcedimento
    - especificacao, assuntos[], interessados[], observacoes[]
    - nivelAcesso, hipoteseLegal, grauSigilo

    Campos do scraper web (`procedimento_visualizar` / arvore_montar.php):
    - documentos[]: lista completa de documentos com id, label, tipo
    - relacionados[]: processos relacionados (cards na sidebar)

    Se o scraper web falhar (ex: processo não está na inbox da unidade atual),
    a tool ainda retorna os campos REST. Se a REST falhar, retorna pelo menos
    o que o scraper conseguiu extrair.

    Quando o processo é restrito ou sigiloso (nivelAcesso 1 ou 2), a resposta
    inclui o campo `_aviso_acesso` — um aviso INFORMATIVO de privacidade,
    NÃO um erro de permissão. Os metadados foram retornados com sucesso.
    """
    try:
        client = _get_client(ctx)
        merged: dict = {}
        warnings: list[str] = []

        if _web_scraper_enabled():
            # Modo híbrido (opt-in): REST + scraper web em paralelo.
            web = _get_web_client(ctx)
            if web._inbox_url is None:
                try:
                    await web.login()
                except Exception as e:
                    logger.warning(f"web login falhou, seguindo só com REST: {e}")
            rest_task = asyncio.create_task(client.consultar_processo_completo(protocolo_formatado))
            web_task = asyncio.create_task(web.consultar_processo(protocolo_formatado))
            rest_result, web_result = await asyncio.gather(
                rest_task, web_task, return_exceptions=True
            )
            if isinstance(rest_result, Exception):
                warnings.append(f"REST falhou: {rest_result}")
            else:
                merged.update(rest_result)
            if isinstance(web_result, Exception):
                warnings.append(f"Web scraper falhou: {web_result}")
            else:
                # Web traz documentos[], relacionados[] e id_procedimento (snake_case).
                # Não sobrescreve campos da REST (fonte canônica); web complementa.
                for k, v in web_result.items():
                    if k not in merged:
                        merged[k] = v
        else:
            # REST puro (padrão). Para a lista de documentos use sei_listar_documentos.
            merged.update(await client.consultar_processo_completo(protocolo_formatado))

        if not merged:
            return _error("Consulta do processo falhou: " + " | ".join(warnings))

        if warnings:
            merged["_warnings"] = warnings

        nivel, hipotese = access_control.extrair_nivel(merged)
        if access_control.precisa_disclaimer(nivel):
            merged["_aviso_acesso"] = access_control.construir_disclaimer_acompanhante(
                nivel, hipotese,
                alvo={"tipo": "processo", "protocolo": protocolo_formatado},
            )

        return _json(merged)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_arvore_processo(
    protocolo_formatado: str,
    ctx: Context = None,
) -> str:
    """Mostra a árvore (lista) de documentos de um processo SEI.

    Aceita o protocolo formatado (ex: 50300.000123/2025-00) ou IdProcedimento.

    Para ler o conteúdo de um documento, use sei_ler_documento com o id.

    Por padrão usa a REST (`/documento/listar/{id}`). O scraper web (mais
    rápido, mas inativo desde o SSO Microsoft da ANTAQ) só é usado se
    SEI_WEB_SCRAPER=1.
    """
    try:
        if _web_scraper_enabled():
            web = _get_web_client(ctx)
            if web._inbox_url is None:
                await web.login()
            return _json(await web.listar_documentos(protocolo_formatado))
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, protocolo_formatado)
        docs = await client.listar_documentos(id_proc, limit=200)
        return _json({"id_procedimento": id_proc, "documentos": docs, "total": len(docs)})
    except Exception as e:
        return _error(str(e))


def _shape_documento_resumido(doc: dict) -> dict:
    """List view enxuta de um documento (o payload completo estoura a janela).

    Mantém o que identifica e localiza o documento; o resto sai. Para os
    metadados completos de um documento específico use
    sei_consultar_documento_interno / sei_consultar_documento_externo.
    """
    attrs = doc.get("atributos", {}) or {}
    status = attrs.get("status", {}) or {}
    nivel_bruto = "publico"
    if str(status.get("documentoSigiloso")) == "S":
        nivel_bruto = "sigiloso"
    elif str(status.get("documentoRestrito")) == "S":
        nivel_bruto = "restrito"
    return {
        "id": str(doc.get("id", "")),
        "protocoloFormatado": attrs.get("protocoloFormatado", ""),
        "tipo": attrs.get("tipo", ""),
        # staDocumento do core SEI: I=editor interno, X=externo; formulários e
        # e-mail usam outras letras (F, A, …) — não assumir só I/X ao filtrar.
        "tipo_documento": attrs.get("tipoDocumento", ""),
        "unidade": attrs.get("siglaUnidade", ""),
        "nome": attrs.get("nomeComposto") or attrs.get("nome", ""),
        "assinado": str(status.get("documentoAssinado")) == "S",
        "cancelado": str(status.get("documentoCancelado")) == "S",
        "acesso": nivel_bruto,
    }


@mcp.tool()
async def sei_listar_documentos(
    protocolo_formatado: str,
    ordem: Literal["asc", "desc"] = "asc",
    limite: int = 50,
    offset: int = 0,
    resumido: bool = True,
    ctx: Context = None,
) -> str:
    """Lista os documentos de um processo SEI, com ordenação e paginação.

    Aceita o protocolo formatado (ex: 50300.000123/2025-00) ou o IdProcedimento.

    - ordem: 'asc' (ordem da árvore, o padrão do SEI) ou 'desc' (mais recentes
      primeiro). Em processo antigo e volumoso, 'desc' é como achar o documento
      que você acabou de criar sem varrer a lista inteira.
    - limite/offset: recorte da lista já ordenada (offset é em ITENS, não página).
    - resumido: True (padrão) devolve id, protocoloFormatado, tipo, unidade,
      nome, flags de assinado/cancelado e nível de acesso. False devolve o
      payload bruto da wssei — completo, porém grande o bastante para estourar a
      janela de contexto em processos com muitos documentos.

    A resposta traz `total` (do servidor), `retornados` e `truncado`. Para ler o
    conteúdo de um documento, use sei_ler_documento com o `id`. A listagem não
    inclui data — ela vem de sei_consultar_documento_interno (`dataElaboracao`).

    Nota: com SEI_WEB_SCRAPER=1 a listagem vem do scraper web e estes parâmetros
    de ordem/paginação não se aplicam (o scraper devolve a árvore inteira).
    """
    try:
        if _web_scraper_enabled():
            web = _get_web_client(ctx)
            if web._inbox_url is None:
                await web.login()
            return _json(await web.listar_documentos(protocolo_formatado))

        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, protocolo_formatado)

        # A listagem do wssei é sempre ASC por sequência e sem parâmetro de
        # ordem: para 'desc' ou offset alto é preciso ter a coleção inteira.
        pagina_unica = ordem == "asc" and offset + limite <= 200
        if pagina_unica:
            dados = await client.listar_documentos_pagina(id_proc, limit=200, start=0)
            docs, total, truncado = dados["documentos"], dados["total"], False
        else:
            dados = await client.listar_documentos_todos(id_proc)
            docs, total, truncado = dados["documentos"], dados["total"], dados["truncado"]

        if ordem == "desc":
            docs = list(reversed(docs))
        recorte = docs[offset:offset + limite] if limite > 0 else docs[offset:]

        return _json({
            "id_procedimento": id_proc,
            "total": total,
            "retornados": len(recorte),
            "offset": offset,
            "ordem": ordem,
            "truncado": truncado,
            "documentos": [_shape_documento_resumido(d) for d in recorte]
            if resumido else recorte,
        })
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_buscar_documento(
    numero_sei: str,
    processo: str = "",
    ctx: Context = None,
) -> str:
    """Busca um documento pelo número SEI (ex: SEI 2843449, SEI nº 2843449).

    O número SEI é o protocoloFormatado que o usuário vê no sistema.
    A API do SEI não busca documentos diretamente por esse número,
    então esta tool usa a estratégia:

    1. Se processo informado: busca direto nesse processo (rápido).
       Aceita protocolo formatado (ex: 50300.018905/2018-67) ou IdProcedimento.
    2. Se não: pesquisa o número via busca textual (Solr) para encontrar
       o processo, depois lista os documentos para localizar o id interno

    Retorna o documento com seu id interno (necessário para sei_ler_documento),
    tipo, metadados e o processo onde está.
    """
    try:
        client = _get_client(ctx)
        numero_sei = numero_sei.strip()

        def _match(proto: str) -> bool:
            return proto == numero_sei or proto.lstrip("0") == numero_sei.lstrip("0")

        # Estratégia 1: processo conhecido → busca direto
        if processo:
            id_procedimento = await _resolver_processo(client, processo)
            docs = await client.listar_documentos(id_procedimento, limit=200)
            for d in docs:
                proto = d.get("atributos", {}).get("protocoloFormatado", "")
                if _match(proto):
                    return _json({
                        "encontrado": True,
                        "id_procedimento": id_procedimento,
                        "documento": d,
                    })
            return _json({
                "encontrado": False,
                "mensagem": f"SEI {numero_sei} não encontrado no processo {id_procedimento}",
            })

        # Estratégia 2: pesquisa textual (Solr) para achar o processo
        result = await client.pesquisar_processos(palavras_chave=numero_sei, limit=20)
        processos_candidatos = result.get("processos", [])

        for p in processos_candidatos:
            id_proc = str(p.get("idProcedimento", ""))
            if not id_proc:
                continue
            try:
                docs = await client.listar_documentos(id_proc, limit=200)
                for d in docs:
                    proto = d.get("atributos", {}).get("protocoloFormatado", "")
                    if _match(proto):
                        return _json({
                            "encontrado": True,
                            "processo": p.get("protocoloFormatadoProcedimento", ""),
                            "id_procedimento": id_proc,
                            "documento": d,
                        })
            except Exception:
                continue

        return _json({
            "encontrado": False,
            "processos_pesquisados": len(processos_candidatos),
            "mensagem": f"SEI {numero_sei} não encontrado via pesquisa textual",
            "dica": "A pesquisa Solr pode não indexar esse documento. "
                    "Informe o número do processo (id_procedimento) para busca direta, "
                    "ou use sei_arvore_processo com o protocolo do processo.",
        })
    except Exception as e:
        return _error(str(e))


async def _resolver_documento(client: SEIClient, referencia: str) -> tuple[str, str]:
    """Resolve uma referência de documento para (id_interno, tipo_documento).

    Aceita:
    - id interno numérico (ex: "3121831") — usa direto
    - número SEI / protocoloFormatado (ex: "2843449") — pesquisa via Solr

    Estratégia:
    1. Consulta direta por protocoloFormatado (`/documento/interno/formatado/
       consultar`) — resolve no banco, então funciona inclusive para documento
       recém-criado que o Solr ainda não indexou.
    2. Pesquisa Solr (caminho histórico, cobre casos que a consulta direta não pega)
    3. Tenta como id interno direto

    Retorna (id_documento, tipo_documento) ou levanta exceção.
    """
    referencia = referencia.strip()

    # Estratégia 1: consulta direta pelo número SEI (não depende de indexação)
    numero_existe_sem_acesso = None
    if referencia.isdigit():
        try:
            doc = await client.consultar_documento_interno_formatado(referencia)
            id_interno = str(doc.get("idDocumento") or "")
            if id_interno:
                tipo = "I"
                # A consulta por protocolo não devolve o discriminador I/X; ele
                # vem da listagem do processo (campo `protocolo` = idProcedimento).
                id_proc = str(doc.get("protocolo") or "")
                if id_proc:
                    try:
                        itens = await client.listar_documentos(id_proc, limit=200)
                        for d in itens:
                            if str(d.get("id")) == id_interno:
                                tipo = d.get("atributos", {}).get("tipoDocumento", "I")
                                break
                    except Exception:
                        pass
                return id_interno, tipo
        except Exception as e:
            # consultarDocumentoInternoFormatado acha o número em QUALQUER
            # unidade e só depois checa acesso: "não encontrado" = o número SEI
            # não existe; outro erro = existe, mas a unidade atual não o vê.
            msg = str(e)
            if "não encontrado" not in msg.lower() and "nao encontrado" not in msg.lower():
                numero_existe_sem_acesso = msg

    # Estratégia 2: Pesquisa Solr (mais confiável, evita confusão id/proto)
    try:
        result = await client.pesquisar_processos(palavras_chave=referencia, limit=20)
        processos = result.get("processos", [])

        for p in processos:
            id_proc = str(p.get("idProcedimento", ""))
            if not id_proc:
                continue
            try:
                docs = await client.listar_documentos(id_proc, limit=200)
                for d in docs:
                    proto = d.get("atributos", {}).get("protocoloFormatado", "")
                    if proto == referencia or proto.lstrip("0") == referencia.lstrip("0"):
                        doc_id = str(d["id"])
                        tipo = d.get("atributos", {}).get("tipoDocumento", "I")
                        return doc_id, tipo
            except Exception:
                continue
    except Exception:
        pass

    # O número é de um documento que existe mas não está acessível na unidade
    # atual. Tratá-lo como id interno acertaria OUTRO documento (números SEI e
    # ids internos têm a mesma ordem de grandeza): visto ao vivo, "24847106"
    # caiu num Empenho de outra secretaria.
    if numero_existe_sem_acesso:
        raise ValueError(
            f"O documento SEI {referencia} existe, mas a unidade atual não tem acesso a ele "
            f"({numero_existe_sem_acesso}). Troque para a unidade do processo com "
            "sei_trocar_unidade e tente de novo."
        )

    # Estratégia 3: Tentar como id direto (para quando o usuário informa o id interno)
    # Só tenta se o Solr não encontrou nada — para evitar confusão
    # entre protocoloFormatado e id (são números diferentes no SEI)
    try:
        raw = await client.visualizar_documento_interno(referencia)
        # Validar que realmente retornou conteúdo (não erro mascarado)
        if raw and len(raw) > 10:
            return referencia, "I"
    except Exception as e:
        msg = str(e)
        # "não autorizado" pode significar que o id existe mas sem permissão
        # OU que o protocoloFormatado coincidiu com outro id — não confiável
        if "não autorizado" not in msg.lower() and "nao autorizado" not in msg.lower():
            pass  # Erro diferente, tentar externo

    # Não tentar como externo automaticamente — risco alto de confusão id/proto
    # O fallback para externo só deve ser usado com id_procedimento conhecido

    raise Exception(
        f"Documento '{referencia}' não encontrado via pesquisa. "
        "Se é um documento recém-criado, o Solr pode não ter indexado ainda. "
        "Use sei_arvore_processo com o protocolo do processo para encontrá-lo."
    )


def _secao_regenerada_pelo_sei(secao: dict) -> bool:
    """Seção cujo conteúdo o SEI reconstrói sozinho — o que enviamos é descartado.

    `EditorRN::montarConteudoSecao` (core do SEI; verificado idêntico em 3.1.7 e
    5.0.x): quando SinDinamica == 'S' o conteúdo gravado vem de ConteudoOriginal,
    lido do banco, com as tags substituídas — `getStrConteudo()`, que é o que veio
    no POST, nem chega a ser consultado. Enviar essas seções VAZIAS é portanto
    lossless, e tira do corpo do POST bytes que não são nossos: o brasão em
    base64, o rodapé e o título do template.

    O critério é SinDinamica, NÃO somenteLeitura. Uma seção somenteLeitura='S'
    com SinDinamica='N' cai no ramo `else` e grava o que enviamos — esvaziá-la
    apagaria a seção do documento.
    """
    return str(secao.get("DinamicaSecaoDocumento") or "") == "S"


_RE_STYLE_ATTR = re.compile(r"""(style\s*=\s*)(["'])(.*?)\2""", re.IGNORECASE | re.DOTALL)
_RE_COR_HEX = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")


def _converter_cores_hex(conteudo: str) -> tuple[str, int]:
    """Troca cores hexadecimais CSS por `rgb()` equivalente. Renderiza idêntico.

    Medido em produção: o managed ruleset do Cloudflare pontua o `#` dentro de
    atributo como marcador de comentário SQL e barra o POST inteiro. Basta o
    próprio template do SEI (`border-top:medium double #333`) ou um `color:#000000`
    vindo de conteúdo colado para o documento ficar inescrevível pela API.

    A troca é restrita ao VALOR de atributos `style` — de propósito. Um regex solto
    casaria entidades numéricas (`&#233;` → `&#233` + `;`) e corromperia o texto.

    Devolve (conteúdo, quantidade de cores trocadas).
    """
    trocas = 0

    def _rgb(m: re.Match) -> str:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        return f"rgb({r},{g},{b})"

    def _no_style(m: re.Match) -> str:
        nonlocal trocas
        valor, n = _RE_COR_HEX.subn(_rgb, m.group(3))
        trocas += n
        return f"{m.group(1)}{m.group(2)}{valor}{m.group(2)}"

    return _RE_STYLE_ATTR.sub(_no_style, conteudo), trocas


# Versão que não pode existir. EditorRN::adicionarVersaoInterno compara `versao`
# com a última do documento e lança a validação ANTES de qualquer escrita, então
# uma sonda com esta versão atravessa o WAF com o corpo real e o SEI a recusa sem
# tocar no documento. É o que torna a localização do gatilho segura.
_VERSAO_SONDA_WAF = "999999999"
_MAX_SONDAS_WAF = 14


async def _waf_localizar_gatilho(
    client: SEIClient, doc_id: str, secoes_enviar: list[dict]
) -> dict:
    """Descobre, SEM GRAVAR NADA, qual seção (e qual trecho) o WAF está barrando.

    Cada sonda repete o POST real trocando só a `versao` pela `_VERSAO_SONDA_WAF`:
    o corpo inteiro passa pela inspeção da borda, e o SEI recusa por versão antes
    de escrever. Primeiro descobre a seção culpada esvaziando uma de cada vez;
    depois bissecta por prefixo dentro dela.

    Best-effort e limitado a `_MAX_SONDAS_WAF` requisições: diagnóstico nunca é
    obrigatório, e falhar aqui não pode piorar o erro original.
    """
    sondas = 0

    async def bloqueia(secoes: list[dict]) -> bool:
        """True = barrado na borda; False = chegou ao SEI (recusa por versão)."""
        nonlocal sondas
        sondas += 1
        try:
            await client.alterar_secao_documento(
                id_documento=doc_id, secoes=secoes, versao=_VERSAO_SONDA_WAF,
            )
        except SEICloudflareBlocked:
            return True
        except Exception:
            return False  # erro do próprio SEI ⇒ o corpo passou pelo WAF
        return False

    def com(idx: int, conteudo: str) -> list[dict]:
        return [
            {**sec, "conteudo": conteudo} if i == idx else sec
            for i, sec in enumerate(secoes_enviar)
        ]

    try:
        candidatos = [i for i, s in enumerate(secoes_enviar) if s["conteudo"]]
        culpada = None
        for i in candidatos:
            if sondas >= _MAX_SONDAS_WAF:
                break
            if not await bloqueia(com(i, "")):
                culpada = i  # sem esta seção o corpo passa
                break
        if culpada is None:
            return {}

        sec = secoes_enviar[culpada]
        achado = {
            "secao_culpada": sec["idSecaoModelo"],
            "bytes_da_secao": len(sec["conteudo"].encode("utf-8", "replace")),
        }

        # bissecção por prefixo: menor pedaço do início que já dispara a regra
        texto = sec["conteudo"]
        lo, hi = 0, len(texto)
        while hi - lo > 1 and sondas < _MAX_SONDAS_WAF:
            mid = (lo + hi) // 2
            if await bloqueia(com(culpada, texto[:mid])):
                hi = mid
            else:
                lo = mid
        if hi < len(texto) or lo > 0:
            achado["trecho_ate_o_gatilho"] = texto[max(0, hi - 160):hi]
            achado["posicao_no_texto"] = hi
        achado["sondas_usadas"] = sondas
        return achado
    except Exception as e:  # diagnóstico nunca pode mascarar o erro original
        logger.info("Localização do gatilho de WAF falhou: %s", e)
        return {}


def _msg_waf_esgotado(tentativas: list[dict], doc_id: str, gatilho: dict = None) -> str:
    """Relata o que foi tentado e, quando a sondagem conseguiu, ONDE está o gatilho.

    Quando a localização não conclui, não substitui o vazio por palpite: o
    Cloudflare não informa qual regra casou, e afirmar causa aqui seria chute.
    """
    linhas = "\n".join(
        f"  {i}) {t['estrategia']} → {t['resultado']}"
        for i, t in enumerate(tentativas, 1)
    )
    msg = (
        f"Não foi possível gravar as seções do documento {doc_id}: o Cloudflare "
        "devolveu bloqueio de WAF em todas as tentativas.\n"
        f"Tentativas:\n{linhas}\n"
    )
    if gatilho and gatilho.get("secao_culpada"):
        msg += (
            f"\nLocalização (sondas com versão inválida, nada foi gravado): o corpo "
            f"passa a ser aceito quando a seção {gatilho['secao_culpada']} sai do "
            f"POST — o gatilho está nela.\n"
        )
        if gatilho.get("trecho_ate_o_gatilho"):
            msg += (
                f"O bloqueio começa por volta do caractere "
                f"{gatilho['posicao_no_texto']} dessa seção, logo após:\n"
                f"  …{gatilho['trecho_ate_o_gatilho']}\n"
                "Reescrever esse trecho (cores hexadecimais como #333, atributos "
                "style longos, tags auto-fechadas) costuma bastar.\n"
            )
    else:
        msg += (
            "O que se sabe: alguma coisa no corpo do POST casou uma managed rule. "
            "O Cloudflare não informa qual trecho, e a sondagem automática não "
            "conseguiu isolar — a origem exata NÃO está determinada.\n"
        )
    msg += (
        "Caminhos possíveis: (a) chamar de novo com dry_run=true e inspecionar o "
        "payload exato; (b) simplificar o HTML do trecho apontado; (c) editar pela "
        "interface web; (d) pedir ao órgão exceção de WAF para /sei/modulos/wssei/."
    )
    return msg


_RE_ANCORA_SEI = re.compile(
    r"""<a\b[^>]*\bid\s*=\s*["']lnkSei(\d+)["'][^>]*>(.*?)</a\s*>""",
    re.IGNORECASE | re.DOTALL,
)
_RE_TAGS = re.compile(r"<[^>]+>")
_RE_NUM_PROCESSO = re.compile(r"^\d{4,6}\.\d{6}/\d{4}-\d{2}$")
_MAX_ANCORAS_VALIDADAS = 12


async def _validar_ancoras_sei(client: SEIClient, conteudos: list[str]) -> list[dict]:
    """Detecta âncora `id="lnkSeiNNNN"` cujo id não corresponde ao texto do link.

    Espelha a regra do core (`EditorRN::processarLinkProtocolo`): a âncora só
    sobrevive ao salvamento se o TEXTO do link for exatamente o protocoloFormatado
    do protocolo cujo id está em `lnkSei`; senão o SEI descarta a tag e deixa só o
    texto. Então a checagem parte do texto — que é o que o usuário vê — e pergunta
    ao SEI qual id lhe corresponde.

    Não opina sobre âncora de PROCESSO (texto no formato NNNNN.NNNNNN/AAAA-NN):
    `idProtocolo` abrange processos, e a rota de consulta aqui é de documento, então
    qualquer veredito seria chute. A versão anterior avisava nesses casos e chegou a
    induzir a "correção" de uma âncora que estava certa, quebrando o link.

    Best-effort: falha de rede aqui nunca bloqueia a edição.
    """
    ancoras: list[tuple[str, str]] = []
    for c in conteudos:
        for id_ancora, texto in _RE_ANCORA_SEI.findall(c or ""):
            texto = html.unescape(_RE_TAGS.sub("", texto)).replace("\xa0", " ").strip()
            if (id_ancora, texto) not in ancoras:
                ancoras.append((id_ancora, texto))
    if not ancoras:
        return []

    avisos: list[dict] = []
    for id_ancora, texto in ancoras[:_MAX_ANCORAS_VALIDADAS]:
        if _RE_NUM_PROCESSO.match(texto):
            continue  # âncora de processo — fora do alcance desta checagem
        if not texto.isdigit():
            continue  # texto não é um nº SEI puro; nada a comparar
        try:
            doc = await client.consultar_documento_interno_formatado(texto)
        except Exception:
            continue  # nº não resolvido → sem base para opinar
        id_correto = str(doc.get("idDocumento") or "")
        if not id_correto or id_correto == id_ancora:
            continue  # id bate com o texto: âncora correta
        avisos.append({
            "ancora": f"lnkSei{id_ancora}",
            "texto_do_link": texto,
            "problema": f"o texto do link é o nº SEI {texto}, cujo id interno é "
                        f"{id_correto} — mas a âncora aponta para {id_ancora}. O SEI "
                        "exige que os dois correspondam, senão descarta a tag ao salvar.",
            "id_interno_correto": id_correto,
            "documento": doc.get("nomeDocumento", ""),
            "correcao": f'troque id="lnkSei{id_ancora}" por id="lnkSei{id_correto}" '
                        "(sei_gerar_referencia monta o HTML já com o id certo).",
        })
    if len(ancoras) > _MAX_ANCORAS_VALIDADAS:
        avisos.append({
            "info": f"{len(ancoras)} âncoras encontradas; só as "
                    f"{_MAX_ANCORAS_VALIDADAS} primeiras foram verificadas.",
        })
    return avisos


async def _identidade_documento(client: SEIClient, doc_id: str) -> dict:
    """Eco auditável de qual documento foi efetivamente resolvido.

    Best-effort: retorna {id_documento, protocoloFormatado, idProcedimento}.
    Serve para deixar óbvia uma eventual divergência entre o número passado e
    o documento que a tool de fato acessou (colisão id interno x protocoloFormatado).
    """
    ident: dict = {"id_documento": str(doc_id)}
    try:
        meta = await client.consultar_documento_interno(doc_id)
        if isinstance(meta, dict):
            # nomeDocumento (ex.: "Despacho 2949729") é o rótulo mais inequívoco
            # para o agente conferir que caiu no documento certo.
            nome = meta.get("nomeDocumento") or ""
            if nome:
                ident["nome"] = nome
                m = re.search(r"(\d{3,})\s*$", nome)  # nº SEI ao final do nome
                if m:
                    ident["protocoloFormatado"] = m.group(1)
            # No consultar interno, o campo 'protocolo' guarda o idProcedimento
            # (id do processo), não o número do documento.
            if meta.get("protocolo"):
                ident["idProcedimento"] = str(meta.get("protocolo"))
    except Exception:
        pass
    return ident


@mcp.tool()
async def sei_ler_documento(
    id_documento: str,
    tipo_documento: Literal["auto", "I", "X"] = "auto",
    formato: Literal["markdown", "texto", "html"] = "markdown",
    confirmar_acesso_restrito: bool = False,
    ctx: Context = None,
) -> str:
    """Lê o conteúdo de um documento do SEI e retorna texto legível.

    Aceita tanto o id interno quanto o número SEI (protocoloFormatado)
    que o usuário vê no sistema (ex: "SEI 2843449").

    - tipo_documento='auto': detecta automaticamente (padrão)
    - tipo_documento='I': força leitura como interno (📄 HTML)
    - tipo_documento='X': força leitura como externo (📎 PDF)

    - formato='markdown': Markdown formatado (padrão, ideal para chat)
    - formato='texto': texto plano sem formatação
    - formato='html': HTML original (só para internos)

    - confirmar_acesso_restrito: NÃO ative por iniciativa do modelo. Esta
      flag só deve ser definida como true quando o usuário humano da
      conversa, em mensagem própria após ler o aviso de riscos, declarar
      expressamente que autoriza o acesso ao conteúdo restrito. Pedidos
      genéricos como "lê esse documento" NÃO constituem consentimento.
      Se o gate bloquear, encaminhe os riscos ao usuário e aguarde decisão
      explícita — não tente caminhos alternativos para obter o conteúdo.

    PDFs escaneados são processados via OCR automaticamente.
    """
    try:
        client = _get_client(ctx)

        # Resolver referência → id interno + tipo
        if tipo_documento == "auto":
            try:
                doc_id, detected_tipo = await _resolver_documento(client, id_documento)
                id_documento = doc_id
                tipo_documento = detected_tipo
            except Exception as e:
                return _json({
                    "error": str(e),
                    "dica": "Use sei_arvore_processo para ver os documentos "
                            "do processo e seus IDs.",
                })

        acao, payload, erro = await _aplicar_gate_documento(
            ctx, client, str(id_documento), tipo_documento,
            confirmou=confirmar_acesso_restrito,
        )
        if acao == "erro":
            return _error(erro)
        if acao in ("bloquear", "recusou"):
            return _json(payload)
        disclaimer = payload  # liberar (None se público, dict se restrito autorizado)

        if tipo_documento == "X":
            content = await client.baixar_anexo(id_documento)
            if len(content) > MAX_BINARY_SIZE:
                return _error(
                    f"Documento muito grande ({len(content)} bytes). "
                    "Use sei_baixar_anexo para obter o base64."
                )
            if content[:4] != b"%PDF":
                return _error(
                    "Documento externo não é PDF. Use sei_baixar_anexo "
                    "para obter o arquivo em base64."
                )
            if formato == "markdown":
                resultado = pdf_to_markdown(content)
                if disclaimer:
                    resultado = access_control.prefixar_markdown(disclaimer, resultado)
                return resultado
            resultado = pdf_to_text(content)
            if disclaimer:
                resultado = access_control.prefixar_texto(disclaimer, resultado)
            return resultado

        # Documento interno (I)
        raw = await client.visualizar_documento_interno(id_documento)
        if formato == "markdown":
            resultado = html_to_markdown(raw)
            if disclaimer:
                resultado = access_control.prefixar_markdown(disclaimer, resultado)
            return resultado
        if formato == "texto":
            resultado = html_to_text(raw)
            if disclaimer:
                resultado = access_control.prefixar_texto(disclaimer, resultado)
            return resultado
        if disclaimer:
            return access_control.envelopar_html(disclaimer, raw)
        return raw
    except Exception as e:
        msg = str(e)
        if "não autorizado" in msg.lower() or "nao autorizado" in msg.lower():
            return _json({
                "error": msg,
                "dica": "Acesso negado. Troque para a unidade geradora "
                        "com sei_trocar_unidade.",
            })
        return _error(msg)


@mcp.tool()
async def sei_baixar_anexo(
    id_documento: str,
    confirmar_acesso_restrito: bool = False,
    ctx: Context = None,
) -> str:
    """Baixa um documento externo (anexo) do SEI em base64.

    Aceita tanto o id interno (ex: "3149544") quanto o número SEI /
    protocoloFormatado (ex: "2867926") — auto-resolve via pesquisa Solr.

    Use para documentos com tipoDocumento='X' (📎).
    Para PDFs com texto, prefira sei_ler_documento(tipo_documento='X')
    que já extrai o texto legível.

    Retorna base64 + tamanho. Limite: 10 MB.

    confirmar_acesso_restrito: NÃO ative por iniciativa do modelo. Esta
    flag só deve ser definida como true quando o usuário humano da conversa,
    em mensagem própria após ler o aviso de riscos, declarar expressamente
    que autoriza o acesso. Se o gate bloquear, encaminhe os riscos ao
    usuário e aguarde decisão explícita — não tente caminhos alternativos.
    """
    try:
        client = _get_client(ctx)

        # Auto-resolver número SEI → id interno (igual a sei_ler_documento)
        try:
            doc_id, _ = await _resolver_documento(client, id_documento)
            id_documento = doc_id
        except Exception as e:
            return _json({
                "error": str(e),
                "dica": "Use sei_arvore_processo ou sei_buscar_documento para "
                        "encontrar o id correto do documento.",
            })

        acao, payload, erro = await _aplicar_gate_documento(
            ctx, client, str(id_documento), "X",
            confirmou=confirmar_acesso_restrito,
        )
        if acao == "erro":
            return _error(erro)
        if acao in ("bloquear", "recusou"):
            return _json(payload)
        disclaimer = payload

        content = await client.baixar_anexo(id_documento)
        if len(content) > MAX_BINARY_SIZE:
            return _error(
                f"Documento muito grande ({len(content)} bytes, limite {MAX_BINARY_SIZE}). "
                "Baixe manualmente pelo SEI."
            )
        resposta = {
            "base64": base64.b64encode(content).decode(),
            "size_bytes": len(content),
        }
        if disclaimer:
            resposta["aviso_acesso"] = disclaimer
        return _json(resposta)
    except Exception as e:
        return _error(str(e))


# ---------------------------------------------------------------------------
# Tools de escrita
# ---------------------------------------------------------------------------


@mcp.tool()
async def sei_criar_documento(
    processo: str,
    id_serie: str,
    descricao: str = "",
    nivel_acesso: str = "0",
    id_unidade: str = "",
    hipotese_legal: str = "",
    ctx: Context = None,
) -> str:
    """Cria um novo documento interno (nativo) em um processo SEI.

    Parâmetros:
    - processo: protocolo formatado (ex: 50300.018905/2018-67) ou IdProcedimento
    - id_serie: tipo do documento — id (ex.: "290") ou nome (ex.: "Ofício",
      "despacho"; sem acento/maiúsculas, nome exato tem precedência)
    - descricao: descrição/título do documento
    - nivel_acesso: publico, restrito ou sigiloso (ou 0, 1, 2)
    - hipotese_legal: id ou nome da hipótese — obrigatória para restrito/sigiloso.
      Sem ela (ou com nome ambíguo) NADA é criado e a resposta lista as
      hipóteses válidas para o usuário escolher.
    - id_unidade: ID da unidade geradora (opcional)

    O documento é criado vazio. Use sei_listar_secoes e sei_editar_secao
    para inserir conteúdo.
    """
    try:
        client = _get_client(ctx)
        id_procedimento = await _resolver_processo(client, processo)
        serie = str(id_serie).strip()
        nome_tipo = ""
        if not serie.isdigit():
            serie, nome_tipo = await _resolver_tipo_documento(client, serie)

        nivel = _normalizar_nivel(nivel_acesso) or "0"
        hipotese = ""
        if nivel in ("1", "2"):
            # Resolve ANTES de criar: sem hipótese válida, nada é criado.
            hipotese, escolha = await _resolver_hipotese(client, nivel, hipotese_legal)
            if escolha:
                return _json(escolha)

        # A rota de criação não aceita hipótese legal: cria público e, se for o
        # caso, aplica nível + hipótese em seguida (preservando o resto).
        result = await client.criar_documento_interno(
            id_procedimento=id_procedimento,
            id_serie=serie,
            descricao=descricao,
            nivel_acesso="0" if nivel in ("1", "2") else nivel,
            id_unidade=id_unidade,
        )
        if nivel in ("1", "2") and isinstance(result, dict) and result.get("idDocumento"):
            try:
                await client.alterar_documento_interno(
                    id_documento=str(result["idDocumento"]), nivel_acesso=nivel,
                    id_hipotese_legal=hipotese,
                )
                result["nivel_acesso"] = nivel
                result["hipotese_legal"] = hipotese
            except Exception as e:
                result["_aviso"] = (
                    "O documento foi criado PÚBLICO, mas não foi possível torná-lo "
                    f"{'restrito' if nivel == '1' else 'sigiloso'}: {e}. Ajuste com "
                    "sei_alterar_documento_interno antes de inserir conteúdo sensível."
                )
        if isinstance(result, dict):
            result.setdefault("nivel_acesso", nivel)
            result["nivel_acesso_descricao"] = access_control.ROTULOS.get(
                str(result["nivel_acesso"]), str(result["nivel_acesso"]))
            if nome_tipo:
                result["tipo"] = nome_tipo
        return _json(result)
    except Exception as e:
        return _error(str(e))


_RE_TAGS = re.compile(r"<[^>]+>")
_RE_MARCADOR_MODELO = re.compile(r"@[a-z_]+@")
_RE_PARAGRAFO = re.compile(r"<p\b[^>]*>.*?</p>", re.S | re.I)
_INICIO_DESTINATARIO = ("ao senhor", "a senhora", "aos senhores", "as senhoras",
                        "a sua excelencia", "ao excelentissimo", "a excelentissima",
                        "ao ilustrissimo", "a ilustrissima", "ao sr", "a sra")


def _texto_secao(html_secao: str) -> str:
    return _normalizar_busca(_RE_TAGS.sub(" ", html_secao))


def _papel_secao(secao: dict, conteudo: str) -> str:
    """Papel de uma seção num modelo de documento do SEI.

    principal | assunto | destinatario | vocativo | outra. Modelos de tipos
    diferentes usam ids de seção diferentes (Despacho 461–470, Ofício
    3157–3167 em Roraima), então o papel sai do conteúdo e dos marcadores.
    """
    if secao.get("PrincipalSecaoDocumento") == "S":
        return "principal"
    bruto = conteudo.lower()
    texto = _texto_secao(conteudo)
    if "@vocativo" in bruto:
        return "vocativo"
    if "_destinatario@" in bruto or texto.startswith(_INICIO_DESTINATARIO):
        return "destinatario"
    if texto.startswith("assunto"):
        return "assunto"
    paragrafos = [p for p in _RE_PARAGRAFO.findall(conteudo) if _texto_secao(p)]
    if len(paragrafos) == 1 and texto.endswith(",") and len(texto) <= 60:
        return "vocativo"
    return "outra"


def _secoes_editaveis(resultado: dict) -> list[tuple[dict, str]]:
    saida = []
    for secao in resultado.get("secoes") or []:
        if not isinstance(secao, dict):
            continue
        if secao.get("DinamicaSecaoDocumento") == "S" or secao.get("somenteLeitura") == "S":
            continue
        saida.append((secao, html.unescape(str(secao.get("conteudo") or ""))))
    return saida


def _separar_vocativo(corpo: str) -> tuple[str, str]:
    """Tira do início do corpo um parágrafo de vocativo ("Senhor Secretário,")."""
    for paragrafo in _RE_PARAGRAFO.findall(corpo):
        texto = _texto_secao(paragrafo)
        if not texto:
            continue
        if texto.endswith(",") and len(texto) <= 60:
            return paragrafo, corpo.replace(paragrafo, "", 1)
        break
    return "", corpo


async def _resolver_tipo_documento(client: SEIClient, tipo: str) -> tuple[str, str]:
    """(id_serie, nome) a partir do id ou do nome do tipo de documento."""
    tipo = str(tipo or "").strip()
    r = await _tipos_documento(client, filtro="" if tipo.isdigit() else tipo, limit=200)
    tipos = r.get("tipos") or []
    if tipo.isdigit():
        return tipo, next((t.get("nome", "") for t in tipos if str(t.get("id")) == tipo), "")
    alvo = _normalizar_busca(tipo)
    exatos = [t for t in tipos if _normalizar_busca(t.get("nome", "")) == alvo]
    candidatos = exatos or tipos
    if len(candidatos) == 1:
        return str(candidatos[0]["id"]), candidatos[0].get("nome", "")
    raise ValueError(
        f"Tipo de documento {tipo!r} " + ("não encontrado." if not tipos else "ambíguo: ")
        + ", ".join(f"{t.get('nome')} (id {t.get('id')})" for t in tipos[:10])
    )


@mcp.tool()
async def sei_converter_documento(
    id_documento: str,
    tipo_destino: str,
    descricao: str = "",
    ctx: Context = None,
) -> str:
    """Converte um documento interno em outro tipo (ex.: Despacho → Ofício).

    O SEI não troca o tipo de um documento existente. Esta tool cria, no mesmo
    processo, um documento do tipo `tipo_destino` e copia:
    - destinatário, assunto, vocativo e corpo para as seções equivalentes do
      novo modelo (pelo papel de cada seção, não pelo id);
    - os metadados: descrição, nível de acesso, hipótese legal, assuntos,
      interessados, destinatários e observação.
    Marcadores do modelo novo que ficarem sem conteúdo (ex.: @vocativo_destinatario@)
    são limpos. O documento ORIGINAL NÃO é excluído (a API do SEI não permite):
    se não servir mais, exclua-o pela interface do SEI.

    Parâmetros:
    - id_documento: número SEI ou id interno do documento de origem
    - tipo_destino: id ou nome do tipo (ex.: "Ofício", "290")
    - descricao: descrição do novo documento (vazio = a da origem)
    """
    try:
        client = _get_client(ctx)
        doc_id, _ = await _resolver_documento(client, id_documento)
        origem_meta = await client.consultar_documento_interno(doc_id)
        id_proc = str(origem_meta.get("protocolo") or "")
        if not id_proc:
            return _error("Não foi possível identificar o processo do documento de origem.")
        id_serie, nome_tipo = await _resolver_tipo_documento(client, tipo_destino)

        await _ativar_unidade_geradora(client, doc_id)
        origem_secoes = await client.listar_secao_documento(doc_id)

        nova_descricao = descricao or html.unescape(str(origem_meta.get("descricao") or ""))
        criado = await client.criar_documento_interno(
            id_procedimento=id_proc, id_serie=id_serie, descricao=nova_descricao, nivel_acesso="0",
        )
        novo_id = str(criado.get("idDocumento") or "")
        novo_numero = str(criado.get("protocoloDocumentoFormatado") or "")
        resultado = {
            "documento_original": {"id": str(doc_id), "nome": html.unescape(str(origem_meta.get("nomeDocumento") or ""))},
            "novo_documento": {"id": novo_id, "numero_sei": novo_numero, "tipo": nome_tipo},
        }

        # Conteúdo: por papel da seção
        por_papel: dict[str, str] = {}
        nao_copiadas = []
        for secao, conteudo in _secoes_editaveis(origem_secoes):
            papel = _papel_secao(secao, conteudo)
            if not _texto_secao(conteudo):
                continue
            if papel in por_papel or papel == "outra":
                nao_copiadas.append({"idSecaoModelo": secao.get("idSecaoModelo"), "papel": papel})
                continue
            por_papel[papel] = conteudo

        destino_secoes = await client.listar_secao_documento(novo_id)
        papeis_destino = {_papel_secao(s, c) for s, c in _secoes_editaveis(destino_secoes)}
        if "vocativo" in papeis_destino and "vocativo" not in por_papel and "principal" in por_papel:
            vocativo, resto = _separar_vocativo(por_papel["principal"])
            if vocativo:
                por_papel["vocativo"], por_papel["principal"] = vocativo, resto

        envio, copiadas, limpas = [], [], []
        for secao, conteudo in _secoes_editaveis(destino_secoes):
            papel = _papel_secao(secao, conteudo)
            if papel in por_papel:
                envio.append({"idSecaoModelo": secao["idSecaoModelo"], "conteudo": por_papel.pop(papel)})
                copiadas.append(papel)
            elif _RE_MARCADOR_MODELO.search(conteudo) or "digite aqui" in conteudo.lower():
                envio.append({"idSecaoModelo": secao["idSecaoModelo"],
                              "conteudo": '<p class="Texto_Alinhado_Esquerda">&nbsp;</p>'})
                limpas.append(secao["idSecaoModelo"])
        nao_copiadas += [{"papel": p} for p in por_papel]  # sem seção equivalente no destino

        if envio:
            gravado = json.loads(await sei_editar_secao(id_documento=novo_id, secoes=envio, ctx=ctx))
            if isinstance(gravado, dict) and gravado.get("error"):
                resultado["erro_conteudo"] = gravado["error"]
        resultado["secoes_copiadas"] = copiadas
        if limpas:
            resultado["marcadores_limpos"] = limpas
        if nao_copiadas:
            resultado["nao_copiadas"] = nao_copiadas

        try:
            await client.copiar_metadados_documento_interno(origem_meta, novo_id, nova_descricao)
            resultado["metadados_copiados"] = True
        except Exception as e:
            resultado["metadados_copiados"] = False
            resultado["erro_metadados"] = str(e)

        resultado["_aviso"] = (
            "O documento original não foi excluído (a API do SEI não permite). "
            "Confira o novo documento e, se o original não servir mais, exclua-o pela interface do SEI."
        )
        return _json(resultado)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_listar_secoes(
    id_documento: str,
    incluir_dinamicas: bool = False,
    ctx: Context = None,
) -> str:
    """Lista as seções editáveis de um documento interno SEI.

    Aceita o número SEI (protocoloFormatado, ex: 2943731) OU o id interno — a
    tool resolve automaticamente (igual sei_ler_documento). O número que o
    usuário vê (protocoloFormatado) É DIFERENTE do id interno; a resposta inclui
    `_documento_resolvido` (protocoloFormatado + idProcedimento) para você
    conferir que caiu no documento certo antes de editar.

    Retorna as seções com IDs, conteúdo atual (HTML) e a versão do documento
    (campo ultimaVersaoDocumento), necessária para usar sei_editar_secao.

    Seções dinâmicas (cabeçalho com brasão, título, data, rodapé) são geradas
    pelo SEI e não editáveis; por padrão vêm sem conteúdo (`conteudo_omitido`),
    pois o brasão em base64 sozinho passa de 700 KB. `incluir_dinamicas=true`
    devolve tudo. sei_editar_secao não depende desse conteúdo.
    """
    try:
        client = _get_client(ctx)
        doc_id, _ = await _resolver_documento(client, id_documento)
        result = await client.listar_secao_documento(doc_id)
        if isinstance(result, dict):
            if not incluir_dinamicas:
                for secao in result.get("secoes") or []:
                    if isinstance(secao, dict) and secao.get("DinamicaSecaoDocumento") == "S":
                        tamanho = len(secao.get("conteudo") or "")
                        if tamanho:
                            secao["conteudo"] = ""
                            secao["conteudo_omitido"] = f"seção dinâmica gerada pelo SEI ({tamanho} caracteres)"
            result["_documento_resolvido"] = await _identidade_documento(client, doc_id)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_gerar_referencia(
    numero_sei: str,
    ctx: Context = None,
) -> str:
    """Gera o HTML de referência (hiperlink dinâmico) para um documento ou processo SEI.

    Aceita número SEI de documento (ex: 2599818) ou número de processo
    (ex: 26101.018680/2026.11). Para documento, resolve o id interno e retorna
    o snippet HTML pronto para inserir no conteúdo de um documento.

    O SEI renderiza isso como link clicável na interface web.
    Use ao citar documentos SEI no texto de Despachos, Notas Técnicas, etc.

    Exemplo: "SEI nº <resultado>" vira link clicável para o documento.
    """
    try:
        client = _get_client(ctx)
        numero_sei = numero_sei.strip()
        if "/" in numero_sei:
            # Número de PROCESSO: o link usa o id do processo e o número
            # exatamente como o SEI o formata.
            alvo = await _vincular_referencias(client, numero_sei, {}, vinculados := [], [])
            if not vinculados:
                return _error(f"Processo {numero_sei} não encontrado.")
            return _json({
                "numero_processo": vinculados[0]["numero"],
                "id_procedimento": vinculados[0]["id"],
                "html": alvo,
                "uso": f"...Processo SEI n&ordm; {alvo}...",
            })
        doc_id, _ = await _resolver_documento(client, numero_sei)
        snippet = html_referencia_sei(doc_id, numero_sei)
        return _json({
            "numero_sei": numero_sei,
            "id_documento": doc_id,
            "html": snippet,
            "uso": f'...SEI n&ordm; {snippet}...',
        })
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_estilos(categoria: str = "", ctx: Context = None) -> str:
    """Lista os estilos CSS disponíveis para formatação de documentos no SEI.

    O SEI usa classes CSS padronizadas em todos os documentos governamentais.
    Use esta tool para descobrir a classe correta para cada tipo de parágrafo.

    Categorias: "texto", "titulo", "lista", "tabela", "destaque", "todos"
    Sem parâmetro: retorna os atalhos rápidos (intenção → classe).

    CONVENÇÃO para documentos (Despachos, Notas Técnicas, etc.):
    - Corpo/mérito do texto: usar Paragrafo_Numerado_Nivel1 (autonumera 1. 2. 3.)
    - Endereçamento (À SFC...): usar Texto_Alinhado_Esquerda
    - Assunto: usar Texto_Justificado com <strong> para o título
    - Fecho (Atenciosamente): usar Texto_Justificado_Recuo_Primeira_Linha
    - Nome do signatário: usar Texto_Centralizado_Maiusculas
    - Cargo: usar Texto_Centralizado
    """
    try:
        if not categoria or categoria == "atalhos":
            return _json({
                "atalhos": STYLE_SHORTCUTS,
                "dica": "Use sei_estilos('todos') para ver todos os estilos com exemplos.",
            })

        if categoria == "todos":
            return _json(SEI_STYLES)

        filtros = {
            "texto": ["Texto_"],
            "titulo": ["Texto_Centralizado_Maiusculas", "Texto_Fundo_Cinza", "Texto_Espaco_Duplo"],
            "lista": ["Paragrafo_Numerado", "Item_Nivel", "Item_Alinea", "Item_Inciso"],
            "tabela": ["Tabela_"],
            "destaque": ["Citacao", "Tachado", "Texto_Fundo_Cinza", "Texto_Mono"],
        }

        prefixos = filtros.get(categoria, [])
        if not prefixos:
            return _json({
                "error": f"Categoria '{categoria}' não encontrada",
                "categorias": list(filtros.keys()) + ["todos", "atalhos"],
            })

        resultado = {}
        for nome, info in SEI_STYLES.items():
            if any(nome.startswith(p) for p in prefixos):
                resultado[nome] = info

        return _json(resultado)
    except Exception as e:
        return _error(str(e))


# ---------------------------------------------------------------------------
# Links automáticos para números de processo e de documento citados no texto
# ---------------------------------------------------------------------------
# Número de processo: 5 dígitos . 6 dígitos / ano, com dígito após "-" ou "."
_RE_NUM_PROCESSO = re.compile(r"(?<![\w/.-])(\d{5}\.\d{6}/\d{4}[-.]\d{2})(?![\w/-]|\.\d)")
# Número SEI de documento só quando citado como tal: "ep.", "SEI nº", "evento nº"…
_RE_NUM_DOCUMENTO = re.compile(
    r"((?:\bep\.?|\beps\.?|\bevento(?:\s+n[º°o]\.?)?|\bSEI(?:\s+n[º°o]\.?)?|\bdocumento\s+SEI(?:\s+n[º°o]\.?)?)\s*)(\d{6,9})\b",
    re.I,
)
_RE_TAG_OU_TEXTO = re.compile(r"(<[^>]+>)")


def _ancora_sei(id_protocolo: str, texto: str) -> str:
    return ('<span contenteditable="false" style="text-indent:0;">'
            f'<a class="ancoraSei" id="lnkSei{id_protocolo}" style="text-indent:0;">{texto}</a></span>')


async def _vincular_referencias(client: SEIClient, conteudo: str, cache: dict,
                                vinculadas: list, nao_resolvidas: list) -> str:
    """Transforma números de processo e de documento citados em links do SEI.

    Só mexe em TEXTO fora de <a>…</a> (âncoras existentes ficam como estão).
    Documento só quando o número vem citado ("ep. 24837023", "SEI nº …") e
    resolvido pelo número SEI direto no banco — nunca tratado como id interno.
    Processo: o texto do link é o número exatamente como o SEI o formata
    (o EditorRN descarta a âncora se o texto não bater com o protocolo).
    """
    async def processo(numero: str):
        chave = ("P", numero)
        if chave not in cache:
            cache[chave] = None
            for candidato in _variantes_protocolo(numero):
                try:
                    proc = await client.consultar_processo(candidato)
                except Exception:
                    continue
                if proc and proc.get("IdProcedimento"):
                    cache[chave] = (str(proc["IdProcedimento"]),
                                    str(proc.get("ProtocoloProcedimentoFormatado") or candidato))
                    break
        return cache[chave]

    async def documento(numero: str):
        chave = ("D", numero)
        if chave not in cache:
            cache[chave] = None
            try:
                doc = await client.consultar_documento_interno_formatado(numero)
                if doc and doc.get("idDocumento"):
                    cache[chave] = (str(doc["idDocumento"]), numero)
            except Exception:
                pass
        return cache[chave]

    partes = _RE_TAG_OU_TEXTO.split(conteudo)
    dentro_de_a = 0
    for i, parte in enumerate(partes):
        if parte.startswith("<"):
            nome = parte[1:].lstrip("/").split()[0].rstrip(">").lower() if len(parte) > 2 else ""
            if nome == "a":
                dentro_de_a += -1 if parte.startswith("</") else (0 if parte.endswith("/>") else 1)
                dentro_de_a = max(dentro_de_a, 0)
            continue
        if dentro_de_a or not parte.strip():
            continue

        novo = parte
        for m in list(_RE_NUM_PROCESSO.finditer(parte))[::-1]:
            alvo = await processo(m.group(1))
            if alvo:
                novo = novo[:m.start(1)] + _ancora_sei(alvo[0], alvo[1]) + novo[m.end(1):]
                vinculadas.append({"tipo": "processo", "numero": alvo[1], "id": alvo[0]})
            else:
                nao_resolvidas.append(m.group(1))
        # documentos: reaplica sobre o texto já alterado, pulando as âncoras novas
        pedacos = _RE_TAG_OU_TEXTO.split(novo)
        dentro = 0
        for j, pedaco in enumerate(pedacos):
            if pedaco.startswith("<"):
                if pedaco.lower().startswith("<a"):
                    dentro += 1
                elif pedaco.lower().startswith("</a"):
                    dentro = max(dentro - 1, 0)
                continue
            if dentro:
                continue
            for m in list(_RE_NUM_DOCUMENTO.finditer(pedaco))[::-1]:
                alvo = await documento(m.group(2))
                if alvo:
                    pedaco = pedaco[:m.start(2)] + _ancora_sei(alvo[0], alvo[1]) + pedaco[m.end(2):]
                    vinculadas.append({"tipo": "documento", "numero": alvo[1], "id": alvo[0]})
                else:
                    nao_resolvidas.append(m.group(2))
            pedacos[j] = pedaco
        partes[i] = "".join(pedacos)
    return "".join(partes)


async def _erro_documento_em_bloco(ctx, id_documento: str, erro: Exception) -> str:
    """Erro de escrita com contexto quando o documento está em bloco disponibilizado.

    O SEI só diz "Documento disponibilizado em bloco de assinatura". Aqui se
    acrescenta QUAL bloco, para quem foi disponibilizado e o caminho para
    editar — que tira o documento da fila de quem vai assinar, então é decisão
    do usuário, não do agente.
    """
    msg = str(erro)
    if "bloco de assinatura" not in msg.lower():
        return _error(msg)
    blocos = []
    try:
        client = _get_client(ctx)
        doc_id, _ = await _resolver_documento(client, id_documento)
        dados = await client.listar_blocos_documento(doc_id)
        ids = dados.get("blocos") if isinstance(dados, dict) else [b.get("id") for b in dados or []]
        for id_bloco in ids or []:
            info = {"id": str(id_bloco)}
            try:
                r = await client.pesquisar_blocos_assinatura(id_bloco=str(id_bloco), limit=1)
                if r.get("blocos"):
                    resumo = _bloco_assinatura_resumido(r["blocos"][0])
                    info.update({k: resumo[k] for k in
                                 ("descricao", "estado_descricao", "unidade", "disponibilizado_para")})
            except Exception:
                pass
            blocos.append(info)
    except Exception:
        logger.debug("Não foi possível detalhar o bloco do documento", exc_info=True)
    return _json({
        "error": msg,
        "erro_origem": "documento_em_bloco_de_assinatura",
        "blocos": blocos,
        "como_editar": (
            "Nada foi alterado. Para editar: (1) sei_cancelar_disponibilizacao_bloco — o "
            "documento sai temporariamente da fila de quem vai assinar; (2) faça a edição; "
            "(3) sei_disponibilizar_bloco_assinatura para devolvê-lo. Confirme com o "
            "usuário antes, pois afeta quem está com o bloco para assinar."
        ),
    })


@mcp.tool()
async def sei_editar_secao(
    id_documento: str,
    secoes: list[dict],
    versao: str = "",
    dry_run: bool = False,
    validar_referencias: bool = True,
    vincular_referencias: bool = True,
    ctx: Context = None,
) -> str:
    """Altera o conteúdo de seções editáveis de um documento interno SEI.

    Parâmetros:
    - id_documento: ID do documento
    - secoes: lista de seções a alterar, cada uma com:
        - idSecaoModelo: ID do modelo da seção (obtido via sei_listar_secoes)
        - conteudo: novo conteúdo HTML da seção
      (não é necessário incluir seções somenteLeitura — são preenchidas
       automaticamente com o conteúdo original)
    - versao: versão do documento (se omitida, obtida automaticamente)
    - dry_run: NÃO grava nada; devolve o payload exato que seria enviado
      (todas as seções, já normalizadas, com tamanhos em bytes). Use para
      inspecionar/isolar um problema numa chamada só, em vez de tentativa e erro.
    - vincular_referencias (padrão true): números de processo
      (ex.: 26101.018680/2026.11) e de documento citados como "ep. 24837023" ou
      "SEI nº 24837023" viram links clicáveis do SEI. Só o texto fora de links
      existentes é tocado; número não encontrado fica como texto e aparece em
      `_referencias_sem_link`.
    - validar_referencias: confere se alguma âncora `id="lnkSeiNNNN"` está usando
      número SEI em vez do id interno (link morto). Avisos vão em `_avisos`.

    O conteúdo deve ser HTML com as classes CSS do SEI (ex: Texto_Justificado).
    Entidades HTML (`&nbsp;`, `&ccedil;`, `&#233;`, …) são convertidas para UTF-8
    literal antes do envio — o SEI aceita, e isso evita que elas se acumulem a
    cada ciclo ler→reenviar. Caracteres fora do ISO-8859-1 continuam sendo
    convertidos para entidades numéricas (exigência do wssei).

    IMPORTANTE: O SEI exige que TODAS as seções sejam enviadas (lista parcial dá
    "Conteúdo do documento incompleto"). Esta tool faz isso automaticamente —
    basta informar as seções que deseja alterar. As seções dinâmicas (cabeçalho
    com brasão, título, rodapé) vão VAZIAS: o SEI as reconstrói do banco e
    descarta o que for enviado, então mandá-las de volta só engordaria o POST.

    O id_documento aceita número SEI (protocoloFormatado) ou id interno — é
    resolvido automaticamente e o documento resolvido é ecoado no retorno
    (`_documento_resolvido`), para evitar gravar no documento errado.

    Se o Cloudflare bloquear a escrita por WAF, a tool localiza o trecho culpado
    com sondas de versão inválida — que o SEI recusa antes de escrever, então
    nada é gravado — e relata a seção e a posição aproximada do gatilho.
    """
    try:
        client = _get_client(ctx)
        import html as html_module

        # Resolve o identificador (número SEI ou id interno) de forma consistente
        # com sei_listar_secoes — assim listar e editar sempre atingem o MESMO doc.
        doc_id, _ = await _resolver_documento(client, id_documento)

        # Buscar todas as seções atuais do documento
        secoes_data = await client.listar_secao_documento(doc_id)
        secoes_atuais = secoes_data.get("secoes", [])
        if not versao:
            versao = str(secoes_data.get("ultimaVersaoDocumento", "1"))

        # Indexar seções novas por idSecaoModelo
        alteracoes = {}
        for s in secoes:
            modelo = s.get("idSecaoModelo", "")
            if modelo:
                alteracoes[modelo] = s.get("conteudo", "")

        # Números de processo e documentos citados no texto viram links do SEI
        vinculadas: list[dict] = []
        nao_resolvidas: list[str] = []
        if vincular_referencias:
            cache_refs: dict = {}
            for modelo, conteudo_novo in list(alteracoes.items()):
                alteracoes[modelo] = await _vincular_referencias(
                    client, conteudo_novo, cache_refs, vinculadas, nao_resolvidas,
                )

        # Montar payload completo com TODAS as seções (o SEI recusa lista parcial:
        # EditorRN compara a contagem com a do banco e lança "Conteúdo do documento
        # incompleto"). As seções dinâmicas, porém, vão VAZIAS: o SEI as reconstrói
        # a partir do banco e descarta o que enviamos, então reenviá-las só engorda
        # o corpo do POST — que é justamente o que o WAF da borda inspeciona.
        secoes_enviar = []
        regeneradas: set[str] = set()
        dinamicas_pedidas: list[str] = []
        for s in secoes_atuais:
            if not isinstance(s, dict):
                continue
            sid = s.get("id") or s.get("IdSecaoDocumento")
            modelo = s.get("idSecaoModelo") or s.get("IdSecaoModelo")
            if not sid or not modelo:
                continue

            if _secao_regenerada_pelo_sei(s):
                if str(modelo) in alteracoes:
                    dinamicas_pedidas.append(str(modelo))
                conteudo = ""
                regeneradas.add(str(modelo))
            elif str(modelo) in alteracoes:
                # Seção alterada pelo usuário — já vem como HTML real
                conteudo = alteracoes[str(modelo)]
            else:
                # Seção original: o wssei devolve HTML-escaped no transporte
                conteudo = html_module.unescape(s.get("conteudo", "") or "")

            # Normaliza entidades em TODAS as seções — inclusive nas relidas do
            # próprio SEI, que é onde elas nascem e se acumulam.
            conteudo = normalizar_entidades_html(conteudo)
            conteudo = corrigir_classe_ancora_sei(conteudo)

            secoes_enviar.append({
                "id": str(sid),
                "idSecaoModelo": str(modelo),
                "conteudo": sanitize_iso8859(conteudo),
            })

        # idSecaoModelo que não existe no documento seria gravado como no-op e
        # devolveria "sucesso" — falso positivo caro, porque o agente segue em frente.
        modelos_doc = {sec["idSecaoModelo"] for sec in secoes_enviar}
        nao_encontrados = [m for m in alteracoes if m not in modelos_doc]
        if nao_encontrados and len(nao_encontrados) == len(alteracoes):
            return _error(
                f"Nenhuma das seções informadas existe no documento {doc_id}: "
                f"{nao_encontrados}. Seções disponíveis: {sorted(modelos_doc)}. "
                "Confira os idSecaoModelo com sei_listar_secoes — gravar assim "
                "não alteraria nada e ainda assim retornaria sucesso."
            )

        avisos = []
        if dinamicas_pedidas:
            avisos.append({
                "secoes_ignoradas": dinamicas_pedidas,
                "problema": "são seções dinâmicas: o SEI as reconstrói a partir do "
                            "banco ao salvar e descarta o conteúdo enviado, então "
                            "o texto informado NÃO foi gravado.",
                "onde_editar": "cabeçalho, título e rodapé vêm do modelo do "
                               "documento — mudam pelo template, não por esta tool.",
            })
        if nao_encontrados:
            avisos.append({
                "secoes_ignoradas": nao_encontrados,
                "problema": "idSecaoModelo não existe neste documento — o conteúdo "
                            "correspondente NÃO foi gravado.",
                "secoes_disponiveis": sorted(modelos_doc),
            })
        if validar_referencias and alteracoes:
            try:
                avisos += await _validar_ancoras_sei(client, list(alteracoes.values()))
            except Exception as e:  # validação nunca bloqueia a edição
                logger.info("Validação de âncoras falhou: %s", e)

        if dry_run:
            saida = {
                "dry_run": True,
                "nada_foi_gravado": True,
                "endpoint": "POST /documento/secao/alterar",
                "documento": str(doc_id),
                "versao": versao,
                "secoes": secoes_enviar,
                "resumo": [
                    {
                        "idSecaoModelo": sec["idSecaoModelo"],
                        "id": sec["id"],
                        "alterada_por_voce": sec["idSecaoModelo"] in alteracoes,
                        "regenerada_pelo_sei":
                            sec["idSecaoModelo"] in regeneradas,
                        "bytes": len(sec["conteudo"].encode("utf-8", "replace")),
                    }
                    for sec in secoes_enviar
                ],
                "bytes_total": sum(
                    len(sec["conteudo"].encode("utf-8", "replace")) for sec in secoes_enviar
                ),
                "_documento_resolvido": await _identidade_documento(client, doc_id),
            }
            if avisos:
                saida["_avisos"] = avisos
            return _json(saida)

        # --- Envio -----------------------------------------------------------
        tentativas: list[dict] = []
        estrategia = (
            "envio normalizado, seções dinâmicas vazias "
            f"({sorted(regeneradas)})" if regeneradas else "envio normalizado"
        )
        waf_info = None
        try:
            result = await client.alterar_secao_documento(
                id_documento=doc_id, secoes=secoes_enviar, versao=versao,
            )
            tentativas.append({"estrategia": estrategia, "resultado": "sucesso"})
        except SEICloudflareBlocked as e1:
            # 403 da borda ⇒ nada foi gravado, versão intacta.
            tentativas.append({
                "estrategia": estrategia,
                "resultado": f"bloqueado pelo Cloudflare: {e1}",
            })

            # Degrau 2: cores hexadecimais → rgb(). É o gatilho medido em produção
            # e a troca é neutra na renderização, então não altera o documento aos
            # olhos de quem o lê.
            secoes_rgb, trocas = [], 0
            for sec in secoes_enviar:
                novo, n = _converter_cores_hex(sec["conteudo"])
                trocas += n
                secoes_rgb.append({**sec, "conteudo": novo})

            if not trocas:
                gatilho = await _waf_localizar_gatilho(client, doc_id, secoes_enviar)
                raise Exception(_msg_waf_esgotado(tentativas, doc_id, gatilho)) from e1

            try:
                result = await client.alterar_secao_documento(
                    id_documento=doc_id, secoes=secoes_rgb, versao=versao,
                )
                tentativas.append({
                    "estrategia": f"cores hexadecimais trocadas por rgb() ({trocas})",
                    "resultado": "sucesso",
                })
                waf_info = {
                    "info": "O Cloudflare barrou o corpo por causa de cor hexadecimal "
                            "em atributo style ('#' lido como comentário SQL pelo "
                            "managed ruleset). As cores foram reescritas como rgb(), "
                            "que renderiza igual, e a gravação passou.",
                    "cores_convertidas": trocas,
                    "tentativas": tentativas,
                }
            except SEICloudflareBlocked as e2:
                tentativas.append({
                    "estrategia": f"cores hexadecimais trocadas por rgb() ({trocas})",
                    "resultado": f"bloqueado pelo Cloudflare: {e2}",
                })
                gatilho = await _waf_localizar_gatilho(client, doc_id, secoes_rgb)
                raise Exception(_msg_waf_esgotado(tentativas, doc_id, gatilho)) from e2

        # Verificação: as seções dinâmicas foram enviadas vazias porque o SEI
        # deveria reconstruí-las. Se alguma voltou vazia, ele NÃO reconstruiu e o
        # documento perdeu cabeçalho/rodapé — falha alto em vez de reportar sucesso.
        if regeneradas:
            verif = await client.listar_secao_documento(doc_id)
            vazias = [
                str(v.get("idSecaoModelo"))
                for v in verif.get("secoes", [])
                if str(v.get("idSecaoModelo")) in regeneradas
                and not (v.get("conteudo") or "").strip()
            ]
            if vazias:
                raise Exception(
                    f"As seções dinâmicas {vazias} do documento {doc_id} NÃO foram "
                    "regeneradas pelo SEI e ficaram vazias — cabeçalho/rodapé podem "
                    "ter sido perdidos. Confira o documento na interface web antes "
                    "de continuar editando."
                )

        out = result if isinstance(result, dict) else {"resultado": result}
        out["_documento_resolvido"] = await _identidade_documento(client, doc_id)
        if waf_info:
            out["_waf_contornado"] = waf_info
        if regeneradas:
            out["_secoes_regeneradas_pelo_sei"] = sorted(regeneradas)
        if vinculadas:
            out["_referencias_vinculadas"] = vinculadas
        if nao_resolvidas:
            out["_referencias_sem_link"] = sorted(set(nao_resolvidas))
        if avisos:
            out["_avisos"] = avisos
        return _json(out)
    except Exception as e:
        return await _erro_documento_em_bloco(ctx, id_documento, e)


# ---------------------------------------------------------------------------
# Tools de processos — listar, pesquisar, criar, tramitar
# ---------------------------------------------------------------------------


@mcp.tool()
async def sei_listar_processos(
    pagina: int = 0,
    apenas_meus: str = "",
    tipo: str = "",
    filtro: str = "",
    limit: int = 50,
    incluir_detalhe: bool = False,
    apenas_contar: bool = False,
    ctx: Context = None,
) -> str:
    """Lista processos da caixa da unidade atual (Controle de Processos), em
    formato ENXUTO e tipado, próprio para consumo por agente.

    IMPORTANTE: chame sei_trocar_unidade ANTES para que `atribuido_unidade_atual`
    seja resolvido corretamente para a unidade consultada (sem isso vem `null`).

    Parâmetros:
    - pagina: número da página (0=primeira). Cada página tem `limit` itens.
    - limit: itens por página (padrão 50).
    - apenas_meus: "S" para só processos atribuídos ao usuário (server-side).
    - filtro: busca textual server-side (protocolo, tipo, especificação, etc.).
    - tipo: substring (case-insensitive) no nome do tipo processual (client-side).
    - apenas_contar: se True, retorna só {total_itens, paginas} — barato, sem
      baixar a página pesada.
    - incluir_detalhe: se True, reanexa `ciencias` e `anotacoes` completas a cada
      item (fora da list view por padrão, para manter o payload pequeno).

    Cada processo (list view) traz campos derivados e tipados:
    - id_procedimento, protocolo, tipo, descricao (texto limpo, sem entidades HTML)
    - acesso: "publico" | "restrito" | "sigiloso"
    - atribuido_unidade_atual: {id_usuario, nome} resolvido para a unidade da
      sessão, ou `null` se não houver atribuição nela
    - gerado_ou_recebido, em_tramitacao, sobrestado, bloqueado, tem_documento_novo,
      tem_anotacao, tem_ciencia — todos BOOLEAN
    - marcador: {nome, cor} ou null · prazo: data ISO (aaaa-mm-dd) ou null
    - aberto_em_unidades: lista de siglas das unidades onde o processo está aberto

    NOTA: processos sobrestados e concluídos não aparecem. Para agrupamento
    estatístico use sei_resumo_processos.
    """
    try:
        if _web_scraper_enabled():
            web = _get_web_client(ctx)
            if web._inbox_url is None:
                await web.login()
            return _json(await web.listar_processos(
                detalhada=True,
                pagina=pagina,
                apenas_meus=(apenas_meus.upper() == "S"),
                tipo=tipo,
                filtro=filtro,
            ))

        client = _get_client(ctx)

        # Modo contagem barato (D-6): pega só o total, sem baixar a página cheia.
        if apenas_contar:
            head = await client.listar_processos(
                limit=1, start=0, apenas_meus=apenas_meus, filtro=filtro,
            )
            total = int(head.get("total_itens", 0))
            page_size = max(1, limit)
            paginas = (total + page_size - 1) // page_size
            return _json({"total_itens": total, "itens_por_pagina": page_size, "paginas": paginas})

        result = await client.listar_processos(
            limit=limit, start=pagina, apenas_meus=apenas_meus, filtro=filtro,
        )
        unidade_ativa_id = getattr(client, "_unidade_ativa", None)
        processos = [
            shape_processo_resumido(p, unidade_ativa_id, incluir_detalhe=incluir_detalhe)
            for p in result.get("processos", [])
        ]
        # `tipo`: substring client-side sobre o tipo já normalizado.
        if tipo:
            tl = tipo.lower()
            processos = [p for p in processos if tl in p.get("tipo", "").lower()]

        out = {
            "processos": processos,
            "pagina_atual": pagina,
            "itens_pagina": len(processos),
            "total_itens": result.get("total_itens"),
            "tem_proxima": result.get("tem_proxima", False),
        }
        if not unidade_ativa_id and processos:
            out["_nota"] = ("atribuido_unidade_atual veio null: chame "
                            "sei_trocar_unidade antes para resolver a atribuição "
                            "na unidade consultada.")
        return _json(out)
    except Exception as e:
        return _error(str(e))


_CAMPOS_AGRUPAMENTO = {
    "tipo": {
        "desc": "Tipo processual",
        "extract": lambda a, s: a.get("tipoProcesso", "Sem tipo"),
    },
    "atribuido": {
        "desc": "Usuário atribuído",
        "extract": lambda a, s: a.get("usuarioAtribuido") or "Sem atribuição",
    },
    "acesso": {
        "desc": "Nível de acesso",
        "extract": lambda a, s: {"0": "Público", "1": "Restrito", "2": "Sigiloso"}.get(
            s.get("nivelAcessoGlobal", "0"), "Desconhecido"
        ),
    },
    "tramitacao": {
        "desc": "Em tramitação",
        "extract": lambda a, s: "Em tramitação" if s.get("processoEmTramitacao") == "S" else "Fora de tramitação",
    },
    "sobrestado": {
        "desc": "Sobrestamento",
        "extract": lambda a, s: "Sobrestado" if s.get("processoSobrestado") == "S" else "Ativo",
    },
    "bloqueado": {
        "desc": "Bloqueio",
        "extract": lambda a, s: "Bloqueado" if s.get("processoBloqueado") == "S" else "Desbloqueado",
    },
    "novo": {
        "desc": "Documento novo",
        "extract": lambda a, s: "Com documentos novos" if s.get("documentoNovo") == "S" else "Sem documentos novos",
    },
    "anotacao": {
        "desc": "Anotação",
        "extract": lambda a, s: (
            "Anotação prioritária" if s.get("anotacaoPrioridade") == "S"
            else "Com anotação" if s.get("anotacao") == "S"
            else "Sem anotação"
        ),
    },
    "retorno": {
        "desc": "Retorno programado",
        "extract": lambda a, s: (
            f"Atrasado ({s.get('retornoData', '')})" if s.get("retornoAtrasado") == "S"
            else f"Programado ({s.get('retornoData', '')})" if s.get("retornoProgramado") == "S"
            else "Sem retorno"
        ),
    },
    "lido_usuario": {
        "desc": "Acessado pelo usuário",
        "extract": lambda a, s: "Lido" if s.get("processoAcessadoUsuario") == "S" else "Não lido",
    },
    "lido_unidade": {
        "desc": "Acessado pela unidade",
        "extract": lambda a, s: "Lido" if s.get("processoAcessadoUnidade") == "S" else "Não lido",
    },
    "origem": {
        "desc": "Gerado/Recebido",
        "extract": lambda a, s: "Gerado na unidade" if s.get("processoGeradoRecebido") == "G" else "Recebido",
    },
    "anexado": {
        "desc": "Anexado",
        "extract": lambda a, s: "Anexado" if s.get("processoAnexado") == "S" else "Independente",
    },
    "unidades": {
        "desc": "Unidades de abertura",
        "extract": lambda a, s: ", ".join(
            u.get("sigla", "") for u in a.get("dadosAbertura", {}).get("lista", [])
        ) or "N/A",
    },
    "marcador": {
        "desc": "Marcador",
        "extract": lambda a, s: ", ".join(
            m.get("nome", "") for m in a.get("marcador", [])
        ) or "Sem marcador",
    },
    "ciencia": {
        "desc": "Ciência",
        "extract": lambda a, s: "Com ciência" if s.get("ciencia") == "S" else "Sem ciência",
    },
}


@mcp.tool()
async def sei_resumo_processos(
    agrupar_por: str = "tipo",
    agrupar_por_2: str = "",
    apenas_meus: str = "",
    filtro: str = "",
    ctx: Context = None,
) -> str:
    """Gera um resumo agrupado dos processos da caixa da unidade atual.

    Busca TODOS os processos e agrupa por um ou dois campos.

    Campos disponíveis para agrupar_por e agrupar_por_2:
    - tipo: Tipo processual
    - atribuido: Usuário atribuído
    - acesso: Nível de acesso (Público/Restrito/Sigiloso)
    - tramitacao: Em tramitação ou não
    - sobrestado: Sobrestado ou ativo
    - bloqueado: Bloqueado ou não
    - novo: Com/sem documentos novos
    - anotacao: Com/sem anotação (inclui prioridade)
    - retorno: Retorno programado (inclui data e atraso)
    - lido_usuario: Acessado pelo usuário
    - lido_unidade: Acessado pela unidade
    - origem: Gerado na unidade ou recebido
    - anexado: Anexado a outro processo
    - unidades: Unidades onde está aberto
    - marcador: Marcador/etiqueta
    - ciencia: Com/sem ciência

    Exemplos:
    - agrupar_por="tipo" → quantidade por tipo processual
    - agrupar_por="atribuido" → distribuição por pessoa
    - agrupar_por="tipo", agrupar_por_2="atribuido" → cruzamento tipo × pessoa
    - agrupar_por="retorno" → processos com prazo vencido
    """
    try:
        campo1 = _CAMPOS_AGRUPAMENTO.get(agrupar_por)
        if not campo1:
            campos = ", ".join(sorted(_CAMPOS_AGRUPAMENTO.keys()))
            return _error(f"Campo '{agrupar_por}' inválido. Disponíveis: {campos}")

        campo2 = None
        if agrupar_por_2:
            campo2 = _CAMPOS_AGRUPAMENTO.get(agrupar_por_2)
            if not campo2:
                campos = ", ".join(sorted(_CAMPOS_AGRUPAMENTO.keys()))
                return _error(f"Campo '{agrupar_por_2}' inválido. Disponíveis: {campos}")

        client = _get_client(ctx)

        # Busca todos os processos
        todos = []
        pg = 0
        while True:
            result = await client.listar_processos(
                limit=200, start=pg, apenas_meus=apenas_meus, filtro=filtro,
            )
            todos.extend(result["processos"])
            if not result.get("tem_proxima"):
                break
            pg += 1

        # Agrupar
        grupos: dict = {}
        for p in todos:
            a = p.get("atributos", {})
            s = a.get("status", {})
            chave1 = campo1["extract"](a, s)

            if campo2:
                chave2 = campo2["extract"](a, s)
                chave = f"{chave1} | {chave2}"
            else:
                chave = chave1

            if chave not in grupos:
                grupos[chave] = {"quantidade": 0, "processos": []}
            grupos[chave]["quantidade"] += 1
            grupos[chave]["processos"].append(a.get("numero", ""))

        # Ordenar por quantidade decrescente
        resumo = []
        for chave in sorted(grupos.keys(), key=lambda k: -grupos[k]["quantidade"]):
            g = grupos[chave]
            item = {"grupo": chave, "quantidade": g["quantidade"]}
            # Incluir lista de processos se grupo pequeno (≤ 20)
            if g["quantidade"] <= 20:
                item["processos"] = g["processos"]
            resumo.append(item)

        header = campo1["desc"]
        if campo2:
            header += f" × {campo2['desc']}"

        return _json({
            "agrupamento": header,
            "total_processos": len(todos),
            "total_grupos": len(resumo),
            "grupos": resumo,
        })
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_pesquisar_processos(
    palavras_chave: str = "",
    descricao: str = "",
    busca_rapida: str = "",
    data_inicio: str = "",
    data_fim: str = "",
    sta_tipo_data: str = "",
    id_unidade_geradora: str = "",
    id_assunto: str = "",
    grupo: str = "",
    limit: int = 50,
    pagina: int = 0,
    ctx: Context = None,
) -> str:
    """Pesquisa processos no SEI por texto, descrição, datas, unidade ou assunto.

    Use palavras_chave para busca geral ou busca_rapida para busca simplificada.
    Datas no formato DD/MM/AAAA.

    Filtros adicionais:
    - sta_tipo_data: tipo de período — "30" (últimos 30 dias), "60" (últimos 60 dias)
      ou "0" (personalizado, requer data_inicio/data_fim)
    - id_unidade_geradora: id da unidade que gerou o processo (use sei_listar_unidades)
    - id_assunto: id do assunto (use sei_pesquisar_assuntos para obter o id)
    - grupo: id do grupo de acompanhamento (use sei_listar_grupos_acompanhamento)

    Paginação: pagina=0 é a primeira página, pagina=1 a segunda, etc.
    """
    try:
        client = _get_client(ctx)
        result = await client.pesquisar_processos(
            palavras_chave=palavras_chave,
            descricao=descricao,
            busca_rapida=busca_rapida,
            data_inicio=data_inicio,
            data_fim=data_fim,
            sta_tipo_data=sta_tipo_data,
            id_unidade_geradora=id_unidade_geradora,
            id_assunto=id_assunto,
            grupo=grupo,
            limit=limit,
            start=pagina,
        )
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_pesquisar_hipoteses_legais(
    filtro: str = "",
    limit: int = 50,
    pagina: int = 0,
    ctx: Context = None,
) -> str:
    """Pesquisa hipóteses legais disponíveis no SEI.

    Necessário ao criar processos ou documentos com nível de acesso
    restrito ou sigiloso. Use o 'id' retornado no parâmetro
    hipotese_legal de sei_criar_processo.

    Exemplos: "pessoal", "controle interno", "sigilo fiscal"
    """
    try:
        client = _get_client(ctx)
        result = await client.pesquisar_hipoteses_legais(
            filtro=filtro, limit=limit, start=pagina,
        )
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_pesquisar_tipos_processo(
    filtro: str = "",
    favoritos: str = "",
    limit: int = 50,
    pagina: int = 0,
    ctx: Context = None,
) -> str:
    """Pesquisa tipos de processo disponíveis no SEI.

    Parâmetros:
    - filtro: texto para filtrar por nome (ex: "Plano Anual", "Fiscalização")
    - favoritos: "S" para apenas favoritos
    - limit/pagina: paginação

    Use o 'id' retornado como tipo_processo em sei_criar_processo.
    """
    try:
        client = _get_client(ctx)
        result = await client.pesquisar_tipos_processo(
            filtro=filtro, favoritos=favoritos, limit=limit, start=pagina,
        )
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_alterar_processo(
    processo: str,
    especificacao: str = "",
    nivel_acesso: str = "",
    hipotese_legal: str = "",
    observacao: str = "",
    ctx: Context = None,
) -> str:
    """Altera metadados de um processo no SEI.

    Parâmetros:
    - processo: protocolo formatado (ex: 50300.009752/2026-77) ou IdProcedimento
    - especificacao: nova descrição/especificação do processo
    - nivel_acesso: 0=público, 1=restrito, 2=sigiloso
    - hipotese_legal: ID da hipótese legal (obrigatório se restrito/sigiloso).
      Use sei_pesquisar_hipoteses_legais para descobrir o ID.
    - observacao: observações adicionais

    Informe apenas os campos que deseja alterar.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.alterar_processo(
            id_procedimento=id_proc,
            especificacao=especificacao,
            nivel_acesso=nivel_acesso,
            hipotese_legal=hipotese_legal,
            observacao=observacao,
        )
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_criar_processo(
    tipo_processo: str,
    especificacao: str = "",
    assuntos: str = "",
    interessados: str = "",
    observacoes: str = "",
    nivel_acesso: str = "0",
    hipotese_legal: str = "",
    ctx: Context = None,
) -> str:
    """Cria um novo processo no SEI.

    Parâmetros:
    - tipo_processo: ID do tipo de processo (use sei_pesquisar_tipos_processo)
    - especificacao: descrição do processo (recomendado para organizar a caixa)
    - assuntos: IDs dos assuntos (separados por vírgula)
    - interessados: IDs dos interessados (separados por vírgula)
    - observacoes: observações adicionais
    - nivel_acesso: 0=público (padrão), 1=restrito, 2=sigiloso
    - hipotese_legal: ID da hipótese legal (obrigatório se restrito/sigiloso).
      Use sei_pesquisar_hipoteses_legais para descobrir o ID.

    Retorna o IdProcedimento e ProtocoloFormatado do processo criado.

    Para assuntos, use sei_pesquisar_tipos_processo para ver as sugestões
    de assunto do tipo de processo escolhido (endpoint /processo/assunto/sugestao).
    """
    try:
        client = _get_client(ctx)
        result = await client.criar_processo(
            tipo_processo=tipo_processo,
            especificacao=especificacao,
            assuntos=assuntos,
            interessados=interessados,
            observacoes=observacoes,
            nivel_acesso=nivel_acesso,
            hipotese_legal=hipotese_legal,
        )
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_enviar_processo(
    numero_processo: str,
    unidades_destino: str,
    manter_aberto: str = "N",
    remover_anotacao: str = "N",
    enviar_email: str = "N",
    data_retorno: str = "",
    dias_retorno: str = "",
    ctx: Context = None,
) -> str:
    """Envia (tramita) um processo para outra(s) unidade(s) no SEI.

    Parâmetros:
    - numero_processo: protocolo formatado (ex: 50300.000123/2025-00)
    - unidades_destino: sigla da unidade (ex: "SFC", "ECP-SFC") OU ID numérico.
      Para múltiplas unidades, separe por vírgula.
      Se informar sigla, resolve o ID automaticamente via pesquisa.
    - manter_aberto: "N" fechar na unidade atual (padrão), "S" manter aberto
    - remover_anotacao: "S" remover anotações, "N" manter (padrão)
    - enviar_email: "S" notificar por email (só se o usuário pedir)
    - data_retorno: data de retorno programado DD/MM/AAAA (só se o usuário pedir)
    - dias_retorno: prazo em dias para retorno (alternativa à data, só se pedir)
    """
    try:
        client = _get_client(ctx)

        # Resolver unidades destino: aceita sigla ou ID
        destinos = [d.strip() for d in unidades_destino.split(",")]
        ids_resolvidos = []
        for destino in destinos:
            if destino.isdigit():
                ids_resolvidos.append(destino)
            else:
                # Pesquisar pela sigla/nome
                result = await client.pesquisar_unidades(filtro=destino, limit=10)
                unidades = result.get("unidades", [])
                encontrou = False
                for u in unidades:
                    sigla = u.get("sigla", "")
                    if sigla.upper() == destino.upper():
                        ids_resolvidos.append(str(u.get("id", "")))
                        encontrou = True
                        break
                if not encontrou:
                    if unidades:
                        # Usar a primeira que contém o texto
                        ids_resolvidos.append(str(unidades[0].get("id", "")))
                    else:
                        return _json({
                            "error": f"Unidade '{destino}' não encontrada",
                            "dica": "Use sei_pesquisar_unidades para buscar.",
                        })

        result = await client.enviar_processo(
            numero_processo=numero_processo,
            unidades_destino=",".join(ids_resolvidos),
            manter_aberto=manter_aberto,
            remover_anotacao=remover_anotacao,
            enviar_email=enviar_email,
            data_retorno=data_retorno,
            dias_retorno=dias_retorno,
        )
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_marcar_nao_lido(
    numero_processo: str,
    ctx: Context = None,
) -> str:
    """Marca um processo como não lido na unidade atual.

    O SEI não possui funcionalidade nativa para isso. Esta tool usa
    o workaround de enviar o processo para a própria unidade, o que
    faz o SEI tratar como novo recebimento (não lido).

    - numero_processo: protocolo formatado (ex: 50300.012639/2023-26)
    """
    try:
        client = _get_client(ctx)
        if not client._unidade_ativa:
            return _error(
                "Unidade ativa não definida. Use sei_trocar_unidade primeiro."
            )
        result = await client.enviar_processo(
            numero_processo=numero_processo,
            unidades_destino=client._unidade_ativa,
            manter_aberto="S",
            remover_anotacao="N",
            enviar_email="N",
        )
        return _json({
            "mensagem": "Processo marcado como não lido.",
            "detalhe": result.get("mensagem", ""),
        })
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_concluir_processo(numero_processo: str, ctx: Context = None) -> str:
    """Conclui um processo na unidade atual do SEI.

    O processo é removido da caixa da unidade mas permanece acessível.
    Use sei_reabrir_processo para reverter.
    """
    try:
        client = _get_client(ctx)
        result = await client.concluir_processo(numero_processo)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_reabrir_processo(processo: str, ctx: Context = None) -> str:
    """Reabre um processo que foi concluído na unidade.

    - processo: protocolo formatado (ex: 50300.018905/2018-67) ou IdProcedimento

    O processo volta para a caixa da unidade atual.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.reabrir_processo(id_proc)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_atribuir_processo(
    numero_processo: str,
    usuario: str,
    ctx: Context = None,
) -> str:
    """Atribui um processo a um usuário da unidade.

    Parâmetros:
    - numero_processo: protocolo formatado (ex: 50300.000123/2025-00)
    - usuario: ID numérico do usuário OU nome/parte do nome
      (ex: "100001860" ou "Karina" ou "Karina Shimoishi")

    Quando um nome é informado, busca os usuários correspondentes
    e tenta atribuir a cada um até encontrar um com permissão
    na unidade atual.
    """
    try:
        client = _get_client(ctx)

        # Se parece ser um ID numérico, usa direto
        if usuario.isdigit():
            result = await client.atribuir_processo(numero_processo, usuario)
            return _json(result)

        # Busca por nome
        result = await client.listar_usuarios(filtro=usuario)
        candidatos = result.get("usuarios", [])
        if not candidatos:
            return _json({
                "error": f"Nenhum usuário encontrado com '{usuario}'",
                "dica": "Use sei_listar_usuarios para ver os usuários disponíveis.",
            })

        # Tentar cada candidato até um funcionar
        erros = []
        for u in candidatos:
            id_u = u.get("id_usuario", "")
            nome = u.get("nome", "")
            sigla = u.get("sigla", "")
            try:
                result = await client.atribuir_processo(numero_processo, id_u)
                return _json({
                    "mensagem": result.get("mensagem", "Processo atribuído com sucesso!"),
                    "usuario": {"id": id_u, "nome": nome, "sigla": sigla},
                })
            except Exception as e:
                erros.append(f"{nome} ({sigla}): {e}")
                continue

        return _json({
            "error": f"Nenhum dos {len(candidatos)} usuários com '{usuario}' tem permissão na unidade atual",
            "tentativas": erros,
            "dica": "Verifique se está na unidade correta com sei_trocar_unidade.",
        })
    except Exception as e:
        return _error(str(e))


# ---------------------------------------------------------------------------
# Tools de documentos — assinar, pesquisar tipos
# ---------------------------------------------------------------------------


@mcp.tool()
async def sei_cancelar_assinatura(
    id_documento: str,
    ctx: Context = None,
) -> str:
    """Tenta cancelar (derrubar) a assinatura de um documento no SEI.

    Aceita id interno ou número SEI (protocoloFormatado).

    A API do SEI não possui endpoint direto para cancelar assinatura.
    Esta tool tenta forçar uma edição mínima no documento para que o
    SEI remova a assinatura automaticamente (comportamento padrão ao editar).

    LIMITAÇÃO: só funciona se o processo não foi enviado/lido por outra
    unidade. Se falhar, o usuário deve cancelar a assinatura pela
    interface web do SEI (botão "Editar Conteúdo" no documento).
    """
    try:
        client = _get_client(ctx)
        import html as html_module

        # Resolver número SEI → id interno
        doc_id = id_documento.strip()
        try:
            doc_id, _ = await _resolver_documento(client, doc_id)
        except Exception:
            pass

        # Verificar se está assinado
        secoes_data = await client.listar_secao_documento(doc_id)
        versao = str(secoes_data.get("ultimaVersaoDocumento", "1"))

        # Montar payload com todas as seções (mesmo conteúdo)
        secoes_enviar = []
        for s in secoes_data.get("secoes", []):
            if not isinstance(s, dict):
                continue
            sid = s.get("id")
            modelo = s.get("idSecaoModelo")
            conteudo = html_module.unescape(s.get("conteudo", "") or "")
            secoes_enviar.append({
                "id": str(sid),
                "idSecaoModelo": str(modelo),
                "conteudo": sanitize_iso8859(conteudo),
            })

        # Tentar editar (derruba assinatura se permitido)
        result = await client.alterar_secao_documento(doc_id, secoes_enviar, versao)
        return _json({
            "mensagem": "Assinatura cancelada com sucesso. O documento foi editado (nova versão).",
            "versao": result,
        })
    except Exception as e:
        msg = str(e)
        if "assinado" in msg.lower():
            return _json({
                "error": "Não foi possível cancelar a assinatura via API.",
                "motivo": msg,
                "dica": "O processo pode ter sido enviado ou lido por outra unidade. "
                        "Cancele a assinatura pela interface web do SEI: "
                        "abra o documento → clique em 'Editar Conteúdo'.",
            })
        return _error(msg)


async def _pedir_cargo(client: SEIClient) -> str:
    """Resposta das tools de assinatura chamadas sem `cargo`: os cargos disponíveis."""
    try:
        resp = await client._request("GET", "/assinante/listar")
        cargos = resp.json().get("data", [])
    except Exception:
        cargos = []
    return _json({
        "error": "Cargo/Função não informado — é obrigatório para assinatura.",
        "cargos_disponiveis": cargos,
        "dica": "O cargo é escolha do usuário, entre os de cargos_disponiveis, e vai "
                "no parâmetro `cargo`. A tool não guarda o cargo entre chamadas.",
    })


async def _resolver_cargo(client: SEIClient, cargo: str) -> tuple[str, str | None]:
    """Devolve (nome_do_cargo_no_SEI, None) ou ("", resposta_para_escolher).

    O SEI assina pelo NOME exato do cargo. Aceita o id da lista (ex.: "7376"),
    o nome exato ou parte dele (sem acento, palavras em qualquer ordem). Nome
    exato tem precedência ("Diretor do …" × "Diretora do …"). Se a lista não
    puder ser lida, segue com o texto informado, como antes.
    """
    try:
        resp = await client._request("GET", "/assinante/listar")
        cargos = [c for c in (resp.json().get("data") or []) if isinstance(c, dict)]
    except Exception:
        return cargo, None
    if not cargos:
        return cargo, None

    def nome(c):
        return html.unescape(str(c.get("nome") or ""))

    texto = str(cargo).strip()
    if texto.isdigit():
        por_id = [c for c in cargos if str(c.get("id") or "") == texto]
        if len(por_id) == 1:
            return nome(por_id[0]), None
    alvo = _normalizar_busca(texto)
    exatos = [c for c in cargos if _normalizar_busca(nome(c)) == alvo]
    if len(exatos) == 1:
        return nome(exatos[0]), None
    termos = alvo.split()
    parecidos = [c for c in cargos if termos and all(t in _normalizar_busca(nome(c)) for t in termos)]
    if len(parecidos) == 1:
        return nome(parecidos[0]), None
    return "", _json({
        "error": (f"Nenhum cargo corresponde a {texto!r} na unidade atual." if not parecidos
                  else f"Mais de um cargo corresponde a {texto!r}."),
        "cargos_disponiveis": parecidos or cargos,
        "dica": "Nada foi assinado. Informe o `id` ou o nome exato do cargo em `cargo`.",
    })


async def _ativar_unidade_geradora(client: SEIClient, doc_id: str) -> str | None:
    """Ativa a unidade geradora do documento, se o usuário tiver acesso a ela.

    O SEI valida cargo e permissão contra a unidade ativa da sessão. Ativar a
    unidade geradora evita o erro "cargo não associado" quando o usuário está
    lotado em mais de uma unidade. É melhor-esforço: se o documento for de
    outra unidade (ex.: recebido para assinatura), ou se alguma consulta
    falhar, nada é trocado e o próprio SEI decide sobre a assinatura.

    Retorna o id da unidade ativada, ou None.
    """
    try:
        meta = await client.consultar_documento_interno(doc_id)
        id_procedimento = str(meta.get("protocolo") or meta.get("idProcedimento") or "").strip()
        if not id_procedimento:
            return None

        dados = await client.listar_documentos_todos(id_procedimento)
        id_unidade = ""
        for item in dados.get("documentos", []):
            if not isinstance(item, dict):
                continue
            atrib = item.get("atributos") if isinstance(item.get("atributos"), dict) else {}
            id_item = str(item.get("id") or atrib.get("idProtocolo") or "").strip()
            if id_item == str(doc_id).strip():
                id_unidade = str(atrib.get("idUnidade") or "").strip()
                break
        if not id_unidade:
            return None

        unidades = await client.listar_unidades_usuario()
        permitidas = {
            str(u.get("id") or u.get("idUnidade") or "").strip()
            for u in unidades if isinstance(u, dict)
        }
        if id_unidade not in permitidas:
            return None

        await client.trocar_unidade(id_unidade)
        return id_unidade
    except Exception:
        logger.debug("Não foi possível ativar a unidade geradora de %s", doc_id, exc_info=True)
        return None


@mcp.tool()
async def sei_assinar_documento(
    id_documento: str,
    cargo: str = "",
    orgao: str = "",
    ctx: Context = None,
) -> str:
    """Assina eletronicamente um documento no SEI.

    A autenticação é automática — basta informar o documento e o cargo.

    O parâmetro `cargo` é obrigatório para assinar. Chamada sem cargo não assina:
    devolve a lista de cargos disponíveis, para o usuário escolher. O cargo não
    fica guardado entre chamadas.

    Parâmetros:
    - id_documento: ID interno do documento ou número SEI (protocoloFormatado).
      Se for número SEI, resolve automaticamente via pesquisa Solr.
    - cargo: cargo/função para assinatura (ex: "Agente Público").
      OBRIGATÓRIO. Se omitido, retorna a lista de cargos disponíveis.
    - orgao: código do órgão (usa o padrão se omitido)
    """
    try:
        client = _get_client(ctx)
        login = client._usuario
        senha = client._senha

        # Resolver número SEI → id interno (sempre, pois ambos são numéricos
        # e indistinguíveis pelo formato; o resolver tenta Solr primeiro
        # e só cai para id direto se Solr não achar)
        doc_id = id_documento.strip()
        try:
            doc_id, _ = await _resolver_documento(client, doc_id)
        except Exception:
            doc_id = id_documento.strip()  # Manter original se resolver falhar

        if not cargo:
            return await _pedir_cargo(client)

        # Identidade do assinante: IdUsuario e IdOrgao vêm do loginData da
        # MESMA autenticação que gerou o token (o AssinaturaRN do SEI exige os
        # dois). Não se pesquisa identidade em /usuario/listar.
        await client._get_headers()
        if not client._id_usuario:
            await client.autenticar()
        id_usuario = str(client._id_usuario or "").strip()
        id_orgao_usuario = str(client._id_orgao_usuario or client._orgao or "").strip()

        if not id_usuario:
            return _json({
                "error": "Assinatura não enviada: a autenticação do SEI não "
                         "informou o IdUsuario (loginData)."
            })
        if not id_orgao_usuario:
            return _json({
                "error": "Assinatura não enviada: órgão do usuário não identificado."
            })

        unidade_ativada = await _ativar_unidade_geradora(client, doc_id)

        cargo, escolha = await _resolver_cargo(client, cargo)
        if escolha:
            return escolha

        result = await client.assinar_documento(
            id_documento=doc_id,
            login=login,
            senha=senha,
            cargo=cargo,
            orgao=orgao or id_orgao_usuario,
            id_usuario=id_usuario,
        )
        if isinstance(result, dict) and unidade_ativada:
            result.setdefault("_unidade_ativada", unidade_ativada)
        return _json(result)
    except Exception as e:
        return _error(str(e))


async def _tipos_documento(client: SEIClient, filtro: str = "", favoritos: str = "",
                           aplicabilidade: str = "", limit: int = 50, pagina: int = 0) -> dict:
    """Busca de tipos de documento (nativa + varredura sem acento). Ver a tool."""
    result = await client.pesquisar_tipos_documento(
        filtro=filtro,
        favoritos=favoritos,
        aplicabilidade=aplicabilidade,
        limit=limit,
        start=pagina,
    )
    termos = _normalizar_busca(filtro).split()
    if not termos or pagina:
        return result

    # A busca do SEI é por substring exata e sensível a acento ("Oficio"
    # não acha "Ofício"). Completa com a lista inteira comparada
    # normalizada — lida uma vez e mantida no cache do cliente (1 h).
    chave = f"tipos_documento:{favoritos}:{aplicabilidade}"
    todos = client._cache_get(chave)
    if todos is None:
        todos, ids = [], set()
        for pg in range(10):
            r = await client.pesquisar_tipos_documento(
                favoritos=favoritos, aplicabilidade=aplicabilidade, limit=500, start=pg
            )
            itens = r.get("tipos") or []
            novos = [t for t in itens if str(t.get("id")) not in ids]
            ids.update(str(t.get("id")) for t in novos)
            todos.extend(novos)
            if len(itens) < 500 or not novos:
                break
        client._cache_set(chave, todos)

    vistos = {str(t.get("id")) for t in result.get("tipos") or []}
    extras = [
        t for t in todos
        if str(t.get("id")) not in vistos
        and all(x in _normalizar_busca(t.get("nome", "")) for x in termos)
    ]
    tipos, vistos = [], set()
    for t in list(result.get("tipos") or []) + extras:
        ident = str(t.get("id"))
        if ident not in vistos and all(x in _normalizar_busca(t.get("nome", "")) for x in termos):
            vistos.add(ident)
            tipos.append(t)
    return ({
        "tipos": tipos[:limit],
        "pagina_atual": 0,
        "itens_pagina": min(len(tipos), limit),
        "total_itens": len(tipos),
        "tem_proxima": len(tipos) > limit,
    })


@mcp.tool()
async def sei_pesquisar_tipos_documento(
    filtro: str = "",
    favoritos: str = "",
    aplicabilidade: str = "",
    limit: int = 50,
    pagina: int = 0,
    ctx: Context = None,
) -> str:
    """Pesquisa tipos de documento (séries) disponíveis no SEI.

    Parâmetros:
    - filtro: texto para filtrar por nome do tipo
    - favoritos: "S" para apenas favoritos
    - aplicabilidade: "I" para internos, "F" para externos, ou "I,F" para ambos
    - limit: quantidade por página
    - pagina: número da página (0=primeira)

    O filtro ignora acentos e maiúsculas e aceita as palavras em qualquer
    ordem ("oficio circular" encontra "Ofício-Circular").

    Use o 'id' retornado como id_serie em sei_criar_documento.
    """
    try:
        client = _get_client(ctx)
        return _json(await _tipos_documento(client, filtro, favoritos, aplicabilidade, limit, pagina))
    except Exception as e:
        return _error(str(e))


# ---------------------------------------------------------------------------
# Tools de anotação
# ---------------------------------------------------------------------------


@mcp.tool()
async def sei_sobrestar_processo(
    processo: str,
    motivo: str,
    processo_vinculado: str = "",
    ctx: Context = None,
) -> str:
    """Sobresta um processo no SEI.

    Parâmetros:
    - processo: protocolo formatado (ex: 50300.018905/2018-67) ou IdProcedimento
    - motivo: motivo do sobrestamento (obrigatório)
    - processo_vinculado: protocolo de outro processo para vincular (opcional).
      Se informado, o sobrestamento fica vinculado ao andamento desse processo.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)

        proto_vinculado = ""
        if processo_vinculado:
            proto_vinculado = await _resolver_processo(client, processo_vinculado)

        result = await client.sobrestar_processo(
            id_procedimento=id_proc,
            motivo=motivo,
            protocolo_vinculado=proto_vinculado,
        )
        return _json(result)
    except Exception as e:
        msg = str(e)
        # Erro comum: processo aberto em outras unidades
        if "aberto" in msg.lower() or "unidade" in msg.lower() or "sobrestar" in msg.lower():
            # Tentar listar unidades onde o processo está aberto
            try:
                proc = await client.consultar_processo(processo)
                resp = await client._request(
                    "GET", f"/processo/listar/unidades/{id_proc}"
                )
                data = resp.json()
                unidades = data.get("data", [])
                nomes = [f"{u.get('sigla', '')} ({u.get('id', '')})" for u in unidades]
                return _json({
                    "error": msg,
                    "unidades_abertas": nomes,
                    "dica": "O processo precisa estar aberto somente na unidade atual "
                            "para ser sobrestado. Conclua o processo nas unidades "
                            "listadas acima antes de sobrestar.",
                })
            except Exception:
                pass
        return _error(msg)


@mcp.tool()
async def sei_remover_sobrestamento(
    processo: str,
    ctx: Context = None,
) -> str:
    """Remove o sobrestamento de um processo no SEI.

    - processo: protocolo formatado (ex: 50300.018905/2018-67) ou IdProcedimento
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.remover_sobrestamento(id_proc)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_dar_ciencia(
    referencia: str,
    tipo: Literal["documento", "processo"] = "documento",
    ctx: Context = None,
) -> str:
    """Dá ciência em um documento ou processo no SEI.

    Parâmetros:
    - referencia: número SEI do documento OU protocolo/IdProcedimento do processo
    - tipo: "documento" (padrão) ou "processo"

    Exemplos:
    - sei_dar_ciencia("1482875", tipo="documento")  → ciência na NT 16
    - sei_dar_ciencia("50300.018905/2018-67", tipo="processo")  → ciência no processo
    """
    try:
        client = _get_client(ctx)

        if tipo == "documento":
            # Resolver número SEI → id interno
            doc_id, _ = await _resolver_documento(client, referencia)
            result = await client.dar_ciencia_documento(doc_id)
            return _json(result)
        else:
            # Resolver protocolo → IdProcedimento
            id_proc = await _resolver_processo(client, referencia)
            result = await client.dar_ciencia_processo(id_proc)
            return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_listar_ciencias(
    referencia: str,
    tipo: Literal["documento", "processo"] = "documento",
    ctx: Context = None,
) -> str:
    """Lista as ciências registradas em um documento ou processo.

    Parâmetros:
    - referencia: número SEI do documento OU protocolo/IdProcedimento do processo
    - tipo: "documento" (padrão) ou "processo"
    """
    try:
        client = _get_client(ctx)

        if tipo == "documento":
            doc_id, _ = await _resolver_documento(client, referencia)
            result = await client.listar_ciencias_documento(doc_id)
        else:
            id_proc = await _resolver_processo(client, referencia)
            result = await client.listar_ciencias_processo(id_proc)
        return _json(result)
    except Exception as e:
        return _error(str(e))


# ---------------------------------------------------------------------------
# Tools adicionais de processo
# ---------------------------------------------------------------------------


@mcp.tool()
async def sei_remover_atribuicao(
    processo: str,
    ctx: Context = None,
) -> str:
    """Remove a atribuição de um processo (desatribui de qualquer usuário).

    - processo: protocolo formatado ou IdProcedimento
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.remover_atribuicao(id_proc)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_receber_processo(
    processo: str,
    ctx: Context = None,
) -> str:
    """Confirma o recebimento de um processo na unidade atual.

    - processo: protocolo formatado ou IdProcedimento
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.receber_processo(id_proc)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_listar_unidades_processo(
    processo: str,
    ctx: Context = None,
) -> str:
    """Lista as unidades onde o processo está aberto e a quem está atribuído.

    Aceita protocolo formatado ou IdProcedimento. Em processo sigiloso, o SEI
    informa o usuário com quem está aberto em vez da atribuição.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.listar_unidades_processo(id_proc)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_listar_interessados(
    processo: str,
    ctx: Context = None,
) -> str:
    """Lista os interessados de um processo."""
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.listar_interessados(id_proc)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_listar_sobrestamentos(
    processo: str,
    ctx: Context = None,
) -> str:
    """Lista o histórico de sobrestamentos de um processo."""
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.listar_sobrestamentos(id_proc)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_listar_assinaturas(
    id_documento: str,
    ctx: Context = None,
) -> str:
    """Lista as assinaturas de um documento."""
    try:
        client = _get_client(ctx)
        result = await client.listar_assinaturas(id_documento)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_registrar_andamento(
    processo: str,
    descricao: str,
    ctx: Context = None,
) -> str:
    """Registra um andamento (atividade) no processo.

    - processo: protocolo formatado ou IdProcedimento
    - descricao: texto do andamento
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.registrar_andamento(id_proc, descricao)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_pesquisar_contatos(
    filtro: str = "",
    limit: int = 50,
    ctx: Context = None,
) -> str:
    """Pesquisa contatos cadastrados no SEI."""
    try:
        client = _get_client(ctx)
        result = await client.pesquisar_contatos(filtro=filtro, limit=limit)
        return _json(result)
    except Exception as e:
        return _error(str(e))


# Teto local do upload por base64. O limite do próprio SEI não serve de teto
# prático (na ANTAQ, p.ex., SEI_TAM_MB_DOC_EXTERNO = 5124 MB): o gargalo real é
# trafegar o arquivo inteiro dentro de uma mensagem MCP, em base64 (+33%).
_MAX_UPLOAD_BASE64_MB = float(os.environ.get("SEI_MAX_UPLOAD_BASE64_MB", "50"))


async def _limite_upload_bytes(client: SEIClient, nome_arquivo: str = "") -> int:
    """Limite de upload do SEI em bytes (0 = não foi possível determinar).

    `/upload/parametros` devolve tamanhos em MB: `tamanhoDocDefault` e um
    `tamanho` por extensão. Best-effort — se a consulta falhar, o upload segue e
    quem recusa é o SEI.
    """
    try:
        params = await client.parametros_upload()
    except Exception:
        return 0
    mb = params.get("tamanhoDocDefault")
    ext = nome_arquivo.rsplit(".", 1)[-1].lower() if "." in nome_arquivo else ""
    if ext:
        for e in params.get("extensoes", []) or []:
            if str(e.get("extensao", "")).lower() == ext and e.get("tamanho"):
                mb = e["tamanho"]
                break
    try:
        return int(float(mb) * 1024 * 1024)
    except (TypeError, ValueError):
        return 0


def _decodificar_upload_base64(arquivo_base64: str, nome_arquivo: str) -> tuple[bytes, str]:
    """Decodifica o anexo em base64 (aceita data URI). Retorna (bytes, erro)."""
    if not nome_arquivo:
        return b"", (
            "nome_arquivo é obrigatório com arquivo_base64 — o SEI usa a "
            "extensão (ex: 'parecer.pdf') para determinar o tipo do anexo."
        )
    try:
        # Aceita data URI (data:application/pdf;base64,...) e base64 puro
        bruto = arquivo_base64.split(",", 1)[-1] if arquivo_base64.startswith("data:") \
            else arquivo_base64
        conteudo = base64.b64decode(bruto, validate=True)
    except Exception:
        return b"", "arquivo_base64 não é base64 válido."
    if not conteudo:
        return b"", "arquivo_base64 decodificou para 0 bytes."

    teto_local = int(_MAX_UPLOAD_BASE64_MB * 1024 * 1024)
    if len(conteudo) > teto_local:
        dica = "" if _http_mode else (
            " Para arquivos maiores, coloque-o no disco do servidor MCP e use arquivo_path."
        )
        return b"", (
            f"Arquivo com {len(conteudo) / 1048576:.1f} MB excede o teto "
            f"desta tool para envio em base64 ({_MAX_UPLOAD_BASE64_MB:g} MB, "
            f"ajustável em SEI_MAX_UPLOAD_BASE64_MB).{dica}"
        )
    return conteudo, ""


_ERRO_ARQUIVO_PATH_REMOTO = (
    "arquivo_path está desabilitado neste servidor: ele roda remotamente e o "
    "caminho seria lido do disco DELE, não do seu. Envie o arquivo em "
    "arquivo_base64 + nome_arquivo."
)


@mcp.tool()
async def sei_criar_documento_externo(
    processo: str,
    id_serie: str,
    arquivo_path: str = "",
    descricao: str = "",
    nivel_acesso: str = "0",
    arquivo_base64: str = "",
    nome_arquivo: str = "",
    data_elaboracao: str = "",
    ctx: Context = None,
) -> str:
    """Cria um documento externo (upload de arquivo) em um processo SEI.

    - processo: protocolo formatado ou IdProcedimento
    - id_serie: tipo do documento (use sei_pesquisar_tipos_documento)
    - descricao: descrição do documento
    - nivel_acesso: 0=público (padrão), 1=restrito, 2=sigiloso
    - data_elaboracao: dd/mm/aaaa (padrão: hoje)

    O arquivo entra por UM dos dois caminhos:
    - arquivo_base64 + nome_arquivo: conteúdo do arquivo em base64. É o caminho
      a usar quando o arquivo não está no disco do servidor MCP — por exemplo
      um PDF vindo do Drive, gerado na conversa ou baixado de outra tool.
      O nome_arquivo importa: o SEI usa a extensão para tipar o anexo.
    - arquivo_path: caminho local NO SERVIDOR onde o MCP roda (não no seu
      computador). Só serve para arquivos que já estão lá, e só no modo local
      (stdio) — no servidor remoto está desabilitado.

    O limite de tamanho é o do próprio SEI (veja sei_parametros_upload).
    """
    try:
        if arquivo_path and _http_mode:
            return _error(_ERRO_ARQUIVO_PATH_REMOTO)

        client = _get_client(ctx)

        if arquivo_base64 and arquivo_path:
            return _error("Informe arquivo_base64 OU arquivo_path, não os dois.")
        if not arquivo_base64 and not arquivo_path:
            return _error(
                "Informe arquivo_base64 + nome_arquivo (conteúdo em memória) ou "
                "arquivo_path (caminho no servidor onde o MCP roda)."
            )

        conteudo = b""
        if arquivo_base64:
            conteudo, erro = _decodificar_upload_base64(arquivo_base64, nome_arquivo)
            if erro:
                return _error(erro)

            limite = await _limite_upload_bytes(client, nome_arquivo)
            if limite and len(conteudo) > limite:
                return _error(
                    f"Arquivo com {len(conteudo)} bytes excede o limite do SEI "
                    f"({limite} bytes). Veja sei_parametros_upload."
                )

        id_proc = await _resolver_processo(client, processo)
        result = await client.criar_documento_externo(
            id_procedimento=id_proc, id_serie=id_serie,
            arquivo_path=arquivo_path, descricao=descricao,
            nivel_acesso=nivel_acesso,
            arquivo_bytes=conteudo, nome_arquivo=nome_arquivo,
            data_elaboracao=data_elaboracao,
        )
        if isinstance(result, dict):
            result = {**result, "bytes_enviados": len(conteudo) or None}
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_assinar_bloco(
    id_bloco: str,
    cargo: str = "",
    ctx: Context = None,
) -> str:
    """Assina TODOS os documentos de um bloco de assinatura.

    A autenticação é automática — basta informar o bloco e o cargo.

    O parâmetro `cargo` é obrigatório para assinar. Chamada sem cargo não assina:
    devolve a lista de cargos disponíveis, para o usuário escolher. O cargo não
    fica guardado entre chamadas.

    - id_bloco: ID do bloco
    - cargo: cargo/função — OBRIGATÓRIO (se omitido, lista opções disponíveis)
    """
    try:
        client = _get_client(ctx)
        login = client._usuario
        senha = client._senha
        if not cargo:
            return await _pedir_cargo(client)
        cargo, escolha = await _resolver_cargo(client, cargo)
        if escolha:
            return escolha
        result = await client.assinar_bloco(
            id_bloco=id_bloco, login=login, senha=senha, cargo=cargo,
        )
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_assinar_documentos_bloco(
    documentos: str,
    cargo: str = "",
    ctx: Context = None,
) -> str:
    """Assina documentos específicos de um bloco de assinatura.

    A autenticação é automática — basta informar os documentos e o cargo.

    O parâmetro `cargo` é obrigatório para assinar. Chamada sem cargo não assina:
    devolve a lista de cargos disponíveis, para o usuário escolher. O cargo não
    fica guardado entre chamadas.

    - documentos: ID(s) de documento(s) separados por vírgula
    - cargo: cargo/função — OBRIGATÓRIO (se omitido, lista opções disponíveis)
    """
    try:
        client = _get_client(ctx)
        login = client._usuario
        senha = client._senha
        if not cargo:
            return await _pedir_cargo(client)
        cargo, escolha = await _resolver_cargo(client, cargo)
        if escolha:
            return escolha
        result = await client.assinar_documentos_bloco(
            login=login, senha=senha, cargo=cargo, documentos=documentos,
        )
        return _json(result)
    except Exception as e:
        return _error(str(e))


# ---------------------------------------------------------------------------
# Tools de marcador
# ---------------------------------------------------------------------------


@mcp.tool()
async def sei_criar_marcador(
    nome: str,
    id_cor: str = "",
    ctx: Context = None,
) -> str:
    """Cria um marcador na unidade atual.

    - nome: nome do marcador
    - id_cor: ID da cor (use sei_listar_cores_marcador para ver opções).
      Se omitido, lista as cores disponíveis para escolha.
    """
    try:
        client = _get_client(ctx)
        if not id_cor:
            cores = await client.listar_cores_marcador()
            return _json({
                "error": "Cor não informada — escolha uma das cores disponíveis.",
                "cores": cores,
            })
        result = await client.criar_marcador(nome, id_cor)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_excluir_marcador(
    ids_marcadores: str,
    ctx: Context = None,
) -> str:
    """Exclui marcador(es). IDs separados por vírgula."""
    try:
        client = _get_client(ctx)
        result = await client.excluir_marcadores(ids_marcadores)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_marcar_processo(
    processo: str,
    marcador: str,
    texto: str = "",
    ctx: Context = None,
) -> str:
    """Adiciona ou altera marcador (etiqueta colorida) em um processo.

    Parâmetros:
    - processo: protocolo formatado ou IdProcedimento
    - marcador: ID do marcador (use sei_pesquisar_marcadores para listar)
    - texto: texto/comentário associado ao marcador (opcional)

    Para remover, use marcador vazio ou marque com outro marcador.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.marcar_processo(id_proc, marcador, texto)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_pesquisar_marcadores(
    filtro: str = "",
    limit: int = 50,
    ctx: Context = None,
) -> str:
    """Lista marcadores disponíveis na unidade atual.

    Use o 'id' retornado em sei_marcar_processo.
    """
    try:
        client = _get_client(ctx)
        result = await client.pesquisar_marcadores(filtro=filtro, limit=limit)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_consultar_marcador_processo(
    processo: str,
    ctx: Context = None,
) -> str:
    """Consulta os marcadores ativos de um processo."""
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.consultar_marcador_processo(id_proc)
        return _json(result)
    except Exception as e:
        return _error(str(e))


# ---------------------------------------------------------------------------
# Tools de acompanhamento especial
# ---------------------------------------------------------------------------


@mcp.tool()
async def sei_acompanhar_processo(
    processo: str,
    grupo: str = "",
    observacao: str = "",
    ctx: Context = None,
) -> str:
    """Adiciona acompanhamento especial em um processo.

    Parâmetros:
    - processo: protocolo formatado ou IdProcedimento
    - grupo: ID do grupo de acompanhamento (use sei_listar_grupos_acompanhamento)
    - observacao: observação/anotação do acompanhamento
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.acompanhar_processo(id_proc, grupo, observacao)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_remover_acompanhamento(
    processo: str,
    ctx: Context = None,
) -> str:
    """Remove acompanhamento especial de um processo.

    Consulta o acompanhamento ativo e remove.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        acomp = await client.consultar_acompanhamento(id_proc)
        if not acomp:
            return _json({"mensagem": "Nenhum acompanhamento ativo neste processo."})
        id_acomp = str(acomp.get("idAcompanhamento", acomp.get("id", "")))
        if not id_acomp:
            return _error("Não foi possível identificar o acompanhamento.")
        result = await client.excluir_acompanhamento(id_acomp)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_criar_grupo_acompanhamento(
    nome: str,
    ctx: Context = None,
) -> str:
    """Cria um grupo de acompanhamento especial no SEI."""
    try:
        client = _get_client(ctx)
        result = await client.criar_grupo_acompanhamento(nome)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_excluir_grupo_acompanhamento(
    ids_grupos: str,
    ctx: Context = None,
) -> str:
    """Exclui grupo(s) de acompanhamento especial. IDs separados por vírgula."""
    try:
        client = _get_client(ctx)
        result = await client.excluir_grupo_acompanhamento(ids_grupos)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_listar_grupos_acompanhamento(
    filtro: str = "",
    ctx: Context = None,
) -> str:
    """Lista grupos de acompanhamento disponíveis."""
    try:
        client = _get_client(ctx)
        result = await client.listar_grupos_acompanhamento(filtro=filtro)
        return _json(result)
    except Exception as e:
        return _error(str(e))


# ---------------------------------------------------------------------------
# Tools de bloco interno
# ---------------------------------------------------------------------------


@mcp.tool()
async def sei_criar_bloco_interno(
    descricao: str,
    ctx: Context = None,
) -> str:
    """Cria um bloco interno no SEI.

    Blocos internos são usados para organizar processos em lotes.
    """
    try:
        client = _get_client(ctx)
        result = await client.criar_bloco_interno(descricao)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_incluir_processo_bloco_interno(
    id_bloco: str,
    processos: str,
    ctx: Context = None,
) -> str:
    """Inclui processo(s) em um bloco interno.

    - id_bloco: ID do bloco
    - processos: IdProcedimento(s) separados por vírgula
    """
    try:
        client = _get_client(ctx)
        result = await client.incluir_processo_bloco_interno(id_bloco, processos)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_retirar_processo_bloco_interno(
    id_bloco: str,
    processos: str,
    ctx: Context = None,
) -> str:
    """Remove processo(s) de um bloco interno.

    - id_bloco: ID do bloco
    - processos: IdProcedimento(s) separados por vírgula
    """
    try:
        client = _get_client(ctx)
        result = await client.retirar_processo_bloco_interno(id_bloco, processos)
        return _json(result)
    except Exception as e:
        return _error(str(e))


# ---------------------------------------------------------------------------
# Tools de bloco de assinatura
# ---------------------------------------------------------------------------


@mcp.tool()
async def sei_criar_bloco_assinatura(
    descricao: str,
    unidades: str = "",
    ctx: Context = None,
) -> str:
    """Cria um bloco de assinatura no SEI.

    Parâmetros:
    - descricao: descrição do bloco
    - unidades: sigla(s) ou ID(s) das unidades para disponibilizar
      (separados por vírgula). Se informar sigla, resolve automaticamente.
    """
    try:
        client = _get_client(ctx)

        # Resolver siglas de unidades para IDs
        if unidades:
            destinos = [u.strip() for u in unidades.split(",")]
            ids = []
            for d in destinos:
                if d.isdigit():
                    ids.append(d)
                else:
                    result = await client.pesquisar_unidades(filtro=d, limit=5)
                    found = False
                    for u in result.get("unidades", []):
                        if u.get("sigla", "").upper() == d.upper():
                            ids.append(str(u.get("id", "")))
                            found = True
                            break
                    if not found and result.get("unidades"):
                        ids.append(str(result["unidades"][0].get("id", "")))
            unidades = ",".join(ids)

        result = await client.criar_bloco_assinatura(descricao, unidades)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_incluir_documento_bloco_assinatura(
    id_bloco: str,
    documentos: str,
    ctx: Context = None,
) -> str:
    """Inclui documento(s) em um bloco de assinatura.

    - id_bloco: ID do bloco de assinatura
    - documentos: ID(s) de documento(s) separados por vírgula
    """
    try:
        client = _get_client(ctx)
        result = await client.incluir_documento_bloco_assinatura(id_bloco, documentos)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_disponibilizar_bloco_assinatura(
    id_bloco: str,
    ctx: Context = None,
) -> str:
    """Disponibiliza um bloco de assinatura para as unidades configuradas.

    Após disponibilizar, os usuários das unidades podem assinar os documentos.
    """
    try:
        client = _get_client(ctx)
        result = await client.disponibilizar_bloco_assinatura(id_bloco)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_cancelar_disponibilizacao_bloco(
    id_bloco: str,
    ctx: Context = None,
) -> str:
    """Cancela a disponibilização de um bloco de assinatura.

    O bloco volta ao estado aberto e pode ser editado novamente.
    """
    try:
        client = _get_client(ctx)
        result = await client.cancelar_disponibilizacao_bloco_assinatura(id_bloco)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_pesquisar_blocos_assinatura(
    filtro: str = "",
    estado: str = "",
    limit: int = 50,
    ctx: Context = None,
) -> str:
    """Pesquisa blocos de assinatura pelo nome (descrição).

    Inclui os blocos da unidade atual e os disponibilizados a ela por outras
    unidades. A busca ignora acentos e maiúsculas e aceita as palavras em
    qualquer ordem. Sem `filtro`, lista os mais recentes.

    Parâmetros:
    - filtro: palavras do nome do bloco (vazio = todos)
    - estado: códigos separados por vírgula — A=Aberto, D=Disponibilizado,
      R=Retornado, C=Concluído, B=Recebido
    - limit: máximo de blocos devolvidos (padrão 50)

    Use o `id` em sei_listar_documentos_bloco_assinatura e sei_assinar_bloco.
    """
    try:
        client = _get_client(ctx)
        return _json(await _pesquisar_blocos(
            client.pesquisar_blocos_assinatura, _bloco_assinatura_resumido, filtro, estado, limit))
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_criar_anotacao(
    processo: str,
    descricao: str,
    prioridade: str = "1",
    ctx: Context = None,
) -> str:
    """Cria uma anotação (post-it) em um processo no SEI.

    Parâmetros:
    - processo: protocolo formatado (ex: 50300.018905/2018-67) ou IdProcedimento
    - descricao: texto da anotação
    - prioridade: nível de prioridade (1=normal)
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.criar_anotacao(
            protocolo=id_proc,
            descricao=descricao,
            prioridade=prioridade,
        )
        return _json(result)
    except Exception as e:
        return _error(str(e))


# ---------------------------------------------------------------------------
# Endpoints adicionais do mod-wssei v2
# Todos disponíveis desde mod-wssei 2.0.0 (SEI 4.0.x), exceto:
#   - sei_listar_relacionamentos → requer mod-wssei 3.0.2+ (SEI 5.0.x)
# Se um endpoint falhar, use sei_versao para verificar a versão instalada.
# Compatibilidade: SEI 4.0.x=mod-wssei 2.0.x | SEI 4.1.1=2.2.0 | SEI 5.0.x=3.0.x
# ---------------------------------------------------------------------------


# -- Sistema / Informações --


@mcp.tool()
async def sei_versao(ctx: Context) -> str:
    """Retorna a versão do SEI e do módulo wssei instalado.

    Útil para verificar compatibilidade de funcionalidades.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.versao()
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_listar_orgaos(ctx: Context) -> str:
    """Lista os órgãos cadastrados na instalação do SEI.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.listar_orgaos()
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_listar_contextos(id_orgao: str, ctx: Context) -> str:
    """Lista os contextos disponíveis para um órgão.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.listar_contextos(id_orgao)
        return _json(result)
    except Exception as e:
        return _error(str(e))


# -- Usuários --


@mcp.tool()
async def sei_pesquisar_usuarios(
    filtro: str = "",
    id_orgao: str = "",
    limit: int = 50,
    pagina: int = 0,
    ctx: Context = None,
) -> str:
    """Pesquisa usuários por palavra-chave no órgão.

    Diferente de sei_listar_usuarios (que lista por unidade),
    este pesquisa no servidor por nome/sigla em todo o órgão.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.pesquisar_usuarios(
            filtro=filtro, id_orgao=id_orgao, limit=limit, start=pagina
        )
        return _json(result)
    except Exception as e:
        return _error(str(e))


# -- Unidades --


@mcp.tool()
async def sei_pesquisar_outras_unidades(
    filtro: str = "",
    limit: int = 50,
    pagina: int = 0,
    ctx: Context = None,
) -> str:
    """Pesquisa unidades excluindo a unidade atual.

    Útil para tramitação — já filtra a unidade do usuário.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.pesquisar_outras_unidades(
            filtro=filtro, limit=limit, start=pagina
        )
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_pesquisar_textos_padrao(
    filtro: str = "",
    limit: int = 50,
    pagina: int = 0,
    ctx: Context = None,
) -> str:
    """Pesquisa textos padrão internos disponíveis na unidade.

    Textos padrão são modelos reutilizáveis para preencher documentos
    automaticamente ao criar um novo documento interno.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.pesquisar_textos_padrao(
            filtro=filtro, limit=limit, start=pagina
        )
        return _json(result)
    except Exception as e:
        return _error(str(e))


# -- Documentos --


@mcp.tool()
async def sei_consultar_documento_interno(
    id_documento: str,
    ctx: Context = None,
) -> str:
    """Consulta metadados de um documento interno (gerado no SEI).

    Aceita o número SEI (protocoloFormatado) ou o id interno. Retorna tipo,
    número, descrição, nível de acesso, hipótese legal, data de elaboração,
    observação, assuntos, interessados e destinatários — o que a tela
    "Consultar/Alterar Documento" mostra. Não traz o texto: para isso use
    sei_ler_documento.

    Quando o documento é restrito ou sigiloso, a resposta inclui o campo
    `_aviso_acesso` — aviso INFORMATIVO de privacidade, não erro de permissão.
    """
    try:
        client = _get_client(ctx)
        doc_id, _ = await _resolver_documento(client, id_documento)
        bruto = await client.consultar_documento_interno(doc_id)

        def nomes(itens):
            saida = []
            for item in itens or []:
                if isinstance(item, dict):
                    saida.append({
                        "id": str(item.get("id") or ""),
                        "nome": html.unescape(str(item.get("nomeformatado") or item.get("nome")
                                                  or item.get("descricao") or "")),
                        **({"codigo": item["codigoestruturadoformatado"]}
                           if item.get("codigoestruturadoformatado") else {}),
                    })
            return saida

        nivel = str(bruto.get("nivelAcesso") or "")
        result = {
            "id_documento": str(bruto.get("idDocumento") or doc_id),
            "nome": html.unescape(str(bruto.get("nomeDocumento") or "")),
            "id_procedimento": str(bruto.get("protocolo") or ""),
            "tipo": html.unescape(str(bruto.get("nomeSerie") or "")),
            "id_serie": str(bruto.get("idSerie") or ""),
            "numero": bruto.get("numero"),
            "descricao": html.unescape(str(bruto.get("descricao") or "")),
            "data_elaboracao": bruto.get("dataElaboracao"),
            "nivel_acesso": nivel,
            "nivel_acesso_descricao": access_control.ROTULOS.get(nivel, nivel),
            "hipotese_legal": bruto.get("nomeHipoteseLegal"),
            "base_legal": bruto.get("baseLegal"),
            "observacao": bruto.get("observacao"),
            "observacoes_outras_unidades": bruto.get("observacoesUnidades") or [],
            "assuntos": nomes(bruto.get("assuntos")),
            "interessados": nomes(bruto.get("interessados")),
            "destinatarios": nomes(bruto.get("destinatarios")),
        }
        n, hipotese = access_control.extrair_nivel(bruto)
        if access_control.precisa_disclaimer(n):
            result["_aviso_acesso"] = access_control.construir_disclaimer_acompanhante(
                n, hipotese,
                alvo={"tipo": "documento", "id": str(doc_id), "tipo_documento": "I"},
            )
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_consultar_documento_externo(
    id_documento: str,
    ctx: Context = None,
) -> str:
    """Consulta metadados de um documento externo pelo ID.

    Aceita tanto o id interno (ex: "3149544") quanto o número SEI /
    protocoloFormatado (ex: "2867926") — auto-resolve via pesquisa Solr
    quando necessário.

    Retorna informações como tipo, data, nível de acesso, etc.
    Para baixar o conteúdo use sei_baixar_anexo ou sei_ler_documento.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).

    Quando o documento é restrito ou sigiloso (nivelAcesso 1 ou 2), a
    resposta inclui o campo `_aviso_acesso` — aviso INFORMATIVO de
    privacidade, NÃO erro de permissão. Os metadados foram retornados
    normalmente; não tente trocar de unidade ou rotas alternativas.
    Se falhar com erro inesperado, use sei_versao para verificar a versão.
    """
    try:
        client = _get_client(ctx)
        try:
            result = await client.consultar_documento_externo(id_documento)
        except Exception as primeira:
            msg = str(primeira)
            low = msg.lower()
            # Se não autorizado, pode ser id errado (passou número SEI). Tenta resolver.
            if "não autorizado" in low or "nao autorizado" in low:
                try:
                    doc_id, _ = await _resolver_documento(client, id_documento)
                    if doc_id != id_documento:
                        id_documento = doc_id
                        result = await client.consultar_documento_externo(id_documento)
                    else:
                        raise primeira
                except Exception:
                    return _json({
                        "error": msg,
                        "dica": (
                            "SEI retornou 'não autorizado' para o id "
                            f"{id_documento!r}. Verifique se você passou o id "
                            "INTERNO do documento (ex.: 3149544) e não o número "
                            "SEI / protocoloFormatado (ex.: 2867926). Use "
                            "sei_buscar_documento para resolver número SEI → id."
                        ),
                    })
            else:
                raise

        nivel, hipotese = access_control.extrair_nivel(result)
        if access_control.precisa_disclaimer(nivel):
            result["_aviso_acesso"] = access_control.construir_disclaimer_acompanhante(
                nivel, hipotese,
                alvo={"tipo": "documento", "id": str(id_documento), "tipo_documento": "X"},
            )
        return _json(result)
    except Exception as e:
        return _error(str(e))


_NIVEIS_ACESSO = {
    "0": "0", "publico": "0", "público": "0",
    "1": "1", "restrito": "1",
    "2": "2", "sigiloso": "2",
}


def _normalizar_nivel(nivel: str) -> str:
    """Aceita 0/1/2 ou publico/restrito/sigiloso. Vazio = manter."""
    bruto = str(nivel or "").strip().lower()
    if not bruto:
        return ""
    if bruto not in _NIVEIS_ACESSO and _normalizar_busca(bruto) not in _NIVEIS_ACESSO:
        raise ValueError(
            f"Nível de acesso inválido: {nivel!r}. Use publico (0), restrito (1) ou sigiloso (2)."
        )
    return _NIVEIS_ACESSO.get(bruto) or _NIVEIS_ACESSO[_normalizar_busca(bruto)]


async def _resolver_hipotese(client: SEIClient, nivel: str, hipotese: str) -> tuple[str, dict | None]:
    """Devolve (id_hipotese, None) ou ("", resposta_para_o_usuario_escolher).

    Restrito e sigiloso exigem hipótese legal. Aceita o id ou parte do nome
    (sem acento, palavras em qualquer ordem). Sem hipótese, ou com nome
    ambíguo, nada é alterado: devolve as opções válidas para o nível.
    """
    hipotese = str(hipotese or "").strip()
    if hipotese.isdigit():
        return hipotese, None

    r = await client.pesquisar_hipoteses_legais(limit=300, nivel_acesso=nivel)
    opcoes = [
        {"id": str(h.get("id")), "nome": html.unescape(str(h.get("nome") or "")),
         "base_legal": html.unescape(str(h.get("baselegal") or ""))}
        for h in r.get("hipoteses") or [] if isinstance(h, dict)
    ]
    rotulo = "restrito" if nivel == "1" else "sigiloso"
    if hipotese:
        alvo = _normalizar_busca(hipotese)
        # Nome exato tem precedência: no SEI de Roraima, "Acesso à Informação"
        # (id 18) e "Acesso à Informações" (id 22) se contêm mutuamente.
        exatas = [o for o in opcoes if _normalizar_busca(o["nome"]).rstrip(".") == alvo.rstrip(".")]
        if len(exatas) == 1:
            return exatas[0]["id"], None
        termos = alvo.split()
        achadas = [
            o for o in opcoes
            if all(t in _normalizar_busca(f"{o['nome']} {o['base_legal']}") for t in termos)
        ]
        if len(achadas) == 1:
            return achadas[0]["id"], None
        return "", {
            "error": (f"Nenhuma hipótese legal corresponde a {hipotese!r}." if not achadas
                      else f"Mais de uma hipótese legal corresponde a {hipotese!r}."),
            "hipoteses_disponiveis": achadas or opcoes,
            "dica": "Nada foi alterado. Informe o `id` da hipótese escolhida em `hipotese_legal`.",
        }
    return "", {
        "error": f"Para tornar o documento {rotulo}, o SEI exige uma hipótese legal.",
        "hipoteses_disponiveis": opcoes,
        "dica": "Nada foi alterado. A hipótese é escolha do usuário: repita a chamada "
                "com o `id` (ou o nome) em `hipotese_legal`.",
    }


async def _alterar_nivel_documento(client, id_documento, descricao, nivel_acesso,
                                   hipotese_legal, externo: bool, **extra) -> str:
    """Fluxo comum de sei_alterar_documento_interno/_externo."""
    doc_id, _ = await _resolver_documento(client, id_documento)
    nivel = _normalizar_nivel(nivel_acesso)
    hipotese = str(hipotese_legal or "").strip()
    if nivel in ("1", "2") or (hipotese and not hipotese.isdigit()):
        if not nivel:
            atual = await (client.consultar_documento_externo(doc_id) if externo
                           else client.consultar_documento_interno(doc_id))
            nivel = str(atual.get("nivelAcesso") or "")
        if nivel in ("1", "2"):
            hipotese, escolha = await _resolver_hipotese(client, nivel, hipotese)
            if escolha:
                return _json(escolha)
    # Como na assinatura: o SEI só altera documento de processo aberto na
    # unidade ativa. Ativa a unidade geradora se o usuário tiver acesso a ela
    # (melhor-esforço; sem acesso, o próprio SEI explica a recusa).
    unidade_ativada = None if externo else await _ativar_unidade_geradora(client, doc_id)
    metodo = client.alterar_documento_externo if externo else client.alterar_documento_interno
    result = await metodo(
        id_documento=doc_id, descricao=descricao, nivel_acesso=nivel,
        id_hipotese_legal=hipotese, **extra,
    )
    if isinstance(result, dict) and unidade_ativada:
        result["_unidade_ativada"] = unidade_ativada
    if isinstance(result, dict) and nivel:
        result["nivel_acesso"] = nivel
        result["nivel_acesso_descricao"] = access_control.ROTULOS.get(nivel, nivel)
        if hipotese:
            result["hipotese_legal"] = hipotese
    return _json(result)


@mcp.tool()
async def sei_alterar_documento_interno(
    id_documento: str,
    descricao: str = "",
    nivel_acesso: str = "",
    hipotese_legal: str = "",
    ctx: Context = None,
) -> str:
    """Altera metadados de um documento interno (não o conteúdo HTML).

    Para alterar o conteúdo, use sei_editar_secao. Só os campos informados
    mudam: os demais (descrição, nível de acesso, assuntos, interessados,
    destinatários, observação) são lidos do documento e reenviados, pois o
    SEI apagaria o que não viesse na alteração.
    - id_documento: número SEI ou id interno do documento
    - descricao: nova descrição (vazio = mantém a atual)
    - nivel_acesso: publico, restrito ou sigiloso (ou 0, 1, 2); vazio = mantém
    - hipotese_legal: id ou parte do nome da hipótese (ex.: "informação pessoal").
      Obrigatória para restrito/sigiloso: sem ela, ou se o nome for ambíguo,
      nada é alterado e a resposta lista as hipóteses válidas para o nível,
      para o usuário escolher. Ao tornar público, a hipótese é removida.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        return await _alterar_nivel_documento(
            client, id_documento, descricao, nivel_acesso, hipotese_legal, externo=False,
        )
    except Exception as e:
        return await _erro_documento_em_bloco(ctx, id_documento, e)


@mcp.tool()
async def sei_alterar_documento_externo(
    id_documento: str,
    descricao: str = "",
    nivel_acesso: str = "",
    hipotese_legal: str = "",
    arquivo_path: str = "",
    arquivo_base64: str = "",
    nome_arquivo: str = "",
    ctx: Context = None,
) -> str:
    """Altera metadados de um documento externo (e opcionalmente substitui o arquivo).

    Só os campos informados mudam: os demais (descrição, nível de acesso,
    número, tipo, data de elaboração, remetente, assuntos, interessados,
    destinatários, observação) são lidos do documento e reenviados, pois o
    SEI apagaria o que não viesse na alteração.
    - id_documento: número SEI ou id interno do documento
    - descricao: nova descrição (vazio = mantém a atual)
    - nivel_acesso: publico, restrito ou sigiloso (ou 0, 1, 2); vazio = mantém
    - hipotese_legal: id ou parte do nome da hipótese (ex.: "informação pessoal").
      Obrigatória para restrito/sigiloso: sem ela, ou se o nome for ambíguo,
      nada é alterado e a resposta lista as hipóteses válidas para o nível,
      para o usuário escolher. Ao tornar público, a hipótese é removida.
    - arquivo_base64 + nome_arquivo: novo arquivo para substituir (opcional)
    - arquivo_path: caminho de novo arquivo NO SERVIDOR do MCP (opcional; só
      no modo local/stdio — desabilitado no servidor remoto)

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        if arquivo_path and _http_mode:
            return _error(_ERRO_ARQUIVO_PATH_REMOTO)
        if arquivo_base64 and arquivo_path:
            return _error("Informe arquivo_base64 OU arquivo_path, não os dois.")

        conteudo = b""
        if arquivo_base64:
            conteudo, erro = _decodificar_upload_base64(arquivo_base64, nome_arquivo)
            if erro:
                return _error(erro)

        client = _get_client(ctx)
        return await _alterar_nivel_documento(
            client, id_documento, descricao, nivel_acesso, hipotese_legal, externo=True,
            arquivo_path=arquivo_path, arquivo_bytes=conteudo, nome_arquivo=nome_arquivo,
        )
    except Exception as e:
        return await _erro_documento_em_bloco(ctx, id_documento, e)


@mcp.tool()
async def sei_pesquisar_tipos_conferencia(
    filtro: str = "",
    limit: int = 50,
    pagina: int = 0,
    ctx: Context = None,
) -> str:
    """Pesquisa tipos de conferência para documentos externos.

    Tipo de conferência indica se o documento externo é cópia autenticada,
    cópia simples, original, etc.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.pesquisar_tipos_conferencia(
            filtro=filtro, limit=limit, start=pagina
        )
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_sugestao_assuntos_documento(
    id_serie: str,
    ctx: Context = None,
) -> str:
    """Lista sugestões de assuntos para um tipo de documento (série).

    Use o id_serie obtido via sei_pesquisar_tipos_documento.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.sugestao_assuntos_documento(id_serie)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_listar_blocos_documento(
    id_documento: str,
    ctx: Context = None,
) -> str:
    """Lista blocos de assinatura em que um documento está incluído.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.listar_blocos_documento(id_documento)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_pesquisar_tipos_documento_externo(
    filtro: str = "",
    limit: int = 50,
    pagina: int = 0,
    ctx: Context = None,
) -> str:
    """Pesquisa tipos de documento para documentos externos (séries externas).

    Diferente de sei_pesquisar_tipos_documento que lista todos os tipos,
    este retorna apenas os tipos aplicáveis a documentos externos.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.pesquisar_tipos_documento_externo(
            filtro=filtro, limit=limit, start=pagina
        )
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_parametros_upload(ctx: Context) -> str:
    """Retorna parâmetros de upload do SEI (extensões permitidas, tamanhos máximos).

    Útil antes de criar documentos externos para saber os limites.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.parametros_upload()
        return _json(result)
    except Exception as e:
        return _error(str(e))


# -- Processos: assuntos, atribuição, acesso, relacionamentos --


@mcp.tool()
async def sei_pesquisar_assuntos(
    filtro: str = "",
    limit: int = 50,
    pagina: int = 0,
    ctx: Context = None,
) -> str:
    """Pesquisa assuntos disponíveis para processos.

    Use o ID retornado no campo 'assuntos' ao criar processos.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.pesquisar_assuntos(
            filtro=filtro, limit=limit, start=pagina
        )
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_sugestao_assuntos_processo(
    id_tipo_processo: str,
    ctx: Context = None,
) -> str:
    """Lista sugestões de assuntos para um tipo de processo.

    Use o id do tipo obtido via sei_pesquisar_tipos_processo.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.sugestao_assuntos_processo(id_tipo_processo)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_consultar_atribuicao(
    processo: str,
    ctx: Context = None,
) -> str:
    """Consulta a atribuição atual de um processo (quem está responsável).

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.consultar_atribuicao(id_proc)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_verificar_acesso(
    processo: str,
    ctx: Context = None,
) -> str:
    """Verifica se o usuário tem acesso a um processo.

    Útil para checar permissão antes de operações em processos restritos.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.verificar_acesso(id_proc)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_listar_relacionamentos(
    processo: str,
    ctx: Context = None,
) -> str:
    """Lista processos relacionados a um processo.

    REQUER mod-wssei 3.0.2+ (SEI 5.0.x). Não disponível em versões anteriores.
    Se falhar, use sei_versao para verificar. Precisa ser >= 3.0.2.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.listar_relacionamentos(id_proc)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_listar_atividades(
    processo: str,
    ctx: Context = None,
) -> str:
    """Lista o histórico de atividades/andamentos de um processo.

    Retorna as ações registradas (tramitações, assinaturas, edições, etc.).
    Aceita protocolo formatado (ex: 50300.000123/2025-00) ou IdProcedimento.

    Por padrão usa a REST (`/atividade/listar`). O scraper web (mais detalhado,
    mas inativo desde o SSO Microsoft da ANTAQ) só é usado se SEI_WEB_SCRAPER=1.
    """
    try:
        if _web_scraper_enabled():
            web = _get_web_client(ctx)
            if web._inbox_url is None:
                await web.login()
            return _json(await web.listar_atividades(processo))
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.listar_atividades(id_proc, limit=200)
        return _json(result)
    except Exception as e:
        return _error(str(e))


# -- Acompanhamento: meus, da unidade, alterar --


@mcp.tool()
async def sei_listar_meus_acompanhamentos(
    limit: int = 50,
    pagina: int = 0,
    ctx: Context = None,
) -> str:
    """Lista processos que o usuário está acompanhando (acompanhamento especial).

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.listar_meus_acompanhamentos(limit=limit, start=pagina)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_listar_acompanhamentos_unidade(
    limit: int = 50,
    pagina: int = 0,
    ctx: Context = None,
) -> str:
    """Lista processos com acompanhamento especial na unidade atual.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.listar_acompanhamentos_unidade(limit=limit, start=pagina)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_alterar_acompanhamento(
    processo: str,
    grupo: str = "",
    observacao: str = "",
    ctx: Context = None,
) -> str:
    """Altera acompanhamento especial de um processo.

    - processo: protocolo formatado ou IdProcedimento
    - grupo: novo grupo de acompanhamento
    - observacao: nova observação

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.alterar_acompanhamento(id_proc, grupo, observacao)
        return _json(result)
    except Exception as e:
        return _error(str(e))


# -- Credenciamento (processos sigilosos) --


@mcp.tool()
async def sei_listar_credenciamentos(
    processo: str,
    ctx: Context = None,
) -> str:
    """Lista credenciamentos de acesso a um processo sigiloso.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.listar_credenciamentos(id_proc)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_conceder_credenciamento(
    processo: str,
    id_usuario: str,
    ctx: Context = None,
) -> str:
    """Concede credenciamento de acesso a um processo sigiloso para um usuário.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.conceder_credenciamento(id_proc, id_usuario)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_renunciar_credenciamento(
    processo: str,
    ctx: Context = None,
) -> str:
    """Renuncia ao credenciamento de acesso a um processo sigiloso.

    O próprio usuário perde o acesso ao processo.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.renunciar_credenciamento(id_proc)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_cassar_credenciamento(
    processo: str,
    id_usuario: str,
    ctx: Context = None,
) -> str:
    """Cassa (revoga) credenciamento de acesso de um usuário a processo sigiloso.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.cassar_credenciamento(id_proc, id_usuario)
        return _json(result)
    except Exception as e:
        return _error(str(e))


# -- Assinantes e Observação --


@mcp.tool()
async def sei_listar_assinantes(ctx: Context) -> str:
    """Lista signatários (cargos/funções) disponíveis na unidade atual.

    Retorna os cargos que podem ser usados em sei_assinar_documento.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.listar_assinantes()
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_listar_orgaos_assinante(ctx: Context) -> str:
    """Lista órgãos disponíveis para assinatura.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.listar_orgaos_assinante()
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_criar_observacao(
    processo: str,
    descricao: str,
    ctx: Context = None,
) -> str:
    """Cria observação da unidade em um processo.

    Diferente da anotação (post-it individual), a observação é
    vinculada à unidade e visível por todos os usuários da unidade.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.criar_observacao(id_proc, descricao)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_criar_contato(
    nome: str,
    tipo: str = "",
    email: str = "",
    telefone: str = "",
    ctx: Context = None,
) -> str:
    """Cria novo contato no SEI.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.criar_contato(
            nome=nome, tipo=tipo, email=email, telefone=telefone
        )
        return _json(result)
    except Exception as e:
        return _error(str(e))


# -- Modelos de documento --


@mcp.tool()
async def sei_listar_grupos_modelos(
    limit: int = 50,
    pagina: int = 0,
    ctx: Context = None,
) -> str:
    """Lista grupos de modelos de documento disponíveis.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.listar_grupos_modelos(limit=limit, start=pagina)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_listar_modelos(
    id_grupo: str = "",
    filtro: str = "",
    limit: int = 50,
    pagina: int = 0,
    ctx: Context = None,
) -> str:
    """Lista modelos de documento disponíveis.

    - id_grupo: filtrar por grupo (use sei_listar_grupos_modelos)
    - filtro: texto para filtrar por nome

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.listar_modelos(
            id_grupo=id_grupo, filtro=filtro, limit=limit, start=pagina
        )
        return _json(result)
    except Exception as e:
        return _error(str(e))


# -- Marcador: desativar, reativar, histórico --


@mcp.tool()
async def sei_desativar_marcador(
    ids_marcadores: str,
    ctx: Context = None,
) -> str:
    """Desativa marcador(es) sem excluir. IDs separados por vírgula.

    Marcadores desativados deixam de aparecer nas pesquisas mas
    mantêm o histórico. Use sei_reativar_marcador para reativar.
    """
    try:
        client = _get_client(ctx)
        result = await client.desativar_marcadores(ids_marcadores)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_reativar_marcador(
    ids_marcadores: str,
    ctx: Context = None,
) -> str:
    """Reativa marcador(es) desativados. IDs separados por vírgula."""
    try:
        client = _get_client(ctx)
        result = await client.reativar_marcadores(ids_marcadores)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_historico_marcador_processo(
    processo: str,
    ctx: Context = None,
) -> str:
    """Lista histórico de marcadores de um processo.

    Mostra quais marcadores foram aplicados/removidos ao longo do tempo.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.historico_marcador_processo(id_proc)
        return _json(result)
    except Exception as e:
        return _error(str(e))


# -- Bloco Interno: operações adicionais --


_ESTADOS_BLOCO = {
    "A": "Aberto",
    "D": "Disponibilizado",
    "R": "Retornado",
    "C": "Concluído",
    "B": "Recebido",
}
_MAX_PAGINAS_BLOCOS = 20
_TAM_PAGINA_BLOCOS = 100


def _normalizar_busca(texto: str) -> str:
    """Minúsculas, sem acento e com espaços simples — para comparar nomes.

    Repara antes o texto gravado com acento corrompido ("AdministraÃ§Ã£o",
    UTF-8 lido como Latin-1), comum em blocos criados antes da correção de
    charset dos formulários.
    """
    import unicodedata

    texto = html.unescape(str(texto or ""))
    if "Ã" in texto or "Â" in texto:
        try:
            texto = texto.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c)
    )
    return " ".join(sem_acento.lower().split())


def _bloco_resumido(bloco: dict) -> dict:
    estado = str(bloco.get("estado") or "")
    descricao = str(bloco.get("descricao") or "")
    if "Ã" in descricao or "Â" in descricao:
        try:
            descricao = descricao.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return {
        "id": str(bloco.get("id") or ""),
        "descricao": descricao,
        "estado": estado,
        "estado_descricao": _ESTADOS_BLOCO.get(estado, estado),
        "unidade": bloco.get("siglaUnidade"),
        "numero_processos": bloco.get("numeroProcessos"),
    }


async def _pesquisar_blocos(buscar, resumir, nome: str, estado: str, limit: int) -> dict:
    """Busca de blocos (interno ou de assinatura) por nome, tolerante a acento.

    1) busca nativa do SEI (`filter`, sensível a acento/charset);
    2) varredura paginada comparada normalizada no servidor MCP.
    Sem nome, para ao atingir `limit` e devolve total desconhecido.
    """
    estado = ",".join(e.strip().upper() for e in str(estado or "").split(",") if e.strip())
    termos = _normalizar_busca(nome).split()
    encontrados: dict[str, dict] = {}
    varridos = 0
    truncado = False
    parou_cedo = False

    def considerar(itens):
        for bruto in itens or []:
            if not isinstance(bruto, dict):
                continue
            bloco = resumir(bruto)
            alvo = _normalizar_busca(bloco["descricao"])
            if bloco["id"] and all(t in alvo for t in termos):
                encontrados.setdefault(bloco["id"], bloco)

    if termos:
        try:
            r = await buscar(filtro=nome, limit=_TAM_PAGINA_BLOCOS, estado=estado)
            considerar(r.get("blocos"))
        except Exception:
            logger.debug("Busca nativa de blocos falhou; seguindo na varredura", exc_info=True)

    for pagina in range(_MAX_PAGINAS_BLOCOS):
        if not termos and len(encontrados) >= limit:
            parou_cedo = True
            break
        r = await buscar(limit=_TAM_PAGINA_BLOCOS, start=pagina, estado=estado)
        itens = r.get("blocos") or []
        varridos += len(itens)
        considerar(itens)
        if len(itens) < _TAM_PAGINA_BLOCOS:
            break
    else:
        truncado = True

    blocos = sorted(encontrados.values(), key=lambda b: int(b["id"] or 0), reverse=True)
    resultado = {
        "nome_pesquisado": nome,
        "estado": estado or "todos",
        "total_encontrado": None if parou_cedo else len(blocos),
        "retornados": min(len(blocos), limit),
        "tem_mais": parou_cedo or len(blocos) > limit,
        "blocos": blocos[:limit],
    }
    if truncado:
        resultado["_aviso"] = (
            f"A unidade tem mais de {varridos} blocos; só os "
            f"{varridos} mais recentes foram verificados. Refine o nome ou o estado."
        )
    return resultado


def _bloco_assinatura_resumido(bruto: dict) -> dict:
    atrib = bruto.get("atributos") if isinstance(bruto.get("atributos"), dict) else bruto
    base = _bloco_resumido({
        "id": bruto.get("id") or atrib.get("idBloco"),
        "descricao": atrib.get("descricao"),
        "estado": atrib.get("estado"),
        "siglaUnidade": atrib.get("siglaUnidade"),
    })
    base.pop("numero_processos", None)
    base["numero_documentos"] = atrib.get("numeroDocumentos")
    base["disponibilizado_para"] = [
        u.get("unidade") for u in atrib.get("unidades") or [] if isinstance(u, dict)
    ]
    return base


@mcp.tool()
async def sei_pesquisar_blocos_internos(
    nome: str = "",
    estado: str = "",
    limit: int = 50,
    ctx: Context = None,
) -> str:
    """Pesquisa blocos internos da unidade atual pelo nome (descrição).

    A busca ignora acentos e maiúsculas e aceita as palavras em qualquer
    ordem: "contratos sejuc" encontra "SEJUC – Contratos 2026". Sem `nome`,
    lista os blocos (mais recentes primeiro).

    Os blocos são da unidade ativa — use sei_trocar_unidade antes para
    pesquisar em outra. Use o `id` retornado em sei_listar_processos_bloco_interno,
    sei_incluir_processo_bloco_interno etc.

    Parâmetros:
    - nome: palavras do nome do bloco (vazio = todos)
    - estado: filtra por estado, códigos separados por vírgula:
      A=Aberto, C=Concluído, D=Disponibilizado, R=Retornado, B=Recebido
    - limit: máximo de blocos devolvidos (padrão 50)
    """
    try:
        client = _get_client(ctx)
        return _json(await _pesquisar_blocos(
            client.pesquisar_blocos_internos, _bloco_resumido, nome, estado, limit))
    except Exception as e:
        return _error(str(e))


# ---------------------------------------------------------------------------
# Prazos e pendências da unidade
# ---------------------------------------------------------------------------

def _hoje():
    """Data de hoje no fuso do órgão (SEI_FUSO_HORARIO, padrão America/Sao_Paulo)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    try:
        fuso = ZoneInfo(os.environ.get("SEI_FUSO_HORARIO", "America/Sao_Paulo"))
    except Exception:
        fuso = ZoneInfo("America/Sao_Paulo")
    return datetime.now(fuso).date()


async def _processos_da_unidade(client: SEIClient, maximo: int) -> list[dict]:
    """Caixa da unidade atual, paginada (start = página), até `maximo` itens."""
    itens: list[dict] = []
    pagina = 0
    while len(itens) < maximo:
        r = await client.listar_processos(limit=50, start=pagina)
        lote = [p for p in (r.get("processos") or []) if isinstance(p, dict)]
        itens.extend(lote)
        if len(lote) < 50:
            break
        pagina += 1
    return itens[:maximo]


def _prazo_do_processo(bruto: dict, hoje) -> dict | None:
    """Prazo de um item de /processo/listar: retorno programado ou marcador com data."""
    from datetime import date

    att = bruto.get("atributos") or {}
    status = att.get("status") or {}
    origem, data_txt = None, None
    retorno = status.get("retornoData")
    if isinstance(retorno, dict) and retorno.get("dataProgramada"):
        data_txt = str(retorno["dataProgramada"])
        origem = f"retorno programado (enviado por {retorno.get('unidade') or '?'})"
    else:
        for m in att.get("marcador") or []:
            if isinstance(m, dict):
                achado = re.search(r"(\d{2})/(\d{2})/(\d{4})", f"{m.get('texto') or ''} {m.get('nome') or ''}")
                if achado:
                    data_txt = achado.group(0)
                    origem = f"marcador {html.unescape(str(m.get('nome') or '')).strip()}"
                    break
    if not data_txt:
        return None
    try:
        d, mth, a = (int(x) for x in re.findall(r"\d+", data_txt)[:3])
        alvo = date(a, mth, d)
    except Exception:
        return None
    dias = (alvo - hoje).days
    if dias < 0:
        situacao = f"vencido há {-dias} dia(s)"
    elif dias == 0:
        situacao = "vence hoje"
    else:
        situacao = f"vence em {dias} dia(s)"
    return {"prazo": alvo.strftime("%d/%m/%Y"), "dias_restantes": dias,
            "situacao": situacao, "origem": origem}


def _processo_resumido(bruto: dict, unidade_ativa: str | None) -> dict:
    p = shape_processo_resumido(bruto, unidade_ativa)
    return {
        "protocolo": p.get("protocolo"),
        "id_procedimento": p.get("id_procedimento"),
        "tipo": p.get("tipo"),
        "descricao": p.get("descricao"),
        "atribuido_a": (p.get("atribuido_unidade_atual") or {}).get("nome")
        if isinstance(p.get("atribuido_unidade_atual"), dict) else p.get("atribuido_unidade_atual"),
    }


@mcp.tool()
async def sei_listar_prazos(
    dias: int = 15,
    incluir_vencidos: bool = True,
    max_processos: int = 200,
    ctx: Context = None,
) -> str:
    """Lista processos da unidade atual com prazo vencido ou vencendo em até `dias`.

    O prazo vem do retorno programado do SEI (data em que o processo deve
    voltar) ou de um marcador cujo texto tenha data (dd/mm/aaaa). Ordenado do
    mais urgente ao menos urgente. Somente leitura.

    Parâmetros:
    - dias: horizonte em dias a partir de hoje (padrão 15)
    - incluir_vencidos: incluir os já vencidos (padrão true)
    - max_processos: quantos processos da caixa examinar (padrão 200)
    """
    try:
        client = _get_client(ctx)
        hoje = _hoje()
        processos = await _processos_da_unidade(client, max_processos)
        itens = []
        for bruto in processos:
            prazo = _prazo_do_processo(bruto, hoje)
            if not prazo or prazo["dias_restantes"] > dias:
                continue
            if prazo["dias_restantes"] < 0 and not incluir_vencidos:
                continue
            itens.append({**_processo_resumido(bruto, client._unidade_ativa), **prazo})
        itens.sort(key=lambda x: x["dias_restantes"])
        return _json({
            "hoje": hoje.strftime("%d/%m/%Y"),
            "horizonte_dias": dias,
            "processos_examinados": len(processos),
            "total": len(itens),
            "vencidos": sum(1 for i in itens if i["dias_restantes"] < 0),
            "prazos": itens,
        })
    except Exception as e:
        return _error(str(e))


async def _documentos_sem_assinatura(client: SEIClient, processos: list[dict],
                                     unidade: str | None, maximo: int) -> list[dict]:
    """Documentos internos gerados pela unidade, não assinados nem cancelados."""
    sem = asyncio.Semaphore(4)
    achados: list[dict] = []

    async def examinar(bruto):
        att = bruto.get("atributos") or {}
        id_proc = str(att.get("idProcedimento") or bruto.get("id") or "")
        if not id_proc:
            return
        async with sem:
            try:
                dados = await client.listar_documentos_todos(id_proc)
            except Exception:
                return
        for d in dados.get("documentos") or []:
            a = d.get("atributos") or {}
            st = a.get("status") or {}
            if (a.get("tipoDocumento") == "I" and str(a.get("idUnidade")) == str(unidade)
                    and st.get("documentoAssinado") == "N" and st.get("documentoCancelado") != "S"):
                achados.append({
                    "_id": str(d.get("id") or a.get("idProtocolo") or ""),
                    "documento": html.unescape(str(a.get("nomeComposto") or "")),
                    "numero_sei": a.get("protocoloFormatado"),
                    "descricao": html.unescape(str(a.get("informacao") or "")),
                    "processo": att.get("numero"),
                })

    await asyncio.gather(*(examinar(b) for b in processos[:maximo]))

    # Idade de cada minuta: a listagem não traz data; a consulta do documento traz.
    from datetime import datetime
    hoje = _hoje()

    async def datar(item):
        async with sem:
            try:
                meta = await client.consultar_documento_interno(item["_id"])
                criado = datetime.strptime(str(meta.get("dataElaboracao")), "%d/%m/%Y").date()
                item["criado_em"] = criado.strftime("%d/%m/%Y")
                item["dias_sem_assinatura"] = (hoje - criado).days
            except Exception:
                pass

    await asyncio.gather(*(datar(i) for i in achados[:40]))
    for item in achados:
        item.pop("_id", None)
    achados.sort(key=lambda i: -(i.get("dias_sem_assinatura") or 0))
    return achados


@mcp.tool()
async def sei_listar_pendencias(
    dias_prazo: int = 7,
    verificar_documentos: int = 20,
    max_processos: int = 200,
    ctx: Context = None,
) -> str:
    """Resumo das pendências da unidade atual — o "o que eu preciso fazer hoje".

    Reúne, numa chamada só e sem alterar nada:
    - prazos vencidos ou vencendo em até `dias_prazo` dias;
    - processos recebidos e ainda não abertos na unidade;
    - processos com documento novo;
    - processos atribuídos a você;
    - blocos de assinatura abertos, disponibilizados ou retornados;
    - documentos gerados pela unidade e ainda SEM ASSINATURA, nos
      `verificar_documentos` processos mais recentes da caixa (cada um custa
      uma consulta ao SEI; 0 desliga).

    Para outra unidade, use sei_trocar_unidade antes.
    """
    try:
        client = _get_client(ctx)
        hoje = _hoje()
        processos = await _processos_da_unidade(client, max_processos)
        unidade = client._unidade_ativa
        meu_id = str(getattr(client, "_id_usuario", "") or "")
        meu_nome = _normalizar_busca(getattr(client, "_nome_usuario", "") or "")

        prazos, nao_lidos, doc_novo, meus = [], [], [], []
        for bruto in processos:
            att = bruto.get("atributos") or {}
            st = att.get("status") or {}
            resumo = _processo_resumido(bruto, unidade)
            prazo = _prazo_do_processo(bruto, hoje)
            if prazo and prazo["dias_restantes"] <= dias_prazo:
                prazos.append({**resumo, **prazo})
            if str(st.get("processoAcessadoUnidade")) == "N":
                nao_lidos.append(resumo)
            if str(st.get("documentoNovo")) == "S":
                doc_novo.append(resumo)
            atrib = shape_processo_resumido(bruto, unidade).get("atribuido_unidade_atual")
            if isinstance(atrib, dict) and (
                (meu_id and str(atrib.get("id_usuario") or "") == meu_id)
                or (meu_nome and _normalizar_busca(atrib.get("nome") or "") == meu_nome)
            ):
                meus.append(resumo)
        prazos.sort(key=lambda x: x["dias_restantes"])

        blocos = []
        try:
            r = await client.pesquisar_blocos_assinatura(limit=100, estado="A,D,R")
            sigla_atual = next((u.get("sigla") for u in await client.listar_unidades_usuario()
                                if u.get("atual")), None)
            for b in r.get("blocos") or []:
                resumo_b = _bloco_assinatura_resumido(b)
                if not resumo_b.get("numero_documentos") or str(resumo_b["numero_documentos"]) == "0":
                    continue
                resumo_b["origem"] = ("recebido de outra unidade"
                                      if sigla_atual and resumo_b.get("unidade") != sigla_atual
                                      else "da unidade")
                blocos.append(resumo_b)
        except Exception:
            logger.debug("Falha ao listar blocos de assinatura", exc_info=True)

        sem_assinatura = (await _documentos_sem_assinatura(client, processos, unidade, verificar_documentos)
                          if verificar_documentos > 0 else [])

        return _json({
            "hoje": hoje.strftime("%d/%m/%Y"),
            "processos_na_caixa": len(processos),
            "resumo": {
                "prazos_ate_%d_dias" % dias_prazo: len(prazos),
                "nao_lidos": len(nao_lidos),
                "com_documento_novo": len(doc_novo),
                "atribuidos_a_mim": len(meus),
                "blocos_de_assinatura": len(blocos),
                "documentos_sem_assinatura": len(sem_assinatura),
            },
            "prazos": prazos,
            "nao_lidos": nao_lidos,
            "com_documento_novo": doc_novo,
            "atribuidos_a_mim": meus,
            "blocos_de_assinatura": blocos,
            "documentos_sem_assinatura": sem_assinatura,
            "_nota": (f"Documentos sem assinatura conferidos nos {min(verificar_documentos, len(processos))} "
                      "processos mais recentes da caixa."),
        })
    except Exception as e:
        return _error(str(e))


# ---------------------------------------------------------------------------
# Assinatura em lote, modelos de criação, prazos no texto e marcar prazo
# ---------------------------------------------------------------------------

@mcp.tool()
async def sei_assinar_documentos(
    documentos: str,
    cargo: str = "",
    ctx: Context = None,
) -> str:
    """Assina vários documentos avulsos de uma vez, sem precisar de bloco.

    Parâmetros:
    - documentos: números SEI (ou ids internos) separados por vírgula ou espaço
    - cargo: id (ex.: "7376") ou nome do cargo. Sem cargo — ou ambíguo — nada
      é assinado e a resposta lista os cargos disponíveis.

    Os documentos precisam ser da mesma unidade: ela é ativada antes de assinar.
    A resposta confere, documento a documento, se a assinatura entrou.
    """
    try:
        client = _get_client(ctx)
        referencias = [r for r in re.split(r"[,\s;]+", documentos or "") if r.strip()]
        if not referencias:
            return _error("Informe ao menos um documento.")
        ids, nomes = [], {}
        for ref in referencias:
            doc_id, _ = await _resolver_documento(client, ref)
            ids.append(doc_id)
            nomes[doc_id] = ref
        if not cargo:
            return await _pedir_cargo(client)

        await client._get_headers()
        if not client._id_usuario:
            await client.autenticar()
        if not client._id_usuario:
            return _error("Assinatura não enviada: a autenticação do SEI não informou o IdUsuario.")
        unidade_ativada = await _ativar_unidade_geradora(client, ids[0])
        cargo, escolha = await _resolver_cargo(client, cargo)
        if escolha:
            return escolha

        resultado = await client.assinar_documentos(
            ids, login=client._usuario, senha=client._senha, cargo=cargo,
            orgao=str(client._id_orgao_usuario or client._orgao or ""),
            id_usuario=str(client._id_usuario),
        )
        conferencia = []
        for doc_id in ids:
            try:
                assinaturas = await client.listar_assinaturas(doc_id)
                assinado = any(_normalizar_busca(a.get("cargo") or "") == _normalizar_busca(cargo)
                               for a in assinaturas or [] if isinstance(a, dict))
            except Exception:
                assinado = None
            conferencia.append({"documento": nomes[doc_id], "id": doc_id, "assinado": assinado})
        saida = {"resultado": resultado, "cargo": cargo, "documentos": conferencia}
        if unidade_ativada:
            saida["_unidade_ativada"] = unidade_ativada
        return _json(saida)
    except Exception as e:
        return _error(str(e))


async def _resolver_tipo_processo(client: SEIClient, tipo: str) -> tuple[str, str]:
    tipo = str(tipo or "").strip()
    if tipo.isdigit():
        return tipo, ""
    r = await client.pesquisar_tipos_processo(limit=500)
    tipos = r.get("tipos") or r.get("tipos_processo") or r.get("data") or []
    alvo = _normalizar_busca(tipo)
    exatos = [t for t in tipos if _normalizar_busca(t.get("nome", "")) == alvo]
    parecidos = exatos or [t for t in tipos if all(x in _normalizar_busca(t.get("nome", "")) for x in alvo.split())]
    if len(parecidos) == 1:
        return str(parecidos[0]["id"]), parecidos[0].get("nome", "")
    raise ValueError(
        f"Tipo de processo {tipo!r} " + ("não encontrado." if not parecidos else "ambíguo: ")
        + ", ".join(f"{t.get('nome')} (id {t.get('id')})" for t in parecidos[:10])
    )


@mcp.tool()
async def sei_consultar_modelo_documento(
    tipo: str,
    processo: str = "",
    ctx: Context = None,
) -> str:
    """O que o SEI sugere ao criar um documento do tipo: assuntos, se aceita
    interessados/destinatários e demais regras do tipo no órgão.

    - tipo: id ou nome do tipo de documento (ex.: "Ofício")
    - processo: opcional; algumas sugestões dependem do processo
    """
    try:
        client = _get_client(ctx)
        id_serie, nome = (str(tipo).strip(), "") if str(tipo).strip().isdigit() \
            else await _resolver_tipo_documento(client, tipo)
        id_proc = await _resolver_processo(client, processo) if processo else ""
        dados = await client.modelo_documento(id_serie, id_proc)
        return _json({"tipo": nome or id_serie, "id_serie": id_serie, "modelo": dados})
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_consultar_modelo_processo(tipo: str, ctx: Context = None) -> str:
    """O que o SEI sugere ao criar um processo do tipo: assuntos padrão, níveis
    de acesso permitidos e hipótese legal sugerida.

    - tipo: id ou nome do tipo de processo (ex.: "Aquisição de Material Permanente")
    """
    try:
        client = _get_client(ctx)
        id_tipo, nome = await _resolver_tipo_processo(client, tipo)
        dados = await client.modelo_processo(id_tipo)
        return _json({"tipo": nome or id_tipo, "id_tipo_processo": id_tipo, "modelo": dados})
    except Exception as e:
        return _error(str(e))


_MESES = {"janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6, "julho": 7,
          "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12}
_RE_PRAZO_DATA = re.compile(
    r"(at[eé]|prazo|data[- ]limite|impreterivelmente|no dia|vencimento)[^.;]{0,80}?"
    r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})", re.I)
_RE_PRAZO_EXTENSO = re.compile(
    r"(at[eé]|prazo|data[- ]limite|no dia)[^.;]{0,60}?(\d{1,2})\s+de\s+([a-zç]+)\s+de\s+(\d{4})", re.I)
_RE_PRAZO_RELATIVO = re.compile(
    r"(?:no\s+)?prazo\s+(?:m[aá]ximo\s+)?de\s+(\d{1,3})\s*(?:\([^)]{0,30}\)\s*)?dias?(\s+[uú]teis)?", re.I)


def _somar_dias(inicio, dias: int, uteis: bool):
    from datetime import timedelta
    if not uteis:
        return inicio + timedelta(days=dias)
    atual, faltam = inicio, dias
    while faltam > 0:
        atual += timedelta(days=1)
        if atual.weekday() < 5:
            faltam -= 1
    return atual


def _prazos_no_texto(texto: str, data_documento=None) -> list[dict]:
    """Datas de prazo citadas num texto, com o trecho de onde saíram."""
    from datetime import date

    achados = []
    plano = " ".join(texto.split())
    sem_acento = _normalizar_busca(plano)

    def trecho(ini, fim):
        return plano[max(0, ini - 60):min(len(plano), fim + 40)].strip()

    for m in _RE_PRAZO_DATA.finditer(plano):
        try:
            alvo = date(int(m.group(4)), int(m.group(3)), int(m.group(2)))
        except ValueError:
            continue
        achados.append({"data": alvo, "tipo": "data", "trecho": trecho(m.start(), m.end())})
    for m in _RE_PRAZO_EXTENSO.finditer(sem_acento):
        mes = _MESES.get(m.group(3))
        if not mes:
            continue
        try:
            alvo = date(int(m.group(4)), mes, int(m.group(2)))
        except ValueError:
            continue
        achados.append({"data": alvo, "tipo": "data por extenso", "trecho": trecho(m.start(), m.end())})
    if data_documento:
        for m in _RE_PRAZO_RELATIVO.finditer(plano):
            uteis = bool(m.group(2))
            alvo = _somar_dias(data_documento, int(m.group(1)), uteis)
            achados.append({"data": alvo, "tipo": f"{m.group(1)} dias{' úteis' if uteis else ''} a partir do documento",
                            "trecho": trecho(m.start(), m.end())})
    vistos, unicos = set(), []
    for a in achados:
        if a["data"] not in vistos:
            vistos.add(a["data"])
            unicos.append(a)
    return unicos


@mcp.tool()
async def sei_buscar_prazos_no_texto(
    processo: str,
    documentos: int = 5,
    ctx: Context = None,
) -> str:
    """Procura prazos escritos no texto dos documentos mais recentes de um processo.

    Encontra "até 04/11/2026", "prazo: 04/11/2026", "até 4 de novembro de 2026"
    e "no prazo de 5 (cinco) dias úteis" (contado da data do documento). Útil
    quando o prazo não foi programado no SEI, só escrito num ofício. Para que
    ele passe a aparecer em sei_listar_prazos, use sei_marcar_prazo.
    Somente leitura; lê só documentos gerados no SEI (HTML).

    - processo: número do processo ou IdProcedimento
    - documentos: quantos documentos, a partir do mais recente (padrão 5)
    """
    try:
        from datetime import datetime

        client = _get_client(ctx)
        hoje = _hoje()
        id_proc = await _resolver_processo(client, processo)
        dados = await client.listar_documentos_todos(id_proc)
        internos = [d for d in dados.get("documentos") or []
                    if (d.get("atributos") or {}).get("tipoDocumento") == "I"
                    and ((d.get("atributos") or {}).get("status") or {}).get("documentoCancelado") != "S"]
        sugestoes = []
        for d in internos[-documentos:][::-1]:
            a = d.get("atributos") or {}
            doc_id = str(d.get("id") or a.get("idProtocolo") or "")
            try:
                bruto = await client.visualizar_documento_interno(doc_id)
            except Exception:
                continue
            data_doc = None
            try:
                meta = await client.consultar_documento_interno(doc_id)
                data_doc = datetime.strptime(str(meta.get("dataElaboracao")), "%d/%m/%Y").date()
            except Exception:
                pass
            for p in _prazos_no_texto(html_to_text(bruto), data_doc):
                dias = (p["data"] - hoje).days
                sugestoes.append({
                    "prazo": p["data"].strftime("%d/%m/%Y"),
                    "dias_restantes": dias,
                    "situacao": ("vencido há %d dia(s)" % -dias if dias < 0
                                 else "vence hoje" if dias == 0 else "vence em %d dia(s)" % dias),
                    "como_achado": p["tipo"],
                    "trecho": p["trecho"],
                    "documento": html.unescape(str(a.get("nomeComposto") or "")),
                    "numero_sei": a.get("protocoloFormatado"),
                })
        sugestoes.sort(key=lambda x: x["dias_restantes"])
        return _json({
            "processo": processo,
            "documentos_lidos": min(documentos, len(internos)),
            "prazos_encontrados": sugestoes,
            "dica": "Confira o trecho antes de usar. Para registrar o prazo no processo, use sei_marcar_prazo.",
        })
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_marcar_prazo(
    processo: str,
    data: str,
    descricao: str = "",
    marcador: str = "Prazo",
    ctx: Context = None,
) -> str:
    """Registra um prazo no processo com um marcador da unidade.

    O texto do marcador fica "Prazo: dd/mm/aaaa – descrição", e o prazo passa
    a aparecer em sei_listar_prazos e sei_listar_pendencias.

    - processo: número do processo ou IdProcedimento
    - data: dd/mm/aaaa
    - descricao: o que vence (ex.: "resposta à SELC")
    - marcador: nome (ou id) de um marcador EXISTENTE da unidade; padrão "Prazo".
      Se não existir, nada é feito e a resposta lista os marcadores da unidade
      (criar marcador muda a lista de toda a unidade — decisão do usuário).
    """
    try:
        from datetime import datetime

        client = _get_client(ctx)
        try:
            alvo = datetime.strptime(str(data).strip(), "%d/%m/%Y").date()
        except ValueError:
            return _error("Data inválida: use dd/mm/aaaa (ex.: 04/11/2026).")
        id_proc = await _resolver_processo(client, processo)

        id_marcador, nome_marcador = str(marcador).strip(), str(marcador).strip()
        if not id_marcador.isdigit():
            r = await client.pesquisar_marcadores(limit=200)
            marcadores = r.get("marcadores") or r.get("data") or []
            alvo_nome = _normalizar_busca(marcador)
            achados = [m for m in marcadores if _normalizar_busca(str(m.get("nome") or "")) == alvo_nome] \
                or [m for m in marcadores if alvo_nome in _normalizar_busca(str(m.get("nome") or ""))]
            if len(achados) != 1:
                return _json({
                    "error": (f"Não há marcador {marcador!r} na unidade." if not achados
                              else f"Mais de um marcador corresponde a {marcador!r}."),
                    "marcadores_disponiveis": [{"id": m.get("id"), "nome": m.get("nome")}
                                               for m in (achados or marcadores)],
                    "dica": "Nada foi feito. Escolha um marcador existente (parâmetro `marcador`) "
                            "ou peça para criar um com sei_criar_marcador.",
                })
            id_marcador, nome_marcador = str(achados[0].get("id")), str(achados[0].get("nome"))

        texto = f"Prazo: {alvo.strftime('%d/%m/%Y')}" + (f" – {descricao.strip()}" if descricao.strip() else "")
        resultado = await client.marcar_processo(id_proc, id_marcador, texto)
        dias = (alvo - _hoje()).days
        return _json({
            "resultado": resultado,
            "processo": processo,
            "marcador": nome_marcador,
            "texto": texto,
            "situacao": ("vencido há %d dia(s)" % -dias if dias < 0
                         else "vence hoje" if dias == 0 else "vence em %d dia(s)" % dias),
        })
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_listar_processos_bloco_interno(
    id_bloco: str,
    ctx: Context = None,
) -> str:
    """Lista processos de um bloco interno.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.listar_processos_bloco_interno(id_bloco)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_alterar_bloco_interno(
    id_bloco: str,
    descricao: str,
    ctx: Context = None,
) -> str:
    """Altera descrição de um bloco interno.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.alterar_bloco_interno(id_bloco, descricao)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_excluir_bloco_interno(
    ids_blocos: str,
    ctx: Context = None,
) -> str:
    """Exclui bloco(s) interno(s). IDs separados por vírgula.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.excluir_blocos_internos(ids_blocos)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_concluir_bloco_interno(
    ids_blocos: str,
    ctx: Context = None,
) -> str:
    """Conclui bloco(s) interno(s). IDs separados por vírgula.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.concluir_blocos_internos(ids_blocos)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_reabrir_bloco_interno(
    id_bloco: str,
    ctx: Context = None,
) -> str:
    """Reabre bloco interno concluído.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.reabrir_bloco_interno(id_bloco)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_anotar_processo_bloco_interno(
    id_bloco: str,
    processo: str,
    descricao: str,
    ctx: Context = None,
) -> str:
    """Cria anotação em processo dentro de um bloco interno.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.anotar_processo_bloco_interno(id_bloco, id_proc, descricao)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_alterar_anotacao_bloco_interno(
    id_bloco: str,
    processo: str,
    descricao: str,
    ctx: Context = None,
) -> str:
    """Altera anotação de processo em um bloco interno.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.alterar_anotacao_bloco_interno(id_bloco, id_proc, descricao)
        return _json(result)
    except Exception as e:
        return _error(str(e))


# -- Bloco de Assinatura: operações adicionais --


@mcp.tool()
async def sei_listar_documentos_bloco_assinatura(
    id_bloco: str,
    ctx: Context = None,
) -> str:
    """Lista documentos de um bloco de assinatura."""
    try:
        client = _get_client(ctx)
        result = await client.listar_documentos_bloco_assinatura(id_bloco)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_retirar_documentos_bloco_assinatura(
    id_bloco: str,
    documentos: str,
    ctx: Context = None,
) -> str:
    """Retira documento(s) de um bloco de assinatura.

    - documentos: ID(s) de documento(s) separados por vírgula
    """
    try:
        client = _get_client(ctx)
        result = await client.retirar_documento_bloco_assinatura(id_bloco, documentos)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_alterar_bloco_assinatura(
    id_bloco: str,
    descricao: str,
    ctx: Context = None,
) -> str:
    """Altera descrição de um bloco de assinatura.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.alterar_bloco_assinatura(id_bloco, descricao)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_excluir_bloco_assinatura(
    ids_blocos: str,
    ctx: Context = None,
) -> str:
    """Exclui bloco(s) de assinatura. IDs separados por vírgula.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.excluir_blocos_assinatura(ids_blocos)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_concluir_bloco_assinatura(
    ids_blocos: str,
    ctx: Context = None,
) -> str:
    """Conclui bloco(s) de assinatura. IDs separados por vírgula.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.concluir_blocos_assinatura(ids_blocos)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_reabrir_bloco_assinatura(
    id_bloco: str,
    ctx: Context = None,
) -> str:
    """Reabre bloco de assinatura concluído.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.reabrir_bloco_assinatura(id_bloco)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_retornar_bloco_assinatura(
    id_bloco: str,
    ctx: Context = None,
) -> str:
    """Retorna bloco de assinatura para a unidade de origem.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.retornar_bloco_assinatura(id_bloco)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_anotar_documento_bloco_assinatura(
    id_bloco: str,
    documento: str,
    descricao: str,
    ctx: Context = None,
) -> str:
    """Cria anotação em documento dentro de um bloco de assinatura.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.anotar_documento_bloco_assinatura(id_bloco, documento, descricao)
        return _json(result)
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def sei_alterar_anotacao_bloco_assinatura(
    id_bloco: str,
    documento: str,
    descricao: str,
    ctx: Context = None,
) -> str:
    """Altera anotação de documento em um bloco de assinatura.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.alterar_anotacao_bloco_assinatura(id_bloco, documento, descricao)
        return _json(result)
    except Exception as e:
        return _error(str(e))


# ---------------------------------------------------------------------------
# Tool annotations (hints MCP) — ajudam o CLIENTE a decidir sobre auto-aprovação.
# A confirmação "No approval received" é gate do cliente (ex.: Claude.ai), NÃO do
# código nem do SEI; o servidor não tem flag de "requires confirmation" — só pode
# sinalizar via annotations. Leitura pura (readOnlyHint) costuma ser auto-aprovada.
# Classificado por convenção de nome (sei_<verbo>_...) para cobrir as ~116 tools
# de forma consistente, sem anotar 116 decorators à mão.
# ---------------------------------------------------------------------------
_ANNOT_VERBOS_LEITURA = {
    "listar", "pesquisar", "consultar", "buscar", "ler", "arvore", "gerar", "baixar",
    "estilos", "resumo", "versao", "parametros", "verificar", "historico", "sugestao",
}
# Critério do diretório da Anthropic: destructiveHint=True em tool que modifica
# ou apaga dado. Aqui: apaga (excluir, remover, retirar, cancelar, cassar,
# renunciar, desativar), sobrescreve (alterar, editar) ou tem efeito que a API
# não desfaz / tira o processo da unidade (assinar, enviar, concluir). Criar,
# incluir, anotar e registrar só acrescentam → False.
_ANNOT_VERBOS_DESTRUTIVOS = {
    "excluir", "remover", "retirar", "cancelar", "cassar", "renunciar", "desativar",
    "alterar", "editar", "assinar", "enviar", "concluir",
}
_ANNOT_VERBOS_IDEMPOTENTES = {"alterar", "editar"}  # reaplicar o mesmo conteúdo converge

# Título legível de cada tool (campo `title` do MCP, exigido pelo diretório).
_TITULOS_TOOLS = {
    "sei_listar_unidades": "Listar unidades do usuário",
    "sei_trocar_unidade": "Trocar unidade ativa",
    "sei_pesquisar_unidades": "Pesquisar unidades",
    "sei_listar_usuarios": "Listar usuários",
    "sei_consultar_processo": "Consultar processo",
    "sei_arvore_processo": "Árvore do processo",
    "sei_listar_documentos": "Listar documentos do processo",
    "sei_buscar_documento": "Buscar documento pelo número SEI",
    "sei_ler_documento": "Ler documento",
    "sei_baixar_anexo": "Baixar anexo (documento externo)",
    "sei_criar_documento": "Criar documento interno",
    "sei_listar_secoes": "Listar seções do documento",
    "sei_gerar_referencia": "Gerar referência a documento",
    "sei_estilos": "Estilos de formatação do SEI",
    "sei_editar_secao": "Editar seções do documento",
    "sei_listar_processos": "Listar processos da unidade",
    "sei_resumo_processos": "Resumo dos processos da unidade",
    "sei_pesquisar_processos": "Pesquisar processos",
    "sei_pesquisar_hipoteses_legais": "Pesquisar hipóteses legais",
    "sei_pesquisar_tipos_processo": "Pesquisar tipos de processo",
    "sei_alterar_processo": "Alterar processo",
    "sei_criar_processo": "Criar processo",
    "sei_enviar_processo": "Enviar (tramitar) processo",
    "sei_marcar_nao_lido": "Marcar processo como não lido",
    "sei_concluir_processo": "Concluir processo na unidade",
    "sei_reabrir_processo": "Reabrir processo",
    "sei_atribuir_processo": "Atribuir processo",
    "sei_cancelar_assinatura": "Cancelar assinatura",
    "sei_assinar_documento": "Assinar documento",
    "sei_pesquisar_tipos_documento": "Pesquisar tipos de documento",
    "sei_sobrestar_processo": "Sobrestar processo",
    "sei_remover_sobrestamento": "Remover sobrestamento",
    "sei_dar_ciencia": "Dar ciência",
    "sei_listar_ciencias": "Listar ciências",
    "sei_remover_atribuicao": "Remover atribuição",
    "sei_receber_processo": "Receber processo",
    "sei_listar_unidades_processo": "Unidades onde o processo está aberto",
    "sei_listar_interessados": "Listar interessados do processo",
    "sei_listar_sobrestamentos": "Histórico de sobrestamentos",
    "sei_listar_assinaturas": "Listar assinaturas do documento",
    "sei_registrar_andamento": "Registrar andamento",
    "sei_pesquisar_contatos": "Pesquisar contatos",
    "sei_criar_documento_externo": "Criar documento externo (upload)",
    "sei_assinar_bloco": "Assinar bloco de assinatura",
    "sei_assinar_documentos_bloco": "Assinar documentos do bloco",
    "sei_criar_marcador": "Criar marcador",
    "sei_excluir_marcador": "Excluir marcador",
    "sei_marcar_processo": "Marcar processo",
    "sei_pesquisar_marcadores": "Pesquisar marcadores",
    "sei_consultar_marcador_processo": "Marcadores do processo",
    "sei_acompanhar_processo": "Acompanhar processo",
    "sei_remover_acompanhamento": "Remover acompanhamento especial",
    "sei_criar_grupo_acompanhamento": "Criar grupo de acompanhamento",
    "sei_excluir_grupo_acompanhamento": "Excluir grupo de acompanhamento",
    "sei_listar_grupos_acompanhamento": "Listar grupos de acompanhamento",
    "sei_criar_bloco_interno": "Criar bloco interno",
    "sei_incluir_processo_bloco_interno": "Incluir processo em bloco interno",
    "sei_retirar_processo_bloco_interno": "Retirar processo de bloco interno",
    "sei_criar_bloco_assinatura": "Criar bloco de assinatura",
    "sei_incluir_documento_bloco_assinatura": "Incluir documento em bloco de assinatura",
    "sei_disponibilizar_bloco_assinatura": "Disponibilizar bloco de assinatura",
    "sei_cancelar_disponibilizacao_bloco": "Cancelar disponibilização de bloco",
    "sei_pesquisar_blocos_assinatura": "Pesquisar blocos de assinatura",
    "sei_pesquisar_blocos_internos": "Pesquisar blocos internos",
    "sei_listar_prazos": "Listar prazos da unidade",
    "sei_assinar_documentos": "Assinar vários documentos",
    "sei_consultar_modelo_documento": "Consultar modelo de tipo de documento",
    "sei_consultar_modelo_processo": "Consultar modelo de tipo de processo",
    "sei_buscar_prazos_no_texto": "Buscar prazos no texto dos documentos",
    "sei_marcar_prazo": "Marcar prazo no processo",
    "sei_listar_pendencias": "Listar pendências da unidade",
    "sei_criar_anotacao": "Criar anotação no processo",
    "sei_versao": "Versão do SEI e do wssei",
    "sei_listar_orgaos": "Listar órgãos",
    "sei_listar_contextos": "Listar contextos do órgão",
    "sei_pesquisar_usuarios": "Pesquisar usuários",
    "sei_pesquisar_outras_unidades": "Pesquisar outras unidades",
    "sei_pesquisar_textos_padrao": "Pesquisar textos padrão",
    "sei_consultar_documento_externo": "Consultar documento externo",
    "sei_consultar_documento_interno": "Consultar documento interno",
    "sei_converter_documento": "Converter documento para outro tipo",
    "sei_alterar_documento_interno": "Alterar metadados de documento interno",
    "sei_alterar_documento_externo": "Alterar documento externo",
    "sei_pesquisar_tipos_conferencia": "Pesquisar tipos de conferência",
    "sei_sugestao_assuntos_documento": "Sugerir assuntos para tipo de documento",
    "sei_listar_blocos_documento": "Blocos de assinatura do documento",
    "sei_pesquisar_tipos_documento_externo": "Pesquisar tipos de documento externo",
    "sei_parametros_upload": "Parâmetros de upload",
    "sei_pesquisar_assuntos": "Pesquisar assuntos",
    "sei_sugestao_assuntos_processo": "Sugerir assuntos para tipo de processo",
    "sei_consultar_atribuicao": "Consultar atribuição do processo",
    "sei_verificar_acesso": "Verificar acesso ao processo",
    "sei_listar_relacionamentos": "Listar processos relacionados",
    "sei_listar_atividades": "Histórico de andamentos do processo",
    "sei_listar_meus_acompanhamentos": "Meus acompanhamentos especiais",
    "sei_listar_acompanhamentos_unidade": "Acompanhamentos especiais da unidade",
    "sei_alterar_acompanhamento": "Alterar acompanhamento especial",
    "sei_listar_credenciamentos": "Listar credenciamentos",
    "sei_conceder_credenciamento": "Conceder credenciamento",
    "sei_renunciar_credenciamento": "Renunciar a credenciamento",
    "sei_cassar_credenciamento": "Cassar credenciamento",
    "sei_listar_assinantes": "Listar cargos de assinatura",
    "sei_listar_orgaos_assinante": "Listar órgãos para assinatura",
    "sei_criar_observacao": "Criar observação da unidade",
    "sei_criar_contato": "Criar contato",
    "sei_listar_grupos_modelos": "Listar grupos de modelos",
    "sei_listar_modelos": "Listar modelos de documento",
    "sei_desativar_marcador": "Desativar marcador",
    "sei_reativar_marcador": "Reativar marcador",
    "sei_historico_marcador_processo": "Histórico de marcadores do processo",
    "sei_listar_processos_bloco_interno": "Processos do bloco interno",
    "sei_alterar_bloco_interno": "Alterar bloco interno",
    "sei_excluir_bloco_interno": "Excluir bloco interno",
    "sei_concluir_bloco_interno": "Concluir bloco interno",
    "sei_reabrir_bloco_interno": "Reabrir bloco interno",
    "sei_anotar_processo_bloco_interno": "Anotar processo em bloco interno",
    "sei_alterar_anotacao_bloco_interno": "Alterar anotação em bloco interno",
    "sei_listar_documentos_bloco_assinatura": "Documentos do bloco de assinatura",
    "sei_retirar_documentos_bloco_assinatura": "Retirar documentos de bloco de assinatura",
    "sei_alterar_bloco_assinatura": "Alterar bloco de assinatura",
    "sei_excluir_bloco_assinatura": "Excluir bloco de assinatura",
    "sei_concluir_bloco_assinatura": "Concluir bloco de assinatura",
    "sei_reabrir_bloco_assinatura": "Reabrir bloco de assinatura",
    "sei_retornar_bloco_assinatura": "Retornar bloco de assinatura",
    "sei_anotar_documento_bloco_assinatura": "Anotar documento em bloco de assinatura",
    "sei_alterar_anotacao_bloco_assinatura": "Alterar anotação em bloco de assinatura",
}


def _aplicar_tool_annotations() -> None:
    """Aplica title e ToolAnnotations às tools registradas, por convenção de nome.

    Respeita annotations já declaradas explicitamente no decorator (não sobrescreve).
    """
    try:
        from mcp.types import ToolAnnotations
        tools = getattr(getattr(mcp, "_tool_manager", None), "_tools", None) or {}
        for nome, tool in tools.items():
            if not tool.title:
                tool.title = _TITULOS_TOOLS.get(nome)
            if getattr(tool, "annotations", None) is not None:
                if tool.annotations.title is None:
                    tool.annotations = tool.annotations.model_copy(update={"title": tool.title})
                continue
            partes = nome.split("_")
            verbo = partes[1] if len(partes) > 1 else ""
            if verbo in _ANNOT_VERBOS_LEITURA:
                tool.annotations = ToolAnnotations(
                    title=tool.title, readOnlyHint=True, openWorldHint=True,
                )
            else:
                tool.annotations = ToolAnnotations(
                    title=tool.title,
                    readOnlyHint=False,
                    destructiveHint=verbo in _ANNOT_VERBOS_DESTRUTIVOS,
                    idempotentHint=True if verbo in _ANNOT_VERBOS_IDEMPOTENTES else None,
                    openWorldHint=True,
                )
    except Exception as e:  # nunca deixar a annotation quebrar o boot
        logger.warning("Falha ao aplicar tool annotations: %s", e)


_aplicar_tool_annotations()


def _autor_auditoria() -> dict:
    """Quem está chamando: sessão OAuth (HTTP) ou SEI_USUARIO (stdio)."""
    from mcp_seipro import auditoria

    if _http_mode:
        try:
            sid, creds = _credenciais_sessao()
            return auditoria.identificar(sid, creds.get("sei_usuario"))
        except Exception:
            return auditoria.identificar(None, None)
    return auditoria.identificar(None, os.environ.get("SEI_USUARIO"))


def _aplicar_auditoria() -> None:
    """Envolve as tools de ESCRITA para registrar autor, alvo e resultado.

    Usa a mesma classificação das annotations: verbo fora de
    `_ANNOT_VERBOS_LEITURA` é escrita. O SDK chama `tool.fn(**args, ctx=...)`
    com os argumentos já validados, então o wrapper não muda o schema.
    """
    import functools
    from mcp_seipro import auditoria

    tools = getattr(getattr(mcp, "_tool_manager", None), "_tools", None) or {}
    for nome, tool in tools.items():
        partes = nome.split("_")
        verbo = partes[1] if len(partes) > 1 else ""
        if verbo in _ANNOT_VERBOS_LEITURA or getattr(tool.fn, "_auditada", False):
            continue

        original = tool.fn

        @functools.wraps(original)
        async def auditada(*args, __nome=nome, __fn=original, **kwargs):
            inicio = time.monotonic()
            try:
                retorno = await __fn(*args, **kwargs)
            except Exception as e:
                auditoria.registrar({
                    **_autor_auditoria(), "tool": __nome,
                    "args": auditoria.resumir_argumentos(kwargs),
                    "ok": False, "resultado": {"erro": str(e)[:300]},
                    "ms": int((time.monotonic() - inicio) * 1000),
                })
                raise
            ok, resumo = auditoria.resumir_resultado(retorno)
            auditoria.registrar({
                **_autor_auditoria(), "tool": __nome,
                "args": auditoria.resumir_argumentos(kwargs),
                "ok": ok, "resultado": resumo,
                "ms": int((time.monotonic() - inicio) * 1000),
            })
            return retorno

        auditada._auditada = True
        tool.fn = auditada


_aplicar_auditoria()


def _chave_limite() -> str:
    """Cota por sessão OAuth (HTTP) ou única (stdio)."""
    if _http_mode:
        try:
            sid, _ = _credenciais_sessao()
            return sid
        except Exception:
            return "anonimo"
    return "local"


def _aplicar_limites() -> None:
    """Envolve TODAS as tools com o limite de uso por usuário (limites.py).

    Fica por fora da auditoria: chamada bloqueada não chega ao SEI, mas a
    escrita bloqueada é registrada na auditoria como "bloqueado_limite".
    """
    import functools
    from mcp_seipro import auditoria, limites

    tools = getattr(getattr(mcp, "_tool_manager", None), "_tools", None) or {}
    for nome, tool in tools.items():
        if getattr(tool.fn, "_limitada", False):
            continue
        partes = nome.split("_")
        escrita = (partes[1] if len(partes) > 1 else "") not in _ANNOT_VERBOS_LEITURA
        original = tool.fn

        @functools.wraps(original)
        async def limitada(*args, __nome=nome, __fn=original, __escrita=escrita, **kwargs):
            bloqueio = limites.registrar(_chave_limite(), __escrita)
            if bloqueio:
                if __escrita:
                    auditoria.registrar({
                        **_autor_auditoria(), "tool": __nome,
                        "args": auditoria.resumir_argumentos(kwargs),
                        "ok": "bloqueado_limite", "resultado": {"motivo": bloqueio},
                    })
                return _json({"error": bloqueio, "erro_origem": "limite_de_uso"})
            return await __fn(*args, **kwargs)

        limitada._limitada = True
        limitada._auditada = getattr(original, "_auditada", False)
        tool.fn = limitada


_aplicar_limites()


_INICIO_SERVIDOR = time.time()
_STATUS_SEI_CACHE: dict = {}


async def _status_sei() -> dict:
    """Testa o SEI pela rota pública /orgao/listar (sem credencial), com cache de 30 s."""
    agora = time.monotonic()
    if _STATUS_SEI_CACHE and agora - _STATUS_SEI_CACHE["em"] < 30:
        return _STATUS_SEI_CACHE["dados"]
    url = os.environ.get("SEI_URL", "").rstrip("/")
    if not url:
        dados = {"configurado": False}
    else:
        import httpx
        from urllib.parse import urlparse

        inicio = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=8.0) as cli:
                r = await cli.get(f"{url}/orgao/listar")
            dados = {"host": urlparse(url).hostname, "acessivel": r.status_code == 200,
                     "http": r.status_code, "ms": int((time.monotonic() - inicio) * 1000)}
        except Exception as e:
            dados = {"host": urlparse(url).hostname, "acessivel": False,
                     "erro": type(e).__name__, "ms": int((time.monotonic() - inicio) * 1000)}
    _STATUS_SEI_CACHE.update({"em": agora, "dados": dados})
    return dados


async def _pagina_status() -> dict:
    """Situação do servidor, sem dados de usuário (página pública)."""
    from importlib import metadata
    from mcp_seipro import limites

    try:
        versao = metadata.version("mcp-seipro")
    except metadata.PackageNotFoundError:
        versao = "desconhecida"
    sessoes = {chave[1] for chave in _cache_clientes if chave[0] == "rest"}
    return {
        "servico": "ok",
        "versao": versao,
        "no_ar_desde": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_INICIO_SERVIDOR)),
        "sessoes_ativas": len(sessoes),
        "chamadas_ultimos_5_min": limites.chamadas_recentes(),
        "limites_por_usuario": {
            "chamadas_por_minuto": limites.limite_chamadas() or "sem limite",
            "escritas_por_minuto": limites.limite_escritas() or "sem limite",
        },
        "sei": await _status_sei(),
    }



def main():
    if _http_mode:
        import uvicorn
        from pathlib import Path
        from starlette.routing import Route
        from starlette.responses import Response

        from mcp_seipro.auth import login_page, login_submit

        # Favicon / ícone do SEI Pro — busca em vários locais possíveis
        _icon_bytes = b""
        for _candidate in [
            Path(__file__).resolve().parent.parent.parent / "icon.png",  # dev: repo root
            Path("/app/icon.png"),  # Docker
        ]:
            if _candidate.exists():
                _icon_bytes = _candidate.read_bytes()
                break

        from starlette.responses import HTMLResponse

        async def favicon(request):
            return Response(_icon_bytes, media_type="image/png",
                            headers={"Cache-Control": "public, max-age=86400"})

        _base = os.environ.get("BASE_URL", f"http://localhost:{_http_port}")
        _root_html = f"""<!DOCTYPE html>
<html><head>
<link rel="icon" type="image/png" href="{_base}/favicon.ico">
<link rel="icon" type="image/png" sizes="128x128" href="{_base}/icon.png">
<link rel="apple-touch-icon" href="{_base}/icon.png">
<title>SEI Pro MCP Server</title>
</head><body><h1>SEI Pro MCP Server</h1></body></html>"""

        async def root_page(request):
            return HTMLResponse(_root_html)

        app = mcp.streamable_http_app()
        # Adiciona rotas extras
        app.routes.insert(0, Route("/", root_page, methods=["GET"]))
        app.routes.insert(1, Route("/favicon.ico", favicon, methods=["GET"]))
        app.routes.insert(2, Route("/icon.png", favicon, methods=["GET"]))
        app.routes.insert(3, Route("/login", login_page, methods=["GET"]))
        app.routes.insert(4, Route("/login", login_submit, methods=["POST"]))

        from starlette.responses import JSONResponse

        async def status_page(request):
            return JSONResponse(await _pagina_status(), headers={"Cache-Control": "no-store"})

        app.routes.insert(5, Route("/status", status_page, methods=["GET"]))

        config = uvicorn.Config(
            app,
            host=_http_host,
            port=_http_port,
            log_level="info",
        )
        import anyio
        anyio.run(uvicorn.Server(config).serve)
    else:
        mcp.run(transport="stdio")
