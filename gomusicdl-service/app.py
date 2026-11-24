"""gomusicdl HTTP 服务：把 go-music-dl（music-dl web）包装成与 musicdl-service 同契约的音源 API.

go-music-dl 是 Go 实现的多平台音乐搜索/下载工具（直连各平台官方接口），
自带 QQ、B站、汽水等 musicdl Python 库没有的音源。本服务以 sidecar 形式
与其同容器部署：supervisor 拉起 go 二进制（music-dl web，内部端口 8006），
本服务包装其 HTTP 接口（对外端口 8007），供 fnmusic-ext 代理（WS2）调用。

统一曲目 ID 契约与 musicdl-service 完全一致："<source>:<identifier>"，
例如 "qq:5257138"、"bilibili:BVxxx"、"soda:xxx"。

职责划分：
- 搜索：透传 go 的 /music/song_search（本仓库为此新增的 JSON 端点），
  投影成 musicdl-service 条目契约并写入内存缓存。
- 详情：缓存命中 + go 的 /music/download_lrc 取歌词。
- 取流：默认流式透传 go 的 /music/download?stream=1（go 侧自带各源
  鉴权头、汽水/咪咕解密与 Range 支持）；proxy=false 时先经
  /music/inspect 解析直链做 302 重定向，失败回退流式透传。
"""

import json
import logging
import os
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse

logger = logging.getLogger("gomusicdl_service")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

GO_URL = os.environ.get("GOMUSICDL_GO_URL", "http://127.0.0.1:8006").rstrip("/")
DEFAULT_SOURCES = [s.strip().lower() for s in os.environ.get("GOMUSICDL_SOURCES", "qq,bilibili,soda").split(",") if s.strip()]
SEARCH_TIMEOUT = float(os.environ.get("GOMUSICDL_SEARCH_TIMEOUT", "20"))
LIMIT_PER_SOURCE = int(os.environ.get("GOMUSICDL_LIMIT_PER_SOURCE", "10"))
CACHE_MAX = int(os.environ.get("GOMUSICDL_CACHE_MAX", "3000"))
URL_TTL = int(os.environ.get("GOMUSICDL_URL_TTL", "900"))
# go 侧取流/解直链的预算：汽水走签名器+解密，整轨下载可能明显慢于普通直链
RESOLVE_TIMEOUT = float(os.environ.get("GOMUSICDL_RESOLVE_TIMEOUT", "30"))
STREAM_READ_TIMEOUT = float(os.environ.get("GOMUSICDL_STREAM_READ_TIMEOUT", "60"))
# .env 路径（容器内 /repo/.env）：FNMUSIC_GOMUSICDL_SOURCES 在 WebUI 保存后热生效，
# 与 musicdl-service 实时读 .env 的做法一致；无 .env 时回退环境变量/默认值。
ENV_FILE = os.environ.get("GOMUSICDL_ENV_FILE", "/repo/.env")
# go 后端探活预算：healthz 每轮都会探一次，必须足够小，不能拖慢容器健康检查
GO_PROBE_TIMEOUT = float(os.environ.get("GOMUSICDL_GO_PROBE_TIMEOUT", "2.5"))


