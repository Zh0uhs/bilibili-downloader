# -*- coding: utf-8 -*-
"""
B 站批量视频下载器 v1.3
========================
现代化暗色科技感 **桌面窗口**（Tkinter，无需浏览器）
- 扫码登录 / Cookie 粘贴 / 浏览器 Cookie 导入
- 多行链接批量下载（每行一个）
- 实时任务列表：状态、进度、速度、ETA
- 大会员码流支持（4K / 1080p+）
- 暗色霓虹主题（青蓝 + 品红渐变 + 发光按钮）
"""

from __future__ import annotations

import os
import re
import sys
import queue
import threading
import time
import io
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Dict, Any

try:
    import yt_dlp
except ImportError:  # pragma: no cover
    yt_dlp = None

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

try:
    import qrcode
    from PIL import Image as PILImage, ImageTk
    HAS_QR = True
except ImportError:
    HAS_QR = False

import tkinter as tk
from tkinter import ttk, filedialog, messagebox


# =========================================================================
# 常量 & 路径
# =========================================================================

APP_VERSION = 'v1.4'
APP_TITLE = f'B 站批量视频下载器 · {APP_VERSION}'

APP_HOME = Path(os.path.expanduser('~')) / '.bili_downloader'
APP_HOME.mkdir(parents=True, exist_ok=True)
COOKIE_FILE = APP_HOME / 'bilibili_cookies.txt'
LOG_FILE = APP_HOME / 'run.log'

def _secure_path(p: Path, is_dir: bool = False) -> None:
    """尽可能收紧文件/目录权限，防止其他进程读取 Cookie 等敏感数据。"""
    try:
        if sys.platform.startswith('win'):
            import ctypes
            FILE_ATTRIBUTE_HIDDEN = 0x02
            # 隐藏文件/目录（Windows 无原生权限控制，退而求其次）
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(p))
            if attrs != -1:
                ctypes.windll.kernel32.SetFileAttributesW(str(p), attrs | FILE_ATTRIBUTE_HIDDEN)
        else:
            # Unix / macOS：用户可读写，其他人无权限
            p.chmod(0o700 if is_dir else 0o600)
    except Exception:
        pass  # 非关键路径，失败不影响主功能


BILI_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
           'AppleWebKit/537.36 (KHTML, like Gecko) '
           'Chrome/128.0.0.0 Safari/537.36')

QR_GENERATE_URL = 'https://passport.bilibili.com/x/passport-login/web/qrcode/generate'
QR_POLL_URL = 'https://passport.bilibili.com/x/passport-login/web/qrcode/poll'
NAV_URL = 'https://api.bilibili.com/x/web-interface/nav'

# 主题色（霓虹科技）
C_BG       = '#0b1020'
C_BG_ALT   = '#0c1328'
C_PANEL    = '#111833'
C_PANEL_LT = '#172149'
C_LINE     = '#1f2a4a'
C_CYAN     = '#00d4ff'
C_MAGENTA  = '#ff00aa'
C_GREEN    = '#00ffa3'
C_AMBER    = '#ffb300'
C_RED      = '#ff5577'
C_TEXT     = '#e7f2ff'
C_MUTED    = '#7a89b3'
C_INPUT_BG = '#0a0f20'
C_INPUT_FG = '#ffffff'


# =========================================================================
# 格式化工具函数
# =========================================================================

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')


def strip_ansi(s: str) -> str:
    """去除字符串中的 ANSI 转义序列（yt-dlp 输出可能包含颜色码）。"""
    if not s:
        return ''
    return _ANSI_RE.sub('', str(s)).strip()


def format_bytes(num_bytes: Optional[int]) -> str:
    """将字节数格式化为人类可读字符串（B / KB / MB / GB）。"""
    if num_bytes is None or num_bytes <= 0:
        return '—'
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    size = float(num_bytes)
    idx = 0
    while size >= 1024.0 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    if idx == 0:
        return f'{int(size)}{units[idx]}'
    return f'{size:.2f}{units[idx]}'


def format_size_dual(downloaded: Optional[int], total: Optional[int]) -> str:
    """格式化 "当前 / 总大小"，如 12.34MB / 128.00MB。"""
    if total and total > 0:
        return f'{format_bytes(downloaded)} / {format_bytes(total)}'
    return format_bytes(downloaded)


def format_speed(bytes_per_sec: Optional[float]) -> str:
    """格式化速度。"""
    if bytes_per_sec is None or bytes_per_sec <= 0:
        return '—'
    return f'{format_bytes(int(bytes_per_sec))}/s'


def format_eta(seconds: Optional[float]) -> str:
    """将秒数格式化为 MM:SS / HH:MM:SS。"""
    if seconds is None or seconds < 0:
        return '—'
    sec = int(seconds)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f'{h:02d}:{m:02d}:{s:02d}'
    return f'{m:02d}:{s:02d}'


def mask_username(name: str) -> str:
    """对用户名脱敏：仅保留首字符，其余替换为 ***。"""
    if not name:
        return '(未知)'
    return name[0] + '***'


# =========================================================================
# 任务数据
# =========================================================================

@dataclass
class Task:
    id: int
    url: str
    title: str = ''
    status: str = '等待'      # 等待 / 下载中 / 已完成 / 失败 / 已取消
    pct: float = 0.0          # 0.0 ~ 100.0
    size: str = ''            # 格式化后的 "当前 / 总大小" 字符串
    speed: str = ''           # 格式化后的速度字符串
    eta: str = ''             # 格式化后的 ETA
    output: str = ''
    error: str = ''
    started_at: str = ''
    finished_at: str = ''
    # 原始数值（用于计算 / 重新渲染）
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed_bytes: float = 0.0
    eta_seconds: float = 0.0


# =========================================================================
# 全局状态（线程安全）
# =========================================================================

class AppState:
    def __init__(self):
        self._lock = threading.RLock()
        self._tasks: List[Task] = []
        self._next_id = 1
        self._log_lines: List[str] = []
        self._downloading = False
        self._cancelled = False
        self._session = requests.Session() if requests else None
        if self._session:
            self._session.headers.update({
                'User-Agent': BILI_UA,
                'Referer': 'https://www.bilibili.com/',
                'Accept': 'application/json, text/plain, */*',
                'Accept-Language': 'zh-CN,zh;q=0.9',
            })
        self._qr_key: Optional[str] = None
        self._username: Optional[str] = None
        self._try_restore_user()

    # ----- 任务 -----
    def add_tasks(self, urls: List[str]) -> int:
        with self._lock:
            for u in urls:
                self._tasks.append(Task(id=self._next_id, url=u))
                self._next_id += 1
            return len(urls)

    def tasks(self) -> List[Task]:
        with self._lock:
            return list(self._tasks)

    def reset_tasks(self) -> None:
        with self._lock:
            if self._downloading:
                return
            self._tasks = []
            self._next_id = 1

    def update_task(self, tid: int, **fields) -> None:
        with self._lock:
            for t in self._tasks:
                if t.id == tid:
                    for k, v in fields.items():
                        setattr(t, k, v)
                    return

    # ----- 下载状态 -----
    def set_downloading(self, flag: bool) -> None:
        with self._lock:
            self._downloading = flag

    def is_downloading(self) -> bool:
        with self._lock:
            return self._downloading

    def set_cancelled(self, flag: bool = True) -> None:
        with self._lock:
            self._cancelled = flag

    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    # ----- 用户状态 -----
    def set_username(self, name: Optional[str]) -> None:
        with self._lock:
            self._username = name

    def username(self) -> Optional[str]:
        with self._lock:
            return self._username

    def cookie_has_login(self) -> bool:
        if not COOKIE_FILE.exists():
            return False
        try:
            txt = COOKIE_FILE.read_text(encoding='utf-8', errors='ignore')
            return 'SESSDATA' in txt and 'bili_jct' in txt
        except OSError:
            return False

    def _try_restore_user(self) -> None:
        if not self.cookie_has_login() or self._session is None:
            return
        try:
            jar = _read_cookie_jar(str(COOKIE_FILE))
            if jar is None:
                return
            for k, v in jar.items():
                self._session.cookies.set(k, v, domain='.bilibili.com')
            data = self._session.get(NAV_URL, timeout=6).json()
            if data.get('code') == 0 and data.get('data', {}).get('uname'):
                self._username = data['data']['uname']
                self.log(f'自动恢复登录状态：{mask_username(self._username)}', '登录')
        except Exception as e:
            self.log(f'恢复 Cookie 失败: {e}', 'WARN')

    # ----- 日志 -----
    def log(self, msg: str, tag: str = 'INFO') -> None:
        ts = datetime.now().strftime('%H:%M:%S')
        line = f'[{ts}] [{tag}] {msg}'
        with self._lock:
            self._log_lines.append(line)
            if len(self._log_lines) > 3000:
                self._log_lines = self._log_lines[-3000:]

    def log_tail(self, n: int = 200) -> str:
        with self._lock:
            return '\n'.join(self._log_lines[-n:])


