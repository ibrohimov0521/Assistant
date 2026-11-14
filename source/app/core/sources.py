from __future__ import annotations

import asyncio
import hashlib
from html.parser import HTMLParser
import re
import ipaddress
import json
import socket
import ssl
import time
import unicodedata
from difflib import SequenceMatcher
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from cryptography import x509
from defusedxml.common import DefusedXmlException
from defusedxml import ElementTree as SafeET


@dataclass(frozen=True)
class NewsItem:
    title: str
    source: str
    published: str
    link: str
    topic: str
    other_sources: tuple[tuple[str, str], ...] = ()
    summary: str = ""


class _FeedText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style"}:
            self._hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style"} and self._hidden_depth:
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth:
            self.parts.append(data)


def _feed_summary(value: str) -> str:
    parser = _FeedText()
    try:
        parser.feed(value[:10000])
        parser.close()
    except Exception:
        return ""
    return " ".join(" ".join(parser.parts).split())[:500]


async def validate_public_https_url(url: str) -> list[str]:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("Faqat credential’siz HTTPS feed manzili qabul qilinadi")
    if parts.port not in (None, 443):
        raise ValueError("Feed faqat 443 portida bo‘lishi kerak")
    secret_query_names = {"token", "api_key", "apikey", "secret", "password", "passwd", "auth", "session"}
    if secret_query_names.intersection(key.casefold() for key, _ in parse_qsl(parts.query, keep_blank_values=True)):
        raise ValueError("Maxfiy kalit yoki sessiya parametrli manzil kuzatilmaydi")
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("Feed hosti topilmadi") from exc
    ips = {ipaddress.ip_address(item[4][0].split("%", 1)[0]) for item in addresses}
    if not ips or any(not ip.is_global for ip in ips):
        raise ValueError("Feed private yoki maxsus IP manzilga yo‘nalmoqda")
    return sorted(str(ip) for ip in ips)


class _VisiblePageText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style", "noscript", "svg"}:
            self._hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript", "svg"} and self._hidden_depth:
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth:
            self.parts.append(data)


class _JsonLdScripts(HTMLParser):
    """Collect only bounded JSON-LD script bodies from a public HTML page."""

    MAX_SCRIPTS = 40
    MAX_SCRIPT_CHARS = 200_000

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self._active = False
        self._current: list[str] = []
        self._current_size = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "script" or len(self.scripts) >= self.MAX_SCRIPTS:
            return
        values = {key.casefold(): (value or "") for key, value in attrs}
        content_type = values.get("type", "").split(";", 1)[0].strip().casefold()
        self._active = content_type == "application/ld+json"
        if self._active:
            self._current = []
            self._current_size = 0

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._active:
            value = "".join(self._current)
            if value.strip():
                self.scripts.append(value)
            self._active = False
            self._current = []
            self._current_size = 0

    def handle_data(self, data: str) -> None:
        if not self._active or self._current_size >= self.MAX_SCRIPT_CHARS:
            return
        remaining = self.MAX_SCRIPT_CHARS - self._current_size
        piece = data[:remaining]
        self._current.append(piece)
        self._current_size += len(piece)


