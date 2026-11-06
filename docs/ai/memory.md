# Memória

Tipos: **semântica** (fatos e preferências), **episódica** (eventos), **procedural** (como fazer) e **operacional** (estado). A memória de curto prazo é o próprio histórico da conversa.

SQLite é a fonte da verdade; FTS5 e ChromaDB são índices reconstruíveis. A busca combina os dois (reciprocal rank fusion) e continua funcionando por texto se o ChromaDB cair. O filtro de usuário é aplicado no SQL.

| Recurso | Comportamento |
|---|---|
| Deduplicação | Texto igual ou muito parecido não cria nova linha |
| Conflito | Mesmo assunto com valor diferente marca os dois como "em conflito" até você decidir |
| Origem | Toda memória registra de onde veio |
| Não confiável | E-mail, web e documentos entram como hipótese, nunca alteram um fato e ficam fora da busca padrão |
| Versões | Correções guardam o histórico |
| Validade | Memórias vencidas não são recuperadas |
| Exclusão | Normal oculta; `purge=true` apaga texto e histórico |

A memória do Jarvis soma-se à do Odysseus (`data/memory.json`), que não é alterada. Para copiar: **Importar do Odysseus**, na aba Memória.
