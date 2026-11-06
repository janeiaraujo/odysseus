# Embeddings

Reaproveita o cliente do Odysseus: FastEmbed local e gratuito (`all-MiniLM-L6-v2`) e, opcionalmente, um endpoint configurado. Coleções do Jarvis: `jarvis_memories` e `jarvis_docs`, separadas das do Odysseus. A indexação de memórias roda em segundo plano para não atrasar respostas.

Trocou de modelo? Use **Reindexar** (`POST /api/jarvis/memory/reindex`).