STATE = AppState()


def _read_cookie_jar(path: str) -> Optional[Dict[str, str]]:
    if not COOKIE_FILE.exists():
        return None
    jar = {}
    for line in Path(path).read_text(encoding='utf-8', errors='ignore').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split('\t')
        if len(parts) >= 7:
            jar[parts[5]] = parts[6]
        else:
            m = re.match(r'^([A-Za-z0-9_\-]+)\s*=\s*(.+)$', line.rstrip(';'))
            if m:
                jar[m.group(1)] = m.group(2).strip().strip('"')
    return jar


# =========================================================================
# 二维码登录（单独弹窗）
# =========================================================================

def generate_qr_bytes(content: str, box_size: int = 10, border: int = 2) -> bytes:
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=box_size, border=border)
    qr.add_data(content)
    qr.make(fit=True)
    img = qr.make_image(fill_color=C_CYAN, back_color=C_BG).convert('RGB')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


def fetch_qr() -> Optional[str]:
    if not HAS_QR or STATE._session is None:
        return None
    try:
        resp = STATE._session.get(QR_GENERATE_URL, timeout=8)
        data = resp.json()
        if data.get('code') != 0:
            return None
        STATE._qr_key = data['data']['qrcode_key']
        return data['data']['url']
    except Exception as e:
        STATE.log(f'获取二维码失败: {e}', 'ERROR')
        return None


def poll_qr() -> str:
    """轮询状态，返回 'pending' / 'scanned' / 'expired' / 'success' / 'error' / 'none'"""
    if not STATE._qr_key or STATE._session is None:
        return 'none'
    try:
        resp = STATE._session.get(QR_POLL_URL,
                                   params={'qrcode_key': STATE._qr_key},
                                   timeout=8)
        data = resp.json()
        if data.get('code') != 0:
            return 'error'
        poll_code = data.get('data', {}).get('code')
        if poll_code == 0:
            # 登录成功 — 把 session 里的 cookie 写到磁盘
            jar = STATE._session.cookies
            lines = ['# Netscape HTTP Cookie File',
                     '# Generated by bili_downloader ' + APP_VERSION, '']
            for cookie in jar:
                domain = cookie.domain or '.bilibili.com'
                if 'bilibili.com' not in domain and 'b23.tv' not in domain:
                    continue
                secure = 'TRUE' if cookie.secure else 'FALSE'
                path_ = cookie.path or '/'
                expires = int(cookie.expires) if cookie.expires else 0
                lines.append(f'{domain}\tTRUE\t{path_}\t{secure}\t{expires}\t{cookie.name}\t{cookie.value}')
            COOKIE_FILE.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            _secure_path(COOKIE_FILE)
            _secure_path(APP_HOME, is_dir=True)
            # 更新用户名
            try:
                nd = STATE._session.get(NAV_URL, timeout=6).json()
                if nd.get('code') == 0 and nd.get('data', {}).get('uname'):
                    STATE.set_username(nd['data']['uname'])
            except Exception:
                pass
            return 'success'
        if poll_code == 86101:
            return 'pending'
        if poll_code == 86090:
            return 'scanned'
        if poll_code == 86038:
            return 'expired'
        return 'error'
    except Exception as e:
        STATE.log(f'轮询失败: {e}', 'ERROR')
        return 'error'


