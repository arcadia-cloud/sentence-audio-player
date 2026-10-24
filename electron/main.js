const { app, BrowserWindow, Menu, shell, dialog } = require('electron');
const path = require('path');
const fs = require('fs');
const net = require('net');
const http = require('http');
const { spawn, spawnSync } = require('child_process');

const tracePath = path.join(__dirname, '..', 'data', 'logs', 'electron.log');
function trace(msg) {
  try {
    fs.mkdirSync(path.dirname(tracePath), { recursive: true });
    fs.appendFileSync(tracePath, `[${new Date().toISOString()}] ${msg}\n`);
  } catch (e) { /* ignore */ }
}
trace('module loaded');
app.commandLine.appendSwitch('no-proxy-server');

process.on('uncaughtException', (err) => {
  try {
    const log = path.join(require('os').tmpdir(), 'listening-studio-boot.log');
    fs.appendFileSync(log, `\n[uncaught] ${new Date().toISOString()}\n${err && err.stack ? err.stack : err}\n`);
  } catch (e) { /* ignore */ }
  console.error('[uncaught]', err);
});

const isDev = !app.isPackaged;
let win = null;
let backend = null;
let backendPort = 0;
let quitting = false;
let logStream = null;

function appRoot() {
  if (isDev) return path.resolve(__dirname, '..');
  return app.getAppPath().replace(/app\.asar([\\/])?/, 'app.asar.unpacked$1');
}

function logsDir() {
  const dir = path.join(dataDir(), 'logs');
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

function logPath() {
  return path.join(logsDir(), 'backend.log');
}

function dataDir() {
  const dir = process.env.LISTENING_DATA
    || (isDev ? path.join(appRoot(), 'data') : path.join(app.getPath('userData'), 'data'));
  fs.mkdirSync(path.join(dir, 'uploads'), { recursive: true });
  fs.mkdirSync(path.join(dir, 'audio'), { recursive: true });
  fs.mkdirSync(path.join(dir, 'work'), { recursive: true });
  return dir;
}

function findPython(root) {
  if (process.env.LISTENING_PYTHON) return process.env.LISTENING_PYTHON;
  const candidates = [
    path.join(root, '.venv', 'Scripts', 'python.exe'),
    path.join(root, '.venv', 'bin', 'python'),
    path.join(root, '..', 'listening-spike', '.venv', 'Scripts', 'python.exe'),
  ];
  for (const c of candidates) if (fs.existsSync(c)) return c;
  return process.platform === 'win32' ? 'python' : 'python3';
}

function findModel(root) {
  if (process.env.LISTENING_MODEL) return process.env.LISTENING_MODEL;
  const candidates = [
    path.join(root, '..', 'listening-spike', 'out', 'models', 'small.en'),
    path.join(root, 'models', 'small.en'),
    path.join(process.resourcesPath || '', 'models', 'small.en'),
  ];
  for (const c of candidates) if (c && fs.existsSync(c)) return c;
  return 'small.en';
}

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const port = srv.address().port;
      srv.close(() => resolve(port));
    });
  });
}

function getJson(url, timeoutMs = 2500) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, { timeout: timeoutMs }, (res) => {
      let body = '';
      res.on('data', (c) => { body += c; });
      res.on('end', () => {
        if (res.statusCode && res.statusCode < 400) resolve(body);
        else reject(new Error(`HTTP ${res.statusCode}`));
      });
    });
    req.on('timeout', () => { req.destroy(new Error('timeout')); });
    req.on('error', reject);
  });
}

async function waitForBackend(port, timeoutMs = 120000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      await getJson(`http://127.0.0.1:${port}/api/config`, 1500);
      return true;
    } catch (e) {
      if (backend && backend.exitCode !== null) throw new Error(`backend exited (${backend.exitCode})`);
      await new Promise((r) => setTimeout(r, 400));
    }
  }
  throw new Error('backend startup timed out');
}

function showSplash() {
  const splash = `<!doctype html><html><head><meta charset="utf-8"><style>
    html,body{height:100%;margin:0;background:#0e1116;color:#e6edf3;
      font:14px "Segoe UI",system-ui,sans-serif;display:flex;align-items:center;justify-content:center}
    .box{text-align:center;width:360px}
    .ring{width:34px;height:34px;border:3px solid #26303b;border-top-color:#4aa8ff;
      border-radius:50%;margin:0 auto 18px;animation:spin 1s linear infinite}
    @keyframes spin{to{transform:rotate(360deg)}}
    h1{font-size:18px;margin:0 0 8px;font-weight:600}
    h1 span{color:#4aa8ff}
    p{color:#8b949e;margin:0;font-size:12.5px}
  </style></head><body><div class="box">
    <div class="ring"></div><h1>Listening<span>Studio</span></h1>
    <p>正在启动本地转写引擎…</p><p style="margin-top:8px;font-size:11px">首次启动会加载模型，请稍候</p>
  </div></body></html>`;
  win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(splash));
}

