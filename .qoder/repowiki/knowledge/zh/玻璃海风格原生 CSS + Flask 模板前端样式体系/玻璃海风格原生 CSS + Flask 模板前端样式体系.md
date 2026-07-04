---
kind: frontend_style
name: 玻璃海风格原生 CSS + Flask 模板前端样式体系
category: frontend_style
scope:
    - '**'
source_files:
    - web/static/css/style.css
    - web/templates/base.html
    - web/static/js/app.js
---

## 系统概述
本项目采用**纯原生 CSS + Flask Jinja2 模板**的前端样式方案，未引入任何 UI 组件库（如 Bootstrap、Ant Design）或构建工具（Webpack、Vite），所有样式集中在单一 `style.css` 文件中，通过 Flask 的 `static` 目录静态资源服务直接加载。

## 核心文件与结构
- `web/static/css/style.css` — 全部样式定义（约 1100+ 行），按功能模块分段组织
- `web/templates/base.html` — 全局布局模板，统一注入导航栏、CSS/JS 引用
- `web/static/js/app.js` — 仅包含 `formatDate`、`showToast` 两个全局工具函数
- `web/templates/*.html` — 各页面模板继承 base.html 的 block 扩展点

## 设计系统与约定
### 设计令牌（Design Tokens）
通过 CSS `:root` 变量集中管理：
- **色彩体系**：主色 `--primary: #0891b2`（青色），语义色 success/warning/danger/info 均配套 bg/border 三件套
- **文字层级**：`--text-1` ~ `--text-4` 四级灰度，遵循 Apple 风格
- **圆角**：`--r-xs` ~ `--r-xl` + `--r-full` 胶囊形态
- **阴影**：`--shadow-xs` ~ `--shadow-lg` 四级深度
- **动效**：统一的 `--ease` 缓动曲线与 `--t1/t2` 过渡时长

### 视觉风格
- **毛玻璃效果**：大量使用 `backdrop-filter: blur(20px)` 实现 Glassmorphism 卡片/导航
- **渐变背景**：body 使用多段线性渐变营造「玻璃海」氛围
- **Apple 风格排版**：Inter/SF Pro/PingFang SC 字体栈，负字间距 `-0.035em`，tabular-nums 数字对齐
- **微交互**：按钮悬停上浮、表格行悬停高亮、状态徽章脉冲光点等细腻动画

### 响应式策略
- 基于 CSS Grid 的自适应网格（`grid-template-columns: repeat(auto-fill, minmax(220px, 1fr))`）
- 固定顶部导航 + 内容区左右边距（`padding: calc(var(--nav-h) + 40px) 40px 40px`）
- 无媒体查询断点，依赖弹性布局自然适配

### 组件约定
- **卡片**：`.section` / `.card` — 白底半透明 + 毛玻璃 + 细边框 + 小阴影
- **数据表**：`.data-table` — 粘性表头、斑马纹、行悬停浮起、学校列左侧彩色指示条
- **按钮**：`.btn-primary/.btn-secondary/.btn-success/.btn-danger` 统一圆角胶囊形态
- **表单**：输入框聚焦时青色外发光 `box-shadow: 0 0 0 3px var(--primary-soft)`
- **状态标签**：`.status-badge` 系列带前置彩色圆点的胶囊标签
- **弹窗**：`.modal-overlay` + `.modal-card` 全屏遮罩 + 毛玻璃卡片
- **提示消息**：`.toast` 右上角滑入通知，支持 success/error/info 三种类型

## 开发者规范
1. **新增样式优先复用 CSS 变量**，不要硬编码颜色值
2. **组件类名保持语义化**，参考现有 `.stat-card`、`.filter-bar`、`.progress-item` 命名模式
3. **避免内联样式**，除动态计算场景外一律写入 style.css
4. **Jinja2 模板中只负责结构**，样式通过 class 绑定到已有组件类
5. **不引入第三方 CSS 框架**，保持单文件可维护性