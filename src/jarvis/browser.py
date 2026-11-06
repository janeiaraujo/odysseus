"""Browser automation with Playwright, connected to an isolated browser when configured.

Every request a page makes, including subresources and redirects, is checked:
private, loopback and metadata addresses are blocked, so a hostile page can't reach
internal services. Sessions are encrypted per user and never shown to a model.
CAPTCHA or login walls stop the run and ask for a human, with no attempt to bypass.
Form submission needs approval through the policy layer.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from src.constants import JARVIS_BROWSER_DIR
from src.jarvis import audit, config, policy
from src.prompt_security import untrusted_context_message
from src.secret_storage import decrypt, encrypt
from src.url_safety import check_outbound_url

logger = logging.getLogger(__name__)
_CAPTCHA = re.compile(r"(recaptcha|hcaptcha|cf-turnstile|captcha|verify you are human|confirme que voce e humano)", re.I)
_IRREVERSIBLE = re.compile(r"(comprar|pagar|finalizar|confirmar pedido|checkout|excluir|apagar|delete|buy|pay|purchase|order now|transfer)", re.I)


class BrowserUnavailable(RuntimeError):
    pass


class BlockedUrl(ValueError):
    pass


class HumanRequired(RuntimeError):
    pass


def url_allowed(url: str, resolver: Optional[Callable[[str], List[str]]] = None) -> tuple:
    if url.startswith(("data:", "blob:", "about:")):
        return True, ""
    return check_outbound_url(url, block_private=True, resolver=resolver)


def _state_path(owner: str) -> Path:
    import hashlib
    return Path(JARVIS_BROWSER_DIR) / f"{hashlib.sha256((owner or 'default').encode()).hexdigest()[:24]}.state"


def _load_state(owner: str) -> Optional[Dict[str, Any]]:
    path = _state_path(owner)
    if not path.is_file():
        return None
    try:
        return json.loads(decrypt(path.read_text(encoding="utf-8")))
    except Exception as exc:
        logger.warning("browser session unreadable: %s", type(exc).__name__)
        return None


def _save_state(owner: str, state: Dict[str, Any]) -> None:
    path = _state_path(owner)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encrypt(json.dumps(state)), encoding="utf-8")
    os.chmod(path, 0o600)


def forget_session(owner: str) -> bool:
    path = _state_path(owner)
    if path.is_file():
        path.unlink()
        audit.record("browser.forget_session", owner=owner, risk="reversible")
        return True
    return False


class Session:
    """One isolated page. Use as `async with Session(owner) as s`."""

    def __init__(self, owner: str, *, resolver: Optional[Callable[[str], List[str]]] = None, persist: bool = False, launch_options: Optional[Dict[str, Any]] = None):
        self.owner, self.resolver, self.persist = owner or "", resolver, persist
        self.launch_options = launch_options or {}
        self.blocked: List[str] = []
        self._playwright = self._browser = self._context = self.page = None

    async def __aenter__(self) -> "Session":
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise BrowserUnavailable("Playwright não instalado; veja docs/integrations/browser.md") from None
        self._playwright = await async_playwright().start()
        endpoint = os.environ.get("JARVIS_BROWSER_WS")
        try:
            if endpoint:  # isolated browser container
                self._browser = await self._playwright.chromium.connect(endpoint, timeout=15000)
            else:
                options = {"headless": True, **self.launch_options}
                if os.environ.get("JARVIS_BROWSER_CHANNEL"):
                    options.setdefault("channel", os.environ["JARVIS_BROWSER_CHANNEL"])
                self._browser = await self._playwright.chromium.launch(**options)
        except Exception as exc:
            await self._playwright.stop()
            raise BrowserUnavailable(f"navegador indisponível: {type(exc).__name__}") from None
        kwargs: Dict[str, Any] = {"accept_downloads": False}
        state = _load_state(self.owner) if self.persist else None
        if state:
            kwargs["storage_state"] = state
        self._context = await self._browser.new_context(**kwargs)
        self._context.set_default_timeout(int(config.get("browser.timeout_seconds", 30)) * 1000)
        await self._context.route("**/*", self._guard)
        self.page = await self._context.new_page()
        return self

    async def _guard(self, route: Any) -> None:
        url = route.request.url
        ok, reason = url_allowed(url, self.resolver)
        if ok:
            await route.continue_()
        else:
            self.blocked.append(urlparse(url).netloc or url[:60])
            audit.record("browser.blocked", owner=self.owner, risk="read", outcome="denied", target=url[:200], reason=reason)
            await route.abort("blockedbyclient")

    async def __aexit__(self, *_exc: Any) -> None:
        try:
            if self.persist and self._context:
                _save_state(self.owner, await self._context.storage_state())
        finally:
            for closer in (self._context and self._context.close, self._browser and self._browser.close, self._playwright and self._playwright.stop):
                if closer:
                    try:
                        await closer()
                    except Exception as exc:
                        logger.debug("browser close: %s", exc)

    async def visit(self, url: str) -> Dict[str, Any]:
        ok, reason = url_allowed(url, self.resolver)
        if not ok:
            raise BlockedUrl(f"URL bloqueada: {reason}")
        response = await self.page.goto(url, wait_until="domcontentloaded")
        html = await self.page.content()
        if _CAPTCHA.search(html):
            audit.record("browser.human_required", owner=self.owner, target=url[:200])
            raise HumanRequired("a página exige verificação humana; não vou tentar contornar")
        limit = int(config.get("browser.max_text_chars", 20000))
        text = (await self.page.inner_text("body"))[:limit] if await self.page.query_selector("body") else ""
        links = await self.page.eval_on_selector_all("a[href]", "els => els.slice(0, 50).map(e => ({text: e.innerText.trim().slice(0, 120), href: e.href}))")
        audit.record("browser.visit", owner=self.owner, target=url[:200], status=response.status if response else 0)
        return {"url": self.page.url, "status": response.status if response else 0, "title": await self.page.title(), "text": text, "links": links, "blocked": list(self.blocked)}

    async def screenshot(self, full_page: bool = False) -> bytes:
        return await self.page.screenshot(full_page=full_page, type="png")

    async def fill(self, fields: Dict[str, str]) -> int:
        """Typing into a form is local to the page; nothing is submitted."""
        count = 0
        for selector, value in fields.items():
            await self.page.fill(selector, str(value))
            count += 1
        audit.record("browser.fill", owner=self.owner, risk="reversible", fields=list(fields))
        return count

    async def submit(self, selector: str, *, approved: bool = False) -> Dict[str, Any]:
        """Submitting has external effects. Refused until a human approved this exact action."""
        label = ""
        try:
            label = (await self.page.inner_text(selector))[:80]
        except Exception as exc:
            logger.debug("label unavailable: %s", exc)
        risk = policy.FINANCIAL if _IRREVERSIBLE.search(f"{label} {selector}") else policy.EXTERNAL
        decision = policy.evaluate("browser.submit", risk, self.owner)
        if decision.mode == policy.DENY or (decision.mode == policy.CONFIRM and not approved):
            raise policy.ApprovalRequired(policy.request_approval("browser.submit", risk, self.owner, f"Enviar formulário em {self.page.url[:120]} ({label or selector})", {"url": self.page.url, "selector": selector}))
        await self.page.click(selector)
        audit.record("browser.submit", owner=self.owner, risk=risk, target=self.page.url[:200])
        return {"url": self.page.url}


def as_untrusted(page: Dict[str, Any]) -> Dict[str, Any]:
    """Page text for a model: always wrapped as untrusted data."""
    return untrusted_context_message(f"web page {page.get('url', '')[:100]}", f"{page.get('title', '')}\n\n{page.get('text', '')}")


async def report(owner: str, urls: List[str], *, session_factory: Callable[..., Session] = Session) -> Dict[str, Any]:
    """Visit pages and produce a markdown report, recording what could not be read."""
    lines, errors = ["# Relatório de navegação", ""], []
    async with session_factory(owner) as session:
        for url in urls[:10]:
            try:
                page = await session.visit(url)
                lines += [f"## {page['title'] or url}", f"Fonte: {page['url']}", "", page["text"][:1500], ""]
            except (BlockedUrl, HumanRequired) as exc:
                errors.append({"url": url, "error": str(exc)})
            except Exception as exc:
                errors.append({"url": url, "error": type(exc).__name__})
    return {"markdown": "\n".join(lines), "errors": errors}