async function startBackend() {
  backendPort = await freePort();
  const root = appRoot();
  const python = findPython(root);
  const model = findModel(root);
  const data = dataDir();
  const backendDir = path.join(root, 'backend');
  const frontendDir = path.join(root, 'frontend');

  logStream = fs.createWriteStream(logPath(), { flags: 'a' });
  logStream.write(`\n\n===== ${new Date().toISOString()} =====\n`);
  logStream.write(`python=${python}\nmodel=${model}\ndata=${data}\nroot=${root}\n`);

  const env = {
    ...process.env,
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
    LISTENING_DATA: data,
    LISTENING_FRONTEND: frontendDir,
    LISTENING_MODEL: model,
    LISTENING_DEVICE: process.env.LISTENING_DEVICE || 'cuda',
  };

  backend = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(backendPort)], {
    cwd: backendDir,
    env,
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  if (backend.stdout) backend.stdout.on('data', (d) => logStream.write(d));
  if (backend.stderr) backend.stderr.on('data', (d) => logStream.write(d));
  backend.on('exit', (code, signal) => {
    logStream?.write(`\nbackend exited code=${code} signal=${signal}\n`);
    if (!quitting && win) {
      dialog.showErrorBox('转写引擎已退出', `退出码 ${code}。\n日志：${logPath()}`);
    }
  });

  await waitForBackend(backendPort);
  return `http://127.0.0.1:${backendPort}`;
}

function stopBackend() {
  if (!backend) return;
  const pid = backend.pid;
  backend = null;
  try {
    if (process.platform === 'win32') {
      spawnSync('taskkill', ['/pid', String(pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
    } else {
      process.kill(pid, 'SIGTERM');
    }
  } catch (e) { /* ignore */ }
}

function buildMenu() {
  const template = [
    {
      label: '文件',
      submenu: [
        { label: '打开数据目录', click: () => shell.openPath(dataDir()) },
        { label: '打开引擎日志', click: () => shell.openPath(logPath()) },
        { type: 'separator' },
        { label: '退出', role: 'quit' },
      ],
    },
    {
      label: '视图',
      submenu: [
        { label: '重新加载', role: 'reload' },
        { label: '开发者工具', role: 'toggleDevTools' },
        { type: 'separator' },
        { label: '实际大小', role: 'resetZoom' },
        { label: '放大', role: 'zoomIn' },
        { label: '缩小', role: 'zoomOut' },
      ],
    },
    {
      label: '帮助',
      submenu: [
        {
          label: '关于',
          click: () => dialog.showMessageBox({
            type: 'info',
            title: 'Listening Studio',
            message: 'Listening Studio 0.1.0',
            detail: `本地英文听力工作台\n引擎：faster-whisper\n数据：${dataDir()}`,
          }),
        },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function createWindow() {
  win = new BrowserWindow({
    width: 1320,
    height: 860,
    minWidth: 1024,
    minHeight: 640,
    backgroundColor: '#0e1116',
    show: false,
    autoHideMenuBar: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  win.once('ready-to-show', () => win.show());
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  win.on('closed', () => { win = null; });
  showSplash();
}

async function boot() {
  trace('boot start root=' + appRoot());
  console.log('[boot] root=', appRoot());
  createWindow();
  buildMenu();
  try {
    const url = process.env.LISTENING_URL || await startBackend();
    trace('backend ready ' + url);
    console.log('[boot] backend ready', url);
    if (win) await win.loadURL(url);
    trace('page loaded');
    win?.show();
  } catch (err) {
    trace('boot failed ' + (err && err.stack ? err.stack : err));
    console.error('[boot] failed', err && err.stack ? err.stack : err);
    fs.appendFileSync(path.join(app.getPath('temp'), 'listening-studio-boot.log'),
      `\n[${new Date().toISOString()}] ${err && err.stack ? err.stack : err}\n`);
    const detail = `${err.message}\n\n日志：${logPath()}`;
    dialog.showErrorBox('启动失败', detail);
    app.quit();
  }
}

const gotLock = app.requestSingleInstanceLock();
trace('lock=' + gotLock);
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });

  app.whenReady().then(() => {
    trace('app ready');
    boot();
  });

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
  });

  app.on('before-quit', () => {
    quitting = true;
    stopBackend();
    logStream?.end();
  });

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) boot();
  });
}
