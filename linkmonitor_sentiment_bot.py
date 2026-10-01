from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv
from playwright.sync_api import Error, Page, TimeoutError, sync_playwright


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
ERROR_DIR = ROOT / "screenshots_errors"

SENTIMENT_TO_COLOR = {
    "positivo": "green",
    "neutro": "yellow",
    "negativo": "red",
}

COLOR_TO_INDEX = {
    "green": 0,
    "yellow": 1,
    "red": 2,
}

RESULT_SCROLL_STEP_PX = 280
STABLE_END_ATTEMPTS = 5


@dataclass
class Config:
    email: str
    password: str
    url: str
    topic_name: str
    headless: bool
    force_update: bool
    dry_run: bool
    max_links: int | None
    confidence_threshold: float
    slow_mo_ms: int
    navigation_timeout_ms: int
    queue_file: Path
    decisions_file: Path


@dataclass
class LinkItem:
    index: int
    title: str
    url: str
    snippet: str
    row_text: str
    already_checked: bool
    visible_order: float = 0.0


@dataclass
class Decision:
    url: str
    title: str
    sentiment: str
    confidence: float
    reason: str


@dataclass
class RunRecord:
    date: str
    url: str
    title: str
    sentiment: str
    confidence: float
    reason: str
    update_status: str
    error: str


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Browser automation LinkMonitor. Non classifica il sentiment: "
            "estrae una coda per Codex oppure applica decisioni già prodotte da Codex."
        )
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--extract-for-codex", action="store_true", help="Estrae i link in JSONL per Codex nel CMD.")
    action.add_argument("--apply-decisions", action="store_true", help="Applica su LinkMonitor le decisioni JSONL/CSV di Codex.")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--test", action="store_true", help="Processa al massimo 5 link, salvo --max-links.")
    mode.add_argument("--production", action="store_true", help="Processa tutti i link disponibili.")

    parser.add_argument("--max-links", type=int, default=None, help="Numero massimo di link da estrarre/applicare.")
    parser.add_argument("--headless", action="store_true", help="Esegue il browser in headless.")
    parser.add_argument("--headed", action="store_true", help="Mostra il browser per debug.")
    parser.add_argument("--force-update", action="store_true", help="Include/aggiorna anche i link già taggati controllato.")
    parser.add_argument("--dry-run", action="store_true", help="In apply mode logga senza cliccare su LinkMonitor.")
    parser.add_argument("--topic", default=None, help="Argomento cliente da selezionare nel menu a sinistra.")
    parser.add_argument("--url", default=None, help="URL Analytics alternativo.")
    parser.add_argument("--queue-file", default=None, help="File JSONL creato per Codex.")
    parser.add_argument("--decisions-file", default=None, help="File JSONL/CSV con decisioni prodotte da Codex.")
    return parser.parse_args()


