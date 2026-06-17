# 🛰 Bilibili 批量视频下载器

现代化暗色桌面应用，基于 Tkinter + yt-dlp，支持 B 站视频批量下载、大会员码流、扫码登录。

## ✨ 功能特性

- **扫码登录 / Cookie 导入** — 支持二维码扫码、粘贴 Cookie、浏览器 Cookie 文件导入，获取大会员高清码流
- **批量下载** — 多行链接一次性添加，支持视频 / 合集 / 分 P
- **实时进度** — 任务列表实时显示进度条、速度、ETA、文件大小
- **大会员码流** — 支持 4K / 1080p+ 大会员专属清晰度
- **暗色科技感 UI** — 霓虹渐变主题，无需浏览器
- **安全设计** — Cookie 文件权限自动加固，日志用户名脱敏

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 运行

```bash
python bilibili_downloader.py
```

### 3. 打包为 EXE（可选）

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name "BiliDownloader" bilibili_downloader.py
```

## 📦 依赖

- **yt-dlp** — 视频解析与下载
- **requests** — HTTP 请求
- **Pillow** — 图像处理（二维码显示）
- **qrcode** — 二维码生成

## ⚠️ 注意事项

- 请遵守 B 站用户协议，仅下载你有权访问的内容
- Cookie 文件存储在 `~/.bili_downloader/`，权限已自动加固
- 下载的视频仅供个人学习与研究使用

## 📄 开源协议

MIT License

---

**版本**: v1.4 · 大会员码流支持
