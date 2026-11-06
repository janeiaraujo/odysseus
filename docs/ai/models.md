# Modelos

Os modelos vêm dos endpoints já cadastrados no Odysseus; o Jarvis não os altera. Provedor, nível e capacidades são identificados pela URL e pelo nome do modelo. Identificar por nome é uma aproximação: confira no painel e ajuste com `router.quality_overrides` se precisar.

Níveis: **local** (endereço privado ou `endpoint_kind=local`), **grátis** (padrões em `router.free_model_patterns`, como `:free`) e **pago** (todo o resto, por segurança).

Atualizar modelos: pela interface de endpoints do Odysseus ou pelo Cookbook.