def load_config(args: argparse.Namespace) -> Config:
    load_dotenv(ROOT / ".env")
    email = os.getenv("LINKMONITOR_EMAIL", "").strip()
    password = os.getenv("LINKMONITOR_PASSWORD", "")
    url = args.url or os.getenv("LINKMONITOR_URL", "").strip()
    topic_name = args.topic or os.getenv("TOPIC_NAME", "Società di Gambling").strip()

    if not email:
        raise SystemExit("LINKMONITOR_EMAIL mancante. Impostalo in .env o nelle variabili d'ambiente.")
    if not password:
        raise SystemExit("LINKMONITOR_PASSWORD mancante. Impostalo in .env o nelle variabili d'ambiente.")
    if not url:
        raise SystemExit("LINKMONITOR_URL mancante. Impostalo in .env o nelle variabili d'ambiente.")

    max_links_env = os.getenv("MAX_LINKS", "5").strip()
    max_links = None if max_links_env.lower() in {"", "none", "all", "0"} else int(max_links_env)
    if args.max_links is not None:
        max_links = args.max_links
    if args.production:
        max_links = None
    if args.test:
        max_links = args.max_links if args.max_links is not None else 5

    headless = env_bool("HEADLESS", False)
    if args.headless:
        headless = True
    if args.headed:
        headless = False

    queue_file = Path(args.queue_file or os.getenv("CODEX_QUEUE_FILE", LOG_DIR / "codex_review_queue.jsonl"))
    decisions_file = Path(args.decisions_file or os.getenv("CODEX_DECISIONS_FILE", LOG_DIR / "codex_decisions.jsonl"))

    return Config(
        email=email,
        password=password,
        url=url,
        topic_name=topic_name,
        headless=headless,
        force_update=args.force_update or env_bool("FORCE_UPDATE", False),
        dry_run=args.dry_run or env_bool("DRY_RUN", False),
        max_links=max_links,
        confidence_threshold=float(os.getenv("CONFIDENCE_THRESHOLD", "0.75")),
        slow_mo_ms=env_int("SLOW_MO_MS", 0),
        navigation_timeout_ms=env_int("NAVIGATION_TIMEOUT_MS", 45000),
        queue_file=queue_file if queue_file.is_absolute() else ROOT / queue_file,
        decisions_file=decisions_file if decisions_file.is_absolute() else ROOT / decisions_file,
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def ensure_dirs() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    ERROR_DIR.mkdir(exist_ok=True)


def first_visible(locator_candidates: Iterable[Any], timeout_ms: int = 800) -> Any | None:
    for locator in locator_candidates:
        try:
            if locator.count() and locator.first.is_visible(timeout=timeout_ms):
                return locator.first
        except Exception:
            continue
    return None


def click_if_visible(page: Page, locators: Iterable[Any], timeout_ms: int = 1500) -> bool:
    locator = first_visible(locators, timeout_ms=timeout_ms)
    if not locator:
        return False
    try:
        locator.click(timeout=timeout_ms)
        return True
    except Exception:
        try:
            locator.click(timeout=timeout_ms, force=True)
            return True
        except Exception:
            return False


def body_looks_like_login(page: Page) -> bool:
    try:
        body_text = page.locator("body").inner_text(timeout=2000)
    except Exception:
        return False
    return bool(re.search(r"welcome back|please login|enter user email|\bemail\b\s+next|login", body_text, re.I))


def has_visible_input_js(page: Page) -> bool:
    return bool(
        page.evaluate(
            """
            () => {
              const isVisible = el => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
              };
              return [...document.querySelectorAll('input')].some(input => isVisible(input) && !input.disabled && !input.readOnly);
            }
            """
        )
    )


def fill_visible_input_js(page: Page, value: str, kind: str) -> bool:
    return bool(
        page.evaluate(
            """
            ({ value, kind }) => {
              const isVisible = el => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
              };
              const inputs = [...document.querySelectorAll('input')]
                .filter(isVisible)
                .filter(input => !input.disabled && !input.readOnly);
              const scored = inputs.map(input => {
                const attrs = [
                  input.type || '',
                  input.name || '',
                  input.id || '',
                  input.placeholder || '',
                  input.autocomplete || '',
                  input.getAttribute('aria-label') || ''
                ].join(' ').toLowerCase();
                let score = 0;
                if (kind === 'email') {
                  if (input.type === 'email') score += 5;
                  if (/email|e-mail|user|utente|username/.test(attrs)) score += 4;
                  if (input.type === 'text' || !input.type) score += 1;
                  if (input.type === 'password') score -= 10;
                } else {
                  if (input.type === 'password') score += 8;
                  if (/password|pass/.test(attrs)) score += 4;
                }
                return { input, score };
              }).filter(x => x.score > 0).sort((a, b) => b.score - a.score);
              const target = scored[0]?.input;
              if (!target) return false;
              target.focus();
              target.value = value;
              target.dispatchEvent(new Event('input', { bubbles: true }));
              target.dispatchEvent(new Event('change', { bubbles: true }));
              return true;
            }
            """,
            {"value": value, "kind": kind},
        )
    )


def click_login_button_js(page: Page, phase: str) -> bool:
    return bool(
        page.evaluate(
            """
            ({ phase }) => {
              const isVisible = el => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
              };
              const re = phase === 'email'
                ? /next|avanti|continua|prosegui/i
                : /login|log in|sign in|accedi|entra|next|avanti|continua/i;
              const buttons = [...document.querySelectorAll('button, input[type="submit"], [role="button"], a')]
                .filter(isVisible)
                .filter(el => {
                  const text = (el.innerText || el.value || el.textContent || '').trim();
                  const attrs = `${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''}`;
                  return re.test(`${text} ${attrs}`);
                });
              const target = buttons[0];
              if (!target) return false;
              target.click();
              return true;
            }
            """,
            {"phase": phase},
        )
    )


def submit_login_form_js(page: Page) -> bool:
    return bool(
        page.evaluate(
            """
            () => {
              const isVisible = el => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
              };
              const password = [...document.querySelectorAll('input[type="password"]')].find(isVisible);
              if (!password) return false;
              const form = password.closest('form');
              if (form) {
                form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
                if (typeof form.requestSubmit === 'function') {
                  form.requestSubmit();
                  return true;
                }
              }
              const buttons = [...document.querySelectorAll('button, input[type="submit"], [role="button"]')]
                .filter(isVisible)
                .filter(el => /login|log in|sign in|accedi|entra/i.test((el.innerText || el.value || el.textContent || '').trim()));
              const target = buttons[buttons.length - 1];
              if (!target) return false;
              target.focus();
              target.click();
              return true;
            }
            """
        )
    )


def login_error_visible(page: Page) -> bool:
    try:
        return bool(
            page.evaluate(
                """
                () => {
                  const isVisible = el => {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
                  };
                  const nodes = [...document.querySelectorAll('[role="alert"], .error, .invalid-feedback, .alert, mat-error, small, p, div')]
                    .filter(isVisible)
                    .map(el => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim())
                    .filter(Boolean);
                  return nodes.some(text =>
                    /invalid credentials|incorrect password|wrong password|login failed|authentication failed|credenziali non valide|password errata|accesso non riuscito|utente o password/i.test(text)
                  );
                }
                """
            )
        )
    except Error:
        return False


def wait_for_login_or_app(page: Page, config: Config) -> None:
    deadline = time.time() + min(15, config.navigation_timeout_ms / 1000)
    while time.time() < deadline:
        try:
            body_text = page.locator("body").inner_text(timeout=1000)
        except Exception:
            body_text = ""
        try:
            input_count = page.locator("input").count()
        except Exception:
            input_count = 0
        if input_count > 0:
            return
        if re.search(r"welcome back|please login|enter user email|\bemail\b|next|Risultati|Panoramica|ARGOMENTI", body_text, re.I):
            return
        page.wait_for_timeout(500)


def login_if_needed(page: Page, config: Config) -> None:
    page.wait_for_load_state("domcontentloaded", timeout=config.navigation_timeout_ms)
    wait_for_login_or_app(page, config)

    print("Login: controllo schermata.")
    email_field = first_visible(
        [
            page.locator("input[type='email']"),
            page.get_by_label(re.compile("email|e-mail|utente|username|user", re.I)),
            page.get_by_placeholder(re.compile("email|e-mail|utente|username|user", re.I)),
            page.locator("input[type='text']"),
        ],
        timeout_ms=2000,
    )
    password_field = first_visible(
        [
            page.locator("input[type='password']"),
            page.get_by_label(re.compile("password|pass", re.I)),
            page.get_by_placeholder(re.compile("password|pass", re.I)),
        ],
        timeout_ms=2000,
    )

    if not email_field and not password_field:
        if not body_looks_like_login(page) and not has_visible_input_js(page):
            print("Login: nessuna schermata login rilevata.")
            return
        if not fill_visible_input_js(page, config.email, "email"):
            save_login_debug(page, "email_not_found")
            raise RuntimeError("Login non completato: schermata login rilevata ma campo email non trovato.")
        print("Login: email compilata con fallback DOM.")
        clicked_next = click_login_button_js(page, "email")
        if not clicked_next:
            page.keyboard.press("Enter")
        print("Login: click su Next eseguito.")
        page.wait_for_timeout(1200)
        password_field = first_visible(
            [
                page.locator("input[type='password']"),
                page.get_by_label(re.compile("password|pass", re.I)),
                page.get_by_placeholder(re.compile("password|pass", re.I)),
            ],
            timeout_ms=8000,
        )

    if email_field:
        email_field.fill(config.email)
        print("Login: email compilata.")
        if not password_field:
            clicked_next = click_if_visible(
                page,
                [
                    page.get_by_role("button", name=re.compile(r"next|avanti|continua|prosegui", re.I)),
                    page.locator("button[type='submit']"),
                    page.locator("input[type='submit']"),
                ],
                timeout_ms=3000,
            )
            if not clicked_next:
                clicked_next = click_login_button_js(page, "email")
            if not clicked_next:
                page.keyboard.press("Enter")
            print("Login: click su Next eseguito.")
            page.wait_for_timeout(1200)
            password_field = first_visible(
                [
                    page.locator("input[type='password']"),
                    page.get_by_label(re.compile("password|pass", re.I)),
                    page.get_by_placeholder(re.compile("password|pass", re.I)),
                ],
                timeout_ms=8000,
            )

    if not password_field:
        if not fill_visible_input_js(page, config.password, "password"):
            save_login_debug(page, "password_not_found")
            raise RuntimeError("Login non completato: dopo email/Next non trovo il campo password.")
        print("Login: password compilata con fallback DOM.")
        clicked = click_login_button_js(page, "password")
        if not clicked:
            clicked = submit_login_form_js(page)
        if not clicked:
            page.keyboard.press("Enter")
        print("Login: invio credenziali eseguito.")
        wait_until_logged_in(page, config)
        return

    try:
        password_field.fill(config.password)
        print("Login: password compilata.")
    except Exception:
        if not fill_visible_input_js(page, config.password, "password"):
            save_login_debug(page, "password_fill_failed")
            raise
        print("Login: password compilata con fallback DOM.")

    clicked = click_if_visible(
        page,
        [
            page.get_by_role("button", name=re.compile("accedi|login|entra|sign in|next|avanti|continua", re.I)),
            page.locator("button[type='submit']"),
            page.locator("input[type='submit']"),
        ],
        timeout_ms=3000,
    )
    if not clicked:
        clicked = click_login_button_js(page, "password")
    if not clicked:
        clicked = submit_login_form_js(page)
    if not clicked:
        page.keyboard.press("Enter")
    print("Login: invio credenziali eseguito.")

    wait_until_logged_in(page, config)


def wait_until_logged_in(page: Page, config: Config) -> None:
    deadline = time.time() + (config.navigation_timeout_ms / 1000)
    while time.time() < deadline:
        try:
            page.wait_for_load_state("networkidle", timeout=1500)
        except Exception:
            pass
        try:
            password_visible = page.locator("input[type='password']").first.is_visible(timeout=500)
        except Exception:
            password_visible = False
        try:
            body_text = page.locator("body").inner_text(timeout=1000)
        except Exception:
            body_text = ""
        if not password_visible and re.search(r"Risultati|Panoramica|ARGOMENTI|ANALYTICS", body_text, re.I):
            return
        if password_visible and login_error_visible(page):
            save_login_debug(page, "login_error")
            raise RuntimeError("Login non completato: LinkMonitor mostra un errore credenziali.")
        page.wait_for_timeout(800)
    save_login_debug(page, "login_timeout")
    raise RuntimeError("Login non completato: timeout dopo invio credenziali.")


def save_login_debug(page: Page, reason: str) -> None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_path = LOG_DIR / f"debug_login_{reason}_{stamp}.png"
    text_path = LOG_DIR / f"debug_login_{reason}_{stamp}.txt"
    page.screenshot(path=str(screenshot_path), full_page=True)
    body_text = page.locator("body").inner_text(timeout=5000)
    text_path.write_text(body_text[:20000], encoding="utf-8")
    print(f"Debug login salvato: {screenshot_path} e {text_path}")


def wait_for_dashboard(page: Page, config: Config) -> None:
    dashboard_pattern = re.compile(r"\b(Risultati|Panoramica|ARGOMENTI|ANALYTICS)\b", re.I)
    for attempt in range(2):
        deadline = time.time() + (config.navigation_timeout_ms / 1000)
        while time.time() < deadline:
            try:
                body_text = page.locator("body").inner_text(timeout=1500)
            except Exception:
                body_text = ""
            if dashboard_pattern.search(body_text):
                return
            page.wait_for_timeout(700)
        if attempt == 0:
            print("Dashboard: contenuto non ancora pronto, ricarico la pagina Analytics.")
            page.reload(wait_until="domcontentloaded", timeout=config.navigation_timeout_ms)
    save_page_debug(page, "dashboard_timeout")
    raise RuntimeError("Dashboard LinkMonitor non caricata dopo il login.")


def click_text_js(page: Page, pattern: str, debug_name: str) -> bool:
    return bool(
        page.evaluate(
            """
            ({ pattern }) => {
              const re = new RegExp(pattern, 'i');
              const isVisible = el => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
              };
              const candidates = [...document.querySelectorAll('button, a, [role="button"], li, div, span')]
                .filter(isVisible)
                .map(el => {
                  const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                  const r = el.getBoundingClientRect();
                  return { el, text, area: r.width * r.height };
                })
                .filter(x => x.text && re.test(x.text))
                .sort((a, b) => a.area - b.area);
              const target = candidates[0]?.el;
              if (!target) return false;
              let clickable = target;
              for (let i = 0; i < 5 && clickable; i += 1) {
                const tag = clickable.tagName.toLowerCase();
                const role = clickable.getAttribute('role') || '';
                const cls = String(clickable.className || '');
                if (/^(button|a|li)$/.test(tag) || role === 'button' || /item|sidebar|topic|anchor|node/i.test(cls)) {
                  break;
                }
                clickable = clickable.parentElement;
              }
              clickable = clickable || target;
              clickable.scrollIntoView({ block: 'center', inline: 'center' });
              clickable.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, cancelable: true, view: window }));
              clickable.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
              clickable.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
              clickable.click();
              return true;
            }
            """,
            {"pattern": pattern},
        )
    )


def click_topic_by_coordinates(page: Page, topic_name: str) -> bool:
    box = page.evaluate(
        """
        ({ topicName }) => {
          const norm = value => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
          const wanted = norm(topicName);
          const isVisible = el => {
            const r = el.getBoundingClientRect();
            const s = window.getComputedStyle(el);
            return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
          };
          const matches = [...document.querySelectorAll('span, div, li, a, button')]
            .filter(isVisible)
            .filter(el => norm(el.innerText || el.textContent).includes(wanted));
          let target = matches
            .map(el => {
              let clickable = el;
              for (let i = 0; i < 6 && clickable; i += 1) {
                const tag = clickable.tagName.toLowerCase();
                const cls = String(clickable.className || '');
                const role = clickable.getAttribute('role') || '';
                if (/^(li|button|a)$/.test(tag) || role === 'button' || /sidebar-item|item|topic|anchor|node/i.test(cls)) {
                  break;
                }
                clickable = clickable.parentElement;
              }
              clickable = clickable || el;
              const r = clickable.getBoundingClientRect();
              return { el: clickable, left: r.left, top: r.top, width: r.width, height: r.height, area: r.width * r.height };
            })
            .filter(x => x.width > 20 && x.height > 10)
            .sort((a, b) => a.area - b.area)[0];
          if (!target) return null;
          target.el.scrollIntoView({ block: 'center', inline: 'center' });
          const r = target.el.getBoundingClientRect();
          return { left: r.left, top: r.top, width: r.width, height: r.height };
        }
        """,
        {"topicName": topic_name},
    )
    if not box:
        return False
    y = float(box["top"]) + float(box["height"]) / 2
    x = float(box["left"]) + float(box["width"]) / 2
    page.mouse.move(x, y)
    page.wait_for_timeout(200)
    page.mouse.click(x, y)
    page.wait_for_timeout(800)
    return True


def click_topic_center_if_present(page: Page, topic_name: str) -> bool:
    box = page.evaluate(
        """
        ({ topicName }) => {
          const norm = value => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
          const wanted = norm(topicName);
          const isVisible = el => {
            const r = el.getBoundingClientRect();
            const s = window.getComputedStyle(el);
            return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
          };
          const candidates = [...document.querySelectorAll('span, div, li, a, button')]
            .filter(isVisible)
            .filter(el => norm(el.innerText || el.textContent) === wanted)
            .map(el => {
              let clickable = el;
              for (let i = 0; i < 6 && clickable; i += 1) {
                const tag = clickable.tagName.toLowerCase();
                const cls = String(clickable.className || '');
                const role = clickable.getAttribute('role') || '';
                if (/^(li|button|a)$/.test(tag) || role === 'button' || /sidebar-item|item|topic|anchor|node/i.test(cls)) {
                  break;
                }
                clickable = clickable.parentElement;
              }
              clickable = clickable || el;
              const r = clickable.getBoundingClientRect();
              return { left: r.left, top: r.top, width: r.width, height: r.height, area: r.width * r.height };
            })
            .filter(x => x.left >= 45 && x.left <= 320 && x.top >= 70)
            .filter(x => x.width > 80 && x.height > 10)
            .sort((a, b) => a.area - b.area);
          return candidates[0] || null;
        }
        """,
        {"topicName": topic_name},
    )
    if not box:
        return False
    x = float(box["left"]) + 12
    y = float(box["top"]) + float(box["height"]) / 2
    page.mouse.move(x, y)
    page.wait_for_timeout(200)
    page.mouse.click(x, y)
    page.wait_for_timeout(800)
    return True


def deselect_default_lete(page: Page) -> None:
    clicked = click_topic_center_if_present(page, "Lete")
    if clicked:
        print("Navigazione: Lete cliccato sulla riga per deselezionarlo.")
    else:
        print("Navigazione: Lete non trovato, continuo.")


def deselect_ippodromo_snai(page: Page) -> None:
    clicked = click_topic_center_if_present(page, "Ippodromo Snai")
    if clicked:
        print("Navigazione: Ippodromo Snai cliccato sulla riga per deselezionarlo.")
    else:
        print("Navigazione: Ippodromo Snai non trovato, continuo.")


def save_page_debug(page: Page, reason: str) -> None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_path = LOG_DIR / f"debug_{reason}_{stamp}.png"
    text_path = LOG_DIR / f"debug_{reason}_{stamp}.txt"
    page.screenshot(path=str(screenshot_path), full_page=True)
    body_text = page.locator("body").inner_text(timeout=5000)
    text_path.write_text(body_text[:20000], encoding="utf-8")
    print(f"Debug pagina salvato: {screenshot_path} e {text_path}")


def click_results_section(page: Page) -> None:
    clicked = click_if_visible(
        page,
        [
            page.get_by_role("link", name=re.compile(r"risultati", re.I)),
            page.get_by_role("button", name=re.compile(r"risultati", re.I)),
            page.get_by_text(re.compile(r"^\s*Risultati\s*$", re.I)),
        ],
        timeout_ms=4000,
    )
    if not clicked:
        clicked = click_text_js(page, r"^\\s*Risultati\\s*$", "risultati")
    if not clicked:
        save_page_debug(page, "results_not_found")
        raise RuntimeError("Non trovo/clicco la sezione Risultati.")
    print("Navigazione: sezione Risultati cliccata.")
    page.wait_for_timeout(1500)


def reveal_topics_sidebar(page: Page) -> None:
    # LinkMonitor espande il pannello argomenti quando il mouse si sposta verso destra sulla sidebar.
    for y in (320, 255, 380):
        for x in (45, 80, 120, 170, 220, 280, 335):
            page.mouse.move(x, y)
            page.wait_for_timeout(250)
        try:
            body_text = page.locator("body").inner_text(timeout=1000)
        except Exception:
            body_text = ""
        if re.search(r"ARGOMENTI|Società di Gambling|Cerca argomenti", body_text, re.I):
            print("Navigazione: pannello argomenti visibile.")
            return
    save_page_debug(page, "topics_sidebar_not_visible")
    raise RuntimeError("Pannello argomenti non visibile dopo hover verso destra sulla sidebar.")


def click_topic(page: Page, topic_name: str) -> None:
    escaped = re.escape(topic_name)
    clicked = click_topic_by_coordinates(page, topic_name)
    if not clicked:
        clicked = click_text_js(page, escaped, "topic")
    if not clicked:
        clicked = click_if_visible(
            page,
            [
                page.get_by_text(re.compile(escaped, re.I)),
                page.locator(f"text={topic_name}"),
            ],
            timeout_ms=4000,
        )
    if clicked:
        print(f"Navigazione: argomento '{topic_name}' cliccato.")
        page.wait_for_timeout(1500)
    else:
        save_page_debug(page, "topic_not_found")
        raise RuntimeError(f"Argomento '{topic_name}' non trovato/cliccabile.")


def prepare_analytics(page: Page, config: Config) -> None:
    page.goto(config.url, wait_until="domcontentloaded", timeout=config.navigation_timeout_ms)
    login_if_needed(page, config)
    if page.url != config.url:
        page.goto(config.url, wait_until="domcontentloaded", timeout=config.navigation_timeout_ms)

    wait_for_dashboard(page, config)
    click_results_section(page)
    reveal_topics_sidebar(page)
    deselect_default_lete(page)
    click_topic(page, config.topic_name)
    if "gambling" in config.topic_name.lower():
        deselect_ippodromo_snai(page)
    page.wait_for_load_state("networkidle", timeout=config.navigation_timeout_ms)
    page.wait_for_timeout(2500)


def row_handles(page: Page) -> list[Any]:
    return page.evaluate_handle(
        """
        () => {
          const isVisible = el => {
            const r = el.getBoundingClientRect();
            const s = window.getComputedStyle(el);
            return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
          };
          const viewportWidth = window.innerWidth || 1200;
          const candidates = [];
          const seeds = [...document.querySelectorAll('*')]
            .filter(isVisible)
            .filter(el => {
              const text = (el.innerText || '').trim();
              return /CORRISP\\.|METRICHE/i.test(text);
            });

          for (const seed of seeds) {
            const matches = [];
            let el = seed;
            for (let i = 0; i < 10 && el; i += 1, el = el.parentElement) {
              const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
              const r = el.getBoundingClientRect();
              const hasResultText = /CORRISP\\.|METRICHE/i.test(text)
                && /pubblicato|instagram|twitter|\\bx\\b|facebook|linkedin|tiktok|youtube|\\.com|\\.it|http/i.test(text);
              const publishedCount = (text.match(/\\bpubblicato\\b/gi) || []).length;
              const corrCount = (text.match(/CORRISP\\./gi) || []).length;
              const metricsCount = (text.match(/METRICHE\\b/gi) || []).length;
              const singleResult = Math.max(publishedCount, corrCount, metricsCount) <= 1;
              const rowShape = r.width > viewportWidth * 0.45 && r.height >= 70 && r.height <= 650;
              const notWholePage = text.length >= 80 && text.length <= 4500;
              if (hasResultText && singleResult && rowShape && notWholePage) {
                matches.push(el);
              }
            }
            const best = matches
              .sort((a, b) => {
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                return (ar.width * ar.height) - (br.width * br.height);
              })[0];
            if (best) candidates.push(best);
          }

          const unique = [];
          const seen = new Set();
          for (const el of candidates) {
            if (seen.has(el)) continue;
            seen.add(el);
            unique.push(el);
          }

          unique.sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            return (ar.top - br.top) || ((ar.width * ar.height) - (br.width * br.height));
          });

          const rows = [];
          for (const el of unique) {
            const r = el.getBoundingClientRect();
            const duplicate = rows.some(existing => {
              const er = existing.getBoundingClientRect();
              return Math.abs(er.top - r.top) < 8 && Math.abs(er.height - r.height) < 40;
            });
            if (!duplicate) rows.push(el);
          }
          return rows;
        }
        """
    ).get_properties()


def extract_item_from_row(row: Any, index: int) -> LinkItem:
    data = row.evaluate(
        """
        row => {
          const rawText = (row.innerText || '').replace(/\\r/g, '').trim();
          const text = rawText.replace(/\\s+/g, ' ').trim();
          const beforeMetaText = text
            .split(/\\s+pubblicato\\b|\\s+CORRISP\\.|\\s+METRICHE\\b|\\s+TAG\\b/i)[0]
            .trim();
          const lines = rawText
            .split('\\n')
            .map(line => line.replace(/\\s+/g, ' ').trim())
            .filter(Boolean);
          const metaIndex = lines.findIndex(line =>
            /pubblicato|CORRISP\\.|METRICHE|^TAG$|InstagramItalia|FacebookItalia|YouTubeItalia|twitter\\.com/i.test(line)
          );
          const contentLines = (metaIndex >= 0 ? lines.slice(0, metaIndex) : lines)
            .filter(line => !/^(TAG|CORRISP\\.|METRICHE)$/i.test(line));
          let contentText = contentLines.join('\\n').trim();
          if (beforeMetaText.length > contentText.length) {
            contentText = beforeMetaText;
          }
          contentText = contentText || text;
          const isExternal = href => href && !href.includes('app.linkmonitor.it');
          const publishedLink = row.querySelector('a.published.clickable[href], a.published[href]');
          const titleLinks = [...row.querySelectorAll('a.clickable[href], a[href]')]
            .filter(a => !a.classList.contains('published'))
            .map(a => a.href)
            .filter(isExternal);
          const links = [...row.querySelectorAll('a[href]')]
            .map(a => a.href)
            .filter(isExternal);
          const external =
            (publishedLink && isExternal(publishedLink.href) ? publishedLink.href : '') ||
            titleLinks[0] ||
            links[0] ||
            '';
          const title = contentLines[0] || text.slice(0, 180);
          const checked = /\\bcontrollato\\b/i.test(text);
          const rect = row.getBoundingClientRect();
          const scrollParent = (() => {
            let el = row.parentElement;
            while (el) {
              const s = window.getComputedStyle(el);
              if (
                el.scrollHeight > el.clientHeight + 50 &&
                /(auto|scroll)/i.test(`${s.overflowY} ${s.overflow}`)
              ) {
                return el;
              }
              el = el.parentElement;
            }
            return null;
          })();
          const parentRect = scrollParent ? scrollParent.getBoundingClientRect() : null;
          const top = scrollParent
            ? scrollParent.scrollTop + rect.top - parentRect.top
            : window.scrollY + rect.top;
          return { text, rawText, contentText, external, title, checked, top };
        }
        """
    )
    return LinkItem(
        index=index,
        title=data["title"],
        url=data["external"],
        snippet=data["contentText"][:1800],
        row_text=data["text"],
        already_checked=bool(data["checked"]),
        visible_order=float(data["top"]),
    )


def item_key(item: LinkItem) -> str:
    source = item.url or item.row_text[:320]
    return re.sub(r"\s+", " ", source.strip().lower())[:360]


def visible_items(page: Page) -> list[tuple[Any, LinkItem]]:
    items: list[tuple[Any, LinkItem]] = []
    props = row_handles(page)
    rows = list(props.values())
    rows.sort(
        key=lambda handle: handle.evaluate(
            "el => { const r = el.getBoundingClientRect(); return [r.top, r.left]; }"
        )
    )
    for row in rows:
        try:
            item = extract_item_from_row(row, len(items) + 1)
            items.append((row, item))
        except Error:
            continue
    return items


def collect_visible_items(page: Page, seen_keys: set[str], max_new: int | None) -> list[tuple[Any, LinkItem]]:
    collected: list[tuple[Any, LinkItem]] = []
    for row, item in visible_items(page):
        try:
            key = item_key(item)
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            collected.append((row, item))
            if max_new is not None and len(collected) >= max_new:
                break
        except Error:
            continue
    return collected


def reset_results_scroll(page: Page) -> None:
    page.evaluate(
        """
        () => {
          const visible = el => {
            const r = el.getBoundingClientRect();
            const s = window.getComputedStyle(el);
            return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
          };
          const target = [...document.querySelectorAll('*')]
            .filter(visible)
            .filter(el => {
              const s = window.getComputedStyle(el);
              return el.scrollHeight > el.clientHeight + 50
                && el.getBoundingClientRect().width > 500
                && el.getBoundingClientRect().height > 250
                && /(auto|scroll)/i.test(`${s.overflowY} ${s.overflow}`);
            })
            .map(el => {
              const r = el.getBoundingClientRect();
              const text = (el.innerText || '').slice(0, 6000);
              const resultScore = /CORRISP\\.|METRICHE/i.test(text) ? 1000000000 : 0;
              return { el, score: resultScore + (r.width * r.height) };
            })
            .sort((a, b) => b.score - a.score)[0]?.el;
          if (target) {
            target.scrollTop = 0;
          } else {
            window.scrollTo(0, 0);
          }
        }
        """
    )


def results_scroll_state(page: Page) -> dict[str, float | bool]:
    return dict(
        page.evaluate(
            """
            () => {
              const visible = el => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
              };
              const target = [...document.querySelectorAll('*')]
                .filter(visible)
                .filter(el => {
                  const s = window.getComputedStyle(el);
                  return el.scrollHeight > el.clientHeight + 50
                    && el.getBoundingClientRect().width > 500
                    && el.getBoundingClientRect().height > 250
                    && /(auto|scroll)/i.test(`${s.overflowY} ${s.overflow}`);
                })
                .map(el => {
                  const r = el.getBoundingClientRect();
                  const text = (el.innerText || '').slice(0, 6000);
                  const resultScore = /CORRISP\\.|METRICHE/i.test(text) ? 1000000000 : 0;
                  return { el, score: resultScore + (r.width * r.height) };
                })
                .sort((a, b) => b.score - a.score)[0]?.el;
              if (target) {
                return {
                  top: target.scrollTop,
                  height: target.scrollHeight,
                  client: target.clientHeight,
                  atBottom: target.scrollTop + target.clientHeight >= target.scrollHeight - 4,
                };
              }
              return {
                top: window.scrollY,
                height: document.documentElement.scrollHeight || document.body.scrollHeight,
                client: window.innerHeight,
                atBottom: window.scrollY + window.innerHeight >= (document.documentElement.scrollHeight || document.body.scrollHeight) - 4,
              };
            }
            """
        )
    )


def scroll_results(page: Page, pixels: int = RESULT_SCROLL_STEP_PX) -> bool:
    return bool(
        page.evaluate(
            """
            ({ pixels }) => {
              const beforeWindow = window.scrollY;
              const scrollables = [...document.querySelectorAll('*')]
                .filter(el => {
                  const r = el.getBoundingClientRect();
                  const s = window.getComputedStyle(el);
                  return r.width > 500
                    && r.height > 250
                    && el.scrollHeight > el.clientHeight + 50
                    && /(auto|scroll)/i.test(`${s.overflowY} ${s.overflow}`);
                })
                .map(el => {
                  const r = el.getBoundingClientRect();
                  const text = (el.innerText || '').slice(0, 6000);
                  const resultScore = /CORRISP\\.|METRICHE/i.test(text) ? 1000000000 : 0;
                  return { el, score: resultScore + (r.width * r.height) };
                })
                .sort((a, b) => b.score - a.score)
                .map(item => item.el);
              const target = scrollables[0];
              if (target) {
                const before = target.scrollTop;
                target.scrollTop += pixels;
                return target.scrollTop !== before;
              }
              window.scrollBy(0, pixels);
              return window.scrollY !== beforeWindow;
            }
            """,
            {"pixels": pixels},
        )
    )


def remember_item(items_by_key: dict[str, LinkItem], item: LinkItem) -> bool:
    key = item_key(item)
    if not key:
        return False
    existing = items_by_key.get(key)
    if existing is None:
        items_by_key[key] = item
        return True

    existing.visible_order = min(existing.visible_order, item.visible_order)
    if len(item.snippet) > len(existing.snippet):
        existing.snippet = item.snippet
        existing.row_text = item.row_text
    if item.title and len(item.title) > len(existing.title):
        existing.title = item.title
    if item.url and not existing.url:
        existing.url = item.url
    existing.already_checked = existing.already_checked and item.already_checked
    return False


def collect_extract_items(page: Page, config: Config) -> tuple[list[LinkItem], int]:
    reset_results_scroll(page)
    page.wait_for_timeout(1800)

    items_by_key: dict[str, LinkItem] = {}
    skipped_checked: set[str] = set()
    stable_end_attempts = 0
    last_report_count = 0
    target_candidates = None
    if config.max_links is not None:
        target_candidates = max(config.max_links * 2, config.max_links + 20)

    while True:
        batch = visible_items(page)
        new_count = 0

        for _, item in batch:
            key = item_key(item)
            if not key:
                continue
            if item.already_checked and not config.force_update:
                skipped_checked.add(key)
                continue
            if remember_item(items_by_key, item):
                new_count += 1

        if len(items_by_key) != last_report_count:
            print(f"Estrazione: raccolti {len(items_by_key)} link unici.")
            last_report_count = len(items_by_key)

        if target_candidates is not None and len(items_by_key) >= target_candidates:
            break

        before = results_scroll_state(page)
        moved = scroll_results(page)
        page.wait_for_timeout(1600 if moved else 2400)
        after = results_scroll_state(page)

        at_bottom = bool(after.get("atBottom"))
        scroll_changed = (
            float(before.get("top", 0)) != float(after.get("top", 0))
            or float(before.get("height", 0)) != float(after.get("height", 0))
        )

        if moved:
            print("Estrazione: scroll risultati, attendo caricamento altri link.")

        if new_count == 0 and (not moved or at_bottom) and not scroll_changed:
            stable_end_attempts += 1
        elif new_count == 0 and at_bottom:
            stable_end_attempts += 1
        else:
            stable_end_attempts = 0

        if stable_end_attempts >= STABLE_END_ATTEMPTS:
            break

    items = sorted(items_by_key.values(), key=lambda item: item.visible_order)
    if config.max_links is not None:
        items = items[: config.max_links]
    return items, len(skipped_checked)


def save_zero_extract_debug(page: Page) -> None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_path = LOG_DIR / f"debug_zero_extract_{stamp}.png"
    text_path = LOG_DIR / f"debug_zero_extract_{stamp}.txt"
    page.screenshot(path=str(screenshot_path), full_page=True)
    body_text = page.locator("body").inner_text(timeout=5000)
    text_path.write_text(body_text[:20000], encoding="utf-8")
    print(f"Nessun link estratto. Debug salvato: {screenshot_path} e {text_path}")


def write_codex_prompt(queue_file: Path, decisions_file: Path) -> Path:
    prompt_path = LOG_DIR / "codex_sentiment_prompt.md"
    prompt = f"""# Compito Codex: classificazione sentiment LinkMonitor

Leggi `{queue_file}` e crea `{decisions_file}` in formato JSONL.

Non modificare LinkMonitor. Devi solo decidere il sentiment per ogni riga.

Schema obbligatorio, una riga JSON per link:

```json
{{"url":"...","title":"...","sentiment":"positivo|neutro|negativo","confidence":0.0,"reason":"motivazione sintetica"}}
```

Criterio principale: valuta l'impatto reputazionale per il cliente, non solo il tono linguistico.

Clienti/brand da valutare:
- Sisal
- SisalTipster
- Betsson
- Eurobet
- Lottomatica
- Snai
- Planetwin365
- PokerStars
- 888Casino
- bet365
- Goldbet
- SisalFunClub
- Fantapizza
- My Combo

Negativo:
- cronaca giudiziaria, accuse, indagini, procedimenti, condanne, truffe, scandali;
- recensioni negative o contenuti diffamatori/lesivi;
- betting: non pagano, conto bloccato, cash out negato, bonus ingannevole, sanzioni, ludopatia, pubblicità aggressiva, pratiche scorrette.

Neutro:
- semplice citazione informativa;
- scheda, directory, pagina istituzionale;
- hashtag o post generico su scommesse;
- pronostici, risultati sportivi, Superenalotto, bookmaker senza criticità reputazionale.

Positivo:
- contenuto favorevole/promozionale/celebrativo;
- recensione positiva;
- betting: pagamenti veloci, prelievo rapido, quote competitive, bonus chiaro, app stabile, assistenza efficace, migliori siti/quote, iniziative sociali.

Regola prudenziale:
- se dopo la prima valutazione useresti `confidence` sotto `0.75`, devi visitare/aprire il link associato per un ulteriore controllo del contenuto;
- dopo aver controllato il link, assegna il sentiment solo se raggiungi `confidence >= 0.75`;
- se anche dopo il controllo del link resti sotto `0.75`, lascia `confidence` sotto soglia: lo script applicativo non aggiornerà LinkMonitor e lascerà il risultato invariato.
"""
    prompt_path.write_text(prompt, encoding="utf-8")
    return prompt_path


def extract_for_codex(page: Page, config: Config) -> int:
    items, skipped_checked = collect_extract_items(page, config)

    config.queue_file.parent.mkdir(parents=True, exist_ok=True)
    with config.queue_file.open("w", encoding="utf-8") as f:
        for output_index, item in enumerate(items, start=1):
            item.index = output_index
            f.write(json.dumps(asdict(item), ensure_ascii=False) + "\n")

    prompt_path = write_codex_prompt(config.queue_file, config.decisions_file)
    print(f"Queue per Codex scritta: {config.queue_file} ({len(items)} link)")
    if skipped_checked:
        print(f"Righe già controllate saltate: {skipped_checked}. Usa --force-update per includerle.")
    if not items:
        save_zero_extract_debug(page)
    print(f"Prompt per Codex scritto: {prompt_path}")
    return len(items)


def normalize_key(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"https?://(www\.)?", "", value)
    value = re.sub(r"\s+", " ", value)
    return value[:220]


def load_decisions(path: Path) -> dict[str, Decision]:
    if not path.exists():
        raise SystemExit(f"File decisioni non trovato: {path}")

    decisions: dict[str, Decision] = {}
    if path.suffix.lower() == ".csv":
        with path.open("r", newline="", encoding="utf-8-sig") as f:
            rows = csv.DictReader(f)
            raw_items = list(rows)
    else:
        raw_items = []
        with path.open("r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line:
                    raw_items.append(json.loads(line))

    for raw in raw_items:
        sentiment = str(raw.get("sentiment", "")).strip().lower()
        if sentiment not in SENTIMENT_TO_COLOR:
            raise ValueError(f"Sentiment non valido in decisioni Codex: {sentiment!r}")
        decision = Decision(
            url=str(raw.get("url", "")).strip(),
            title=str(raw.get("title", "")).strip(),
            sentiment=sentiment,
            confidence=float(raw.get("confidence", 0)),
            reason=str(raw.get("reason", "")).strip(),
        )
        if decision.url:
            decisions[normalize_key(decision.url)] = decision
        if decision.title:
            decisions[normalize_key(decision.title)] = decision
    return decisions


def find_decision(item: LinkItem, decisions: dict[str, Decision]) -> Decision | None:
    for key in [item.url, item.title, item.row_text[:180]]:
        if key and normalize_key(key) in decisions:
            return decisions[normalize_key(key)]
    return None


def open_row_menu(row: Any) -> None:
    menu = row.query_selector(
        "button:has-text('⋮'), [role='button']:has-text('⋮'), .fa-ellipsis-v, .fa-ellipsis, .mdi-dots-vertical, .more_vert"
    )
    if menu:
        menu.click()
        return
    row.evaluate(
        """
        row => {
          const visible = el => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
          };
          const candidates = [...row.querySelectorAll('button, [role="button"], i, span, div, a')]
            .filter(visible)
            .filter(el => {
              const text = (el.innerText || el.textContent || '').trim();
              const attrs = `${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''} ${el.className || ''}`;
              return text === '⋮' || /more|altro|menu|opzioni|ellipsis|dots|kebab|fa-ellipsis|more_vert|mdi-dots/i.test(attrs);
            });
          const target = candidates[candidates.length - 1];
          if (!target) throw new Error('menu verticale non trovato nella riga');
          target.click();
        }
        """
    )


def click_sentiment_option(page: Page, sentiment: str) -> None:
    color = SENTIMENT_TO_COLOR[sentiment]
    idx = COLOR_TO_INDEX[color]
    page.wait_for_selector("text=/CAMBIA SENTIMENT/i", timeout=5000)
    clicked = page.evaluate(
        """
        ({ idx }) => {
          const visible = el => {
            const r = el.getBoundingClientRect();
            const s = window.getComputedStyle(el);
            return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
          };
          const panels = [...document.querySelectorAll('*')]
            .filter(visible)
            .filter(el => /CAMBIA SENTIMENT/i.test(el.innerText || ''));
          const panel = panels
            .map(el => {
              const r = el.getBoundingClientRect();
              return { el, left: r.left, width: r.width, height: r.height, area: r.width * r.height };
            })
            .filter(x => x.left > window.innerWidth * 0.45 && x.width < 420 && x.height > 100)
            .sort((a, b) => b.area - a.area)[0]?.el;
          if (!panel) return false;
          const candidates = [...panel.querySelectorAll('.article-flag-panel-content .p-icon, .article-flag-panel-content i')]
            .filter(visible)
            .filter(el => {
              const attrs = `${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''} ${el.className || ''}`;
              return /icon-colorant|tw3-icon-smile|tw3-icon-neutral|tw3-icon-sad/i.test(attrs);
            })
            .map(el => {
              let clickable = el;
              for (let i = 0; i < 4 && clickable; i += 1) {
                const cls = String(clickable.className || '');
                const r = clickable.getBoundingClientRect();
                if (/p-combined-icon|p-icon|icon-colorant/i.test(cls) && r.width >= 20 && r.height >= 20) break;
                clickable = clickable.parentElement;
              }
              clickable = clickable || el;
              const r = clickable.getBoundingClientRect();
              return { el: clickable, left: r.left, top: r.top, width: r.width, height: r.height };
            });
          const unique = [];
          for (const candidate of candidates.sort((a, b) => a.left - b.left)) {
            if (!unique.some(item => Math.abs(item.left - candidate.left) < 4)) {
              unique.push(candidate);
            }
          }
          const target = unique[idx];
          if (!target) return false;
          target.el.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, cancelable: true, view: window }));
          target.el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
          target.el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
          target.el.click();
          return true;
        }
        """,
        {"idx": idx},
    )
    if not clicked:
        raise RuntimeError(f"Opzione sentiment {sentiment} non trovata nel menu.")


def verify_saved(page: Page, row: Any, sentiment: str, timeout_s: float = 8.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        toast_text = ""
        row_text = ""
        try:
            toast_text = page.locator("body").inner_text(timeout=1000)
        except Exception:
            pass
        try:
            row_text = row.inner_text(timeout=1000)
        except Exception:
            pass
        if re.search(rf"taggato come\s+{sentiment}", toast_text, re.I):
            return True
        if re.search(r"\bcontrollato\b", row_text, re.I):
            return True
        page.wait_for_timeout(500)
    return False


def update_sentiment(page: Page, row: Any, decision: Decision, config: Config) -> str:
    if config.dry_run:
        return "dry_run"
    open_row_menu(row)
    page.wait_for_timeout(600)
    click_sentiment_option(page, decision.sentiment)
    if verify_saved(page, row, decision.sentiment):
        return "success"
    raise RuntimeError("Salvataggio non verificato: toast o tag controllato non rilevati.")


def append_csv(path: Path, rows: list[RunRecord]) -> None:
    is_new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()))
        if is_new:
            writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def append_jsonl(path: Path, rows: list[RunRecord]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")


def append_review(path: Path, item: LinkItem, decision: Decision | None, reason: str) -> None:
    is_new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["date", "url", "title", "sentiment", "confidence", "reason", "snippet"],
        )
        if is_new:
            writer.writeheader()
        writer.writerow(
            {
                "date": now_iso(),
                "url": item.url,
                "title": item.title,
                "sentiment": decision.sentiment if decision else "",
                "confidence": decision.confidence if decision else "",
                "reason": reason,
                "snippet": item.snippet[:700],
            }
        )


def save_error_screenshot(page: Page, item: LinkItem) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", item.title[:50]).strip("_") or f"item_{item.index}"
    path = ERROR_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe}.png"
    page.screenshot(path=str(path), full_page=True)
    return str(path)


