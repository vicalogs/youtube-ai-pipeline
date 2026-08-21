# YouTube AI Audio Pipeline

一个偏稳定、低复杂度的 YouTube 财经内容自动采集服务。它将视频任务保存在 PostgreSQL 中，使用 `yt-dlp` 下载最佳音频并转为 MP3，通过 APScheduler 每 30 分钟处理一次待办任务。

## 功能

- 管理频道与 YouTube 视频任务
- 采集频道普通影片、Shorts 和直播目录，包括标题、播放量、地址和缩略图
- 按 YouTube ID 去重，并在重复采集时更新播放量等元数据
- 使用行锁安全领取 `pending` 任务，避免多个实例重复下载
- 使用参数列表调用 `yt-dlp`，不拼接或执行 shell 字符串
- 每个下载周期自动选择20条尚未下载的频道影片
- 下载最佳音频并通过 FFmpeg 转为 MP3，按频道名称分目录保存
- 完整保存成功、失败、重试次数和错误信息
- 文件日志滚动保存到 `logs/app.log`
- 默认失败最多尝试 3 次，失败记录不会被删除
- 使用本地 whisper.cpp 和 Metal 将已下载音频转成中文文字
- 输出兼容旧 Remotion 的 Caption JSON、分页 SRT、TXT 和原始 JSON
- 为 AI 摘要和 Embedding 提供扩展协议

## 目录结构与模块说明

```text
youtube-ai-pipeline/
├── app/
│   ├── main.py                 # 命令行入口与启动错误处理
│   ├── config.py               # 从 .env 读取和校验配置
│   ├── database.py             # PostgreSQL 引擎、会话、初始化与连接检查
│   ├── models.py               # channels、channel_videos、videos、download_logs 模型
│   ├── downloader.py           # 安全调用 yt-dlp 并验证输出文件
│   ├── channel_crawler.py      # 流式采集频道影片 JSON 元数据
│   ├── transcriber.py           # FFmpeg 与 whisper.cpp 本地转录适配器
│   ├── captions.py              # 旧 Caption 格式兼容与分页 SRT
│   ├── scheduler.py            # 每 30 分钟执行下载周期
│   ├── logger.py               # 控制台与滚动文件日志
│   ├── extensions.py           # 转录、摘要、向量接口预留
│   └── services/
│       ├── channel_service.py  # 频道目录 Upsert、查询与下载任务提升
│       ├── video_service.py    # 添加、校验和查询视频任务
│       ├── download_service.py # 领取任务、下载、状态更新与重试
│       └── transcription_service.py # 转录队列、领取、重试与 Worker
├── audio/downloads/            # MP3 输出目录
├── logs/                       # app.log 输出目录
├── tests/                      # 不访问网络的基础单元测试
├── Dockerfile                  # Python 3.12 与 FFmpeg 运行镜像
├── docker-compose.yml          # PostgreSQL 16 与应用服务
├── requirements.txt            # Python 依赖
├── .env                        # 本地开发配置，不应提交
└── .env.example                # 配置模板
```

## 数据状态流

```text
pending -> downloading -> completed
                  |
                  v
                failed -> pending（下一个调度周期且未达重试上限）
```

`MAX_RETRIES=3` 表示最多允许累计 3 次失败。未达到上限的失败任务会在下一个调度周期重新尝试；达到上限后任务保持 `failed`，便于排查和人工处理。

频道采集结果保存在独立的 `channel_videos` 表中。默认每个下载周期会选择其中最新的20条、且尚未在 `videos` 表出现的影片，自动创建 `pending` 任务。已存在的影片不会重复入队。

## 安装方式一：Docker Compose（推荐）

要求：Docker Desktop 或 Docker Engine（支持 Compose v2）。

1. 进入项目目录：

   ```bash
   cd youtube-ai-pipeline
   ```

2. 修改 `.env` 中的开发密码，并添加供 Compose 使用的同一密码：

   ```dotenv
   POSTGRES_PASSWORD=请换成安全密码
   DATABASE_URL=postgresql+psycopg2://youtube:请换成安全密码@localhost:5432/youtube_ai
   ```

   Compose 内的应用会自动使用容器主机名 `postgres`，本机命令仍使用 `localhost`。

