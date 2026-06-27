import asyncio
import os
import glob
import logging
import json
from contextlib import asynccontextmanager
from datetime import datetime

import hmac
import hashlib
from urllib.parse import parse_qsl

from fastapi import FastAPI, Form, Request, HTTPException, Header, Body
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, FSInputFile, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton, MenuButtonWebApp

from playwright.async_api import async_playwright, Page

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ================= CONFIG =================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
YOUR_TELEGRAM_ID = int(os.getenv("YOUR_TELEGRAM_ID", "0"))
SCREENSHOTS_DIR = "screenshots"
SESSIONS_DIR = "session_data"
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "30"))  # секунды между проверками
WEBAPP_URL = os.getenv("WEBAPP_URL", "")  # https://<твой railway домен>

os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
os.makedirs(SESSIONS_DIR, exist_ok=True)

# ================= TELEGRAM =================
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# ================= MONITOR STATE =================
# {account: {"enabled": bool, "last_seen": {chat_id: last_msg_text}}}
monitor_state: dict[str, dict] = {}
monitor_task: asyncio.Task | None = None

# ================= SESSIONS =================
sessions: dict[str, dict] = {}
# {account_name: {"playwright": p, "browser": b, "context": c, "page": page}}


def session_path(account: str) -> str:
    return os.path.join(SESSIONS_DIR, f"{account}_session.json")


def screenshot_path(account: str, suffix: str = "screen") -> str:
    return os.path.join(SCREENSHOTS_DIR, f"{account}_{suffix}.png")


async def create_session(account: str) -> dict:
    """Создаёт новую браузерную сессию для аккаунта."""
    p = await async_playwright().start()
    browser = await p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"]
    )

    saved = session_path(account)
    if os.path.exists(saved):
        log.info(f"[{account}] Восстанавливаем сессию из {saved}")
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            storage_state=saved
        )
    else:
        log.info(f"[{account}] Новая сессия")
        context = await browser.new_context(viewport={"width": 1280, "height": 900})

    page = await context.new_page()
    session = {"playwright": p, "browser": browser, "context": context, "page": page}
    sessions[account] = session
    return session


async def get_or_create_session(account: str) -> dict:
    if account in sessions:
        return sessions[account]
    return await create_session(account)


async def close_session(account: str):
    """Закрывает браузер и удаляет сессию из памяти."""
    if account not in sessions:
        return
    s = sessions.pop(account)
    try:
        await s["browser"].close()
        await s["playwright"].stop()
        log.info(f"[{account}] Сессия закрыта")
    except Exception as e:
        log.warning(f"[{account}] Ошибка при закрытии: {e}")


async def restore_all_sessions():
    """При старте восстанавливает все сохранённые сессии."""
    files = glob.glob(os.path.join(SESSIONS_DIR, "*_session.json"))
    for f in files:
        account = os.path.basename(f).replace("_session.json", "")
        try:
            await create_session(account)
            page = sessions[account]["page"]
            await page.goto("https://web.max.ru/", wait_until="domcontentloaded", timeout=60000)
            log.info(f"[{account}] Сессия восстановлена и страница открыта")
        except Exception as e:
            log.warning(f"[{account}] Не удалось восстановить сессию: {e}")


# ================= MONITOR =================

async def check_new_messages(account: str) -> list[dict]:
    """
    Проверяет непрочитанные чаты в MAX для аккаунта.
    Возвращает список новых сообщений: [{chat, text, time}]
    """
    if account not in sessions:
        return []

    page: Page = sessions[account]["page"]
    new_messages = []

    try:
        # Ищем чаты с непрочитанными сообщениями (бейдж с числом).
        # Селекторы подобраны под типичную структуру web-мессенджеров —
        # при необходимости уточни через DevTools на web.max.ru.
        unread_chats = await page.query_selector_all(
            '[class*="unread"], [class*="badge"], [data-unread="true"]'
        )

        if not unread_chats:
            return []

        state = monitor_state.setdefault(account, {"enabled": True, "last_seen": {}})

        for chat_el in unread_chats:
            try:
                chat_name = await chat_el.evaluate(
                    """el => {
                        const parent = el.closest('[class*="dialog"], [class*="chat"], [class*="item"]');
                        if (!parent) return null;
                        const name = parent.querySelector('[class*="name"], [class*="title"]');
                        return name ? name.innerText.trim() : null;
                    }"""
                )
                last_text = await chat_el.evaluate(
                    """el => {
                        const parent = el.closest('[class*="dialog"], [class*="chat"], [class*="item"]');
                        if (!parent) return null;
                        const msg = parent.querySelector('[class*="message"], [class*="preview"], [class*="last"]');
                        return msg ? msg.innerText.trim() : null;
                    }"""
                )

                if not chat_name:
                    continue

                last_known = state["last_seen"].get(chat_name)
                if last_text and last_text != last_known:
                    state["last_seen"][chat_name] = last_text
                    new_messages.append({
                        "chat": chat_name,
                        "text": last_text or "(нет текста)",
                        "time": datetime.now().strftime("%H:%M"),
                    })

            except Exception as e:
                log.debug(f"[{account}] Ошибка при разборе чата: {e}")

    except Exception as e:
        log.warning(f"[{account}] Ошибка мониторинга: {e}")

    return new_messages


async def monitor_loop():
    """Фоновая задача: каждые MONITOR_INTERVAL секунд проверяет все аккаунты."""
    log.info(f"Монитор запущен (интервал: {MONITOR_INTERVAL}с)")
    while True:
        await asyncio.sleep(MONITOR_INTERVAL)
        for account, state in list(monitor_state.items()):
            if not state.get("enabled", False):
                continue
            if account not in sessions:
                continue
            try:
                msgs = await check_new_messages(account)
                for m in msgs:
                    text = (
                        f"🔔 <b>[{account}]</b> Новое сообщение\n"
                        f"👤 <b>{m['chat']}</b> · {m['time']}\n"
                        f"💬 {m['text']}"
                    )
                    await bot.send_message(YOUR_TELEGRAM_ID, text, parse_mode="HTML")
                    log.info(f"[{account}] Уведомление: {m['chat']}")
            except Exception as e:
                log.warning(f"[{account}] Ошибка в monitor_loop: {e}")


