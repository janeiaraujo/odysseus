# Autenticação

Usa a autenticação do Odysseus: senha bcrypt, sessões e 2FA TOTP. Toda rota `/api/jarvis/*` exige usuário, e uma varredura automática nos testes confirma isso. As únicas exceções são as três rotas públicas de login com Google. Tudo que toca contas externas, dispositivos ou o sistema exige administrador.

OAuth: PKCE, `state` de uso único com validade e vinculado ao usuário. Veja [Google OAuth](../integrations/google-oauth.md).

Acesso remoto: mantenha a autenticação ligada e use Tailscale ou um proxy com HTTPS. Nunca exponha ChromaDB, Ollama ou o banco à internet. O Compose já publica essas portas só em `127.0.0.1`.