3. 构建并启动 PostgreSQL 与定时服务：

   ```bash
   docker compose up -d --build
   ```

4. 查看服务状态与日志：

   ```bash
   docker compose ps
   docker compose logs -f app
   ```

数据库表会在应用启动时自动创建。

## 安装方式二：本机 Python

要求：Python 3.12+、PostgreSQL 16、FFmpeg。

```bash
cd youtube-ai-pipeline
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`，填入真实的 `DATABASE_URL`。确认 PostgreSQL 已启动后：

```bash
python -m app.main check-db
python -m app.main init-db
```

## 添加与查询任务

Docker 运行方式：

```bash
docker compose run --rm app python -m app.main add-video \
  --url "https://www.youtube.com/watch?v=xxxxx" \
  --channel-name "示例财经频道" \
  --channel-url "https://www.youtube.com/@example" \
  --category "财经"

docker compose run --rm app python -m app.main list-videos
docker compose run --rm app python -m app.main list-videos --status failed
```

本机运行时，把 `docker compose run --rm app` 替换为 `python -m app.main`。

频道以 `channel_url` 识别；同一个 `youtube_url` 只能创建一个任务。新任务默认状态为 `pending`。

## 采集频道影片目录

采集普通影片、Shorts 和直播：

```bash
docker compose run --rm app python -m app.main crawl-channel \
  --channel-url "https://www.youtube.com/@vexilla01" \
  --channel-name "我的事务所" \
  --category "时政"
```

只采集普通影片：

```bash
docker compose run --rm app python -m app.main crawl-channel \
  --channel-url "https://www.youtube.com/@example" \
  --tabs videos \
  --max-videos-per-tab 5
```

命令以 JSON 输出本轮结果：

```json
{"channel_id": 1, "discovered": 320, "inserted": 20, "updated": 300}
```

查看采集结果：

```bash
docker compose run --rm app python -m app.main list-channel-videos \
  --channel-url "https://www.youtube.com/@vexilla01" \
  --limit 100
```

输出包含 `youtube_id`、`title`、`view_count`、`youtube_url`、`thumbnail_url`、`source_tab` 和 `published_at`。

也可以绕过自动批次，手动把指定频道影片加入下载队列：

```bash
docker compose run --rm app python -m app.main enqueue-channel-video \
  --youtube-id "影片ID"
```

频道同步采用每行一个 JSON 的流式解析，并以 200 条为一批写入数据库，因此不会一次把 yt-dlp 的全部输出保存在内存中。无法访问或已删除的单个影片会由 yt-dlp 跳过，已成功采集的记录仍会保留。

音频保存结构：

```text
audio/downloads/
├── 老蛮频道/
│   ├── 影片标题1.mp3
│   └── 影片标题2.mp3
└── 其他频道/
    └── 影片标题.mp3
```

频道名称会移除路径分隔符、控制字符和常见非法文件名字符，防止目录穿越或文件保存失败。

本地执行 `crawl-channel` 时会按 `videos`、`shorts`、`streams` 分页显示进度条。YouTube 返回分页总数时会显示百分比、速度和预计剩余时间；总数未知时显示已处理数量。

## 定时分批采集频道

下面的本机命令会为频道建立持久化游标，每30分钟采集下一批10条普通影片：

```bash
python -m app.main schedule-channel \
  --channel-url "https://www.youtube.com/@example" \
  --channel-name "示例财经频道" \
  --category "财经" \
  --tabs videos \
  --batch-size 10 \
  --interval-minutes 30
```

第一次范围是 `1-10`，成功后游标依次推进到 `11-20`、`21-30`。到达频道末尾后自动重置为1，下一轮会重新更新播放量等元数据；重复影片按照 `youtube_id` 覆盖，不会新增重复记录。

查看当前游标和错误：

```bash
python -m app.main list-channel-schedules \
  --channel-url "https://www.youtube.com/@example"
```

不等待定时器，立即运行下一批并显示进度条：

```bash
python -m app.main run-channel-batch \
  --channel-url "https://www.youtube.com/@example" \
  --tab videos
```

修改配置但保留当前游标时，再次执行 `schedule-channel` 即可。需要从第1条重新开始时增加 `--reset`。

暂停：

```bash
python -m app.main set-channel-schedule \
  --channel-url "https://www.youtube.com/@example" \
  --enabled false
```