def _parse_env_file_sources(path: str) -> str:
    """从 .env 里读 FNMUSIC_GOMUSICDL_SOURCES（轻量解析，格式错误静默忽略）。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("FNMUSIC_GOMUSICDL_SOURCES="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _enabled_sources() -> list[str]:
    # 优先级：GOMUSICDL_SOURCES > FNMUSIC_GOMUSICDL_SOURCES（entrypoint 导出的容器 env）
    # > /repo/.env 的 FNMUSIC_GOMUSICDL_SOURCES（WebUI 保存后热生效）> 默认值
    raw = os.environ.get("GOMUSICDL_SOURCES", "").strip()
    if not raw:
        raw = os.environ.get("FNMUSIC_GOMUSICDL_SOURCES", "").strip()
    if not raw:
        raw = _parse_env_file_sources(ENV_FILE)
    sources = [s.strip().lower() for s in raw.split(",") if s.strip()] if raw else DEFAULT_SOURCES
    return sources or DEFAULT_SOURCES

# 与 musicdl-service 的 UNKNOWN_QUALITY 对齐：不猜测档位
UNKNOWN_QUALITY = "unknown"

# 曲目缓存："src:sid" -> {"item": 条目契约, "song": go 原始歌曲, "ts": 时间}
_SONG_CACHE: dict[str, dict[str, Any]] = {}
# 直链缓存："src:sid" -> (过期时间戳, url)
_URL_CACHE: dict[str, tuple[float, str]] = {}
_STATS = {"searches": 0, "streams": 0, "info_hits": 0, "info_miss": 0, "resolve_fails": 0}


def _cache_put(song_id: str, item: dict, song: dict) -> None:
    if len(_SONG_CACHE) >= CACHE_MAX:
        # 简单淘汰：丢最早的 100 条
        for key in list(_SONG_CACHE.keys())[:100]:
            _SONG_CACHE.pop(key, None)
    _SONG_CACHE[song_id] = {"item": item, "song": song, "ts": time.time()}


def _cache_get(song_id: str) -> dict[str, Any] | None:
    return _SONG_CACHE.get(song_id)


def normalize_song(song: dict) -> dict | None:
    """把 go-music-dl 的 model.Song JSON 投影成 musicdl-service 条目契约.

    与 musicdl-service/search_core.normalize 对齐：服务端凭据留在 go 侧，
    对外条目只携带元数据与（可能为空的）download_url。
    """
    if not isinstance(song, dict):
        return None
    src = str(song.get("source") or "").strip().lower()
    sid = str(song.get("id") or "").strip()
    title = str(song.get("name") or "").strip()
    if not src or not sid or not title:
        return None
    ext = str(song.get("ext") or "").strip().lower().lstrip(".") or "mp3"
    item = {
        "id": f"{src}:{sid}",
        "source": src,
        "title": title,
        "artist": str(song.get("artist") or ""),
        "album": str(song.get("album") or ""),
        "duration_s": int(song.get("duration") or 0),
        "ext": ext,
        "file_size": int(song.get("size") or 0),
        "bitrate": int(song.get("bitrate") or 0),
        "samplerate": 0,
        "codec": "",
        "channels": 0,
        # go 侧取流统一走 /music/download?stream=1（HTTP 直链语义，含解密与 Range）
        "protocol": "http",
        "delivery": "direct",
        "unsupported_reason": None,
        "quality": UNKNOWN_QUALITY,
        "cover_url": str(song.get("cover") or ""),
        "link": str(song.get("link") or ""),
        "is_vip": bool(song.get("is_vip")),
    }
    url = str(song.get("url") or "").strip()
    if url.startswith(("http://", "https://")):
        item["download_url"] = url
    return item


def _split_song_id(song_id: str) -> tuple[str, str]:
    parts = (song_id or "").split(":", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise HTTPException(400, f"invalid song id {song_id!r}, expected '<source>:<id>'")
    return parts[0], parts[1]


def _go_song_query(entry: dict[str, Any]) -> dict[str, str]:
    """把缓存条目还原成 go 接口的 song 查询参数（id/source/name/artist/album/extra）。"""
    song = entry.get("song") or {}
    params: dict[str, str] = {
        "id": str(song.get("id") or ""),
        "source": str(song.get("source") or ""),
    }
    for key in ("name", "artist", "album", "cover"):
        value = str(song.get(key) or "").strip()
        if value:
            params[key] = value
    extra = song.get("extra")
    if isinstance(extra, dict) and extra:
        params["extra"] = json.dumps(extra, ensure_ascii=False, separators=(",", ":"))
    return params


def _song_item(entry: dict[str, Any], song_id: str) -> dict:
    item = dict(entry.get("item") or {})
    item.setdefault("id", song_id)
    item["ok"] = True
    return item


app = FastAPI(title="gomusicdl-service")


async def _probe_go() -> tuple[bool, str]:
    """有界探活 go 二进制（web 根路径），返回 (可用, 失败细节).

    只探"HTTP 接口还能应答"，不做真实搜索：supervisor 里 go 进程可能仍是
    RUNNING，但 HTTP 端口已失效（死锁/端口被占/配置库损坏）。适配层若无条件
    返回 ok: true，容器健康检查就永远绿，线上表现为"服务正常但搜不到歌"。
    """
    try:
        async with httpx.AsyncClient(timeout=GO_PROBE_TIMEOUT, follow_redirects=False) as client:
            resp = await client.get(f"{GO_URL}/")
    except Exception as exc:  # noqa: BLE001 — 任何连接/超时异常都算后端不可用
        return False, f"{type(exc).__name__}: {exc}"[:200]
    if resp.status_code >= 500:
        return False, f"go http {resp.status_code}"
    return True, ""


@app.get("/healthz")
async def healthz():
    go_ok, go_detail = await _probe_go()
    payload = {
        "ok": go_ok,
        # go 后端分层结论：与适配层自身存活分开看，方便判断是哪一半坏了
        "go": {"ok": go_ok, "url": GO_URL, "detail": go_detail},
        "go_url": GO_URL,
        "sources": _enabled_sources(),
        "cache_size": len(_SONG_CACHE),
        "url_cache_size": len(_URL_CACHE),
        "stats": _STATS,
    }
    # 后端不可用时返回 503：只认 HTTP 状态的探针（容器 healthcheck）同样能发现
    return JSONResponse(payload, status_code=200 if go_ok else 503)


@app.get("/sources")
async def sources():
    # 声明的白名单 + go 后端存活；不再为了列源去打一次真实搜索（那是 5s 级的
    # 全网请求，状态页每次刷新都触发会明显拖慢管理页）。
    go_ok, go_detail = await _probe_go()
    enabled = _enabled_sources()
    return {
        "enabled": enabled,
        "registered": enabled if go_ok else [],
        "go_ok": go_ok,
        "detail": go_detail,
    }


@app.get("/search")
async def search(
    keyword: str = Query(..., min_length=1),
    limit: int = Query(None, ge=1, le=50),
    sources: str = Query("", description="逗号分隔的源名，空=用默认白名单"),
):
    limit = limit or LIMIT_PER_SOURCE
    requested = [s.strip().lower() for s in sources.split(",") if s.strip()]
    enabled = _enabled_sources()
    use_sources = [s for s in (requested or enabled) if s in enabled] or enabled
    _STATS["searches"] += 1

    params = {
        "q": keyword,
        "limit": limit,
        "sources": ",".join(use_sources),
    }
    errors: dict[str, str] = {}
    items: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT) as client:
            r = await client.get(f"{GO_URL}/music/song_search", params=params)
        if r.status_code != 200:
            errors["gomusicdl"] = f"go search http {r.status_code}"
        else:
            data = r.json()
            for song in data.get("songs") or []:
                item = normalize_song(song)
                if item is None:
                    continue
                _cache_put(item["id"], item, song)
                items.append(item)
            raw_errors = data.get("errors")
            if isinstance(raw_errors, dict):
                errors.update({k: str(v) for k, v in raw_errors.items()})
    except Exception as exc:
        errors["gomusicdl"] = f"{type(exc).__name__}: {exc}"
        logger.warning("search failed for %r: %s", keyword, exc)

    return {
        "ok": True,
        "cached": False,
        "keyword": keyword,
        "items": items,
        "errors": errors,
        "engine": "gomusicdl",
    }


@app.get("/info")
async def info(id: str = Query(..., min_length=1, description="格式 <source>:<id>")):
    entry = _cache_get(id)
    if entry is None:
        _STATS["info_miss"] += 1
        raise HTTPException(
            404,
            f"song id {id!r} not found in cache: cache expired or service restarted; "
            "re-search keyword and source once, then retry",
        )
    _STATS["info_hits"] += 1
    item = _song_item(entry, id)

    # 歌词：go 的 /music/download_lrc 返回纯文本（404 = 无歌词）
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{GO_URL}/music/download_lrc", params=_go_song_query(entry))
        if r.status_code == 200 and r.text.strip():
            item["lyric"] = r.text
        else:
            item["lyric"] = ""
    except Exception as exc:
        logger.warning("lyric fetch failed for %s: %s", id, exc)
        item["lyric"] = ""
    item.setdefault("keyword", "")
    return item


async def _resolve_direct_url(entry: dict[str, Any]) -> str:
    """经 go 的 /music/inspect 解析直链（带 TTL 缓存）。失败返回空串。"""
    song_id = str(entry["item"]["id"])
    cached = _URL_CACHE.get(song_id)
    now = time.time()
    if cached and cached[0] > now:
        return cached[1]
    try:
        async with httpx.AsyncClient(timeout=RESOLVE_TIMEOUT) as client:
            r = await client.get(f"{GO_URL}/music/inspect", params=_go_song_query(entry))
        if r.status_code != 200:
            return ""
        data = r.json()
        if data.get("valid") and str(data.get("url") or "").startswith(("http://", "https://")):
            url = str(data["url"])
            _URL_CACHE[song_id] = (now + URL_TTL, url)
            return url
    except Exception as exc:
        logger.info("inspect resolve failed for %s: %s", song_id, exc)
    _STATS["resolve_fails"] += 1
    return ""


@app.get("/stream")
async def stream(
    id: str = Query(..., min_length=1, description="格式 <source>:<id>"),
    proxy: bool = Query(False, description="true=流式透传；false=优先 302 直链"),
    request: Request = None,  # noqa: RUF013 — FastAPI 自动注入
):
    entry = _cache_get(id)
    if entry is None:
        raise HTTPException(404, f"song id {id!r} not in cache; re-search first")
    _STATS["streams"] += 1
    range_header = request.headers.get("range") if request is not None else None

    if not proxy:
        url = await _resolve_direct_url(entry)
        if url:
            return RedirectResponse(url, status_code=307)
        # 直链解析失败：回退到流式透传，保证播放不中断

    # go 的 /music/download?stream=1 自带：各源鉴权请求头、汽水/咪咕解密、Range 支持
    params = _go_song_query(entry)
    params["stream"] = "1"
    headers: dict[str, str] = {}
    if range_header:
        headers["Range"] = range_header
    timeout = httpx.Timeout(connect=10.0, read=STREAM_READ_TIMEOUT, write=30.0, pool=10.0)

    go_client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    try:
        req = go_client.build_request("GET", f"{GO_URL}/music/download", params=params, headers=headers)
        resp = await go_client.send(req, stream=True)
    except Exception as exc:
        await go_client.aclose()
        logger.warning("go stream failed for %s: %s", id, exc)
        raise HTTPException(502, f"upstream stream failed: {type(exc).__name__}")

    if resp.status_code not in (200, 206):
        body = await resp.aread()
        await resp.aclose()
        await go_client.aclose()
        return Response(
            content=body,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type") or "application/json",
        )

    hop_headers = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "content-encoding"}
    out_headers = {
        k: v for k, v in resp.headers.items()
        if k.lower() not in hop_headers and k.lower() != "content-disposition"
    }

    async def _gen():
        try:
            async for chunk in resp.aiter_bytes(64 * 1024):
                yield chunk
        finally:
            await resp.aclose()
            await go_client.aclose()

    return StreamingResponse(
        _gen(),
        status_code=resp.status_code,
        headers=out_headers,
        media_type=resp.headers.get("content-type") or "application/octet-stream",
    )
