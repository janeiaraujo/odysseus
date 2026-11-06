# Diagramas

## Uma mensagem no chat

```mermaid
flowchart TD
  U[Mensagem] --> I{Comando direto?}
  I -- sim --> D[Resposta do banco, sem tokens]
  I -- não --> C[Monta contexto: persona + memória como dado não confiável]
  C --> F[Regras de custo filtram os fallbacks]
  F --> L[Modelo]
  L --> R[Resposta]
  R --> A[Registra uso e aprende com a mensagem do usuário]
```

## Uma ação com efeito externo

```mermaid
flowchart TD
  P[Pedido: usuário, agente ou missão] --> E{Política}
  E -- negar --> N[Negado e registrado]
  E -- automático --> X[Executa e audita]
  E -- confirmar --> Q[Fila de aprovação]
  Q -- aprovado --> X
  Q -- rejeitado ou expirado --> N
```

O risco é definido no código de cada ação. Ações destrutivas, financeiras e físicas críticas sempre passam pela fila, seja qual for a regra configurada.

## Envio de e-mail sem duplicidade

```mermaid
stateDiagram-v2
  [*] --> awaiting_approval
  [*] --> queued: regra automática
  awaiting_approval --> queued: aprovado
  queued --> sending: claim atômico
  sending --> sent: Gmail aceitou
  sending --> failed: Gmail recusou
  sending --> uncertain: timeout ou queda
  uncertain --> sent: Message-ID encontrado
  uncertain --> queued: ausência confirmada
  queued --> cancelled
```