恢复：

```bash
python -m app.main set-channel-schedule \
  --channel-url "https://www.youtube.com/@example" \
  --enabled true
```

## 启动与单次执行

长期运行调度器：

```bash
python -m app.main scheduler
```

程序启动时会先检查已到期的频道采集批次，然后从 `channel_videos` 自动选择最多20条未入队数据，最后处理全部 `pending` 下载任务。之后每分钟检查一次频道游标，并按 `SCHEDULER_INTERVAL_MINUTES` 周期执行自动入队和下载。每个频道是否采集由其 `interval_minutes` 和 `next_run_at` 决定。

立即执行一轮“自动入队20条 + 下载”并退出：

```bash
python -m app.main run-once
```

## 本地语音转文字

Python项目使用项目目录内的 whisper.cpp 可执行文件和中文 `small` 模型，不再依赖上级项目：

```text
bin/whisper-cli
models/whisper/ggml-small.bin
```

`bin/whisper-cli` 是平台相关文件：当前文件适用于 Apple Silicon macOS。复制项目到其他平台时，需要用目标平台编译的 whisper.cpp `main` 替换它并保持可执行权限。Docker 镜像会在构建阶段自动编译 Linux 版本，无需使用这个 macOS 文件。模型约 465 MB，已被 `.gitignore` 排除；通过压缩包或部署制品传输项目时必须确认模型文件一并包含。

下载成功时会自动创建一条 `pending` 转录任务。历史上已经下载完成、但没有转录任务的影片，也会在 Worker 启动后自动补建。

先处理一条任务进行测试：

```bash
python -m app.main transcribe-once
```

直接指定本地音频文件转写，不创建数据库任务：

```bash
python -m app.main transcribe-audio \
  --audio-path "/绝对路径/音频.mp3" \
  --channel-name "频道名称" \
  --title "节目标题" \
  --progress
```

`--channel-name` 默认是 `direct-audio`，`--title` 默认使用音频文件名。结果写入 `TRANSCRIPT_DIR/<频道>/<标题>/`，命令完成后会输出文本、字幕、SRT 和原始 JSON 文件路径。该命令不查询或写入数据库。

根据 `videos` 表的主键立即转录指定视频（不等待队列顺序）：

```bash
python -m app.main transcribe-video --video-id 110
```

增加 `--progress` 可以显示 FFmpeg 准备阶段和 Whisper 原生识别进度：

```bash
python -m app.main transcribe-video --video-id 110 --progress
```

该视频必须已经下载完成且 `file_path` 指向存在的音频文件。`file_path` 使用相对于 `DOWNLOAD_DIR` 的可移植路径；旧版本保存的绝对路径在项目移动后会自动映射并迁移。已有转录结果时，此命令会重新生成并覆盖该视频标题目录内的结果文件。

持续处理全部转录任务：

```bash
python -m app.main transcription-worker
```

推荐开两个终端：

```text
终端1：python -m app.main scheduler
终端2：python -m app.main transcription-worker
```

`scheduler` 负责频道采集和音频下载；`transcription-worker` 每次只运行一个 Whisper 任务，避免多个模型同时占用Mac内存和计算资源。

查看转录任务：

```bash
python -m app.main list-transcriptions
python -m app.main list-transcriptions --status completed
python -m app.main list-transcriptions --status failed
```

手动重新执行一条失败或被人为中断的任务：

```bash
python -m app.main retry-transcription --task-id 1
```

输出结构：

```text
transcripts/
└── 老蛮频道/
    └── 视频标题/
        ├── captions.json       # 兼容旧 Remotion Caption[]
        ├── transcript.srt      # 30字/6秒/700ms停顿分页
        ├── transcript.txt      # AI摘要和全文检索输入
        └── whisper-raw.json    # 完整词级时间轴和置信度
```

目录名来自 `videos.title`，其中 `/`、`:` 等文件系统非法字符会替换为下划线；标题为空时使用下载音频的文件名。

转录状态为 `pending → transcribing → completed/failed`。失败任务最多重试 `TRANSCRIPTION_MAX_RETRIES` 次；Worker异常退出后，超过 `TRANSCRIPTION_STALE_MINUTES` 的遗留任务会自动恢复。

