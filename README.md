# Listening Studio

本地英文听力学习工作台：上传视频/音频 → 自动分离音轨 → 转写 → 分句 → 点句复听 → 人工校对。

## 现在能做什么

- 上传视频（mp4/mov/mkv/webm…）或音频（mp3/wav/m4a/flac…），自动用 ffmpeg 抽出 16k 单声道音轨
- faster-whisper 本地转写（默认 small.en，GPU 优先，自动回退 CPU int8）
- 自动分句：词级时间戳 + 标点/停顿/长度约束的全局 DP
- 局部重转写：只对无标点、超长、大空白、低置信的句子重跑，并防止改坏专有名词
- 工作台：
  - 波形 + 句子时间区间，点击句子播放对应音频
  - 单句循环、倍速、空格/↑↓ 快捷键
  - 双击改文本、点词后拆分、合并下一句、单句重转写、标记已核对、边界 +0.1s
- 导出 SRT / TXT

## 运行（Windows）

### 方式一：浏览器

```powershell
cd listening-app
.\start.ps1
```

打开 <http://127.0.0.1:8765>。

### 方式二：桌面窗口（Electron）

```powershell
cd listening-app
npm install
.\dev.cmd
```

`dev.cmd` 会打开独立窗口，并自动拉起本地 Python 服务（随机端口）。
Python 解释器优先用 `LISTENING_PYTHON`，否则找 `.venv`，再找 `../listening-spike/.venv`。

打包安装包：

```powershell
npm run dist
```

注意：安装包只包含 Electron 与前后端源码，**不包含 Python 运行时和模型**；正式分发还需要用 PyInstaller 打后端，或要求机器已装 Python。

日志：

- 引擎：`data/logs/backend.log`
- Electron 启动：`data/logs/electron.log`

首次运行会创建 `.venv` 并安装依赖；模型默认复用 `../listening-spike/out/models/small.en`，
找不到时自动下载到 `data/models`（使用 hf-mirror）。

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `LISTENING_MODEL` | 自动 | 模型名或本地目录 |
| `LISTENING_DEVICE` | `cuda` | `cuda` / `cpu` / `auto` |
| `LISTENING_COMPUTE` | `float16` | GPU 用 `float16`，CPU 建议 `int8` |
| `LISTENING_LANGUAGE` | `en` | 目前只做英文 |

## 目录

```
listening-app/
  backend/app/
    main.py      FastAPI 路由（上传/转写/编辑/音频流/导出）
    worker.py    后台转写任务 + 共享模型
    core.py      ASR、DP 分句、局部重转写（从 spike 沉淀）
    audio.py     ffmpeg 抽轨、时长、波形峰值
    db.py        SQLite（media / transcripts / segments / words）
  frontend/      原生 JS 工作台（无构建步骤）
  data/          SQLite、上传文件、wav、缓存
```

## 已知限制

- 音频播放走单文件 + Range 请求（浏览器按时间窗播放），不是逐句切片
- 转写文本仍可能有同音词/专名错误，用「重转写本句」或手工改
- 首次转写约 1–2 分钟（22 分钟材料），CPU 会明显更慢
- 还没有 Electron 壳；现在是本地网页，Electron 只需加载同一个地址

## 下一步

1. Electron 封装（随机端口 + 健康检查 + 单实例 + 退出杀进程）
2. 波形上直接拖拽句子边界
3. 标点恢复模型 / 双语对照 / 生词本
