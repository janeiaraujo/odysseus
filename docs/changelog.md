# Changelog

## jarvis/dev

### Corrigido no Odysseus
- `docker`: vetores do ChromaDB ficavam fora do volume persistente e se perdiam ao recriar o container.
- `deps`: `mcp` 2.x quebrava os servidores MCP; fixado em `mcp<2`.
- O atalho do agente sem ferramentas descartava histórico e memória; agora os preserva quando a persona está ativa.

### Adicionado
- Identidade J.A.R.V.I.S. e painel web em português.
- Política de aprovação, auditoria e mascaramento de segredos.
- Roteador de modelos com regras de custo e registro de uso.
- Memória de longo prazo híbrida e motor de aprendizado.
- Comandos diretos sem consumo de tokens.
- Missões persistentes e notificações.
- Integrações Google, fila de e-mail, Home Assistant e voz.
- Navegador com proteção contra SSRF.
- Armazenamento local e R2, experimento de zero byte e réplica Firestore.
- Backup criptografado e scripts de instalação, atualização e diagnóstico.
- Diretório de segredos fora do repositório, montado somente leitura no container e incluído no backup criptografado.
