# Listening Studio

本地运行的英文精听工作台：上传视频/音频 → 自动抽音轨 → Whisper 转写 → 分句对齐 →
点击句子复听 → 人工校对。

所有转写都在本机完成，不上传云端；有 NVIDIA 显卡时自动使用 GPU。

## 功能

- 上传视频（mp4 / mov / mkv / webm…）或音频（mp3 / wav / m4a / flac…），用 ffmpeg 抽出 16k 单声道音轨
- faster-whisper 本地转写，Silero VAD + 词级时间戳
- 全局 DP 分句：综合标点、停顿、长度、大小写与固定短语
- 精度三档：`small.en`（快速）、`medium.en`（标准）、`large-v3`（高精度）
- 45 分钟以上的长音频自动按静音分块、顺序转写、可断点续跑
- 点击句子播放对应音频；Shift + 点击选择连续多句，按选区播放/循环
- 单句循环、倍速、进度条拖拽、空格/↑↓ 快捷键
- 右侧编辑面板：
  - 修改文本、拆分、合并、单句重转写
  - 句尾边界 ±0.1s
  - 标记已核对，顶部显示核对进度
  - 切换句子/点空白自动保存
  - 撤销历史，可查看最近 20 步并回退多步
- 低置信句用更高一档模型重跑
- 素材重命名、数据目录查看/迁移
- 浅色 / 深色 / 跟随系统
- 导出 SRT / TXT
- 可打包为 Windows 免安装 exe（内置 Python、模型、ffmpeg、CUDA）

## 本地开发

```powershell
cd listening-app
.\start.ps1
```

打开 <http://127.0.0.1:8765>。

也可以手动启动：

```powershell
cd backend
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

依赖见 `requirements.txt`。

## 打包 Windows 免安装版

仓库只包含源码；模型、ffmpeg、CUDA 运行库体积较大，需要放到 `packaging/assets/`：

```
packaging/assets/
  bin/       ffmpeg.exe, ffprobe.exe
  cuda/      cublas64_12.dll, cublasLt64_12.dll, cudart64_12.dll, cudnn*.dll
```

然后：

```powershell
python -m PyInstaller packaging/ListeningStudio.spec
```

输出：`dist/ListeningStudio/ListeningStudio.exe`

启动器会自动：

- 在 `%LOCALAPPDATA%\ListeningStudio` 初始化数据目录
- 设置内置模型、ffmpeg、CUDA DLL 路径
- 启动本地后端，并用 Edge `--app` 打开独立窗口
- Edge 不可用时回退默认浏览器

## 数据位置

- 开发模式：`listening-app/data`
- 免安装版：`%LOCALAPPDATA%\ListeningStudio`

里面包含 `app.db`（素材、转写、句子）、`uploads/`、`audio/`、`config.json`。
删除程序目录不会删除数据。

## 模型与精度

| 档位 | 模型 | 说明 |
|---|---|---|
| 快速 | `small.en` | 默认，速度快 |
| 标准 | `medium.en` | 口音/噪声较多时更好 |
| 高精度 | `large-v3` | 最准、最慢，显存占用高 |

首次使用 medium / large 会自动下载模型（约 1.5GB / 3GB）。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `LISTENING_MODEL` | 自动 | 模型名或本地目录 |
| `LISTENING_DEVICE` | `cuda` | `cuda` / `cpu` / `auto` |
| `LISTENING_COMPUTE` | `float16` | GPU 用 `float16`，CPU 用 `int8` |
| `LISTENING_LANGUAGE` | `en` | 目前只做英文 |
| `LISTENING_DATA` | `data` | 数据目录 |
| `LISTENING_FRONTEND` | `frontend` | 前端目录 |

## 目录结构

```
listening-app/
  backend/app/
    main.py      FastAPI 路由（上传/转写/编辑/音频流/导出）
    worker.py    后台任务、模型缓存、精度与长音频分块
    core.py      ASR、DP 分句、局部重转写
    audio.py     ffmpeg 抽轨、时长、波形峰值
    db.py        SQLite（media / transcripts / segments / words）
  frontend/      原生 JS 工作台，无构建步骤
  packaging/     PyInstaller 启动器与 spec
  electron/      早期 Electron 壳（已不作为发行方式）
  data/          开发模式数据
```

## 已知限制

- 只支持英文转写
- 单机单用户，没有账号与云同步
- 撤销记录只在本次后台运行期间保留，最多 20 步
- 长音频分块顺序执行，暂不支持多 GPU 并行
- GitHub Releases 单个附件上限 2GB，3GB+ 的免安装包需要拆分或放网盘

## License

尚未添加 License 文件。
