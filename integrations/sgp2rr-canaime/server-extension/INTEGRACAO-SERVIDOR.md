# Integração SGP2RR MCPServer — versão adaptada ao servidor real

**Estado:** código pronto para homologação offline; nenhum arquivo da produção foi alterado.

O diagnóstico realizado em 01/11/2026 identificou que:

- O script principal é sgp2rr_mcp.py (571 linhas na versão inspecionada).
- O servidor utiliza mcp.server.mcpserver.MCPServer, não FastMCP.
- As cinco ferramentas existentes usam o decorador @mcp.tool().
- atual() devolve Sessao específica do token OAuth da requisição;
  em modo online, consulta provedor.sessao_do_token(tok.token).
- Sessao tem os métodos carregar(), _url(), lock e client httpx.Client.
- renderizar(Pagina) é o formato textual das consultas existentes.

## Arquivos da extensão

- sgp2rr_search.py: 13 critérios de pesquisa, inclusive código.
- sgp2rr_tools.py: registro de oito ferramentas de leitura.
- sgp2rr_integration.py: allowlist de GET e leitura segura de PDF via httpx.stream.
- sgp2rr_server_bridge.py: integra exatamente as funções atual(),
  Sessao.carregar(), Sessao._url(), renderizar() e Sessao.client.
- prepare_integration.py: prepara cópia do servidor com as oito ferramentas;
  **não altera a produção**.
- tests/: testes offline com dados inteiramente fictícios.

## 1. Baixar e testar em homologação

No servidor Ubuntu, execute:

~~~bash
set -e
cd /home/ubuntu
git clone --depth 1 --branch feat/sgp2rr-canaime-chatgpt \
  https://github.com/Obritorodrigo/mcp-seipro.git \
  sgp2rr-plugin-stage

cd sgp2rr-plugin-stage/integrations/sgp2rr-canaime/server-extension
/home/ubuntu/sgp2rr-mcp/.venv/bin/python -m unittest discover -s tests -v

STAGE_DIR="$(mktemp -d /home/ubuntu/sgp2rr-stage-XXXXXXXX)"
/home/ubuntu/sgp2rr-mcp/.venv/bin/python prepare_integration.py \
  --source /home/ubuntu/sgp2rr-mcp/sgp2rr_mcp.py \
  --output-dir "$STAGE_DIR"

echo "Cópia de homologação: $STAGE_DIR"
~~~

O comando pressupõe que a pasta sgp2rr-plugin-stage ainda não exista.
Se existir, use outra pasta de clone; não sobrescreva nada sem revisão.

Este procedimento não reinicia a unidade systemd, não altera o ambiente
virtual, não instala bibliotecas, não usa logins nem lê dados de custodiados.

## 2. Integração automática na CÓPIA

prepare_integration.py valida a estrutura sintática e os cinco decoradores
existentes e injeta, **após a última ferramenta do arquivo**, o seguinte
bloco (apenas na cópia):

~~~python
from sgp2rr_server_bridge import registrar_sgp2rr_no_servidor as _registrar_sgp2rr
_registrar_sgp2rr(mcp=mcp, atual=atual, renderizar=renderizar)
~~~

sgp2rr_server_bridge.py registra oito ferramentas novas no MCPServer já
instanciado. A sessão é obtida DENTRO da requisição MCP (não no startup).
A pesquisa usa endpoints GET fixos; não depende de sessao.ultima ou da
posição do formulário ativo. PDF usa o httpx.Client da mesma Sessao e
bloqueia redirecionamentos.

## 3. Verificações antes de qualquer deploy

1. Conferir os 30 testes automatizados (mocks) e o parse do script real.
2. Validar as novas ferramentas em um serviço MCP isolado de homologação,
   sem usar a porta ou o service de produção.
3. Homologar pesquisa por nome, mãe, pai, CPF, endereço e código com
   cadastro de teste/autorização institucional.
4. Testar dois tokens OAuth diferentes para confirmar isolamento e revogação.
5. Confirmar limites, MIME e acesso a PDF da sessão correta; para extração,
   a dependência pypdf precisa estar presente no ambiente homologado.
6. Verificar que nenhum parâmetro GET sensível é registrado no proxy,
   trace de exceptions ou logs de requisições HTTP.
7. Testar rollback a partir de backup antes de qualquer modificação de produção.
8. Registrar, sem dados pessoais, quais ferramentas estão disponíveis após
   a homologação.

A opção de busca por foto consta no formulário, mas sua semântica ainda
não foi homologada. O adaptador não exporta imagens.

## 4. Produção

Nenhuma implantação em produção é incluída neste procedimento. Antes
de copiar arquivos para /home/ubuntu/sgp2rr-mcp ou reiniciar
sgp2rr-mcp.service, revisar o diff da cópia, o ponto de inicialização do
MCPServer e a concorrência do gerenciador OAuth. A montagem foi feita
com base na estrutura relatada do código, e não na leitura integral
do sgp2rr_mcp.py.

**Resultado esperado após homologação e deploy futuro:** 13 ferramentas
(5 existentes + 8 novas). Não afirmar que as novas ferramentas já estão
ativas enquanto não aparecerem na descoberta MCP da sessão.