def _jsonld_values(value: Any, *, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 8:
        return []
    if isinstance(value, list):
        return [item for child in value[:100] for item in _jsonld_values(child, depth=depth + 1)]
    if not isinstance(value, dict):
        return []
    graph = value.get("@graph")
    own = [value] if value.get("@type") else []
    return own + (_jsonld_values(graph, depth=depth + 1) if graph is not None else [])


def extract_public_structured_items(html: str, page_url: str, *, max_items: int = 50) -> list[dict[str, Any]]:
    """Extract a small allowlisted set of public Schema.org records from JSON-LD."""
    if type(max_items) is not int or not 1 <= max_items <= 50:
        raise ValueError("Tuzilmali yozuvlar soni 1–50 oralig‘ida bo‘lishi kerak.")
    parser = _JsonLdScripts()
    try:
        parser.feed(html[:2 * 1024 * 1024])
        parser.close()
    except Exception:
        return []

    accepted = {"jobposting", "product", "offer", "event", "governmentservice", "dataset",
                "tender", "procurement", "procurementnotice", "contractnotice", "bid"}
    output: list[dict[str, Any]] = []
    seen: set[str] = set()

    def text(value: Any, limit: int = 400) -> str:
        if isinstance(value, dict):
            value = (value.get("name") or value.get("@id") or value.get("value") or
                     " ".join(str(value.get(key) or "") for key in
                              ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry")))
        if isinstance(value, list):
            value = ", ".join(str(text(item, 100)) for item in value[:5] if item is not None)
        if not isinstance(value, (str, int, float)):
            return ""
        return " ".join(str(value).split())[:limit]

    for script in parser.scripts:
        try:
            decoded = json.loads(script)
        except (json.JSONDecodeError, RecursionError):
            continue
        for item in _jsonld_values(decoded):
            raw_types = item.get("@type")
            types = raw_types if isinstance(raw_types, list) else [raw_types]
            kind = next((str(candidate).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
                         for candidate in types
                         if str(candidate).rsplit("/", 1)[-1].rsplit("#", 1)[-1].casefold() in accepted), "")
            if not kind:
                continue
            title = text(item.get("title") or item.get("name") or item.get("headline"), 200)
            raw_url = text(item.get("url") or item.get("@id"), 1000)
            link = urljoin(page_url, raw_url) if raw_url else page_url
            try:
                split_link = urlsplit(link)
                safe_link = (split_link.scheme in {"http", "https"} and bool(split_link.hostname)
                             and not split_link.username and not split_link.password)
            except ValueError:
                safe_link = False
            if not safe_link:
                link = page_url
            offers = item.get("offers")
            if isinstance(offers, list):
                offers = next((entry for entry in offers if isinstance(entry, dict)), {})
            elif not isinstance(offers, dict):
                offers = {}
            location = item.get("jobLocation") or item.get("location")
            if isinstance(location, list):
                location = location[0] if location else ""
            address = location.get("address") if isinstance(location, dict) else None
            location_text = text(address or location, 160)
            record = {
                "type": kind,
                "title": title or kind,
                "url": link[:1000],
                "date": text(item.get("datePosted") or item.get("startDate") or
                             item.get("datePublished") or item.get("releaseDate"), 80),
                "deadline": text(item.get("validThrough") or item.get("endDate") or
                                 item.get("deadline") or item.get("closingDate"), 80),
                "price": text(offers.get("price") or item.get("price"), 80),
                "currency": text(offers.get("priceCurrency") or item.get("priceCurrency"), 12),
                "availability": text(offers.get("availability") or item.get("availability"), 80),
                "location": location_text,
                "description": text(item.get("description") or item.get("abstract"), 500),
            }
            stable_identity = f"{kind.casefold()}|{record['url']}|{title.casefold()}"
            identifier = hashlib.sha256(stable_identity.encode("utf-8")).hexdigest()[:20]
            if identifier in seen:
                continue
            seen.add(identifier)
            record["id"] = identifier
            record["content_sha256"] = hashlib.sha256(
                json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            output.append(record)
            if len(output) >= max_items:
                return output
    return output


async def fetch_public_page_text(url: str, *, client: httpx.AsyncClient | None = None,
                                max_bytes: int = 2 * 1024 * 1024) -> str:
    """Fetch a public HTTPS page as bounded, visible text; redirects are never followed."""
    await validate_public_https_url(url)
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=10.0, follow_redirects=False, trust_env=False,
                                       headers={"User-Agent": "PersonalTelegramAssistant/0.1"})
    try:
        async with http.stream("GET", url) as response:
            if response.is_redirect:
                raise ValueError("Sayt manzili yo‘naltiradi; kuzatuv bekor qilindi")
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").casefold()
            if not content_type.startswith(("text/html", "application/xhtml+xml", "text/plain")):
                raise ValueError("Sayt matn yoki HTML sahifa qaytarmadi")
            length = response.headers.get("content-length")
            if length and int(length) > max_bytes:
                raise ValueError("Sahifa 2 MB chegaradan katta")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("Sahifa 2 MB chegaradan katta")
                chunks.append(chunk)
            raw = b"".join(chunks)
            encoding = response.encoding or "utf-8"
            try:
                decoded = raw.decode(encoding, errors="replace")
            except LookupError:
                decoded = raw.decode("utf-8", errors="replace")
            if content_type.startswith("text/plain"):
                visible = " ".join(decoded.split())
            else:
                parser = _VisiblePageText()
                parser.feed(decoded)
                parser.close()
                visible = " ".join(" ".join(parser.parts).split())
            return visible[:50000]
    finally:
        if owns_client:
            await http.aclose()


def fetch_youtube_transcript(url: str, *, max_chars: int = 24000) -> dict[str, Any]:
    """Fetch public YouTube captions without a paid API key; never downloads video/audio."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").casefold().rstrip(".")
        if (parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443)
                or host not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}):
            return {"error": "Faqat youtube.com yoki youtu.be dagi ommaviy HTTPS video havolasini yuboring."}
    except ValueError:
        return {"error": "YouTube havolasi noto‘g‘ri."}
    if host == "youtu.be":
        candidate = parts.path.strip("/").split("/", 1)[0]
    elif parts.path == "/watch":
        candidate = dict(parse_qsl(parts.query, keep_blank_values=True)).get("v", "")
    else:
        match = re.fullmatch(r"/(?:shorts|embed|live)/([A-Za-z0-9_-]{11})/?", parts.path)
        candidate = match.group(1) if match else ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate):
        return {"error": "YouTube video ID sini havoladan aniqlay olmadim."}
    if type(max_chars) is not int or not 500 <= max_chars <= 24000:
        return {"error": "Transkript hajmi ruxsat etilgan chegarada emas."}

    from requests import Session
    from youtube_transcript_api import YouTubeTranscriptApi

    session = Session()
    session.trust_env = False
    session.headers.update({"User-Agent": "PersonalTelegramAssistant/0.1"})
    try:
        fetched = YouTubeTranscriptApi(http_client=session).fetch(candidate, languages=["uz", "ru", "en"])
        lines: list[str] = []
        size = 0
        truncated = False
        for snippet in fetched.snippets:
            seconds = max(0, int(float(snippet.start)))
            stamp = f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 else f"{seconds // 60:02d}:{seconds % 60:02d}"
            snippet_text = " ".join(str(snippet.text or "").split())
            if not snippet_text:
                continue
            line = f"[{stamp}] {snippet_text}"
            if size + len(line) + 1 > max_chars:
                truncated = True
                break
            lines.append(line)
            size += len(line) + 1
        transcript = "\n".join(lines)
        if truncated:
            transcript += "\n[Transkript hajm cheklovi sabab qisqartirildi.]"
        if not transcript.strip():
            return {"error": "Videoda o‘qiladigan subtitr topilmadi."}
        return {"video_id": candidate, "transcript": transcript,
                "language": str(getattr(fetched, "language_code", "noma’lum"))[:16],
                "generated": bool(getattr(fetched, "is_generated", False)), "truncated": truncated}
    except Exception as exc:
        error_kind = type(exc).__name__
        if error_kind in {"TranscriptsDisabled", "NoTranscriptFound", "VideoUnavailable", "PoTokenRequired"}:
            message = "Videoda ochiq transkript yo‘q yoki YouTube uni bermadi. Transkriptni yuborsangiz tahlil qilaman."
        elif error_kind in {"IpBlocked", "RequestBlocked"}:
            message = "YouTube server IP manzilidan transkript olishni vaqtincha chekladi. Keyinroq qayta urinib ko‘ring yoki transkriptni yuboring."
        else:
            message = "YouTube transkriptini hozir olib bo‘lmadi. Keyinroq urinib ko‘ring yoki transkriptni yuboring."
        return {"error": message, "error_type": error_kind}
    finally:
        session.close()


async def probe_public_page(url: str, *, client: httpx.AsyncClient | None = None,
                            max_bytes: int = 2 * 1024 * 1024) -> dict[str, Any]:
    """Read a public HTTPS page and return bounded availability/content measurements."""
    await validate_public_https_url(url)
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=10.0, follow_redirects=False, trust_env=False,
                                       headers={"User-Agent": "PersonalTelegramAssistant/0.1"})
    started = time.perf_counter()
    try:
        async with http.stream("GET", url) as response:
            status = response.status_code
            # A redirect proves that the host answered, but is deliberately not followed.
            if response.is_redirect or status >= 400:
                return {"status_code": status, "response_time_ms": round((time.perf_counter() - started) * 1000),
                        "is_up": status < 400, "text": None,
                        "error": f"http_status_{status}" if status >= 400 else "redirect"}
            content_type = response.headers.get("content-type", "").casefold()
            if not content_type.startswith(("text/html", "application/xhtml+xml", "text/plain")):
                return {"status_code": status, "response_time_ms": round((time.perf_counter() - started) * 1000),
                        "is_up": True, "text": None, "error": "unsupported_content_type"}
            length = response.headers.get("content-length")
            if length and int(length) > max_bytes:
                return {"status_code": status, "response_time_ms": round((time.perf_counter() - started) * 1000),
                        "is_up": True, "text": None, "error": "page_too_large"}
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    return {"status_code": status, "response_time_ms": round((time.perf_counter() - started) * 1000),
                            "is_up": True, "text": None, "error": "page_too_large"}
                chunks.append(chunk)
            raw = b"".join(chunks)
            encoding = response.encoding or "utf-8"
            try:
                decoded = raw.decode(encoding, errors="replace")
            except LookupError:
                decoded = raw.decode("utf-8", errors="replace")
            if content_type.startswith("text/plain"):
                visible = " ".join(decoded.split())
            else:
                parser = _VisiblePageText()
                parser.feed(decoded)
                parser.close()
                visible = " ".join(" ".join(parser.parts).split())
            return {"status_code": status, "response_time_ms": round((time.perf_counter() - started) * 1000),
                    "is_up": True, "text": visible[:50000], "error": None,
                    "structured_items": (extract_public_structured_items(decoded, url)
                                         if content_type.startswith(("text/html", "application/xhtml+xml")) else [])}
    finally:
        if owns_client:
            await http.aclose()


async def public_certificate_expiry(url: str) -> str | None:
    """Read the peer certificate's expiry from a resolved public IP without trusting it."""
    parts = urlsplit(url)
    ips = await validate_public_https_url(url)
    hostname = (parts.hostname or "").encode("idna").decode("ascii")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    last_error: Exception | None = None
    for address in ips:
        writer = None
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host=address, port=443, ssl=context, server_hostname=hostname), timeout=8.0)
            ssl_socket = writer.get_extra_info("ssl_object")
            der = ssl_socket.getpeercert(binary_form=True) if ssl_socket else None
            if not der:
                continue
            certificate = x509.load_der_x509_certificate(der)
            return certificate.not_valid_after_utc.astimezone(UTC).isoformat(timespec="seconds")
        except Exception as exc:
            last_error = exc
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except (OSError, ssl.SSLError):
                    pass
    if last_error:
        raise last_error
    return None


_RDAP_BOOTSTRAP_CACHE: tuple[float, list[tuple[str, str]]] | None = None
_RDAP_DOMAIN_CACHE: dict[str, tuple[float, str | None]] = {}


async def _rdap_services(*, client: httpx.AsyncClient) -> list[tuple[str, str]]:
    global _RDAP_BOOTSTRAP_CACHE
    now = time.monotonic()
    if _RDAP_BOOTSTRAP_CACHE and now - _RDAP_BOOTSTRAP_CACHE[0] < 86400:
        return _RDAP_BOOTSTRAP_CACHE[1]
    response = await client.get("https://data.iana.org/rdap/dns.json")
    response.raise_for_status()
    document = response.json()
    services: list[tuple[str, str]] = []
    for item in document.get("services", []):
        if not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], list) or not item[1]:
            continue
        for suffix in item[0]:
            if isinstance(suffix, str):
                services.append((suffix.casefold().strip("."), str(item[1][0]).rstrip("/")))
    services.sort(key=lambda row: len(row[0]), reverse=True)
    _RDAP_BOOTSTRAP_CACHE = (now, services)
    return services