# ================= CHATS LIST =================

MESSAGES_PARSER_JS = """() => {
            const wrappers = Array.from(document.querySelectorAll('[class*="messageWrapper"]'));
            const container = (wrappers[0] && wrappers[0].closest('[class*="scrollListContent"]')) || document.body;

            // Сообщения + разделители дат (capsuleSeparator) в порядке появления
            const allNodes = container.querySelectorAll('[class*="messageWrapper"], [class*="capsuleSeparator"]');
            const items = [];

            allNodes.forEach(node => {
                const cls = typeof node.className === 'string' ? node.className : '';

                // Разделитель даты
                if (/capsuleSeparator/.test(cls)) {
                    const t = (node.innerText || '').trim();
                    if (t && t.length < 40) items.push({ type: 'date', text: t });
                    return;
                }
                if (!/messageWrapper/.test(cls)) return;

                // Входящее/исходящее — по data-bubbles-variant, запасной признак: класс --isOut
                let isOut = /--isOut/i.test(cls);
                const variantEl = node.querySelector('[data-bubbles-variant]');
                if (variantEl) isOut = variantEl.getAttribute('data-bubbles-variant') === 'outgoing';
                const isFirst = /messageWrapper--first/.test(cls);
                const isLast = /messageWrapper--last/.test(cls);

                const bubbleContent = node.querySelector('[class*="bubbleContent"]')
                    || node.querySelector('[class*="bubble"]') || node;

                // Имя автора (шапка: .header .name .text)
                let authorName = '';
                const headerEl = bubbleContent.querySelector('[class*="header"]');
                if (headerEl) {
                    const t = (headerEl.innerText || '').trim();
                    if (t && t.length < 100) authorName = t;
                }

                // Пересланное
                let forwardFrom = '';
                const fwdEl = node.querySelector('[class*="forward"], [class*="Forward"]');
                if (fwdEl) {
                    const t = (fwdEl.innerText || '').trim();
                    if (t) forwardFrom = t.replace(/^Переслано:\\s*/i, '').trim();
                }

                // Текст сообщения: клон bubbleContent без шапки/меты/реакций/служебного
                let text = '';
                {
                    const clone = bubbleContent.cloneNode(true);
                    clone.querySelectorAll('[class*="header"], [class*="meta"], [class*="reactions"], [class*="reaction"], [class*="views"], [class*="counter"], canvas, svg, button').forEach(e => e.remove());
                    text = (clone.innerText || '').trim();
                }
                if (authorName && text.startsWith(authorName)) text = text.slice(authorName.length).trim();

                // Время — из меты ("3,9K 05:58 PM" или "05:58 PM")
                let time = '';
                const metaEl = bubbleContent.querySelector('[class*="meta"]');
                if (metaEl) {
                    const m = (metaEl.innerText || '').match(/\\d{1,2}:\\d{2}(\\s*[AP]M)?/i);
                    if (m) time = m[0].trim();
                }

                // Реакции: button.reaction → canvas(animoji) + span.counter
                const reactions = [];
                const reactionsContainer = node.querySelector('[class*="reactions"]');
                const inPicker = reactionsContainer && reactionsContainer.closest('[class*="picker"], [class*="popup"], [class*="menu"], [role="menu"], [role="dialog"]');
                if (reactionsContainer && !inPicker) {
                    reactionsContainer.querySelectorAll('button[class*="reaction"]').forEach(btn => {
                        const counterEl = btn.querySelector('[class*="counter"]');
                        let count = counterEl ? (counterEl.innerText || '').trim() : '';
                        if (!/\\d/.test(count)) return;
                        // эмодзи отрисован на canvas → снимаем как картинку
                        let emojiImg = '';
                        const canvas = btn.querySelector('canvas');
                        if (canvas) { try { emojiImg = canvas.toDataURL('image/png'); } catch (e) {} }
                        let emoji = '';
                        if (!emojiImg) {
                            const imgEl = btn.querySelector('img');
                            if (imgEl) emoji = (imgEl.alt || '').trim();
                            if (!emoji) emoji = (btn.innerText || '').replace(/\\d+/g, '').trim();
                        }
                        const btnCls = typeof btn.className === 'string' ? btn.className : '';
                        reactions.push({ emoji: emoji, emoji_img: emojiImg, count: count, active: /active/i.test(btnCls) });
                    });
                }

                // Аватар автора (img или canvas)
                let authorAvatar = '';
                if (!isOut) {
                    const avEl = node.querySelector('[class*="avatar"]');
                    if (avEl) {
                        const avImg = avEl.querySelector('img');
                        if (avImg && avImg.src && avImg.src.startsWith('http')) {
                            authorAvatar = avImg.src;
                        } else {
                            const avCanvas = avEl.querySelector('canvas');
                            if (avCanvas) { try { authorAvatar = avCanvas.toDataURL('image/png'); } catch (e) {} }
                        }
                    }
                }

                // Голосовое
                let voiceUrl = '';
                let voiceDuration = '';
                const audioEl = node.querySelector('audio');
                if (audioEl) {
                    voiceUrl = audioEl.src || '';
                    if (!voiceUrl) { const src = audioEl.querySelector('source'); if (src) voiceUrl = src.src; }
                    const durEl = node.querySelector('[class*="duration"], [class*="Duration"]');
                    if (durEl) voiceDuration = (durEl.innerText || '').trim();
                }

                // Картинки/видео
                const mediaUrls = [];
                node.querySelectorAll('img').forEach(img => {
                    const ic = typeof img.className === 'string' ? img.className : '';
                    if (/avatar|reaction|emoji|animoji/i.test(ic)) return;
                    if (img.src && img.src.startsWith('http') && (img.naturalWidth >= 60 || img.width >= 60)) {
                        mediaUrls.push({ type: 'image', url: img.src });
                    }
                });
                node.querySelectorAll('video').forEach(v => {
                    let src = v.src;
                    if (!src) { const s = v.querySelector('source'); if (s) src = s.src; }
                    if (src && src.startsWith('http')) mediaUrls.push({ type: 'video', url: src });
                });

                items.push({
                    type: 'message',
                    text: text.slice(0, 2000),
                    time: time,
                    outgoing: isOut,
                    author_name: isOut ? '' : authorName,
                    author_avatar: isOut ? '' : authorAvatar,
                    is_first_in_group: isFirst,
                    is_last_in_group: isLast,
                    forward_from: forwardFrom,
                    reactions: reactions,
                    voice_url: voiceUrl,
                    voice_duration: voiceDuration,
                    media: mediaUrls,
                });
            });

            return { items: items, total_found: items.length };
        }"""


