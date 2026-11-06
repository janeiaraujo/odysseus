# Roteamento

Perfis: Econômico, Local, Equilibrado, Alta qualidade, Pesquisa profunda e Multimodal.

**Estado atual:** o filtro de fallback e o registro de uso atuam em todo chat. O ranking é usado em missões, voz e análise de fotos. **No chat, o modelo continua sendo o que você escolhe**; ainda não existe a opção "automático" no seletor.

## Regras de custo (`router.paid_fallback_mode`)

| Modo | Efeito |
|---|---|
| `never` | Pagos nunca são usados como fallback |
| `configured` (padrão) | Só os listados em uma cadeia de fallback ou na `paid_model_allowlist` |
| `always` | O roteador pode escolher qualquer um |

Orçamento mensal atingido bloqueia pagos em qualquer modo. Falha na verificação de custo descarta o modelo pago.

## Disponibilidade

Três falhas seguidas pausam o modelo por 5 minutos, e um erro 429 por 60 segundos. Dá para limitar requisições por minuto em `router.rate_limit_rpm`.

## Custo

Preços vêm de `pricing` ou de **Atualizar preços do OpenRouter**. Sem preço conhecido, a chamada fica sem custo registrado e é contada à parte, nunca estimada. Provedores diretos (OpenAI, Anthropic, Google) precisam de preço configurado à mão para contar no orçamento.