async def public_domain_expiry(hostname: str, *, client: httpx.AsyncClient | None = None) -> str | None:
    """Look up a registered domain's published expiry event using IANA RDAP bootstrap."""
    host = hostname.strip().rstrip(".").encode("idna").decode("ascii").casefold()
    if not host or len(host) > 253:
        return None
    cached = _RDAP_DOMAIN_CACHE.get(host)
    if cached and time.monotonic() - cached[0] < 86400:
        return cached[1]
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=8.0, follow_redirects=False, trust_env=False,
                                       headers={"Accept": "application/rdap+json, application/json"})
    try:
        services = await _rdap_services(client=http)
        labels = host.split(".")
        matching = next(((suffix, base) for suffix, base in services
                         if host == suffix or host.endswith("." + suffix)), None)
        if not matching:
            _RDAP_DOMAIN_CACHE[host] = (time.monotonic(), None)
            return None
        suffix, base = matching
        min_labels = len(suffix.split(".")) + 1
        if len(labels) < min_labels:
            _RDAP_DOMAIN_CACHE[host] = (time.monotonic(), None)
            return None
        for index in range(max(0, len(labels) - min_labels - 7), len(labels) - min_labels + 1):
            candidate = ".".join(labels[index:])
            response = await http.get(f"{base}/domain/{quote(candidate, safe='.')}")
            if response.status_code == 404:
                continue
            if response.status_code != 200:
                return None
            try:
                document = response.json()
            except ValueError:
                return None
            for event in document.get("events", []):
                if str(event.get("eventAction", "")).casefold() != "expiration":
                    continue
                raw = str(event.get("eventDate") or "")
                try:
                    expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if expires.tzinfo is not None:
                    result = expires.astimezone(UTC).isoformat(timespec="seconds")
                    _RDAP_DOMAIN_CACHE[host] = (time.monotonic(), result)
                    return result
            _RDAP_DOMAIN_CACHE[host] = (time.monotonic(), None)
            return None
        _RDAP_DOMAIN_CACHE[host] = (time.monotonic(), None)
        return None
    except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError):
        return None
    finally:
        if owns_client:
            await http.aclose()


