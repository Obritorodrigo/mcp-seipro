# SGP2RR — Canaimé · integração ChatGPT

Plugin privado do ChatGPT que conecta um usuário autorizado ao servidor MCP existente do SGP2RR/Canaimé. Este diretório foi adicionado ao repositório do SEI Pro apenas para **versionar o conector ChatGPT**; os projetos, sistemas e respectivos acessos permanecem independentes.

## Estrutura

- `plugin.json`: manifesto portátil do plugin;
- `mcp.json`: endereço MCP remoto e configuração OAuth/DCR;
- `skills/sgp2rr-operacao/SKILL.md`: regras de navegação e tratamento de dados;
- `.codex-plugin/plugin.json` e `.mcp.json`: arquivos de compatibilidade;
- `docs/verification.md`: relatório de validação sem informações pessoais.

## Servidor

`https://sgp2rr.projetogpt.com.br/mcp`

O MCP remoto já existe e utiliza autenticação OAuth individual. Este diretório **não contém o código do serviço hospedado**, não implanta o serviço e não autentica automaticamente outros usuários.

## Uso

1. Adicione o plugin privado no ChatGPT usando o pacote da integração.
2. Conclua o fluxo de autenticação do servidor MCP na interface autorizada.
3. Teste primeiro uma ferramenta de leitura de status e a navegação inicial.
4. Respeite as permissões da conta autenticada; alterações exigem pedido explícito.

## Segurança

O repositório GitHub é **público**. Não adicione senhas, tokens, cookies, dados de custodiados, exportações, páginas de sessão, rastros pessoais, arquivos `.env` ou logs brutos do servidor. Leia as regras em `skills/sgp2rr-operacao/SKILL.md`.

A integração não deve ser confundida com o SEI Pro ou usar sua sessão.