class QRLoginDialog(tk.Toplevel):
    """扫码登录弹窗（独立窗口）。"""

    def __init__(self, master, on_success=None):
        super().__init__(master)
        self.title('B 站 · 扫码登录')
        self.configure(bg=C_BG)
        self.geometry('420x540')
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        self._on_success = on_success
        self._qr_img = None
        self._build_ui()
        self._refresh_qr()

    def _build_ui(self):
        tk.Label(self, text='🔐 B 站扫码登录', bg=C_BG, fg=C_CYAN,
                 font=('Microsoft YaHei UI', 15, 'bold')).pack(pady=(18, 4))
        tk.Label(self,
                 text='用 B 站 App 扫描下方二维码，确认登录后可获得大会员码流',
                 bg=C_BG, fg=C_MUTED, font=('Microsoft YaHei UI', 9)).pack(pady=(0, 14))

        # 二维码画布（带发光边框）
        self.qr_canvas = tk.Canvas(self, width=300, height=300, bg=C_BG_ALT,
                                   highlightthickness=2, highlightbackground=C_CYAN,
                                   bd=0, cursor='arrow')
        self.qr_canvas.pack(pady=6)
        self.qr_canvas.bind('<Configure>', lambda e: self._redraw_qr())

        self.status_var = tk.StringVar(value='正在生成二维码…')
        self.status_label = tk.Label(self, textvariable=self.status_var,
                                     bg=C_BG, fg=C_TEXT,
                                     font=('Microsoft YaHei UI', 10, 'bold'))
        self.status_label.pack(pady=(10, 4))

        self.tip_label = tk.Label(self, text='',
                                  bg=C_BG, fg=C_MUTED,
                                  font=('Microsoft YaHei UI', 9))
        self.tip_label.pack(pady=(0, 10))

        btn_row = tk.Frame(self, bg=C_BG)
        btn_row.pack(pady=6)
        self._make_neon_button(btn_row, '🔄 刷新二维码', C_CYAN, self._refresh_qr
                               ).pack(side='left', padx=8)
        self._make_neon_button(btn_row, '✖ 关闭', C_AMBER, self.destroy
                               ).pack(side='left', padx=8)

        self.protocol('WM_DELETE_WINDOW', self.destroy)
        self.after(1500, self._poll_loop)

    def _make_neon_button(self, parent, text, color, cmd):
        btn = tk.Button(parent, text=text, command=cmd,
                        bg=C_PANEL, fg=color,
                        font=('Microsoft YaHei UI', 10, 'bold'),
                        relief='flat', bd=0, padx=16, pady=8,
                        activebackground=C_PANEL_LT, activeforeground=color,
                        cursor='hand2')
        return btn

    def _refresh_qr(self):
        url = fetch_qr()
        if not url:
            self.status_var.set('❌ 无法获取二维码，请检查网络')
            return
        try:
            raw = generate_qr_bytes(url, box_size=9, border=2)
            img = PILImage.open(io.BytesIO(raw)).resize((290, 290))
            self._qr_img = ImageTk.PhotoImage(img)
            self._redraw_qr()
            self.status_var.set('⏳ 请用 B 站 App 扫码（3 分钟内有效）')
        except Exception as e:
            self.status_var.set(f'❌ 生成二维码失败: {e}')

    def _redraw_qr(self):
        self.qr_canvas.delete('all')
        if self._qr_img is not None:
            w = self.qr_canvas.winfo_width() or 300
            h = self.qr_canvas.winfo_height() or 300
            self.qr_canvas.create_image(w // 2, h // 2, image=self._qr_img)

    def _poll_loop(self):
        status = poll_qr()
        if status == 'success':
            uname = STATE.username() or '未知用户'
            self.status_var.set(f'✅ 登录成功: {uname}')
            self.tip_label.config(text='Cookie 已保存到 ~/.bili_downloader/bilibili_cookies.txt')
            STATE.log(f'扫码登录成功: {mask_username(uname)}', '登录')
            if self._on_success:
                try:
                    self._on_success()
                except Exception:
                    pass
            self.after(1500, self.destroy)
            return
        if status == 'scanned':
            self.status_var.set('👀 已扫描，请在手机上点「确认」')
        elif status == 'pending':
            self.status_var.set('⏳ 等待扫码…')
        elif status == 'expired':
            self.status_var.set('⌛ 二维码已过期，请点「刷新二维码」')
        elif status == 'error':
            self.status_var.set('❌ 轮询出错，请点「刷新二维码」重试')
        elif status == 'none':
            self.status_var.set('⚠ 未生成二维码')
        self.after(2000, self._poll_loop)


# =========================================================================
# Cookie 粘贴对话框
# =========================================================================

class CookiePasteDialog(tk.Toplevel):
    def __init__(self, master, on_success=None):
        super().__init__(master)
        self.title('粘贴 Cookie')
        self.configure(bg=C_BG)
        self.geometry('560x420')
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        self._on_success = on_success

        tk.Label(self, text='📝 粘贴 B 站 Cookie（需包含 SESSDATA 与 bili_jct）',
                 bg=C_BG, fg=C_TEXT,
                 font=('Microsoft YaHei UI', 11, 'bold')).pack(pady=(16, 8), padx=16, anchor='w')

        self.txt = tk.Text(self, bg=C_INPUT_BG, fg=C_INPUT_FG,
                           insertbackground=C_CYAN, font=('Consolas', 10),
                           bd=0, highlightthickness=1,
                           highlightbackground=C_LINE,
                           highlightcolor=C_CYAN, wrap='word', height=14)
        self.txt.pack(fill='both', expand=True, padx=16, pady=(0, 8))
        self.txt.insert('1.0',
                        '# 粘贴 SESSDATA / bili_jct 等 Cookie\n'
                        'SESSDATA=xxx;\nbili_jct=yyy;\nDedeUserID=123;\n')

        self.status_var = tk.StringVar(value='')
        tk.Label(self, textvariable=self.status_var, bg=C_BG, fg=C_AMBER,
                 font=('Microsoft YaHei UI', 9)).pack(pady=2)

        btn_row = tk.Frame(self, bg=C_BG)
        btn_row.pack(pady=10)
        tk.Button(btn_row, text='💾 保存并校验', command=self._save,
                  bg=C_PANEL, fg=C_CYAN,
                  font=('Microsoft YaHei UI', 10, 'bold'),
                  relief='flat', bd=0, padx=18, pady=8, cursor='hand2',
                  activebackground=C_PANEL_LT, activeforeground=C_CYAN
                  ).pack(side='left', padx=8)
        tk.Button(btn_row, text='取消', command=self.destroy,
                  bg=C_PANEL, fg=C_MUTED,
                  font=('Microsoft YaHei UI', 10),
                  relief='flat', bd=0, padx=18, pady=8, cursor='hand2',
                  activebackground=C_PANEL_LT, activeforeground=C_MUTED
                  ).pack(side='left', padx=8)

    def _save(self):
        text = self.txt.get('1.0', 'end')
        lines = []
        for raw in text.splitlines():
            raw = raw.strip()
            if not raw or raw.startswith('#'):
                continue
            parts = raw.split('\t')
            if len(parts) >= 7:
                lines.append(raw)
                continue
            m = re.match(r'^([A-Za-z0-9_\-]+)\s*=\s*(.+)$', raw.rstrip(';'))
            if m:
                name, value = m.group(1), m.group(2).strip().strip('"').rstrip(';').strip()
                lines.append(f'.bilibili.com\tTRUE\t/\tFALSE\t0\t{name}\t{value}')
        header = ['# Netscape HTTP Cookie File', '# Generated by bili_downloader ' + APP_VERSION, '']
        content = '\n'.join(header + lines) + '\n'
        if 'SESSDATA' not in content or 'bili_jct' not in content:
            self.status_var.set('❌ 未检测到 SESSDATA 或 bili_jct')
            return
        COOKIE_FILE.write_text(content, encoding='utf-8')
        _secure_path(COOKIE_FILE)
        _secure_path(APP_HOME, is_dir=True)
        # 重新读用户名
        STATE._try_restore_user()
        STATE.log('已保存并校验 Cookie', '登录')
        self.status_var.set('✅ 已保存')
        if self._on_success:
            try:
                self._on_success()
            except Exception:
                pass
        self.after(800, self.destroy)


# =========================================================================
# 下载核心
# =========================================================================

def _progress_hook_factory(tid: int):
    def hook(d: Dict[str, Any]):
        status = d.get('status')

        # —— 从整数（yt-dlp 提供的字段 —— #
        downloaded = d.get('downloaded_bytes') or d.get('_downloaded_bytes') or 0
        total = d.get('total_bytes') or d.get('_total_bytes') or d.get('_total_bytes_estimate') or 0
        speed_bytes = d.get('speed') or d.get('_speed_bytes') or 0
        eta_sec = d.get('eta') or d.get('_eta_seconds') or 0

        # 百分比：优先从整数计算；若总大小未知，使用 _percent_str 兜底
        pct = 0.0
        if total and total > 0:
            pct = round(float(downloaded) * 100.0 / float(total), 2)
        else:
            raw_pct = strip_ansi(str(d.get('_percent_str') or '0%'))
            try:
                cleaned = raw_pct.strip('%').strip()
                if cleaned:
                    pct = round(float(cleaned), 2)
            except ValueError:
                pct = 0.0
        pct = max(0.0, min(100.0, pct))

        # 所有从 yt-dlp 可能返回的字符串去除 ANSI 颜色码
        info = d.get('info_dict') or {}
        title = ''
        for key in ('title', 'webpage_url_basename', 'id'):
            val = info.get(key)
            if val:
                title = str(val)
                break
        # 清理标题（防止污染 Treeview 换行
        title = title.strip().replace('\n', ' ').replace('\r', ' ').strip()
        if len(title) > 120:
            title = title[:117] + '...'

        # 格式化显示字符串（size/speed/eta）
        total_int = int(total) if total else 0
        down_int = int(downloaded) if downloaded else 0
        size_str = format_size_dual(down_int, total_int)
        speed_str = format_speed(float(speed_bytes) if speed_bytes else 0.0)
        eta_str = format_eta(float(eta_sec) if eta_sec else 0.0)

        if status == 'downloading':
            STATE.update_task(tid,
                              status='下载中',
                              pct=pct,
                              size=size_str,
                              speed=speed_str,
                              eta=eta_str,
                              title=title,
                              downloaded_bytes=down_int,
                              total_bytes=total_int)
        elif status == 'finished':
            STATE.update_task(tid,
                            pct=100.0,
                            size=format_bytes(down_int) if down_int else '完成',
                            downloaded_bytes=down_int,
                            total_bytes=total_int)
    return hook


def download_one(url: str, out_dir: str, quality: str, mute: bool, tid: int) -> str:
    if yt_dlp is None:
        raise RuntimeError('未安装 yt-dlp')

    if quality == '1080p（大会员码流）':
        video_fmt = 'bestvideo[height<=1080][height>=1080]/bestvideo[height<=1080]'
    elif quality == '720p':
        video_fmt = 'bestvideo[height<=720][height>=720]/bestvideo[height<=720]'
    elif quality == '480p':
        video_fmt = 'bestvideo[height<=480][height>=480]/bestvideo[height<=480]'
    else:
        video_fmt = 'bestvideo'

    fmt = video_fmt if mute else f'{video_fmt}+bestaudio/best'
    outtmpl = os.path.join(out_dir, '%(title).80s [%(id)s].%(ext)s')

    opts = {
        'format': fmt,
        'outtmpl': outtmpl,
        'merge_output_format': 'mp4',
        'ignoreerrors': False,
        'retries': 5,
        'fragment_retries': 10,
        'progress_hooks': [_progress_hook_factory(tid)],
        'quiet': True,
        'no_warnings': True,
        'http_headers': {'User-Agent': BILI_UA, 'Referer': 'https://www.bilibili.com/'},
    }
    if COOKIE_FILE.exists():
        opts['cookiefile'] = str(COOKIE_FILE)
    if mute:
        opts['postprocessors'] = [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}]
    else:
        opts['postprocessors'] = [{'key': 'FFmpegVideoRemuxer', 'preferedformat': 'mp4'}]

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    if info is None:
        raise RuntimeError('yt-dlp 未能解析视频（可能需要登录或地区限制）')

    # 找到最终文件
    entries = info.get('entries') or [info]
    output_path = ''
    for e in entries:
        if e is None:
            continue
        fp = (e.get('requested_downloads') or [{}])[0].get('filepath') or e.get('filepath')
        if fp and os.path.isfile(fp):
            output_path = fp
            break
    title = info.get('title') or url
    return output_path or title