def _news_date(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            result = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if result.tzinfo is None:
        return None
    return result.astimezone(UTC)


def _news_url_key(value: str) -> str:
    parts = urlsplit(value)
    if not parts.hostname:
        return value
    host = parts.hostname.casefold().removeprefix("www.")
    path = parts.path.rstrip("/") or "/"
    query = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
             if not key.casefold().startswith("utm_")
             and key.casefold() not in {"fbclid", "gclid", "yclid", "ref"}]
    return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))


def _news_title_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"[\w]+", normalized, flags=re.UNICODE))


def _same_news_story(left: NewsItem, right: NewsItem) -> bool:
    if _news_url_key(left.link) == _news_url_key(right.link):
        return True
    left_key, right_key = _news_title_key(left.title), _news_title_key(right.title)
    if not left_key or not right_key:
        return False
    left_date, right_date = _news_date(left.published), _news_date(right.published)
    if left_date and right_date and abs(left_date - right_date) > timedelta(days=3):
        return False
    if left_key == right_key:
        return True
    return min(len(left_key), len(right_key)) >= 40 and SequenceMatcher(None, left_key, right_key).ratio() >= 0.92


def _group_news(items: list[NewsItem], max_items: int) -> list[NewsItem]:
    groups: list[list[NewsItem]] = []
    for item in items:
        match = next((group for group in groups if any(_same_news_story(item, prior) for prior in group)), None)
        if match is None:
            groups.append([item])
        else:
            match.append(item)
    grouped: list[NewsItem] = []
    for group in groups:
        primary = group[0]
        published_group = sorted(group, key=lambda item: _news_date(item.published) or datetime.min.replace(tzinfo=UTC),
                                 reverse=True)
        newest = published_group[0]
        source_links: list[tuple[str, str]] = []
        for member in group:
            for source_link in ((member.source, member.link), *member.other_sources):
                if source_link not in source_links:
                    source_links.append(source_link)
        other = tuple(pair for pair in source_links if pair != (newest.source, newest.link))
        grouped.append(NewsItem(newest.title, newest.source, newest.published, newest.link,
                                newest.topic or primary.topic, other, newest.summary))
    return grouped[:max_items]