## 测试方法

不需要网络或数据库的单元测试：

```bash
python -m unittest discover -s tests -v
python -m compileall -q app tests
```

数据库集成测试：

```bash
docker compose up -d postgres
python -m app.main check-db
python -m app.main init-db
python -m app.main add-video \
  --url "https://www.youtube.com/watch?v=真实视频ID" \
  --channel-name "测试频道" \
  --channel-url "https://www.youtube.com/@测试频道" \
  --category "财经"
python -m app.main run-once
python -m app.main list-videos
```

验收时检查：

1. `audio/downloads/` 中出现 `.mp3` 文件。
2. 任务状态变为 `completed`，且 `file_path`、`downloaded_at` 有值。
3. `logs/app.log` 包含启动、下载开始和完成记录。
4. 使用无效视频 URL 后，任务保留并变为 `failed`，`retry_count` 增加，错误写入 `error_message` 与 `download_logs`。

## 配置项

| 变量 | 默认示例 | 说明 |
|---|---|---|
| `DATABASE_URL` | 无 | 必填，PostgreSQL SQLAlchemy URL |
| `DOWNLOAD_DIR` | `audio/downloads` | 音频输出目录 |
| `LOG_FILE` | `logs/app.log` | 日志文件 |
| `SCHEDULER_INTERVAL_MINUTES` | `30` | 下载检查周期 |
| `MAX_RETRIES` | `3` | 允许的失败次数上限 |
| `DOWNLOAD_TIMEOUT_SECONDS` | `3600` | 单个 yt-dlp 进程超时 |
| `LOG_LEVEL` | `INFO` | 日志等级 |
| `CHANNEL_SYNC_TABS` | `videos,shorts,streams` | 默认采集的频道分页 |
| `CHANNEL_BATCH_CHECK_INTERVAL_MINUTES` | `1` | 检查到期频道批次的周期 |
| `AUTO_ENQUEUE_CATALOG` | `true` | 是否自动把频道目录数据加入下载队列 |
| `CATALOG_ENQUEUE_BATCH_SIZE` | `20` | 每个下载周期自动选择的影片数量 |
| `YTDLP_PROXY` | 空 | 可选的 yt-dlp HTTP/SOCKS 代理地址 |
| `WHISPER_CPP_BINARY` | `bin/whisper-cli` | 项目内 whisper.cpp 可执行文件 |
| `WHISPER_MODEL_PATH` | `models/whisper/ggml-small.bin` | 项目内本地模型文件 |
| `WHISPER_MODEL` | `small` | 模型名称及DTW配置 |
| `WHISPER_LANGUAGE` | `zh` | 识别语言 |
| `TRANSCRIPT_DIR` | `transcripts` | 转录文件输出目录 |
| `TRANSCRIPTION_MAX_RETRIES` | `3` | 转录失败次数上限 |
| `TRANSCRIPTION_POLL_SECONDS` | `10` | 空队列轮询周期 |
| `TRANSCRIPTION_TIMEOUT_SECONDS` | `7200` | 单个FFmpeg/Whisper进程超时 |
| `TRANSCRIPTION_STALE_MINUTES` | `120` | 崩溃遗留任务恢复时间 |

## 阶段对应关系

- Phase 1：Compose PostgreSQL、ORM 模型、自动建表、`check-db` 与 `init-db`。
- Phase 2：频道目录采集、`add-video`、`list-videos`、`run-once`、音频下载和状态更新。
- Phase 3：APScheduler、文件/数据库日志、自动重试和异常堆栈。
- Phase 4：本地 whisper.cpp 转录已经实现；`extensions.py` 继续预留摘要和 Embedding 协议，当前不会调用外部 AI 服务。

## 运行注意事项

- `.env` 已被项目内 `.gitignore` 排除；生产环境请使用强密码或 Secret 管理，不要提交密码。
- YouTube 下载依赖网络、视频可用性以及站点策略；某些视频可能需要 Cookies，当前稳定基础版本未自动管理登录凭据。
- 应用不会删除失败任务或已下载音频。重试达到上限后，应先检查 `error_message` 和 `download_logs`。
- 初版使用 SQLAlchemy `create_all` 初始化；模型需要演进时建议后续引入 Alembic 迁移。
