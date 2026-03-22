#!/usr/bin/env python3
"""
GTA World (Invision Community) konu izleyici — yeni ileti gelince Discord webhook ile bildirir.

Konu üyelere özel / misafir erişimine kapalıysa tarayıcıdan dışa aktarılmış oturum
çerezleri gerekir (cookies.txt veya COOKIE ortam değişkeni).

Kullanım:
  .env dosyasına DISCORD_WEBHOOK_URL, TOPIC_URL ve COOKIE yaz (proje klasöründe).
  python forum_discord_webhook.py

Ortam değişkenleriyle de verilebilir: DISCORD_WEBHOOK_URL, TOPIC_URL, COOKIE

Tek seferlik kontrol:
  python forum_discord_webhook.py --once

Son iletiyi şimdi Discord'a at + durumu güncelle:
  python forum_discord_webhook.py --notify-latest

Sürekli izleme (POLL_INTERVAL veya --interval, saniye):
  python forum_discord_webhook.py --interval 600

Windows: start_forum_monitor.bat — önce son ileti, sonra 10 dk döngü; Başlangıç klasörüne kısayol eklenebilir.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.exceptions import ChunkedEncodingError
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import SSLError, Timeout


_SCRIPT_DIR = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv

    load_dotenv(_SCRIPT_DIR / ".env")
except ImportError:
    pass

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
STATE_VERSION = 1


def load_netscape_cookies(path: Path) -> requests.cookies.RequestsCookieJar:
    jar = requests.cookies.RequestsCookieJar()
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, path_s, secure, _expiry, name, value = parts[:7]
        secure_bool = secure.upper() == "TRUE"
        jar.set(name, value, domain=domain.lstrip("."), path=path_s, secure=secure_bool)
    return jar


def parse_cookie_header(header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k.lower() in ("secure", "httponly", "samesite"):
            continue
        out[k] = v
    return out


def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": os.environ.get("HTTP_USER_AGENT", DEFAULT_UA),
            "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml",
        }
    )

    cookie_file = os.environ.get("COOKIES_FILE", "").strip()
    if cookie_file:
        p = Path(cookie_file)
        if not p.is_file():
            raise FileNotFoundError(f"COOKIES_FILE bulunamadı: {p}")
        s.cookies.update(load_netscape_cookies(p))

    raw = os.environ.get("COOKIE", "").strip()
    if raw.startswith("Cookie:"):
        raw = raw.split(":", 1)[1].strip()
    if raw:
        s.cookies.update(parse_cookie_header(raw))

    return s


def is_permission_denied(html: str) -> bool:
    lower = html.lower()
    return (
        "you do not have permission" in lower
        or "sorry, you do not have permission" in lower
        or "erişim iznin yok" in lower
        or ("iznin yok" in lower and "sorry" in lower)
    )


def find_last_page_href(soup: BeautifulSoup, base_url: str) -> str | None:
    last_a = soup.select_one("li.ipsPagination_last a")
    if last_a and last_a.get("href"):
        return urljoin(base_url, last_a["href"])
    for a in soup.select("a[href]"):
        if a.get("data-page") == "last" and a.get("href"):
            return urljoin(base_url, a["href"])
    return None


def strip_text(s: str, max_len: int = 350) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s


def _absolute_url(base: str, url: str | None) -> str | None:
    if not url or url.startswith("data:") or url.startswith("blob:"):
        return None
    return urljoin(base, url.strip())


def _img_src(img) -> str | None:
    if not img:
        return None
    for attr in ("data-src", "data-lazy-src", "src"):
        v = img.get(attr)
        if v and not str(v).startswith("data:"):
            return str(v).strip()
    srcset = img.get("srcset")
    if srcset:
        first = srcset.split(",")[0].strip().split()
        if first:
            return first[0]
    return None


def _is_tiny_or_emoji_img(img) -> bool:
    cls = " ".join(img.get("class", []) or []).lower()
    if "cauthorgroupicon" in cls:
        return True
    if any(x in cls for x in ("ipsemoji", "ipssmilie", "emoticon", "emoji")):
        return True
    src = (_img_src(img) or "").lower()
    if any(x in src for x in ("/emoji/", "/emoticons/", "smilie", "blanktheme")):
        return True
    try:
        w = int(img.get("width") or 0)
        h = int(img.get("height") or 0)
        if w and w < 32 and h and h < 32:
            return True
    except ValueError:
        pass
    return False


def _ancestor_chain(el, depth: int = 8) -> list:
    chain: list = []
    node = el
    for _ in range(depth):
        if not node:
            break
        chain.append(node)
        node = getattr(node, "parent", None)
    return chain


def _avatar_from_scope(scope, base_url: str) -> str | None:
    # NOT: select("a","b") geçersiz — sadece ilk string seçici sayılır; hepsi tek tek denenmeli.
    # IPS'te sınıf çoğunlukla <a class="ipsUserPhoto"> üzerinde; <img> üzerinde değil.
    selectors = (
        "aside.ipsComment_author a.ipsUserPhoto img",
        "li.cAuthorPane_photo a.ipsUserPhoto img",
        ".cAuthorPane_photoWrap a.ipsUserPhoto img",
        ".cAuthorPane_photo img",
        "aside.cAuthorPane a.ipsUserPhoto img",
        "a.ipsUserPhoto img",
    )
    for sel in selectors:
        for img in scope.select(sel):
            if _is_tiny_or_emoji_img(img):
                continue
            src = _img_src(img)
            if src:
                return _absolute_url(base_url, src)
    return None


def _author_from_scope(scope, base_url: str) -> str:
    # GTA World IPS: data-ipshover (küçük harf); link h3 > strong > a.ipsType_break
    for sel in (
        "h3.cAuthorPane_author strong a",
        "aside.ipsComment_author h3 strong a",
        "aside.ipsComment_author h3 a",
        "aside.ipsComment_author a.ipsType_break",
        "aside.ipsComment_author a[data-ipshover]",
        "aside.ipsComment_author a[data-ipsHover]",
        ".ipsComment_author h3 a",
        ".ipsComment_author h3 strong a",
        ".ipsComment_author a[href*='profile']",
        ".ipsComment_author a[data-ipshover]",
        ".ipsComment_author a[data-ipsHover]",
        "h3.cAuthorPane_author a",
        ".cAuthorPane_author.ipsType_break a",
        ".cAuthorPane_author a.ipsType_break",
        "li.cAuthorPane_author a",
        ".cAuthorPane .cAuthorPane_author a",
        "[data-role='activeUser'] a",
        ".cAuthorPane_author a[data-ipshover]",
        ".cAuthorPane_author a[data-ipsHover]",
        "a[data-ipshover][href*='profile']",
        "a[data-ipsHover][href*='profile']",
    ):
        el = scope.select_one(sel)
        if el:
            t = strip_text(el.get_text(), 80)
            if t and t not in ("?", "…"):
                return t
    for el in scope.select(".cAuthorPane_author strong, .cAuthorPane_author span.ipsType_break"):
        t = strip_text(el.get_text(), 80)
        if t and t not in ("?", "…"):
            return t
    el = scope.select_one(".cAuthorPane_author, aside.ipsComment_author")
    if el:
        t = strip_text(el.get_text(), 80)
        if t and len(t) >= 2 and t not in ("?", "…"):
            return t
    for a in scope.select("a[href*='profile/'], a[href*='/profile/']"):
        t = strip_text(a.get_text(), 80)
        if t and len(t) >= 2:
            return t
    ph = scope.select_one("a.ipsUserPhoto img[alt], img.ipsUserPhoto[alt]")
    if ph:
        alt = (ph.get("alt") or "").strip()
        if len(alt) >= 2:
            return strip_text(alt, 80)
    return ""


def _author_and_avatar_for_post(chain: list, base_url: str) -> tuple[str, str | None]:
    author = ""
    avatar: str | None = None
    for scope in chain:
        author = _author_from_scope(scope, base_url)
        avatar = _avatar_from_scope(scope, base_url)
        if author and author != "?":
            return author, avatar
    for scope in chain:
        author = _author_from_scope(scope, base_url)
        if author and author != "?":
            avatar = avatar or _avatar_from_scope(scope, base_url)
            return author, avatar
    for scope in chain:
        avatar = _avatar_from_scope(scope, base_url)
        if avatar:
            author = _author_from_scope(scope, base_url) or "?"
            return author, avatar
    if chain:
        author = _author_from_scope(chain[0], base_url) or "?"
        avatar = _avatar_from_scope(chain[0], base_url)
        return author, avatar
    return "?", None


def _body_and_content_image(scope, base_url: str) -> tuple[str, str | None]:
    body_el = scope.select_one(".cPost_contentWrap")
    if not body_el:
        body_el = scope.select_one(".ipsComment_content .ipsType_richText, .ipsType_richText")

    body = strip_text(body_el.get_text()) if body_el else ""
    image_url: str | None = None
    search_root = body_el or scope

    if search_root:
        for img in search_root.select("img"):
            if _is_tiny_or_emoji_img(img):
                continue
            src = _img_src(img)
            if not src:
                continue
            image_url = _absolute_url(base_url, src)
            if image_url:
                break

    if not image_url and search_root:
        for a in search_root.select("a[data-ipsLightbox], a[data-ipslightbox]"):
            href = (a.get("href") or "").strip()
            if href and re.search(r"\.(jpe?g|png|gif|webp)(\?|$)", href, re.I):
                image_url = _absolute_url(base_url, href)
                if image_url:
                    break
        if not image_url:
            for a in search_root.select("a[href*='applications/core/interface/file/attachment']"):
                href = (a.get("href") or "").strip()
                if href:
                    image_url = _absolute_url(base_url, href)
                    if image_url:
                        break

    return body, image_url


def _body_image_walk_chain(chain: list, base_url: str) -> tuple[str, str | None]:
    for scope in chain:
        body, image_url = _body_and_content_image(scope, base_url)
        if body or image_url:
            return body, image_url
    if chain:
        return _body_and_content_image(chain[0], base_url)
    return "", None


def _permalink_for_post(scope: Any, cid_s: str, page_url: str) -> str:
    link_el = scope.select_one(
        f'a[href*="findComment&comment={cid_s}"], a[href*="comment={cid_s}"], '
        f'a[href*="findComment%3D{cid_s}"]'
    )
    if not link_el:
        link_el = scope.select_one("a.cShareLink, li.cTopicInfo a[href*='topic/']")
    if link_el and link_el.get("href"):
        return urljoin(page_url, link_el["href"])
    return page_url


def parse_posts(html: str, page_url: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    roots: dict[str, Any] = {}

    def take(el) -> None:
        cid = el.get("data-commentid") or el.get("data-commentID")
        if cid and str(cid).isdigit():
            roots.setdefault(str(cid), el)

    for sel in (
        "article[data-commentid]",
        "li.ipsComment[data-commentid]",
        "li[class*='ipsComment'][data-commentid]",
        "div.cPost[data-commentid]",
        "[data-commentid].ipsComment",
        "section[data-commentid]",
    ):
        for el in soup.select(sel):
            take(el)

    for el in soup.select("[id^='elComment_']"):
        m = re.match(r"elComment_(\d+)$", el.get("id", ""))
        if m:
            roots.setdefault(m.group(1), el)

    posts: list[dict[str, Any]] = []
    for cid_s in sorted(roots.keys(), key=int):
        root = roots[cid_s]
        chain = _ancestor_chain(root)
        author, avatar_url = _author_and_avatar_for_post(chain, page_url)
        body, image_url = _body_image_walk_chain(chain, page_url)
        permalink = _permalink_for_post(root, cid_s, page_url)

        posts.append(
            {
                "id": int(cid_s),
                "author": author,
                "avatar_url": avatar_url,
                "snippet": body,
                "image_url": image_url,
                "url": permalink,
            }
        )

    if posts:
        return posts

    # Yedek: data-commentid düz metin
    for m in re.finditer(r'data-commentid=["\'](\d+)["\']', html):
        cid = int(m.group(1))
        posts.append(
            {
                "id": cid,
                "author": "?",
                "avatar_url": None,
                "snippet": "",
                "image_url": None,
                "url": page_url,
            }
        )
    posts.sort(key=lambda p: p["id"])
    seen: set[int] = set()
    uniq: list[dict[str, Any]] = []
    for p in posts:
        if p["id"] not in seen:
            seen.add(p["id"])
            uniq.append(p)
    return uniq


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": STATE_VERSION, "seen_ids": [], "topic_url": ""}
    data = json.loads(path.read_text(encoding="utf-8"))
    if "seen_ids" not in data:
        data["seen_ids"] = []
    if "topic_url" not in data:
        data["topic_url"] = ""
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def send_discord(webhook_url: str, topic_title: str, post: dict[str, Any]) -> None:
    title = strip_text(topic_title, 200) if topic_title else "Forum konusu"
    author_name = strip_text(str(post.get("author") or "?"), 256)
    avatar = post.get("avatar_url")
    footer_text = strip_text(
        os.environ.get("DISCORD_EMBED_FOOTER", "Yeni bir ileti gönderdi!"),
        2048,
    )

    # Kompakt görünüm: yazar + pp, konu başlığı (link), altta sabit metin — gövde/snippet ve büyük görsel yok
    embed: dict[str, Any] = {
        "title": title,
        "url": post["url"],
        "color": 0x5865F2,
        "author": {"name": author_name},
        "footer": {"text": footer_text},
    }
    if avatar and str(avatar).startswith("http"):
        embed["author"]["icon_url"] = str(avatar)[:2048]

    payload = {"embeds": [embed]}
    # Geçici ağ/SSL kesintileri (özellikle Windows'ta SSLEOFError) için yeniden dene
    last: Exception | None = None
    for attempt in range(5):
        try:
            r = requests.post(
                webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=(10, 45),
            )
            r.raise_for_status()
            return
        except (SSLError, RequestsConnectionError, Timeout, ChunkedEncodingError) as e:
            last = e
            if attempt < 4:
                time.sleep(min(8.0, 1.2 * (2**attempt)))
    assert last is not None
    raise last


def fetch_topic_page(session: requests.Session, topic_url: str) -> tuple[str, str, str]:
    r = session.get(topic_url, timeout=45)
    r.raise_for_status()
    html = r.text
    if is_permission_denied(html):
        raise PermissionError(
            "Forum sayfasına erişim reddedildi (giriş veya konu izni gerekli). "
            "COOKIE veya COOKIES_FILE ile oturum çerezlerini verin."
        )

    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.select_one("h1.ipsType_pageTitle span, h1.ipsType_pageTitle")
    topic_title = strip_text(title_el.get_text(), 300) if title_el else ""

    last_href = find_last_page_href(soup, topic_url) or topic_url

    if last_href != topic_url:
        r2 = session.get(last_href, timeout=45)
        r2.raise_for_status()
        html = r2.text
        if is_permission_denied(html):
            raise PermissionError("Son sayfa için erişim reddedildi.")
        soup2 = BeautifulSoup(html, "html.parser")
        title_el2 = soup2.select_one("h1.ipsType_pageTitle span, h1.ipsType_pageTitle")
        if title_el2:
            topic_title = strip_text(title_el2.get_text(), 300)

    return html, topic_title, last_href


def run_once(
    session: requests.Session,
    topic_url: str,
    state_path: Path,
    webhook_url: str,
) -> list[dict[str, Any]]:
    html, topic_title, _page_url = fetch_topic_page(session, topic_url)
    posts = parse_posts(html, _page_url)
    if not posts:
        raise RuntimeError(
            "Sayfada ileti bulunamadı. Konu boş, şablon farklı veya HTML yapısı değişmiş olabilir."
        )

    state = load_state(state_path)
    topic_key = topic_url.strip()
    stored_topic = (state.get("topic_url") or "").strip()
    if stored_topic and stored_topic != topic_key:
        state["seen_ids"] = []
    state["topic_url"] = topic_key

    seen: set[int] = set(int(x) for x in state.get("seen_ids", []))

    current_ids = {p["id"] for p in posts}
    new_ids = sorted(current_ids - seen)

    if not seen:
        # İlk çalıştırma (baz): mevcut iletileri kaydet. BASELINE_SEND_LATEST=1 ise en son iletiyi de Discord'a at.
        state["seen_ids"] = sorted(current_ids)
        save_state(state_path, state)
        if os.environ.get("BASELINE_SEND_LATEST", "").strip().lower() in ("1", "true", "yes"):
            latest = max(posts, key=lambda p: p["id"])
            send_discord(webhook_url, topic_title, latest)
            print(f"Baz + son ileti bildirildi: #{latest['id']} — {latest.get('author', '?')}")
        return []

    notifications: list[dict[str, Any]] = []
    id_to_post = {p["id"]: p for p in posts}

    for nid in new_ids:
        p = id_to_post.get(nid)
        if not p:
            # Sayfada yok (çok sayfalı konu); sadece ID ile minimal bildirim
            p = {
                "id": nid,
                "author": "?",
                "avatar_url": None,
                "snippet": "",
                "image_url": None,
                "url": topic_url,
            }
        notifications.append(p)
        send_discord(webhook_url, topic_title, p)

    state["seen_ids"] = sorted(seen | current_ids)
    save_state(state_path, state)
    return notifications


def notify_latest_post(
    session: requests.Session,
    topic_url: str,
    state_path: Path,
    webhook_url: str,
) -> None:
    """Konunun son sayfasındaki en yüksek ID'li iletiyi Discord'a gönderir; seen_ids güncellenir (tekrar bildirilmez)."""
    html, topic_title, page_url = fetch_topic_page(session, topic_url)
    posts = parse_posts(html, page_url)
    if not posts:
        raise RuntimeError(
            "Sayfada ileti bulunamadı. Konu boş, şablon farklı veya HTML yapısı değişmiş olabilir."
        )
    latest = max(posts, key=lambda p: p["id"])
    send_discord(webhook_url, topic_title, latest)

    state = load_state(state_path)
    topic_key = topic_url.strip()
    stored_topic = (state.get("topic_url") or "").strip()
    if stored_topic and stored_topic != topic_key:
        state["seen_ids"] = []
    state["topic_url"] = topic_key
    current_ids = {p["id"] for p in posts}
    seen: set[int] = set(int(x) for x in state.get("seen_ids", []))
    state["seen_ids"] = sorted(seen | current_ids)
    save_state(state_path, state)
    print(f"Son ileti gönderildi: #{latest['id']} — {latest.get('author', '?')}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Forum konusu → Discord webhook izleyici")
    ap.add_argument("--once", action="store_true", help="Tek kontrol sonra çık")
    ap.add_argument(
        "--notify-latest",
        action="store_true",
        help="Son iletiyi bir kez Discord'a gönder, durumu güncelle (yeniden başlatırken kullan)",
    )
    ap.add_argument("--interval", type=int, default=int(os.environ.get("POLL_INTERVAL", "120")), help="Saniye (sürekli mod)")
    ap.add_argument(
        "--state-file",
        default=os.environ.get("STATE_FILE", str(Path(__file__).resolve().parent / ".forum_monitor_state.json")),
    )
    ap.add_argument("--topic-url", default=os.environ.get("TOPIC_URL", "").strip())
    ap.add_argument("--webhook", default=os.environ.get("DISCORD_WEBHOOK_URL", "").strip())
    args = ap.parse_args()

    topic_url = args.topic_url
    webhook_url = args.webhook
    if not topic_url:
        print("TOPIC_URL eksik (--topic-url veya ortam değişkeni).", file=sys.stderr)
        return 2
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL eksik (--webhook veya ortam değişkeni).", file=sys.stderr)
        return 2

    state_path = Path(args.state_file)

    try:
        session = build_session()
    except Exception as e:
        print(f"Oturum hazırlanamadı: {e}", file=sys.stderr)
        return 1

    if args.notify_latest:
        try:
            notify_latest_post(session, topic_url, state_path, webhook_url)
        except PermissionError as e:
            print(f"İzin: {e}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"Hata: {e}", file=sys.stderr)
            return 1
        return 0

    def tick() -> None:
        try:
            notes = run_once(session, topic_url, state_path, webhook_url)
            for p in notes:
                print(f"Yeni ileti #{p['id']} — {p['author']}")
        except PermissionError as e:
            print(f"İzin: {e}", file=sys.stderr)
            raise
        except Exception as e:
            print(f"Hata: {e}", file=sys.stderr)
            raise

    if args.once:
        tick()
        return 0

    print(f"İzleniyor: {topic_url}\nAralık: {args.interval}s\nDurum: {state_path}")
    while True:
        try:
            tick()
        except Exception:
            pass
        time.sleep(max(30, args.interval))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