async def load_news(path: Path, *, max_items: int = 5, client: httpx.AsyncClient | None = None,
                    topic: str | None = None, region: str | None = None,
                    language: str | None = None, source: str | None = None,
                    published_after: datetime | None = None) -> list[NewsItem]:
    if max_items <= 0:
        return []
    if not path.exists():
        return []
    try:
        feed_configs = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(feed_configs, list):
        return []
    feed_configs = feed_configs[:20]
    # Keep a small candidate set from each feed so a busy first publisher
    # cannot consume the whole digest before later feeds are checked.
    fetch_limit = min(100, max_items * 2)
    threshold = None
    if published_after is not None:
        threshold = (published_after if published_after.tzinfo else published_after.replace(tzinfo=UTC)).astimezone(UTC)

    def matches_filter(value: Any, expected: str | None) -> bool:
        return (expected is None or expected.strip() == "*"
                or str(value or "").strip().casefold() == expected.strip().casefold())

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=8.0, follow_redirects=False, trust_env=False,
                                       headers={"User-Agent": "PersonalTelegramAssistant/0.1"})
    items_by_feed: list[list[NewsItem]] = []
    try:
        for config in feed_configs:
            if not isinstance(config, dict):
                continue
            if (not matches_filter(config.get("topic"), topic)
                    or not matches_filter(config.get("region"), region)
                    or not matches_filter(config.get("language"), language)
                    or not matches_filter(config.get("name"), source)):
                continue
            url = str(config["https_url"])
            feed_items: list[NewsItem] = []
            try:
                await validate_public_https_url(url)
                async with http.stream("GET", url) as response:
                    if response.is_redirect:
                        continue
                    response.raise_for_status()
                    length = response.headers.get("content-length")
                    if length and int(length) > 1024 * 1024:
                        continue
                    if not response.headers.get("content-type", "").lower().startswith(("application/rss+xml", "application/atom+xml", "application/xml", "text/xml")):
                        continue
                    chunks, total = [], 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > 1024 * 1024:
                            chunks = []
                            break
                        chunks.append(chunk)
                    if not chunks:
                        continue
                    root = SafeET.fromstring(b"".join(chunks))
                for element in root.iter():
                    tag = element.tag.rsplit("}", 1)[-1].lower()
                    if tag not in {"item", "entry"}:
                        continue
                    values = {child.tag.rsplit("}", 1)[-1].lower(): (child.text or "").strip()
                              for child in list(element)}
                    link = next((
                        (child.text or "").strip() or str(child.attrib.get("href") or "").strip()
                        for child in list(element)
                        if child.tag.rsplit("}", 1)[-1].lower() == "link"
                        and ((child.text or "").strip() or child.attrib.get("href"))
                    ), values.get("link", ""))
                    if urlsplit(link).scheme != "https":
                        continue
                    title = values.get("title", "").strip()
                    if title:
                        published = values.get("pubdate") or values.get("published") or values.get("updated", "")
                        summary = _feed_summary(values.get("description") or values.get("summary")
                                                or values.get("content", ""))
                        published_at = _news_date(published)
                        if threshold is not None and (published_at is None or published_at < threshold):
                            continue
                        feed_items.append(NewsItem(title[:300], str(config["name"])[:100],
                                                   published[:50],
                                                   link[:2048], str(config.get("topic", "Umumiy"))[:80],
                                                   summary=summary))
                        if len(feed_items) >= fetch_limit:
                            break
                if feed_items:
                    items_by_feed.append(feed_items)
            except (httpx.HTTPError, OSError, ValueError, KeyError, SafeET.ParseError, DefusedXmlException):
                continue
    finally:
        if owns_client:
            await http.aclose()
    # Interleave feeds before deduplication. If two feeds cover the same story,
    # _group_news still keeps the newest copy and both publisher links.
    items = [item for index in range(fetch_limit) for feed in items_by_feed
             if index < len(feed) for item in (feed[index],)]
    return _group_news(items, max_items)


