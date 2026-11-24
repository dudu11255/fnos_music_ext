# gomusicdl-service

go-music-dl 音源 HTTP 服务：把 [go-music-dl](https://github.com/guohuiyuan/go-music-dl)（music-dl web）包装成与 musicdl-service **同契约**的共享音源微服务，为飞牛音乐扩展（WS2 代理）增加 musicdl Python 库没有的音源（默认启用 **qq、bilibili、soda**，可扩展 kugou/kuwo/migu/qianqian/fivesing/jamendo/joox/apple/spotify）。

## 架构

- **两个进程**（supervisor 管理）：
  - `gomusicdl`：预编译的 go 二进制（`bin/music-dl-linux-{amd64,arm64}`，经 `bin/run-music-dl.sh` 按架构选择），跑 `music-dl web`（内部端口 8006），直连各平台官方接口。
  - `gomusicdl-svc`：本服务 FastAPI 适配层（端口 8007），包装 go 的 HTTP 接口，对外暴露 musicdl-service 兼容契约。
- **统一曲目 ID 契约**：`<source>:<identifier>`（例如 `qq:5257138`、`soda:xxx`），与 musicdl-service 互通。
- **取流路径**：默认经 go 的 `/music/download?stream=1` 流式透传（go 侧自带各源鉴权请求头、汽水/咪咕解密、Range 支持）；`proxy=false` 时先经 `/music/inspect` 解析直链做 302。
- **歌词**：go 的 `/music/download_lrc`（纯文本 LRC）。
- **数据目录**：go 侧相对路径 `data/`（`settings.db` 存 Cookie 等设置）由 `bin/run-music-dl.sh` 按 `GOMUSICDL_DATA_DIR` 创建并切入（容器 `/data/gomusicdl`、native `sources-data/gomusicdl`）。仓库里的 `data/` 只是本地联调残留，已在 `.gitignore` / `.dockerignore` 排除。

### 重编译 go 二进制（升级上游时）

在 go-music-dl 仓库根目录执行，产物覆盖本目录 `bin/` 并重新生成校验和：

```bash
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o gomusicdl-service/bin/music-dl-linux-amd64 ./cmd/music-dl
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -ldflags="-s -w" -o gomusicdl-service/bin/music-dl-linux-arm64 ./cmd/music-dl
cd gomusicdl-service/bin && sha256sum music-dl-linux-amd64 music-dl-linux-arm64 > SHA256SUMS
```

> 注意：go 侧 `/music/song_search`（JSON 搜索端点）是本项目在 go-music-dl 中新增的接口（`internal/web/music.go`），升级 go-music-dl 时需带上该改动。
>
> 镜像构建会用 `bin/SHA256SUMS` 校验二进制（`sha256sum --check`）：换二进制必须同时更新校验和，否则构建直接失败——这是仓库内置二进制的唯一来源凭据。升级时请在同一次提交里记录上游 tag/commit 与补丁说明（本文件「重编译」小节），保证可复现。

## API 端点

| 端点 | 方法 | 参数 | 说明 |
| :--- | :--- | :--- | :--- |
| `/healthz` | GET | 无 | 分层健康：适配层存活 + `go` 子对象给出 go 后端探活结论。go 后端不可达/5xx 时返回 **HTTP 503**，`ok:false`（容器 healthcheck 与安装器按状态码判定，避免"进程 RUNNING、接口已死"的假绿） |
| `/sources` | GET | 无 | `enabled`（配置白名单）+ `registered`（go 后端可用时同 enabled）+ `go_ok`；只做有界探活，不再打真实搜索 |
| `/search` | GET | `keyword` (必填), `limit` (可选), `sources` (可选，逗号分隔) | 聚合搜索；条目契约与 musicdl-service 一致 |
| `/info` | GET | `id` (必填，`<source>:<id>`) | 曲目详情 + 歌词 `lyric`；缓存未命中返回 404 |
| `/stream` | GET | `id` (必填), `proxy` (默认 false), `Range` 头 | `proxy=false` 优先 302 直链（失败回退透传）；`proxy=true` 流式透传，支持 Range/206 |

## 环境变量

| 环境变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `GOMUSICDL_GO_URL` | `http://127.0.0.1:8006` | go 二进制 web 服务地址 |
| `GOMUSICDL_GO_PORT` | `8006` | go 二进制监听端口（run-music-dl.sh 读取） |
| `GOMUSICDL_DATA_DIR` | 空（沿用当前工作目录） | go 侧 `data/` 落点；非空时 run-music-dl.sh 会 `mkdir -p` 并切入 |
| `GOMUSICDL_GO_PROBE_TIMEOUT` | `2.5` | `/healthz` 探活 go 后端的超时（秒），必须小于容器健康检查超时 |
| `GOMUSICDL_ENV_FILE` | `/repo/.env` | 读取 `FNMUSIC_GOMUSICDL_SOURCES` 的 .env 路径（WebUI 保存后热生效） |
| `GOMUSICDL_SOURCES` | `qq,bilibili,soda` | 启用音源白名单（英文逗号分隔） |
| `GOMUSICDL_SEARCH_TIMEOUT` | `20` | 搜索超时（秒） |
| `GOMUSICDL_LIMIT_PER_SOURCE` | `10` | 单源默认返回条数 |
| `GOMUSICDL_CACHE_MAX` | `3000` | 曲目缓存最大条目数 |
| `GOMUSICDL_URL_TTL` | `900` | 直链缓存有效期（秒） |
| `GOMUSICDL_RESOLVE_TIMEOUT` | `30` | inspect 解直链超时（秒） |
| `GOMUSICDL_STREAM_READ_TIMEOUT` | `60` | 流式透传读超时（秒），容忍 CDN 限速 |

## 本地运行与测试

```bash
pip install -r requirements.txt

# 单元测试（mock go 服务，无需真实二进制）
python -m pytest test_app.py -q

# 手动联调：先起 go 服务，再起本服务
GOMUSICDL_DATA_DIR="$PWD/data" ./bin/run-music-dl.sh   # 或 music-dl web -p 8006 --no-browser
GOMUSICDL_GO_URL=http://127.0.0.1:8006 uvicorn app:app --host 0.0.0.0 --port 8007

curl "http://127.0.0.1:8007/search?keyword=周杰伦&limit=5"
```