async def parse_messages_in_dom(page) -> dict:
    """Парсит сообщения из текущего DOM (чат уже должен быть открыт)."""
    result = await page.evaluate(MESSAGES_PARSER_JS)
    return result or {"items": []}


async def open_chat_and_fetch_messages(account: str, chat_name: str) -> dict:
    """
    Кликает на чат с указанным именем в боковой панели MAX и парсит сообщения.
    """
    if account not in sessions:
        raise HTTPException(status_code=400, detail="Аккаунт не активен")
    page: Page = sessions[account]["page"]

    try:
        # Кликаем на чат с нужным именем (поиск по тексту внутри .item)
        clicked = await page.evaluate("""(name) => {
            const container = document.querySelector('.scrollListContent, [class*="scrollListContent"]');
            if (!container) return false;
            const items = container.querySelectorAll(':scope > .item, :scope > [class*="item "], :scope > [class^="item"]');
            for (const el of items) {
                const allText = (el.innerText || '').trim();
                const firstLine = allText.split(String.fromCharCode(10))[0];
                if (firstLine === name) {
                    // Кликаем по нажимаемому элементу внутри
                    const btn = el.querySelector('button, [role="button"], .cell') || el;
                    btn.click();
                    return true;
                }
            }
            return false;
        }""", chat_name)

        if not clicked:
            raise HTTPException(status_code=404, detail=f"Чат '{chat_name}' не найден в списке")

        # Ждём загрузки сообщений
        await asyncio.sleep(1.5)

        # Парсим сообщения через общий хелпер
        return await parse_messages_in_dom(page)
    except HTTPException:
        raise
    except Exception as e:
        log.warning(f"[{account}] open_chat error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def fetch_chats(account: str) -> list[dict]:
    """
    Парсит плоский список чатов из web.max.ru (Svelte-вёрстка).
    Контейнер: .scrollListContent → .item
    """
    if account not in sessions:
        return []
    page: Page = sessions[account]["page"]

    try:
        chats = await page.evaluate("""() => {
            const container = document.querySelector('.scrollListContent, [class*="scrollListContent"]');
            if (!container) return [];

            const items = container.querySelectorAll(':scope > .item, :scope > [class*="item "], :scope > [class^="item"]');
            const result = [];
            const seen = new Set();

            items.forEach(el => {
                let name = '';
                const nameCandidates = el.querySelectorAll('[class*="name"], [class*="title"], [class*="Name"], [class*="Title"]');
                for (const c of nameCandidates) {
                    const t = (c.innerText || '').trim();
                    if (t && t.length < 100) { name = t; break; }
                }

                let lastMsg = '';
                const msgCandidates = el.querySelectorAll('[class*="message"], [class*="preview"], [class*="last"], [class*="text"]');
                for (const c of msgCandidates) {
                    const t = (c.innerText || '').trim();
                    if (t && t !== name) { lastMsg = t; break; }
                }

                if (!name) {
                    const allText = (el.innerText || '').trim();
                    const firstLine = allText.split(String.fromCharCode(10))[0];
                    if (firstLine && firstLine.length < 100) name = firstLine;
                }

                // АВАТАРКА — ищем img внутри элемента
                let avatar = '';
                const imgEl = el.querySelector('img[class*="avatar"], [class*="avatar"] img, img[class*="Image"]');
                if (imgEl && imgEl.src && imgEl.src.startsWith('http')) {
                    avatar = imgEl.src;
                }

                // ВРЕМЯ последнего сообщения
                let time = '';
                const timeCandidates = el.querySelectorAll('[class*="time"], [class*="date"], [class*="Time"], [class*="Date"]');
                for (const c of timeCandidates) {
                    const t = (c.innerText || '').trim();
                    if (t && t.length < 20) { time = t; break; }
                }

                let unread = '';
                const badgeEl = el.querySelector('[class*="unread"], [class*="badge"], [class*="counter"], [class*="Counter"]');
                if (badgeEl) {
                    const t = (badgeEl.innerText || '').trim();
                    if (/^[0-9]+$/.test(t)) unread = t;
                }

                if (!name || seen.has(name)) return;
                seen.add(name);

                result.push({
                    name: name,
                    last_message: lastMsg.slice(0, 100),
                    unread: unread,
                    avatar: avatar,
                    time: time,
                });
            });
            return result;
        }""")
        return chats or []
    except Exception as e:
        log.warning(f"[{account}] fetch_chats error: {e}")
        return []


# ================= TELEGRAM WEBAPP AUTH =================

def verify_init_data(init_data: str) -> dict | None:
    """
    Проверяет подпись initData от Telegram Web App.
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data or not TELEGRAM_TOKEN:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", TELEGRAM_TOKEN.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(calc_hash, received_hash):
            return None

        user = parsed.get("user")
        if user:
            parsed["user"] = json.loads(user)
        return parsed
    except Exception as e:
        log.warning(f"verify_init_data: {e}")
        return None


async def require_webapp_owner(x_telegram_init_data: str = Header(None)) -> dict:
    """FastAPI-зависимость: проверяет, что запрос пришёл от владельца через Web App."""
    data = verify_init_data(x_telegram_init_data or "")
    if not data:
        raise HTTPException(status_code=401, detail="Invalid initData")
    user = data.get("user") or {}
    user_id = user.get("id")
    if YOUR_TELEGRAM_ID and user_id != YOUR_TELEGRAM_ID:
        raise HTTPException(status_code=403, detail="Forbidden")
    return user


# ================= LIFESPAN =================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global monitor_task

    log.info("Восстанавливаем сохранённые сессии...")
    await restore_all_sessions()

    # Включаем мониторинг для всех восстановленных аккаунтов
    for account in sessions:
        monitor_state.setdefault(account, {"enabled": True, "last_seen": {}})

    log.info("Запускаем Telegram-бота...")
    bot_task = asyncio.create_task(dp.start_polling(bot))

    # Устанавливаем кнопку Web App
    await setup_bot_menu()

    log.info("Запускаем монитор сообщений...")
    monitor_task = asyncio.create_task(monitor_loop())

    yield

    # Shutdown
    log.info("Закрываем все сессии...")
    for account in list(sessions.keys()):
        await close_session(account)

    for task in (bot_task, monitor_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    log.info("Завершено")


# ================= APP =================
app = FastAPI(lifespan=lifespan)
app.mount("/screenshot", StaticFiles(directory=SCREENSHOTS_DIR), name="screenshots")


# ================= GUARD =================
def only_owner(user_id: int) -> bool:
    """Проверка, что команду отправил владелец."""
    return YOUR_TELEGRAM_ID == 0 or user_id == YOUR_TELEGRAM_ID


# ================= DEBUG: DOM INSPECTOR =================

@app.get("/api/debug/messages/{account}")
async def api_debug_messages(account: str):
    """Возвращает структуру DOM области сообщений открытого чата."""
    if account not in sessions:
        raise HTTPException(status_code=400, detail="Аккаунт не активен")
    page: Page = sessions[account]["page"]
    try:
        info = await page.evaluate("""() => {
            const result = {};

            // 1. Полный HTML одного messageWrapper с реакциями
            const wrappers = document.querySelectorAll('[class*="messageWrapper"]');
            // ищем тот, у которого есть реакции
            let withReactions = null;
            for (const w of wrappers) {
                if (w.querySelector('[class*="reaction"]')) { withReactions = w; break; }
            }
            const sample = withReactions || wrappers[wrappers.length - 1];
            if (sample) {
                result.sampleWrapperHTML = sample.outerHTML.slice(0, 3500);
            }

            // 2. Структура реакций - как именно вложены
            if (withReactions) {
                const reactionEls = withReactions.querySelectorAll('[class*="reaction"]');
                result.reactionsStructure = Array.from(reactionEls).slice(0, 12).map(r => ({
                    className: typeof r.className === 'string' ? r.className : '',
                    innerText: (r.innerText || '').trim().slice(0, 40),
                    childCount: r.children.length,
                }));
            }

            // 3. Поле ввода сообщения
            const inputs = document.querySelectorAll('[contenteditable="true"], textarea, [class*="composer"], [class*="messageInput"], [class*="input"]');
            result.inputCandidates = Array.from(inputs).slice(0, 8).map(el => ({
                tag: el.tagName,
                className: typeof el.className === 'string' ? el.className : '',
                contentEditable: el.contentEditable,
                placeholder: el.placeholder || el.getAttribute('placeholder') || '',
            }));

            // 4. Контейнер прокрутки сообщений
            const scrollables = [];
            document.querySelectorAll('div').forEach(el => {
                if (el.scrollHeight > el.clientHeight + 50 && el.clientHeight > 200) {
                    const cls = typeof el.className === 'string' ? el.className : '';
                    if (cls) scrollables.push({ className: cls, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight });
                }
            });
            result.scrollContainers = scrollables.slice(0, 6);

            return result;
        }""")
        return info
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/debug/dom/{account}")
async def api_debug_dom(account: str):
    """Возвращает структуру DOM боковой панели MAX — чтобы понять какие селекторы использовать для чатов."""
    if account not in sessions:
        raise HTTPException(status_code=400, detail="Аккаунт не активен")
    page: Page = sessions[account]["page"]
    try:
        info = await page.evaluate("""() => {
            const result = {
                url: window.location.href,
                title: document.title,
                bodyClasses: document.body.className,
                possibleChatContainers: []
            };

            // Ищем все элементы с большим количеством "детей" — это потенциальные списки чатов
            const all = document.querySelectorAll('div, ul, section');
            const candidates = [];
            all.forEach(el => {
                if (el.children.length >= 5 && el.children.length <= 200) {
                    const cls = el.className && typeof el.className === 'string' ? el.className : '';
                    if (cls && (cls.toLowerCase().includes('list') ||
                                cls.toLowerCase().includes('dialog') ||
                                cls.toLowerCase().includes('chat') ||
                                cls.toLowerCase().includes('scroll'))) {
                        candidates.push({
                            tag: el.tagName,
                            className: cls,
                            childCount: el.children.length,
                            firstChildHtml: el.children[0] ? el.children[0].outerHTML.slice(0, 500) : ''
                        });
                    }
                }
            });
            result.possibleChatContainers = candidates.slice(0, 10);

            // Также соберём ВСЕ уникальные классы которые содержат слова chat/dialog/message/item
            const classSet = new Set();
            document.querySelectorAll('[class]').forEach(el => {
                const cls = typeof el.className === 'string' ? el.className : '';
                cls.split(/\\s+/).forEach(c => {
                    if (/chat|dialog|message|item|conversation/i.test(c)) classSet.add(c);
                });
            });
            result.relevantClasses = Array.from(classSet).slice(0, 50);

            return result;
        }""")
        return info
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ================= IMAGE PROXY (для аватарок) =================

@app.get("/api/accounts/{account}/proxy")
async def api_proxy_image(account: str, url: str):
    """Прокачивает картинку с web.max.ru через сессию аккаунта (с куками)."""
    from fastapi.responses import Response
    if account not in sessions:
        raise HTTPException(status_code=400, detail="Аккаунт не активен")
    context = sessions[account]["context"]
    try:
        # запрашиваем картинку через тот же контекст где есть куки
        api_req = context.request
        response = await api_req.get(url, timeout=15000)
        if not response.ok:
            raise HTTPException(status_code=response.status, detail="Не удалось получить картинку")
        body = await response.body()
        content_type = response.headers.get("content-type", "image/jpeg")
        return Response(content=body, media_type=content_type, headers={"Cache-Control": "public, max-age=3600"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ================= STATIC FILES =================
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def webapp_root():
    """Отдаём Telegram Web App."""
    return FileResponse("static/webapp.html")


# ================= API для Web App =================

@app.get("/api/accounts")
async def api_list_accounts(user: dict = None):
    # Не требуем initData при просмотре, но requireOwner ниже на изменениях
    # (читать список безопасно, ничего секретного не возвращаем)
    result = []
    # Активные + сохранённые (даже если не загружены)
    saved_files = glob.glob(os.path.join(SESSIONS_DIR, "*_session.json"))
    saved_names = {os.path.basename(f).replace("_session.json", "") for f in saved_files}
    all_names = saved_names | set(sessions.keys())
    for name in sorted(all_names):
        result.append({
            "name": name,
            "active": name in sessions,
            "saved": name in saved_names,
        })
    return {"accounts": result}


@app.post("/api/accounts/{account}/qr")
async def api_request_qr(account: str, user: dict = None):
    try:
        session = await get_or_create_session(account)
        page: Page = session["page"]
        await page.goto("https://web.max.ru/", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)
        path = screenshot_path(account, "qr")
        await page.screenshot(path=path, full_page=True)
        return {"ok": True, "qr_url": f"/screenshot/{os.path.basename(path)}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/accounts/{account}/password")
async def api_submit_password(account: str, request: Request, user: dict = None):
    if account not in sessions:
        raise HTTPException(status_code=400, detail="Сначала запроси QR")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    password = (payload or {}).get("password", "").strip()
    try:
        session = sessions[account]
        page: Page = session["page"]
        context = session["context"]

        # Если пароль НЕ передан — просто сохраняем текущую сессию (юзер уже залогинен через QR)
        if not password:
            await context.storage_state(path=session_path(account))
            monitor_state.setdefault(account, {"enabled": True, "last_seen": {}})
            return {"ok": True, "mode": "no_password"}

        # Пароль есть — ждём поле пароля и вводим
        try:
            await page.wait_for_selector('input[type="password"]', timeout=30000, state="visible")
        except Exception:
            raise HTTPException(
                status_code=400,
                detail="Поле пароля не появилось. Если ты уже залогинен — нажми 'Сохранить без пароля'."
            )

        await page.fill('input[type="password"]', password)

        clicked = False
        for selector in [
            'button:has-text("Continue")',
            'button:has-text("Войти")',
            'button:has-text("Продолжить")',
            'button[type="submit"]',
        ]:
            try:
                btn = await page.query_selector(selector)
                if btn:
                    await btn.click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            await page.press('input[type="password"]', "Enter")
        await asyncio.sleep(5)

        await context.storage_state(path=session_path(account))
        monitor_state.setdefault(account, {"enabled": True, "last_seen": {}})
        return {"ok": True, "mode": "with_password"}
    except HTTPException:
        raise
    except Exception as e:
        log.exception(f"[{account}] password error")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.post("/api/accounts/{account}/screen")
async def api_screen(account: str, user: dict = None):
    """Свежий скриншот текущей страницы (для проверки: QR/пароль/уже залогинен)."""
    if account not in sessions:
        raise HTTPException(status_code=400, detail="Аккаунт не активен")
    try:
        page: Page = sessions[account]["page"]
        path = screenshot_path(account, "screen")
        await page.screenshot(path=path, full_page=True)
        return {"ok": True, "screen_url": f"/screenshot/{os.path.basename(path)}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/accounts/{account}")
async def api_delete_account(account: str, user: dict = None):
    await close_session(account)
    monitor_state.pop(account, None)
    sp = session_path(account)
    if os.path.exists(sp):
        os.remove(sp)
    return {"ok": True}


@app.get("/api/accounts/{account}/chats/{chat_name}/messages")
async def api_chat_messages(account: str, chat_name: str, user: dict = None):
    """Открывает чат в браузере и возвращает сообщения."""
    if account not in sessions:
        try:
            await create_session(account)
            page = sessions[account]["page"]
            await page.goto("https://web.max.ru/", wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Аккаунт не активен: {e}")
    return await open_chat_and_fetch_messages(account, chat_name)


@app.get("/api/accounts/{account}/chats/{chat_name}/profile")
async def api_chat_profile(account: str, chat_name: str, user: dict = None):
    """Открывает чат и парсит профиль (аватар, имя, инфа)."""
    if account not in sessions:
        raise HTTPException(status_code=400, detail="Аккаунт не активен")
    page: Page = sessions[account]["page"]

    try:
        # Сначала убеждаемся что чат открыт
        clicked = await page.evaluate("""(name) => {
            const container = document.querySelector('.scrollListContent, [class*="scrollListContent"]');
            if (!container) return false;
            const items = container.querySelectorAll(':scope > .item, :scope > [class*="item "], :scope > [class^="item"]');
            for (const el of items) {
                const allText = (el.innerText || '').trim();
                const firstLine = allText.split(String.fromCharCode(10))[0];
                if (firstLine === name) {
                    const btn = el.querySelector('button, [role="button"], .cell') || el;
                    btn.click();
                    return true;
                }
            }
            return false;
        }""", chat_name)

        if not clicked:
            raise HTTPException(status_code=404, detail=f"Чат '{chat_name}' не найден")

        await asyncio.sleep(1.5)

        # Кликаем на шапку чата чтобы открыть профиль
        await page.evaluate("""() => {
            // ищем заголовок чата вверху диалога
            const headers = document.querySelectorAll('[class*="chatHeader"], [class*="conversationHeader"], [class*="ChatHeader"], header');
            for (const h of headers) {
                const btn = h.querySelector('button, [role="button"], [class*="title"]') || h;
                btn.click();
                return true;
            }
            return false;
        }""")
        await asyncio.sleep(1)

        # Парсим профиль из открывшейся панели
        profile = await page.evaluate("""() => {
            // Ищем большой аватар в правой панели / модалке
            let avatar = '';
            const bigAvatars = document.querySelectorAll('img[class*="avatar"], [class*="profile"] img, [class*="Profile"] img');
            for (const img of bigAvatars) {
                if (img.naturalWidth >= 80 && img.src && img.src.startsWith('http')) {
                    avatar = img.src;
                    break;
                }
            }

            // Имя
            let name = '';
            const nameEls = document.querySelectorAll('[class*="profile"] [class*="name"], [class*="profile"] h1, [class*="profile"] h2, [class*="Profile"] [class*="name"]');
            for (const el of nameEls) {
                const t = (el.innerText || '').trim();
                if (t && t.length < 100) { name = t; break; }
            }

            // Дополнительная информация: все строки в панели профиля
            const info = [];
            const infoBlocks = document.querySelectorAll('[class*="profile"] [class*="info"], [class*="Profile"] [class*="row"], [class*="profile"] [class*="field"]');
            const seen = new Set();
            infoBlocks.forEach(el => {
                const t = (el.innerText || '').trim();
                if (t && t.length < 200 && !seen.has(t) && t !== name) {
                    seen.add(t);
                    info.push(t);
                }
            });

            return { avatar, name, info: info.slice(0, 10) };
        }""")

        return profile or {}
    except HTTPException:
        raise
    except Exception as e:
        log.warning(f"[{account}] profile error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/accounts/{account}/chats/{chat_name}/send")
async def api_send_message(account: str, chat_name: str, request: Request, user: dict = None):
    """Отправляет текстовое сообщение в чат через Playwright."""
    if account not in sessions:
        raise HTTPException(status_code=400, detail="Аккаунт не активен")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    text = (payload or {}).get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Пустое сообщение")

    page: Page = sessions[account]["page"]
    try:
        # Открываем нужный чат
        clicked = await page.evaluate("""(name) => {
            const container = document.querySelector('.scrollListContent, [class*="scrollListContent"]');
            if (!container) return false;
            const items = container.querySelectorAll(':scope > .item, :scope > [class*="item "], :scope > [class^="item"]');
            for (const el of items) {
                const allText = (el.innerText || '').trim();
                const firstLine = allText.split(String.fromCharCode(10))[0];
                if (firstLine === name) {
                    const btn = el.querySelector('button, [role="button"], .cell') || el;
                    btn.click();
                    return true;
                }
            }
            return false;
        }""", chat_name)

        if not clicked:
            raise HTTPException(status_code=404, detail=f"Чат '{chat_name}' не найден")

        await asyncio.sleep(1.2)

        # Поле ввода. По реальному DOM: div.input--compact с contentEditable="inherit"
        # — поэтому НЕ требуем атрибут [contenteditable].
        input_selectors = [
            '[class*="input--compact"]',
            '[class*="input--secondary"]',
            '[class*="input--neutral"]',
            'div[contenteditable]',
            '[contenteditable="true"]',
            'textarea',
        ]
        input_found = False
        for sel in input_selectors:
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.click()
                    await asyncio.sleep(0.2)
                    await page.keyboard.type(text, delay=20)
                    input_found = True
                    break
            except Exception:
                continue

        if not input_found:
            raise HTTPException(status_code=500, detail="Поле ввода сообщения не найдено")

        await asyncio.sleep(0.3)
        await page.keyboard.press("Enter")
        await asyncio.sleep(1)

        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        log.warning(f"[{account}] send error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/accounts/{account}/chats/{chat_name}/load_older")
async def api_load_older(account: str, chat_name: str, user: dict = None):
    """Прокручивает виртуализированный список в MAX вверх и парсит подгруженные сообщения."""
    if account not in sessions:
        raise HTTPException(status_code=400, detail="Аккаунт не активен")
    page: Page = sessions[account]["page"]

    try:
        # Находим контейнер прокрутки ИМЕННО ленты сообщений (а не списка чатов слева,
        # у которого тот же класс scrollListScrollable). Идём вверх от messageWrapper.
        for i in range(12):
            scrolled = await page.evaluate("""() => {
                // 1) контейнер, который реально содержит сообщения
                const wrap = document.querySelector('[class*="messageWrapper"]');
                let c = null;
                if (wrap) {
                    let p = wrap.parentElement;
                    while (p && p !== document.body) {
                        const oy = getComputedStyle(p).overflowY;
                        if ((oy === 'auto' || oy === 'scroll') && p.scrollHeight > p.clientHeight + 20) { c = p; break; }
                        p = p.parentElement;
                    }
                }
                // 2) запасной вариант: последний скроллируемый список на странице
                if (!c) {
                    const lists = document.querySelectorAll('[class*="scrollListScrollable"], [class*="scrollList"]');
                    c = lists.length ? lists[lists.length - 1] : null;
                }
                if (!c) return false;
                const before = c.scrollTop;
                // виртуальный список: плавно вверх + событие scroll, чтобы триггернуть догрузку
                c.scrollTop = Math.max(0, c.scrollTop - 1200);
                c.dispatchEvent(new Event('scroll', { bubbles: true }));
                return { before, after: c.scrollTop, scrollHeight: c.scrollHeight };
            }""")
            await asyncio.sleep(0.8)  # ждём подгрузку виртуализированных сообщений

        # Парсим БЕЗ переоткрытия чата (иначе скролл сбросится)
        return await parse_messages_in_dom(page)
    except HTTPException:
        raise
    except Exception as e:
        log.warning(f"[{account}] load_older error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/accounts/{account}/chats")
async def api_list_chats(account: str, user: dict = None):
    if account not in sessions:
        # Пробуем восстановить
        try:
            await create_session(account)
            page = sessions[account]["page"]
            await page.goto("https://web.max.ru/", wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Аккаунт не активен: {e}")
    chats = await fetch_chats(account)
    return {"chats": chats}


# ================= СТАРЫЙ ВЕБ-ИНТЕРФЕЙС (для отладки) =================
@app.get("/admin", response_class=HTMLResponse)
async def index():
    account_list = "".join(
        f"<li><b>{a}</b> — сессия активна {'✅ (сохранена)' if os.path.exists(session_path(a)) else '🟡 (не сохранена)'}</li>"
        for a in sessions
    ) or "<li>нет активных аккаунтов</li>"

    return HTMLResponse(f"""
    <!DOCTYPE html><html><head>
    <meta charset="utf-8"><title>MAX Web Bot</title>
    <style>body{{font-family:sans-serif;max-width:600px;margin:40px auto;}}
    form{{margin-bottom:20px;padding:16px;border:1px solid #ddd;border-radius:8px;}}
    input{{margin:4px 0 8px;padding:6px;width:100%;box-sizing:border-box;}}
    button{{padding:8px 16px;background:#0088cc;color:#fff;border:none;border-radius:4px;cursor:pointer;}}
    </style></head><body>
    <h2>MAX Web Bot</h2>
    <h4>Активные аккаунты:</h4><ul>{account_list}</ul>

    <form action="/qr" method="post">
        <b>Получить QR-код</b><br>
        Аккаунт: <input type="text" name="account" required>
        <button type="submit">Открыть</button>
    </form>

    <form action="/password" method="post">
        <b>Ввести пароль</b><br>
        Аккаунт: <input type="text" name="account" required>
        Пароль: <input type="password" name="password" required>
        <button type="submit">Войти</button>
    </form>

    <form action="/screen" method="post">
        <b>Скриншот текущей страницы</b><br>
        Аккаунт: <input type="text" name="account" required>
        <button type="submit">Скрин</button>
    </form>

    <form action="/close" method="post">
        <b>Закрыть сессию</b><br>
        Аккаунт: <input type="text" name="account" required>
        <button type="submit" style="background:#cc2200">Закрыть</button>
    </form>
    </body></html>
    """)


@app.post("/qr", response_class=HTMLResponse)
async def web_qr(account: str = Form(...)):
    try:
        session = await get_or_create_session(account)
        page: Page = session["page"]
        await page.goto("https://web.max.ru/", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)
        path = screenshot_path(account, "qr")
        await page.screenshot(path=path, full_page=True)
        return HTMLResponse(f"""
        <h3>QR-код — {account}</h3>
        <img src="/screenshot/{os.path.basename(path)}" width="400"><br>
        <a href="/">← Назад</a>
        """)
    except Exception as e:
        return HTMLResponse(f"<pre>Ошибка: {e}</pre><a href='/'>← Назад</a>")


@app.post("/password", response_class=HTMLResponse)
async def web_password(account: str = Form(...), password: str = Form(...)):
    if account not in sessions:
        return HTMLResponse("<pre>Сначала откройте QR для этого аккаунта</pre><a href='/'>← Назад</a>")
    try:
        session = sessions[account]
        page: Page = session["page"]
        context = session["context"]
        await page.fill('input[type="password"]', password)
        await page.click('button:has-text("Continue")')
        await asyncio.sleep(5)
        await context.storage_state(path=session_path(account))
        return HTMLResponse(f"<pre>✅ Пароль введён, сессия [{account}] сохранена!</pre><a href='/'>← Назад</a>")
    except Exception as e:
        return HTMLResponse(f"<pre>Ошибка: {e}</pre><a href='/'>← Назад</a>")


@app.post("/screen", response_class=HTMLResponse)
async def web_screen(account: str = Form(...)):
    if account not in sessions:
        return HTMLResponse("<pre>Сначала откройте QR для этого аккаунта</pre><a href='/'>← Назад</a>")
    try:
        page: Page = sessions[account]["page"]
        path = screenshot_path(account, "screen")
        await page.screenshot(path=path, full_page=True)
        return HTMLResponse(f"""
        <h3>Экран — {account}</h3>
        <img src="/screenshot/{os.path.basename(path)}" width="600"><br>
        <a href="/">← Назад</a>
        """)
    except Exception as e:
        return HTMLResponse(f"<pre>Ошибка: {e}</pre><a href='/'>← Назад</a>")


@app.post("/close", response_class=HTMLResponse)
async def web_close(account: str = Form(...)):
    await close_session(account)
    return HTMLResponse(f"<pre>✅ Сессия [{account}] закрыта</pre><a href='/'>← Назад</a>")


# ================= TELEGRAM БОТ =================

from functools import wraps

def require_owner(func):
    """Декоратор: только владелец может использовать команды."""
    @wraps(func)
    async def wrapper(message: Message, *args, **kwargs):
        if not only_owner(message.from_user.id):
            await message.answer("⛔ Нет доступа.")
            return
        return await func(message, *args, **kwargs)
    return wrapper


@dp.message(Command("start"))
@require_owner
async def cmd_start(message: Message):
    kb = None
    if WEBAPP_URL:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📱 Открыть приложение", web_app=WebAppInfo(url=WEBAPP_URL))
        ]])
    await message.answer(
        "✦ Star Panel\n\n"
        "Открой приложение кнопкой ниже или используй команды:\n\n"
        "Аккаунты:\n"
        "/accounts — список аккаунтов\n"
        "/qr <акк> — QR-код для входа\n"
        "/screen <акк> — скриншот страницы\n"
        "/password <акк> <пароль> — ввести пароль\n"
        "/close <акк> — закрыть сессию\n\n"
        "Мониторинг:\n"
        "/monitor_on <акк>, /monitor_off <акк>, /check <акк>",
        reply_markup=kb,
    )


async def setup_bot_menu():
    """Устанавливает кнопку Web App в меню бота."""
    if not WEBAPP_URL:
        return
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="📱 Приложение", web_app=WebAppInfo(url=WEBAPP_URL))
        )
        log.info(f"Кнопка Web App установлена: {WEBAPP_URL}")
    except Exception as e:
        log.warning(f"Не удалось установить кнопку Web App: {e}")


@dp.message(Command("accounts"))
@require_owner
async def cmd_accounts(message: Message):
    if not sessions:
        await message.answer("Нет активных аккаунтов.")
        return
    lines = []
    for acc in sessions:
        saved = "💾" if os.path.exists(session_path(acc)) else "🟡"
        mon = monitor_state.get(acc, {})
        mon_icon = "🔔" if mon.get("enabled") else "🔕"
        lines.append(f"• {acc}  {saved} сессия  {mon_icon} монитор")
    await message.answer("Активные аккаунты:\n" + "\n".join(lines))


@dp.message(Command("qr"))
@require_owner
async def cmd_qr(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /qr <аккаунт>")
        return
    account = parts[1].strip()
    await message.answer(f"⏳ Открываю сессию для [{account}]...")
    try:
        session = await get_or_create_session(account)
        page: Page = session["page"]
        await page.goto("https://web.max.ru/", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)
        path = screenshot_path(account, "qr")
        await page.screenshot(path=path, full_page=True)
        await message.answer_photo(FSInputFile(path), caption=f"QR-код для [{account}]")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("screen"))
@require_owner
async def cmd_screen(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /screen <аккаунт>")
        return
    account = parts[1].strip()
    if account not in sessions:
        await message.answer(f"❌ Аккаунт [{account}] не активен. Сначала /qr {account}")
        return
    try:
        path = screenshot_path(account, "screen")
        await sessions[account]["page"].screenshot(path=path, full_page=True)
        await message.answer_photo(FSInputFile(path), caption=f"Экран [{account}]")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("password"))
@require_owner
async def cmd_password(message: Message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Использование: /password <аккаунт> <пароль>")
        return
    account, password = parts[1].strip(), parts[2].strip()
    if account not in sessions:
        await message.answer(f"❌ Аккаунт [{account}] не активен. Сначала /qr {account}")
        return
    try:
        session = sessions[account]
        page: Page = session["page"]
        context = session["context"]
        await page.fill('input[type="password"]', password)
        await page.click('button:has-text("Continue")')
        await asyncio.sleep(5)
        await context.storage_state(path=session_path(account))
        await message.answer(f"✅ Пароль введён, сессия [{account}] сохранена!")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("close"))
@require_owner
async def cmd_close(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /close <аккаунт>")
        return
    account = parts[1].strip()
    await close_session(account)
    monitor_state.pop(account, None)
    await message.answer(f"✅ Сессия [{account}] закрыта.")


@dp.message(Command("monitor_on"))
@require_owner
async def cmd_monitor_on(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /monitor_on <аккаунт>")
        return
    account = parts[1].strip()
    if account not in sessions:
        await message.answer(f"❌ Аккаунт [{account}] не активен.")
        return
    monitor_state.setdefault(account, {"enabled": False, "last_seen": {}})["enabled"] = True
    await message.answer(f"✅ Мониторинг для [{account}] включён (каждые {MONITOR_INTERVAL}с).")


@dp.message(Command("monitor_off"))
@require_owner
async def cmd_monitor_off(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /monitor_off <аккаунт>")
        return
    account = parts[1].strip()
    if account in monitor_state:
        monitor_state[account]["enabled"] = False
    await message.answer(f"⏸ Мониторинг для [{account}] выключен.")


@dp.message(Command("check"))
@require_owner
async def cmd_check(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /check <аккаунт>")
        return
    account = parts[1].strip()
    if account not in sessions:
        await message.answer(f"❌ Аккаунт [{account}] не активен.")
        return
    await message.answer(f"🔍 Проверяю сообщения [{account}]...")
    msgs = await check_new_messages(account)
    if not msgs:
        await message.answer("📭 Новых сообщений нет.")
        return
    for m in msgs:
        await message.answer(
            f"🔔 <b>[{account}]</b>\n"
            f"👤 <b>{m['chat']}</b> · {m['time']}\n"
            f"💬 {m['text']}",
            parse_mode="HTML"
        )


# ================= ЗАПУСК =================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
