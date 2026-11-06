# Changelog

## jarvis/dev

### Corrigido no Odysseus
- `docker`: vetores do ChromaDB ficavam fora do volume persistente e se perdiam ao recriar o container.
- `deps`: `mcp` 2.x quebrava os servidores MCP; fixado em `mcp<2`.
- O atalho do agente sem ferramentas descartava histórico e memória; agora os preserva quando a persona está ativa.

### Corrigido: perda de memórias (06/11/2026)
Importar 130 memórias deixava de 4 a 10. Havia três causas somadas no Odysseus:
1. A tarefa Memory Tidy disparava a cada 5 memórias adicionadas e deixava um modelo escolher o que apagar. Agora só remove duplicatas exatas; a limpeza com IA virou opção (`memory_ai_tidy`) e está desligada.
2. Ela lia o arquivo, esperava o modelo e gravava a cópia antiga, apagando o que entrou no intervalo. `MemoryManager.save` agora combina com o que está em disco, com trava entre processos, e guarda cópia em `data/memory_history/` antes de qualquer redução.
3. Arquivos de texto passavam por um modelo com corte de 15 mil caracteres. Listas (`.txt`, `.md`, `.csv`, `.json`) agora entram inteiras, sem modelo, e documentos longos são lidos em partes.

"Salvar tudo" usa uma só requisição (`/api/memory/add-bulk`). As memórias perdidas foram recuperadas do ChromaDB (223 restauradas).

### Adicionado (06/11/2026)
- **Modo conversa** no chat: botão ao lado do microfone que escuta, responde em voz e volta a escutar, com medidor de nível, interrupção por fala e todas as ferramentas do agente.
- **Voz em português no servidor** com Piper; transcrição local com Whisper. Corrigida a incompatibilidade `faster-whisper` x `av`, que fazia a transcrição falhar em silêncio.
- **Espelho de memórias:** o Jarvis copia tudo do Odysseus a cada 30s e segue as exclusões. Fatos de identidade entram em toda resposta.
- **Credenciais pelo painel:** envio dos JSON do Google e do Firebase em Integrações, com teste real do Firestore.

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
