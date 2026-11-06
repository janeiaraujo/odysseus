# App Android

**Estado: planejado; não iniciado.** O foco atual é o servidor.

Será uma interface do servidor (Kotlin, Compose, Room, WorkManager, SSE), sem memória própria conflitante. A base existe em `companion/`, com pareamento por token.

Pendências no servidor antes do app: as rotas `/api/jarvis/*` recusam tokens de API e precisam de escopos próprios; o FCM está implementado, mas não testado; a criptografia da réplica no Firestore precisa de troca de chave.

Observado nesta máquina: SDK com plataforma 35 em `~/Android/Sdk`. Escuta contínua com a tela apagada depende de validação na versão de destino do Android.