@dataclass(frozen=True)
class WeatherForecast:
    text: str
    available: bool = False
    city: str | None = None
    forecast_date: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    precipitation_probability: float | None = None
    wind_speed: float | None = None
    forecast_hour: str | None = None


class WeatherService:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client
        self._cache: dict[tuple[str, str, str | None], tuple[datetime, WeatherForecast]] = {}

    async def forecast(self, city: str, *, tomorrow: bool = False, hour: str | None = None) -> str:
        return (await self.forecast_result(city, tomorrow=tomorrow, hour=hour)).text

    async def forecast_result(self, city: str, *, tomorrow: bool = False,
                              hour: str | None = None,
                              target_date: date | None = None,
                              force_refresh: bool = False) -> WeatherForecast:
        if hour is not None and not re.fullmatch(r"(?:[01]\d|2[0-3]):00", hour):
            return WeatherForecast("Soatni Toshkent vaqti bilan to‘liq soatda, masalan 09:00, yozing.")
        local_today = datetime.now(ZoneInfo("Asia/Tashkent")).date()
        target = target_date or (local_today + timedelta(days=1 if tomorrow else 0))
        day_offset = (target - local_today).days
        if day_offset < 0 or day_offset > 2:
            return WeatherForecast("Open-Meteo hozirgi kundan keyingi ikki kungacha prognoz beradi; tanlangan sana bu oraliqda emas.")
        cache_key = (city.casefold(), target.isoformat(), hour)
        cached = self._cache.get(cache_key)
        if cached and cached[0] > datetime.now(UTC) and not force_refresh:
            return cached[1]
        owns_client = self.client is None
        http = self.client or httpx.AsyncClient(timeout=8.0, follow_redirects=False, trust_env=False)
        try:
            geo = await http.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": city, "count": 5, "language": "uz", "format": "json"})
            geo.raise_for_status()
            results = geo.json().get("results", [])
            if not results:
                return WeatherForecast(f"{city} shahri topilmadi.")
            normalized = city.casefold().strip()
            normalized = {"tashkent": "toshkent", "samarkand": "samarqand",
                          "bukhara": "buxoro"}.get(normalized, normalized)
            choices = [item for item in results if item.get("name", "").casefold() == normalized]
            if not choices:
                names = ", ".join(_place_label(item, city) for item in results[:5])
                return WeatherForecast(f"{city} nomli shahar topilmadi. Yaqin natijalar: {names}.")

            # Open-Meteo can return multiple exact-name settlements, including
            # tiny localities. For one country, prefer a clearly dominant city
            # population (for example, Toshkent city over other Toshkent entries).
            unique: dict[tuple[float, float, str], dict] = {}
            for item in choices:
                key = (round(float(item["latitude"]), 3), round(float(item["longitude"]), 3),
                       str(item.get("country_code", "")))
                current = unique.get(key)
                if current is None or (item.get("population") or 0) > (current.get("population") or 0):
                    unique[key] = item
            choices = list(unique.values())
            if len(choices) == 1:
                place = choices[0]
            else:
                country_codes = {str(item.get("country_code", "")) for item in choices}
                ranked = sorted(choices, key=lambda item: item.get("population") or 0, reverse=True)
                top_population = ranked[0].get("population") or 0
                next_population = ranked[1].get("population") or 0
                if len(country_codes) == 1 and top_population > 0 and (
                        next_population == 0 or top_population >= next_population * 5):
                    place = ranked[0]
                else:
                    names = ", ".join(_place_label(item, city) for item in choices[:5])
                    return WeatherForecast(f"Shahar noaniq. Qaysi birini nazarda tutdingiz: {names}?")
            resp = await http.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": place["latitude"], "longitude": place["longitude"],
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max",
                "hourly": "temperature_2m,precipitation_probability,wind_speed_10m",
                "timezone": "Asia/Tashkent", "forecast_days": 3,
            })
            resp.raise_for_status()
            data = resp.json().get("daily", {})
            ix = day_offset
            fields = ("time", "temperature_2m_min", "temperature_2m_max", "precipitation_probability_max")
            if any(len(data.get(field, [])) <= ix for field in fields):
                return WeatherForecast("Ob-havo prognozi hozircha topilmadi.")
            day, minimum, maximum, rain = (data[field][ix] for field in fields)
            if not day or any(value is None for value in (minimum, maximum, rain)):
                return WeatherForecast("Ob-havo prognozi hozircha topilmadi.")
            name = place["name"]
            wind_values = data.get("wind_speed_10m_max", [])
            wind_max = wind_values[ix] if len(wind_values) > ix else None
            if hour is not None:
                hourly = resp.json().get("hourly", {})
                times = hourly.get("time", [])
                match = next((i for i, value in enumerate(times)
                              if str(value) == f"{day}T{hour}"), None)
                if match is None:
                    return WeatherForecast(f"{day} kuni {hour} uchun soatlik prognoz topilmadi.")
                temperature_values = hourly.get("temperature_2m", [])
                rain_values = hourly.get("precipitation_probability", [])
                hourly_wind_values = hourly.get("wind_speed_10m", [])
                if any(len(values) <= match for values in
                       (temperature_values, rain_values, hourly_wind_values)):
                    return WeatherForecast("Soatlik ob-havo ma’lumoti hozircha topilmadi.")
                temperature, rain_hour, wind_hour = (values[match] for values in
                    (temperature_values, rain_values, hourly_wind_values))
                if any(value is None for value in (temperature, rain_hour, wind_hour)):
                    return WeatherForecast("Soatlik ob-havo ma’lumoti hozircha topilmadi.")
                answer = (f"{name} · {day}, Toshkent vaqti bilan {hour}\n"
                          f"Harorat: {temperature} °C\nYog‘ingarchilik ehtimoli: {rain_hour}%\n"
                          f"Shamol: {wind_hour} km/soat\nManba: Open-Meteo")
                result = WeatherForecast(answer, available=True, city=str(name), forecast_date=str(day),
                    minimum=float(temperature), maximum=float(temperature),
                    precipitation_probability=float(rain_hour), wind_speed=float(wind_hour), forecast_hour=hour)
                self._cache[cache_key] = (datetime.now(UTC) + timedelta(minutes=10), result)
                return result
            wind_line = (f"\nShamol tezligi (kunlik maksimum): {wind_max} km/soat"
                         if wind_max is not None else "")
            answer = (f"{name} · {day}\nHarorat: {minimum}–{maximum} °C\n"
                      f"Yog‘ingarchilik ehtimoli: {rain}%{wind_line}\nManba: Open-Meteo")
            result = WeatherForecast(answer, available=True, city=str(name), forecast_date=str(day),
                                     minimum=float(minimum), maximum=float(maximum),
                                     precipitation_probability=float(rain),
                                     wind_speed=float(wind_max) if wind_max is not None else None)
            self._cache[cache_key] = (datetime.now(UTC) + timedelta(minutes=10), result)
            return result
        except (httpx.HTTPError, KeyError, ValueError, IndexError, TypeError):
            return WeatherForecast("Ob-havo xizmati vaqtincha ishlamayapti.")
        finally:
            if owns_client:
                await http.aclose()