def apply_codex_decisions(page: Page, config: Config) -> list[RunRecord]:
    decisions = load_decisions(config.decisions_file)
    records: list[RunRecord] = []
    seen: set[str] = set()
    processed = 0
    stagnant_scrolls = 0

    while True:
        remaining = None if config.max_links is None else max(0, config.max_links - processed)
        if remaining == 0:
            break

        batch = collect_visible_items(page, seen, remaining)
        if not batch:
            page.wait_for_timeout(2200)
            batch = collect_visible_items(page, seen, remaining)
        if not batch:
            moved = scroll_results(page)
            page.wait_for_timeout(2200)
            stagnant_scrolls = stagnant_scrolls + 1 if not moved else 0
            if stagnant_scrolls >= 3:
                break
            continue

        stagnant_scrolls = 0
        for row, item in batch:
            if config.max_links is not None and processed >= config.max_links:
                break
            processed += 1
            status = "fail"
            error = ""
            decision = find_decision(item, decisions)

            try:
                if item.already_checked and not config.force_update:
                    status = "skipped_already_checked"
                    decision = decision or Decision(item.url, item.title, "neutro", 1.0, "Riga già taggata come controllato.")
                elif not decision:
                    status = "no_codex_decision"
                    append_review(LOG_DIR / "review_needed.csv", item, None, "Nessuna decisione Codex trovata per questa riga.")
                    decision = Decision(item.url, item.title, "neutro", 0.0, "Nessuna decisione Codex trovata.")
                elif decision.confidence < config.confidence_threshold:
                    status = "review_needed"
                    append_review(LOG_DIR / "review_needed.csv", item, decision, "Confidence Codex sotto soglia.")
                else:
                    row.scroll_into_view_if_needed(timeout=5000)
                    status = update_sentiment(page, row, decision, config)
            except Exception as exc:
                error = str(exc)
                try:
                    screenshot = save_error_screenshot(page, item)
                    error = f"{error} | screenshot={screenshot}"
                except Exception as shot_exc:
                    error = f"{error} | screenshot_failed={shot_exc}"

            record = RunRecord(
                date=now_iso(),
                url=item.url,
                title=item.title,
                sentiment=decision.sentiment if decision else "",
                confidence=decision.confidence if decision else 0.0,
                reason=decision.reason if decision else "",
                update_status=status,
                error=error,
            )
            records.append(record)
            append_csv(LOG_DIR / "run_log.csv", [record])
            append_jsonl(LOG_DIR / "run_log.jsonl", [record])

        scroll_results(page)
        page.wait_for_timeout(2200)

    return records


