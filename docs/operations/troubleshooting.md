# Solução de problemas

| Sintoma | Causa provável | O que fazer |
|---|---|---|
| Interface antiga depois de atualizar | Service worker | Recarregue; `CACHE_NAME` muda a cada versão |
| Conta Google em "reconectar" | Token recusado, ou 7 dias em modo de teste | Conecte de novo em Integrações |
| `missing_scope` | Permissão não concedida | Marque o grupo e conecte de novo |
| Voz indisponível | STT desligado | Configurações > STT; local requer `faster-whisper` |
| Navegador indisponível | Playwright ausente | `INSTALL_OPTIONAL=true` no build, ou o perfil `browser` |
| Busca só por texto | ChromaDB fora do ar | `docker compose up -d chromadb` e reindexar |
| E-mail em "verificando" | Envio interrompido | Aguarde: a fila confere no Gmail antes de reenviar |
| Modelo pago ausente no fallback | Regras de custo | Aba Modelos, ou orçamento atingido |
| Falha ao importar `mcp_servers` | `mcp` 2.x | `requirements.txt` agora fixa `mcp<2`; recompile a imagem |
| Modo conversa não reage à voz | Microfone errado ou sem permissão | Veja o medidor verde ao lado do botão: se não mexer, escolha o microfone no navegador. Requer `localhost` ou HTTPS |
| Sem som na resposta | TTS desligado | Configurações > TTS > "Local em português (Piper)" |
| Transcrição errada | Modelo pequeno | Configurações > STT: use `small` (`base` gasta menos memória, mas erra mais em português) |
| "O áudio do navegador não iniciou" | Relógio de áudio parado, sem saída de som utilizável (ex.: fone Bluetooth adormecido) | Escolha outra saída no sistema e recarregue a página |
| Memórias sumiram | Versão antiga da limpeza | Restaure de `data/memory_history/` |