def _place_label(item: dict, fallback: str) -> str:
    parts = []
    for value in (item.get("name") or fallback, item.get("admin1"), item.get("country")):
        label = str(value or "").strip()
        if label and label.casefold() not in {part.casefold() for part in parts}:
            parts.append(label)
    return ", ".join(parts)


_FX_CACHE: dict[tuple[str, str], tuple[datetime, dict[str, Any]]] = {}


async def get_exchange_rate(base: str, quote: str, amount: str | None = None, *,
                            client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Get a no-key reference rate, preferring Uzbekistan CBU for UZS pairs."""
    base, quote = str(base or "").strip().upper(), str(quote or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", base) or not re.fullmatch(r"[A-Z]{3}", quote):
        return {"error": "Valyuta kodlarini uchta harf bilan yozing, masalan USD va UZS."}
    if base == quote:
        rate = Decimal(1)
        source, rate_date = "Bir xil valyuta", datetime.now(UTC).date().isoformat()
        payload = {"base": base, "quote": quote, "rate": "1", "date": rate_date,
                   "source": source, "reference_rate": True}
    else:
        cache_key = (base, quote)
        cached = _FX_CACHE.get(cache_key)
        if cached and cached[0] > datetime.now(UTC):
            payload = dict(cached[1])
        else:
            owns_client = client is None
            http = client or httpx.AsyncClient(timeout=8.0, follow_redirects=False, trust_env=False)
            try:
                if "UZS" in {base, quote}:
                    response = await http.get("https://cbu.uz/uz/arkhiv-kursov-valyut/json/")
                    if len(response.content) > 1_000_000:
                        return {"error": "Markaziy bank kurslari javobi kutilgan hajmdan katta."}
                    response.raise_for_status()
                    entries = response.json()
                    if not isinstance(entries, list):
                        return {"error": "Markaziy bank kurslari javobi noto‘g‘ri formatda."}
                    per_uzs: dict[str, Decimal] = {"UZS": Decimal(1)}
                    rate_dates: list[str] = []
                    for item in entries[:300]:
                        if not isinstance(item, dict):
                            continue
                        code = str(item.get("Ccy") or "").upper()
                        try:
                            nominal = Decimal(str(item.get("Nominal")))
                            official_rate = Decimal(str(item.get("Rate")))
                        except (InvalidOperation, TypeError):
                            continue
                        if re.fullmatch(r"[A-Z]{3}", code) and nominal > 0 and official_rate > 0:
                            per_uzs[code] = official_rate / nominal
                            if item.get("Date"):
                                rate_dates.append(str(item["Date"])[:20])
                    if base not in per_uzs or quote not in per_uzs:
                        return {"error": "Markaziy bank manbasida bu valyuta juftligidan biri topilmadi."}
                    rate = per_uzs[base] / per_uzs[quote]
                    source = "O‘zbekiston Markaziy banki"
                    rate_date = max(rate_dates) if rate_dates else "sana ko‘rsatilmagan"
                else:
                    response = await http.get(
                        f"https://api.frankfurter.dev/v2/rate/{base.lower()}/{quote.lower()}")
                    if len(response.content) > 100_000:
                        return {"error": "Valyuta kursi javobi kutilgan hajmdan katta."}
                    response.raise_for_status()
                    item = response.json()
                    rate = Decimal(str(item["rate"]))
                    source = "Frankfurter reference rates"
                    rate_date = str(item.get("date") or "sana ko‘rsatilmagan")[:20]
                if not rate.is_finite() or rate <= 0:
                    return {"error": "Valyuta kursi manbasi yaroqli qiymat bermadi."}
                payload = {"base": base, "quote": quote, "rate": format(rate.normalize(), "f"),
                           "date": rate_date, "source": source, "reference_rate": True}
                _FX_CACHE[cache_key] = (datetime.now(UTC) + timedelta(hours=1), dict(payload))
            except (httpx.HTTPError, ValueError, KeyError, TypeError, InvalidOperation):
                return {"error": "Valyuta kursi manbasi vaqtincha ishlamayapti."}
            finally:
                if owns_client:
                    await http.aclose()
    raw_amount = str(amount or "").strip()
    if raw_amount:
        try:
            cleaned = re.sub(r"\s+", "", raw_amount)
            if "," in cleaned and "." in cleaned:
                if cleaned.rfind(",") > cleaned.rfind("."):
                    cleaned = cleaned.replace(".", "").replace(",", ".")
                else:
                    cleaned = cleaned.replace(",", "")
            elif "," in cleaned:
                groups = cleaned.split(",")
                if len(groups) > 2:
                    if not (1 <= len(groups[0]) <= 3 and all(len(part) == 3 for part in groups[1:])):
                        raise InvalidOperation
                    cleaned = "".join(groups)
                elif len(groups[-1]) == 3 and 1 <= len(groups[0].lstrip("+-")) <= 3:
                    cleaned = "".join(groups)
                elif len(groups[-1]) in {1, 2}:
                    cleaned = ".".join(groups)
                else:
                    raise InvalidOperation
            value = Decimal(cleaned)
        except InvalidOperation:
            return {"error": "Summani raqam sifatida aniqlay olmadim."}
        if not value.is_finite() or value <= 0 or value > Decimal("999999999999999999"):
            return {"error": "Summa juda katta yoki noto‘g‘ri."}
        converted = value * Decimal(str(payload["rate"]))
        payload["amount"] = format(value.normalize(), "f")
        payload["converted"] = format(converted.quantize(Decimal("0.01")), "f")
    return payload

