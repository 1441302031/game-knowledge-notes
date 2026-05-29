# Ares Knowledge Base

🍃 游戏服务器 & AI 引擎知识库 — 千与千寻 × 龙猫主题

## 结构

```
ares-knowledge-base/
├── index.html          ← 导航首页
├── ghibli.css          ← 公共样式库（16000+ 行）
├── md2html.py          ← Markdown → HTML 转换工具
│
├── 🏛️ 架构方法论
│   └── game-server-methodology.html
│
├── 🎮 Ares 分布式游戏服务器
│   ├── ares_job_study_plan.html        求职学习计划
│   ├── stress-test-report.html         压测报告
│   ├── snhe-architecture.html          分布式架构设计
│   ├── unity-integration-guide.html    Unity SDK 接入指南
│   ├── unity-ue-multiplayer-research.html   Unity vs UE 调研
│   ├── game_perf_ultimate_guide.html   游戏性能终极指南
│   └── game_perf_optimization_guide.html    Rust 服务端性能优化
│
├── 🤖 AI 游戏引擎设计
│   ├── ai-native-engine-manifesto.html     AI 原生引擎宣言
│   ├── ai-native-engine-hd2d-design.html   HD-2D 引擎设计
│   ├── ai-native-engine-v2.html            引擎 v2 架构
│   ├── ai-native-game-engine-report.html   AI 引擎技术调研
│   └── piccolo-learning-report.html        Piccolo 引擎源码分析
│
└── 📦 其他
    └── 牙隐裂跨模态_补充章节.html
```

## 使用方式

### 本地预览

```bash
cd ares-knowledge-base
python3 -m http.server 8666
# 打开 http://localhost:8666
```

### 新增文档

```bash
# 编写 Markdown 后转换
python3 md2html.py docs/new-doc.md
# 自动生成到当前目录，使用 Ghibli 主题
```

### Ghibli 公共样式库

`ghibli.css` 是千与千寻 × 龙猫风格的公共 CSS 库。

在任何 HTML 中引入：
```html
<link rel="stylesheet" href="/ghibli.css">
```

核心 class：`ghibli-body` / `ghibli-sidebar` / `ghibli-main` / `ghibli-table` / `ghibli-card` / `ghibli-callout`

## 技术栈

- 静态 HTML + CSS（零构建工具）
- `ghibli.css` 公共样式库
- `md2html.py`（Python markdown + 自定义模板）
- Nginx 静态文件服务（端口 8666）