def main() -> int:
    ensure_dirs()
    args = parse_args()
    config = load_config(args)
    action = "extract_for_codex" if args.extract_for_codex else "apply_decisions"
    print(
        "Avvio LinkMonitor browser bot "
        f"(action={action}, headless={config.headless}, max_links={config.max_links or 'all'}, "
        f"force_update={config.force_update}, dry_run={config.dry_run})"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=config.headless, slow_mo=config.slow_mo_ms)
        context = browser.new_context(locale="it-IT", viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.set_default_timeout(config.navigation_timeout_ms)
        try:
            prepare_analytics(page, config)
            if args.extract_for_codex:
                extract_for_codex(page, config)
            else:
                records = apply_codex_decisions(page, config)
                success = sum(1 for r in records if r.update_status == "success")
                review = sum(1 for r in records if r.update_status == "review_needed")
                skipped = sum(1 for r in records if r.update_status == "skipped_already_checked")
                failed = sum(1 for r in records if r.update_status == "fail")
                missing = sum(1 for r in records if r.update_status == "no_codex_decision")
                print(
                    f"Fine apply: totale={len(records)}, success={success}, review_needed={review}, "
                    f"skipped={skipped}, no_codex_decision={missing}, fail={failed}"
                )
                if not records or review or missing or failed:
                    print(
                        "Applicazione sentiment incompleta: almeno una menzione non e' stata "
                        "aggiornata correttamente."
                    )
                    return 1
        finally:
            context.close()
            browser.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TimeoutError as exc:
        raise SystemExit(f"Timeout Playwright: {exc}") from exc