def parse_urls(text: str) -> List[str]:
    urls = []
    for line in (text or '').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = re.search(r'https?://[^\s，,、;；]+', line)
        if match:
            urls.append(match.group(0))
    seen, result = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


# =========================================================================
# 主应用（暗色科技感）
# =========================================================================

class BilibiliApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry('1240x840')
        self.root.minsize(1024, 720)
        self.root.configure(bg=C_BG)

        self._setup_style()

        self._progress_canvases: Dict[int, tk.Canvas] = {}
        self._tree_rows: Dict[int, str] = {}  # tid -> tree iid

        self._build_ui()
        self._refresh_login()
        self._start_ui_refresh()

        STATE.log(f'启动 {APP_TITLE}', 'INFO')
        # 加固已有敏感文件权限
        _secure_path(APP_HOME, is_dir=True)
        if COOKIE_FILE.exists():
            _secure_path(COOKIE_FILE)
        if LOG_FILE.exists():
            _secure_path(LOG_FILE)
        if COOKIE_FILE.exists():
            STATE.log(f'检测到 Cookie 文件: {COOKIE_FILE}', 'INFO')
            if STATE.username():
                STATE.log(f'已登录: {mask_username(STATE.username())}', 'INFO')
        else:
            STATE.log('未找到 Cookie，建议先扫码登录以获得大会员码流', 'WARN')

    # ---------- 样式 ----------
    def _setup_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass

        style.configure('.', background=C_BG, foreground=C_TEXT,
                        fieldbackground=C_INPUT_BG,
                        font=('Microsoft YaHei UI', 10))

        style.configure('TFrame', background=C_BG)
        style.configure('Panel.TFrame', background=C_PANEL)
        style.configure('TLabel', background=C_BG, foreground=C_TEXT,
                        font=('Microsoft YaHei UI', 10))
        style.configure('Muted.TLabel', background=C_BG, foreground=C_MUTED,
                        font=('Microsoft YaHei UI', 9))
        style.configure('Title.TLabel', background=C_BG, foreground=C_CYAN,
                        font=('Microsoft YaHei UI', 12, 'bold'))
        style.configure('Panel.TLabelframe', background=C_PANEL, borderwidth=0)
        style.configure('Panel.TLabelframe.Label',
                        background=C_PANEL, foreground=C_CYAN,
                        font=('Microsoft YaHei UI', 10, 'bold'))

        style.configure('TEntry', fieldbackground=C_INPUT_BG, foreground=C_INPUT_FG,
                        bordercolor=C_LINE, lightcolor=C_LINE, darkcolor=C_LINE,
                        insertcolor=C_CYAN, borderwidth=0)
        style.map('TEntry', fieldbackground=[('focus', C_INPUT_BG)])

        style.configure('TCombobox', fieldbackground=C_INPUT_BG, foreground=C_INPUT_FG,
                        background=C_PANEL, arrowcolor=C_CYAN)
        style.map('TCombobox', fieldbackground=[('readonly', C_INPUT_BG)])

        style.configure('TCheckbutton', background=C_BG, foreground=C_TEXT,
                        focuscolor=C_BG)

        style.configure('Neon.TButton', background=C_PANEL, foreground=C_CYAN,
                        borderwidth=0, focusthickness=0, padding=(16, 8),
                        font=('Microsoft YaHei UI', 10, 'bold'))
        style.map('Neon.TButton',
                  background=[('active', C_PANEL_LT), ('pressed', C_PANEL_LT)])

        style.configure('Accent.TButton', background=C_CYAN, foreground=C_BG,
                        borderwidth=0, padding=(20, 10),
                        font=('Microsoft YaHei UI', 10, 'bold'))
        style.map('Accent.TButton',
                  background=[('active', '#66e1ff'), ('pressed', '#66e1ff'),
                              ('disabled', C_LINE)],
                  foreground=[('disabled', C_MUTED)])

        style.configure('Danger.TButton', background=C_PANEL, foreground=C_MAGENTA,
                        borderwidth=0, padding=(16, 8),
                        font=('Microsoft YaHei UI', 10, 'bold'))
        style.map('Danger.TButton', background=[('active', C_PANEL_LT)])

        # Treeview
        style.configure('Treeview',
                        background=C_BG_ALT, foreground=C_TEXT,
                        fieldbackground=C_BG_ALT, rowheight=34, borderwidth=0,
                        font=('Microsoft YaHei UI', 9))
        style.configure('Treeview.Heading',
                        background=C_PANEL_LT, foreground=C_CYAN,
                        font=('Microsoft YaHei UI', 9, 'bold'),
                        borderwidth=0)
        style.map('Treeview',
                  background=[('selected', C_PANEL_LT)],
                  foreground=[('selected', C_TEXT)])
        # 行交替色
        style.configure('Treeview', background=C_BG_ALT,
                        oddbackground=C_BG_ALT, evenbackground='#0d1430')

        # 整体进度条
        style.configure('Neon.Horizontal.TProgressbar',
                        troughcolor=C_INPUT_BG, background=C_CYAN,
                        bordercolor=C_LINE, lightcolor=C_CYAN, darkcolor=C_CYAN,
                        thickness=18)

    # ---------- UI 构建 ----------
    def _build_ui(self):
        # === 顶部标题栏（渐变）===
        self.header = tk.Canvas(self.root, height=78, bg=C_BG, highlightthickness=0, bd=0)
        self.header.pack(fill='x')
        self.header.bind('<Configure>', self._draw_header)

        # 登录 & 总览面板
        top = ttk.Frame(self.root, style='TFrame')
        top.pack(fill='x', padx=16, pady=(4, 8))

        # 登录信息卡片
        login_panel = tk.Frame(top, bg=C_PANEL, highlightthickness=1,
                               highlightbackground=C_LINE)
        login_panel.pack(side='left', fill='x', expand=True, padx=(0, 8))
        tk.Label(login_panel, text='🔐 登录状态', bg=C_PANEL, fg=C_CYAN,
                 font=('Microsoft YaHei UI', 10, 'bold')).pack(anchor='w', padx=14, pady=(10, 0))
        self.login_status_var = tk.StringVar(value='未登录')
        tk.Label(login_panel, textvariable=self.login_status_var,
                 bg=C_PANEL, fg=C_TEXT,
                 font=('Microsoft YaHei UI', 11)).pack(anchor='w', padx=14, pady=(2, 0))
        self.login_tip_var = tk.StringVar(value='建议先扫码登录以获得大会员码流')
        tk.Label(login_panel, textvariable=self.login_tip_var,
                 bg=C_PANEL, fg=C_MUTED,
                 font=('Microsoft YaHei UI', 9)).pack(anchor='w', padx=14, pady=(0, 10))

        # 统计卡片
        stat_panel = tk.Frame(top, bg=C_PANEL, highlightthickness=1,
                              highlightbackground=C_LINE)
        stat_panel.pack(side='left', fill='x', expand=True, padx=8)
        tk.Label(stat_panel, text='📊 任务统计', bg=C_PANEL, fg=C_CYAN,
                 font=('Microsoft YaHei UI', 10, 'bold')).pack(anchor='w', padx=14, pady=(10, 0))
        self.stat_total_var = tk.StringVar(value='总数 0')
        tk.Label(stat_panel, textvariable=self.stat_total_var,
                 bg=C_PANEL, fg=C_TEXT,
                 font=('Microsoft YaHei UI', 11)).pack(anchor='w', padx=14)
        self.stat_detail_var = tk.StringVar(value='等待 0 · 进行中 0 · 完成 0 · 失败 0')
        tk.Label(stat_panel, textvariable=self.stat_detail_var,
                 bg=C_PANEL, fg=C_MUTED,
                 font=('Microsoft YaHei UI', 9)).pack(anchor='w', padx=14, pady=(0, 10))

        # 总体进度
        prog_panel = tk.Frame(top, bg=C_PANEL, highlightthickness=1,
                              highlightbackground=C_LINE)
        prog_panel.pack(side='left', fill='x', expand=True, padx=(8, 0))
        tk.Label(prog_panel, text='🚀 总体进度', bg=C_PANEL, fg=C_CYAN,
                 font=('Microsoft YaHei UI', 10, 'bold')).pack(anchor='w', padx=14, pady=(10, 0))
        self.overall_pct_var = tk.StringVar(value='0%')
        tk.Label(prog_panel, textvariable=self.overall_pct_var,
                 bg=C_PANEL, fg=C_TEXT,
                 font=('Microsoft YaHei UI', 11)).pack(anchor='w', padx=14)
        self.overall_progress = ttk.Progressbar(prog_panel, mode='determinate',
                                                 maximum=100, orient='horizontal',
                                                 style='Neon.Horizontal.TProgressbar')
        self.overall_progress.pack(fill='x', padx=14, pady=(0, 12))

        # === 登录操作按钮栏 ===
        login_bar = ttk.Frame(self.root, style='TFrame')
        login_bar.pack(fill='x', padx=16)

        def neon(parent, text, color, cmd, kind='normal'):
            b = tk.Button(parent, text=text, command=cmd,
                          bg=C_PANEL, fg=color,
                          font=('Microsoft YaHei UI', 10, 'bold'),
                          relief='flat', bd=0, padx=18, pady=9,
                          cursor='hand2',
                          activebackground=C_PANEL_LT, activeforeground=color,
                          highlightthickness=1,
                          highlightbackground=color, highlightcolor=color)
            return b

        neon(login_bar, '📱 扫码登录', C_CYAN, self._open_qr_login).pack(side='left', padx=(0, 8))
        neon(login_bar, '📝 粘贴 Cookie', C_GREEN, self._open_cookie_paste).pack(side='left', padx=8)
        neon(login_bar, '📁 选择 Cookie 文件', C_AMBER, self._choose_cookie_file).pack(side='left', padx=8)
        self.btn_logout = neon(login_bar, '🚪 退出登录', C_RED, self._logout)
        self.btn_logout.pack(side='left', padx=8)

        # === 链接输入 ===
        input_wrap = tk.Frame(self.root, bg=C_PANEL, highlightthickness=1,
                              highlightbackground=C_LINE)
        input_wrap.pack(fill='x', padx=16, pady=(16, 8))
        tk.Label(input_wrap, text='📎 视频链接（每行一个，支持 # 注释）',
                 bg=C_PANEL, fg=C_CYAN,
                 font=('Microsoft YaHei UI', 10, 'bold')).pack(anchor='w', padx=14, pady=(12, 4))

        self.url_text = tk.Text(input_wrap, height=5, wrap='word',
                                bg=C_INPUT_BG, fg=C_INPUT_FG,
                                insertbackground=C_CYAN,
                                font=('Consolas', 10),
                                bd=0, highlightthickness=1,
                                highlightbackground=C_LINE, highlightcolor=C_CYAN)
        self.url_text.pack(fill='x', padx=14, pady=(0, 8))
        self.url_text.insert('1.0',
                             '# 粘贴 B 站视频链接，每行一个（合集 / 分 P / 视频均可）\n'
                             'https://www.bilibili.com/video/BV1GJ411x7h7\n')

        opts_row = tk.Frame(input_wrap, bg=C_PANEL)
        opts_row.pack(fill='x', padx=14, pady=(0, 10))

        tk.Label(opts_row, text='🎯 清晰度:', bg=C_PANEL, fg=C_TEXT,
                 font=('Microsoft YaHei UI', 10)).pack(side='left', padx=(0, 6))
        self.quality_var = tk.StringVar(value='1080p（大会员码流）')
        quality_box = ttk.Combobox(opts_row, textvariable=self.quality_var,
                                   values=['最高画质（4K / 杜比 · 需大会员）',
                                           '1080p（大会员码流）', '720p', '480p'],
                                   width=22, state='readonly')
        quality_box.pack(side='left', padx=(0, 14))

        tk.Label(opts_row, text='💾 输出目录:', bg=C_PANEL, fg=C_TEXT,
                 font=('Microsoft YaHei UI', 10)).pack(side='left', padx=(0, 6))
        self.out_dir_var = tk.StringVar(value=str(Path.home() / 'Downloads'))
        ttk.Entry(opts_row, textvariable=self.out_dir_var, width=32).pack(side='left')
        ttk.Button(opts_row, text='浏览…', command=self._choose_dir,
                   style='Neon.TButton').pack(side='left', padx=6)

        self.mute_var = tk.BooleanVar(value=True)
        tk.Checkbutton(opts_row, text='🔇 静音（仅视频流，体积更小）',
                       variable=self.mute_var,
                       bg=C_PANEL, fg=C_TEXT, selectcolor=C_INPUT_BG,
                       activebackground=C_PANEL, activeforeground=C_TEXT,
                       bd=0, highlightthickness=0,
                       font=('Microsoft YaHei UI', 10),
                       cursor='hand2').pack(side='left', padx=16)

        # === 操作按钮 ===
        action_bar = tk.Frame(self.root, bg=C_BG)
        action_bar.pack(fill='x', padx=16, pady=(2, 8))

        self.btn_start = tk.Button(action_bar, text='🚀 开始下载',
                                   command=self._start,
                                   bg=C_CYAN, fg=C_BG,
                                   font=('Microsoft YaHei UI', 11, 'bold'),
                                   relief='flat', bd=0, padx=22, pady=10,
                                   cursor='hand2',
                                   activebackground='#66e1ff', activeforeground=C_BG)
        self.btn_start.pack(side='left', padx=(0, 8))

        self.btn_cancel = tk.Button(action_bar, text='⏹ 取消',
                                    command=self._cancel, state='disabled',
                                    bg=C_RED, fg='white',
                                    font=('Microsoft YaHei UI', 11, 'bold'),
                                    relief='flat', bd=0, padx=22, pady=10,
                                    cursor='hand2',
                                    activebackground='#ff8898', activeforeground='white')
        self.btn_cancel.pack(side='left', padx=8)

        ttk.Button(action_bar, text='🧹 清空任务', command=self._clear_tasks,
                   style='Neon.TButton').pack(side='left', padx=8)
        ttk.Button(action_bar, text='📂 打开输出目录', command=self._open_output,
                   style='Neon.TButton').pack(side='left', padx=8)

        # === 任务列表 ===
        list_wrap = tk.Frame(self.root, bg=C_PANEL, highlightthickness=1,
                             highlightbackground=C_LINE)
        list_wrap.pack(fill='both', expand=True, padx=16, pady=(2, 8))
        tk.Label(list_wrap, text='📋 任务列表 · 实时进度',
                 bg=C_PANEL, fg=C_CYAN,
                 font=('Microsoft YaHei UI', 10, 'bold')).pack(anchor='w', padx=14, pady=(10, 4))

        tree_frame = tk.Frame(list_wrap, bg=C_PANEL)
        tree_frame.pack(fill='both', expand=True, padx=14, pady=(0, 12))

        cols = ('idx', 'title', 'status', 'size', 'speed', 'eta')
        self.tree = ttk.Treeview(tree_frame, columns=cols, show='headings',
                                 height=10)
        self.tree.heading('idx', text='#')
        self.tree.heading('title', text='标题 / 链接')
        self.tree.heading('status', text='状态')
        self.tree.heading('size', text='当前 / 总大小')
        self.tree.heading('speed', text='速度')
        self.tree.heading('eta', text='ETA')
        self.tree.column('idx', width=50, anchor='center', stretch=False)
        self.tree.column('title', width=340, anchor='w', stretch=True)
        self.tree.column('status', width=80, anchor='center', stretch=False)
        self.tree.column('size', width=180, anchor='e', stretch=False)
        self.tree.column('speed', width=100, anchor='e', stretch=False)
        self.tree.column('eta', width=80, anchor='center', stretch=False)

        # 提前配置每个状态的 tag 颜色（避免每次都 reconfigure）
        self.tree.tag_configure('等待', foreground=C_MUTED)
        self.tree.tag_configure('下载中', foreground=C_CYAN)
        self.tree.tag_configure('已完成', foreground=C_GREEN)
        self.tree.tag_configure('失败', foreground=C_RED)
        self.tree.tag_configure('已取消', foreground=C_AMBER)

        # 进度条列宽：放置在 title 列右侧（140px 宽）
        self._progress_bar_width = 140

        vsb = ttk.Scrollbar(tree_frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)

        self.tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')

        # === 日志 ===
        log_wrap = tk.Frame(self.root, bg=C_PANEL, highlightthickness=1,
                            highlightbackground=C_LINE)
        log_wrap.pack(fill='x', padx=16, pady=(2, 16))
        tk.Label(log_wrap, text='📜 运行日志',
                 bg=C_PANEL, fg=C_CYAN,
                 font=('Microsoft YaHei UI', 10, 'bold')).pack(anchor='w', padx=14, pady=(8, 0))
        self.log_text = tk.Text(log_wrap, height=7, wrap='word',
                                bg=C_INPUT_BG, fg='#9efcff',
                                insertbackground=C_CYAN,
                                font=('Consolas', 10),
                                bd=0, highlightthickness=1,
                                highlightbackground=C_LINE, highlightcolor=C_CYAN)
        self.log_text.pack(fill='x', padx=14, pady=(2, 12))
        self.log_text.configure(state='disabled')

        # 状态栏
        self.status_bar = tk.Label(self.root, text='就绪', bg=C_BG_ALT, fg=C_MUTED,
                                   anchor='w', padx=16,
                                   font=('Microsoft YaHei UI', 9))
        self.status_bar.pack(fill='x', side='bottom')

        # 配置 tree 绑定（滚动时重绘 progress canvas）
        self.tree.bind('<Configure>', lambda e: self._redraw_progresses())
        self.tree.bind('<MouseWheel>', lambda e: self.root.after(10, self._redraw_progresses))
        self.tree.bind('<Button-4>', lambda e: self.root.after(10, self._redraw_progresses))
        self.tree.bind('<Button-5>', lambda e: self.root.after(10, self._redraw_progresses))

        # 更新 yt-dlp 缺失提示
        if yt_dlp is None:
            STATE.log('⚠ 未检测到 yt-dlp，请先安装: pip install yt-dlp', 'ERROR')

    # ---------- 顶部标题栏渐变 ----------
    def _draw_header(self, event=None):
        c = self.header
        c.delete('all')
        w = c.winfo_width() or 1200
        h = c.winfo_height() or 78
        # 渐变背景（横条）
        steps = 40
        for i in range(steps):
            r1, g1, b1 = 0x00, 0x20, 0x48  # 深蓝
            r2, g2, b2 = 0x2a, 0x00, 0x3d  # 深品红
            ratio = i / (steps - 1)
            r = int(r1 + (r2 - r1) * ratio)
            g = int(g1 + (g2 - g1) * ratio)
            b = int(b1 + (b2 - b1) * ratio)
            color = f'#{r:02x}{g:02x}{b:02x}'
            x0 = int(i * w / steps)
            x1 = int((i + 1) * w / steps) + 1
            c.create_rectangle(x0, 0, x1, h, fill=color, outline='')
        # 底部亮线
        c.create_rectangle(0, h - 3, w, h, fill=C_CYAN, outline='')
        # 主标题
        c.create_text(28, h // 2, anchor='w',
                      text='🛰  BILIBILI  批量视频下载器',
                      fill=C_TEXT,
                      font=('Microsoft YaHei UI', 18, 'bold'))
        # 版本标签（霓虹边框圆角感）
        c.create_text(w - 30, h // 2, anchor='e',
                      text=f'{APP_VERSION} · 大会员码流',
                      fill=C_CYAN,
                      font=('Microsoft YaHei UI', 11, 'bold'))

    # ---------- 登录 / 退出 ----------
    def _open_qr_login(self):
        if not HAS_QR:
            messagebox.showwarning(
                '依赖缺失', '需要 Pillow 和 qrcode。\n请先执行：pip install qrcode[pil] Pillow')
            return
        QRLoginDialog(self.root, on_success=self._refresh_login)

    def _open_cookie_paste(self):
        CookiePasteDialog(self.root, on_success=self._refresh_login)

    def _choose_cookie_file(self):
        path = filedialog.askopenfilename(
            title='选择 Cookie 文件（Netscape 格式）',
            filetypes=[('Cookie / Text', '*.txt *.cookie'), ('所有文件', '*.*')])
        if not path:
            return
        try:
            text = Path(path).read_text(encoding='utf-8', errors='ignore')
        except OSError as e:
            messagebox.showerror('错误', f'读取失败: {e}')
            return
        # 规范化写入
        lines = []
        for raw in text.splitlines():
            raw = raw.strip()
            if not raw or raw.startswith('#'):
                continue
            parts = raw.split('\t')
            if len(parts) >= 7:
                lines.append(raw)
                continue
            m = re.match(r'^([A-Za-z0-9_\-]+)\s*=\s*(.+)$', raw.rstrip(';'))
            if m:
                name, value = m.group(1), m.group(2).strip().strip('"').rstrip(';').strip()
                lines.append(f'.bilibili.com\tTRUE\t/\tFALSE\t0\t{name}\t{value}')
        content = ('# Netscape HTTP Cookie File\n# Generated by bili_downloader\n\n'
                   + '\n'.join(lines) + '\n')
        if 'SESSDATA' not in content or 'bili_jct' not in content:
            messagebox.showwarning('提示', '未解析到 SESSDATA / bili_jct。\n请用浏览器 DevTools 复制 Cookies。')
            return
        COOKIE_FILE.write_text(content, encoding='utf-8')
        _secure_path(COOKIE_FILE)
        _secure_path(APP_HOME, is_dir=True)
        STATE._try_restore_user()
        STATE.log('已选择并保存 Cookie 文件', '登录')
        self._refresh_login()

    def _logout(self):
        try:
            if COOKIE_FILE.exists():
                COOKIE_FILE.unlink()
        except OSError:
            pass
        STATE.set_username(None)
        STATE.log('已退出登录，Cookie 文件已移除', '登录')
        self._refresh_login()

    def _refresh_login(self):
        if STATE.username():
            self.login_status_var.set(f'✅ 已登录：{STATE.username()}')
            self.login_tip_var.set(f'Cookie 文件: {COOKIE_FILE}')
        elif STATE.cookie_has_login():
            self.login_status_var.set('⚠ Cookie 文件存在，但未获取到用户名')
            self.login_tip_var.set(f'Cookie 文件: {COOKIE_FILE}')
        else:
            self.login_status_var.set('⚠ 未登录（下载清晰度可能受限）')
            self.login_tip_var.set('点击下方「📱 扫码登录」获得大会员码流')

    # ---------- 目录选择 ----------
    def _choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.out_dir_var.get() or str(Path.home()),
                                    title='选择输出目录')
        if d:
            self.out_dir_var.set(d)

    def _open_output(self):
        p = self.out_dir_var.get().strip() or str(Path.home() / 'Downloads')
        try:
            if sys.platform.startswith('win'):
                os.startfile(p)
            elif sys.platform == 'darwin':
                os.system(f'open "{p}"')
            else:
                os.system(f'xdg-open "{p}" >/dev/null 2>&1 &')
        except Exception as e:
            messagebox.showerror('错误', f'无法打开目录: {e}')

    # ---------- 下载控制 ----------
    def _start(self):
        if yt_dlp is None:
            messagebox.showerror('依赖缺失', '请先安装 yt-dlp:\npip install yt-dlp')
            return
        text = self.url_text.get('1.0', 'end')
        urls = parse_urls(text)
        if not urls:
            messagebox.showwarning('提示', '请粘贴至少一个有效的 B 站视频链接')
            return
        out_dir = self.out_dir_var.get().strip() or str(Path.home() / 'Downloads')
        out_dir = os.path.abspath(os.path.expanduser(out_dir))
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            messagebox.showerror('错误', f'无法创建目录: {e}')
            return

        STATE.reset_tasks()
        self._clear_progress_canvases()
        self.tree.delete(*self.tree.get_children())
        STATE.add_tasks(urls)

        # 初始化 UI 行
        for t in STATE.tasks():
            iid = self.tree.insert(
                '', 'end',
                values=(t.id, self._safe_title(t.title or t.url, maxlen=45),
                        t.status, t.size, t.speed, t.eta),
                tags=(t.status,)
            )
            self._tree_rows[t.id] = iid

        quality = self.quality_var.get()
        mute = bool(self.mute_var.get())
        STATE.set_cancelled(False)
        STATE.set_downloading(True)
        self.btn_start.configure(state='disabled')
        self.btn_cancel.configure(state='normal')
        STATE.log(f'创建 {len(urls)} 个任务 → 输出: {out_dir}', '下载')
        STATE.log(f'清晰度: {quality} · 静音: {"是" if mute else "否"}', '下载')

        threading.Thread(target=self._download_worker,
                         args=(urls, out_dir, quality, mute),
                         daemon=True).start()

    def _download_worker(self, urls, out_dir, quality, mute):
        tasks = STATE.tasks()
        for t in urls:
            pass  # noop
        for task in STATE.tasks():
            if STATE.is_cancelled():
                STATE.update_task(task.id, status='已取消',
                                  finished_at=datetime.now().strftime('%H:%M:%S'))
                STATE.log(f'已取消: {task.url}', '下载')
                continue
            STATE.update_task(task.id, status='下载中',
                              started_at=datetime.now().strftime('%H:%M:%S'),
                              title='解析中…')
            try:
                output = download_one(task.url, out_dir, quality, mute, task.id)
                STATE.update_task(task.id, status='已完成', output=output,
                                  pct=100.0, speed='', eta='',
                                  finished_at=datetime.now().strftime('%H:%M:%S'))
                STATE.log(f'✅ 完成: {output or task.url}', '下载')
            except Exception as e:
                STATE.update_task(task.id, status='失败', error=str(e),
                                  finished_at=datetime.now().strftime('%H:%M:%S'))
                STATE.log(f'❌ 失败: {task.url} → {e}', 'ERROR')
        STATE.set_downloading(False)
        STATE.log('批处理结束', '下载')
        # 刷新按钮状态（通过 after 回到主线程）
        self.root.after(0, lambda: self.btn_start.configure(state='normal'))
        self.root.after(0, lambda: self.btn_cancel.configure(state='disabled'))

    def _cancel(self):
        if STATE.is_downloading():
            STATE.set_cancelled(True)
            STATE.log('已请求取消，当前任务完成后将停止', '下载')
            self.status_bar.config(text='请求取消…')

    def _clear_tasks(self):
        if STATE.is_downloading():
            messagebox.showinfo('提示', '下载进行中，无法清空，请先取消')
            return
        STATE.reset_tasks()
        self.tree.delete(*self.tree.get_children())
        self._clear_progress_canvases()
        STATE.log('已清空任务列表', 'UI')

    # ---------- 进度条（在 Treeview 右侧悬浮 Canvas） ----------
    def _clear_progress_canvases(self):
        for c in list(self._progress_canvases.values()):
            try:
                c.destroy()
            except Exception:
                pass
        self._progress_canvases = {}
        self._tree_rows = {}

    def _redraw_progresses(self):
        """在 title 列右侧叠加一个 Canvas 进度条。
        使用整行 bbox 定位，确保在 Treeview 正确渲染位置。"""
        # 先清除旧位置计算，避免残留
        for tid in list(self._progress_canvases.keys()):
            if tid not in self._tree_rows:
                try:
                    self._progress_canvases[tid].destroy()
                except Exception:
                    pass
                del self._progress_canvases[tid]

        pw = self._progress_bar_width
        # 进度条显示的区域：相对 title 列的右侧
        # title 列真实宽度在 Treeview 里可能与配置不同，使用 bbox(iid, 'title')
        for tid, iid in list(self._tree_rows.items()):
            task = next((t for t in STATE.tasks() if t.id == tid), None)
            if task is None:
                continue
            pct = min(100.0, max(0.0, task.pct))

            try:
                bbox_title = self.tree.bbox(iid, 'title')
            except tk.TclError:
                bbox_title = None

            if not bbox_title:
                # title 列不可见，尝试整行 bbox 并估算位置
                try:
                    bbox_row = self.tree.bbox(iid)
                except tk.TclError:
                    bbox_row = None
                if not bbox_row:
                    # 完全不可见，销毁 canvas 节省资源
                    if tid in self._progress_canvases:
                        try:
                            self._progress_canvases[tid].destroy()
                        except Exception:
                            pass
                        del self._progress_canvases[tid]
                    continue
                x, y, w, h = bbox_row
                # 估算 title 列位置 = idx 列右边界
                x = x + 50
                w = 340
            else:
                x, y, w, h = bbox_title

            ph = max(16, h - 6)
            # 进度条放在 title 列右侧，占 pw 宽
            # 注意：需确保不覆盖 status 列的左边界
            px = x + max(10, w - pw - 6)
            py = y + (h - ph) // 2
            pw_actual = min(pw, w - 20)
            if pw_actual < 40:
                continue

            canvas = self._progress_canvases.get(tid)
            if canvas is None:
                canvas = tk.Canvas(self.tree, width=pw_actual, height=ph,
                                   bg=C_BG_ALT, highlightthickness=0, bd=0)
                self._progress_canvases[tid] = canvas
            canvas.configure(width=pw_actual, height=ph)
            try:
                canvas.place(x=px, y=py, width=pw_actual, height=ph)
            except Exception:
                pass

            # 颜色：依状态而定
            if task.status == '已完成':
                color = C_GREEN
            elif task.status == '失败':
                color = C_RED
            elif task.status == '已取消':
                color = C_AMBER
            else:
                color = C_CYAN

            canvas.delete('all')
            canvas.create_rectangle(0, 0, pw_actual - 1, ph - 1,
                                    outline=C_LINE, fill=C_INPUT_BG)
            fill_w = int((pw_actual - 2) * pct / 100.0)
            if fill_w > 0:
                canvas.create_rectangle(1, 1, 1 + fill_w, ph - 2,
                                        outline='', fill=color)
            canvas.create_text(pw_actual // 2, ph // 2,
                               text=f'{pct:.0f}%',
                               fill=C_TEXT,
                               font=('Microsoft YaHei UI', 8, 'bold'))

    # ---------- UI 刷新（1s 一次） ----------
    def _start_ui_refresh(self):
        self._tick()

    def _tick(self):
        try:
            self._refresh_tasks_ui()
            self._refresh_log_ui()
            self._refresh_overall()
        except Exception:
            pass
        self.root.after(500, self._tick)  # 500ms，更流畅的进度条更新

    def _refresh_tasks_ui(self):
        tasks = STATE.tasks()
        existing_ids = set(self._tree_rows.keys())

        # 新增任务（理论上在 _start 中已经插入，但兜底）
        for t in tasks:
            if t.id not in existing_ids:
                iid = self.tree.insert(
                    '', 'end',
                    values=(t.id, self._safe_title(t.title or t.url, maxlen=45),
                            t.status, t.size, t.speed, t.eta),
                    tags=(t.status,)
                )
                self._tree_rows[t.id] = iid

        # 更新现有行
        for t in tasks:
            iid = self._tree_rows.get(t.id)
            if iid is None:
                continue
            title = self._safe_title(t.title or t.url, maxlen=45)
            self.tree.item(iid,
                           values=(t.id, title, t.status,
                                   t.size, t.speed, t.eta),
                           tags=(t.status,))

        # 重绘所有进度条
        self._redraw_progresses()

    def _safe_title(self, text: str, maxlen: int = 60) -> str:
        """确保标题不会覆盖进度条，并清理换行与控制字符。"""
        text = text.replace('\r', ' ').replace('\n', ' ').replace('\t', ' ').strip()
        text = strip_ansi(text)
        if len(text) > maxlen:
            text = text[: maxlen - 3] + '...'
        return text or '(未命名)'

    def _refresh_overall(self):
        tasks = STATE.tasks()
        total = len(tasks)
        running = sum(1 for t in tasks if t.status == '下载中')
        done = sum(1 for t in tasks if t.status == '已完成')
        failed = sum(1 for t in tasks if t.status == '失败')
        cancelled = sum(1 for t in tasks if t.status == '已取消')
        waiting = total - running - done - failed - cancelled
        self.stat_total_var.set(f'总数 {total}')
        self.stat_detail_var.set(
            f'等待 {waiting} · 进行中 {running} · 完成 {done} · 失败 {failed}'
        )
        if total == 0:
            avg = 0.0
        else:
            avg = sum(t.pct for t in tasks) / total
        self.overall_progress['value'] = avg
        self.overall_pct_var.set(f'{avg:.1f}%' if avg > 0 else '0.0%')
        # 状态栏
        if STATE.is_downloading():
            self.status_bar.config(text=f'下载中… 进度 {avg:.1f}%  成功 {done}  失败 {failed}')
        elif total > 0:
            self.status_bar.config(text=f'完成  成功 {done}  失败 {failed}  取消 {cancelled}')
        else:
            self.status_bar.config(text='就绪')

    def _refresh_log_ui(self):
        tail = STATE.log_tail(200)
        self.log_text.configure(state='normal')
        self.log_text.delete('1.0', 'end')
        self.log_text.insert('1.0', tail)
        self.log_text.see('end')
        self.log_text.configure(state='disabled')


# =========================================================================
# main
# =========================================================================

def main():
    root = tk.Tk()
    try:
        # Windows 下设置 DPI 感知
        if sys.platform.startswith('win'):
            try:
                from ctypes import windll
                windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
    except Exception:
        pass

    # 窗口图标 — 用简单的蓝色矩形
    try:
        icon_img = PILImage.new('RGB', (64, 64), C_BG)
        from PIL import ImageDraw
        d = ImageDraw.Draw(icon_img)
        d.rectangle([6, 6, 57, 57], outline=C_CYAN, width=3)
        d.text((20, 22), 'B', fill=C_CYAN,
               font=None)  # simple letter
        tk_icon = ImageTk.PhotoImage(icon_img)
        root.iconphoto(True, tk_icon)
    except Exception:
        pass

    app = BilibiliApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
