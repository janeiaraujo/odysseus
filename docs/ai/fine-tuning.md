# Treinamento de modelos (opcional)

**Estado: pipeline de dados testado; o treino em si nunca foi executado, pois esta máquina não tem GPU.**

Os pesos nunca mudam a cada interação. O fluxo é: exemplos validados → dataset versionado, com hash e segredos mascarados → job separado → avaliação → ativar ou voltar atrás.

```bash
curl -X POST .../api/jarvis/learning/dataset
scripts/jarvis-train --run ID --dataset ARQUIVO --base-model MODELO --dry-run
```

Treinar requer GPU e `pip install torch transformers peft datasets`. Priorize memória e ferramentas: ensinam mais rápido, custam menos e podem ser desfeitas.
