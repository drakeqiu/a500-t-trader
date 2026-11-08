---
name: A500 Trader
description: "冷静、可信、数据优先的个人 ETF 研究工作台。"
colors:
  workspace: "#f4f6f8"
  surface: "#ffffff"
  ink: "#172331"
  slate-muted: "#596878"
  slate-border: "#dce3e9"
  slate-subtle: "#f7f9fa"
  deep-sea-blue: "#1d4f78"
  navigation-blue: "#163b60"
  focus-blue: "#6398c1"
  fresh-green: "#267354"
  fresh-green-soft: "#edf4ef"
  unavailable-vermilion: "#984224"
  unavailable-soft: "#fff0e8"
typography:
  body:
    fontFamily: '"Noto Sans CJK SC", "Microsoft YaHei", system-ui, sans-serif'
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  page-title:
    fontFamily: '"Noto Sans CJK SC", "Microsoft YaHei", system-ui, sans-serif'
    fontSize: "clamp(28px, 3vw, 36px)"
    fontWeight: 650
    lineHeight: 1.2
    letterSpacing: "-0.025em"
  table-label:
    fontFamily: '"Noto Sans CJK SC", "Microsoft YaHei", system-ui, sans-serif'
    fontSize: "10px"
    fontWeight: 700
    lineHeight: 1.4
    letterSpacing: "0.045em"
  code:
    fontFamily: "ui-monospace, SFMono-Regular, Consolas, monospace"
rounded:
  xs: "4px"
  sm: "6px"
  md: "8px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
  2xl: "32px"
  3xl: "40px"
components:
  button-primary:
    backgroundColor: "{colors.deep-sea-blue}"
    textColor: "{colors.surface}"
    rounded: "{rounded.sm}"
    padding: "0 15px"
    height: "39px"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "#1c476a"
    rounded: "{rounded.sm}"
    padding: "0 15px"
    height: "39px"
  state-direction:
    backgroundColor: "#eaf1f7"
    textColor: "#244f72"
    rounded: "{rounded.xs}"
    padding: "4px 7px"
  state-unavailable:
    backgroundColor: "{colors.unavailable-soft}"
    textColor: "{colors.unavailable-vermilion}"
    rounded: "{rounded.xs}"
    padding: "4px 7px"
---

# Design System: A500 Trader

## Overview

**Creative North Star: “清晰的市场工作台”**

这是为盘中查阅策略状态而设的冷静、可信、数据优先工作界面。让使用者快速辨认状态、理由、事件时间和可用性；以清楚的层级与紧凑的数据呈现支撑核查，不让装饰压过信息。整体以浅灰蓝工作区承托白色内容面，石板蓝灰组织文字与分隔，深海蓝标示导航和主要操作。

平面为主：边框和轻微底色差异组织常驻区域，只有实际浮层使用柔和阴影。桌面优先展示完整工作内容；窄屏保留相同信息次序并将表格转为带标签的信号卡片。旧 Main 页面的遗留外观不属于本系统；本规范以当前确认的 `/signals` 页面和共享 App Shell 为准。

**Key Characteristics:**
- 冷静、可信、数据优先；紧凑、清楚、状态明确。
- 低饱和蓝灰中性色，深蓝承担导航和交互强调。
- 语义状态同时通过文字与颜色表达；时效和不可用必须可辨。
- 平面分层、细边框；阴影仅用于弹出层。

## Colors

浅灰蓝工作区、白色表面与蓝灰文字构成稳定底色；深蓝用于导航和可操作强调，绿与朱红仅表达已确认的状态含义。

### Primary
- **深海蓝**：主要操作与可交互文本的强调色；用于主按钮、筛选交互和导航选中状态。避免把它扩展为大面积装饰色。

### Neutral
- **工作区灰蓝**：主内容外围的页面底色。
- **白色表面**：顶栏、工具条、内容面板和数据表背景。
- **墨蓝黑**：标题、标的标识与主要正文。
- **石板蓝灰**：次级说明、连接状态、注释和辅助文字。
- **浅石板边线**：面板、表格和分隔线；表头另用极浅灰蓝底色。

### Named Rules
**状态有名规则。** 绿、朱红或方向色必须配合文字标签表达；不得只用色彩区分等待、不可用或资格状态。不可用行须保持明显但克制的底色提示。

## Typography

**Display Font:** 无独立展示字体；页面标题使用界面字体栈（Noto Sans CJK SC、Microsoft YaHei、系统无衬线回退）。
**Body Font:** Noto Sans CJK SC、Microsoft YaHei、system-ui、sans-serif。
**Label/Mono Font:** 界面无衬线字体用于标签；代码与参数指纹使用 ui-monospace、SFMono-Regular、Consolas、monospace。

**Character:** 清晰、朴素、适合持续阅读中文业务信息。数字与时间启用等宽数字，参数与代码才使用等宽字族。

### Hierarchy
- **页面标题**（650，28–36px，1.2）：当前业务页面主标题；随视口缩放。
- **正文**（400，14px，1.5）：说明文字、页面介绍与通用阅读内容。
- **导航**（400/650，13px）：常态低对比，当前项提高字重并以底边线定位。
- **表格列标签**（700，10px，约 1.4 行高，0.045em）：紧凑列标题，以大写转换不适用于中文。
- **数据与状态**（10–14px，数字等宽）：标的、数量、事件时间和辅助状态按重要程度区分字重。

### Named Rules
**测量数据规则。** 数量、事件时间及重要数字使用 tabular numerals；等宽字体仅用于代码或参数指纹，不作泛化的“技术风格”装饰。

## Layout

共享 App Shell 顶栏约 68px 高，白底、细底边线；宽屏导航与连接状态同列。主内容最大宽度 1380px，桌面左右留白 32px，顶部留白 40px；页面区块以 24px 间距组织。页面标题区采用标题与操作并列，工具条在同一内容宽度内分布状态与元数据。信号数据以完整表格为核心；表格保持可读的最小宽度，在 681–1100px 区间收紧单元格横向留白。

900px 以下，顶栏允许换行，导航移至下一行并横向滚动；主内容留白调整为 20px。600px 以下，主内容左右留白 14px、顶部留白 24px，标题/操作和工具条改为纵向排列。680px 以下信号表转换成带显式字段标签的双列信息卡片，保留标的、状态、数量、原因、时间和资格的阅读顺序；移动端不强迫整表横向滚动。

布局以 4px 基数的紧凑间距节奏为主；常用间距为 8、12、16、24、32 与 40px。具体页面可按内容密度取用，不必强行补齐空白。

## Elevation & Depth

平面为主。常驻区域以白/浅灰蓝表面差异与 1px 细边框分组，不叠加常规投影。策略详情与筛选选项等定位浮层使用轻柔下投影，表明其覆盖关系；投影不是卡片的默认装饰。

### Named Rules
**浮层才投影规则。** 阴影只提示临时浮层高于内容层；普通内容区优先使用表面色和边线分组。

## Shapes

采用克制的小圆角和清晰矩形轮廓：常见边角 4–8px；信号标签 4px，按钮与浮层常用 5–6px，表格容器和品牌标记可至 8px。边界以细实线呈现；不使用胶囊形作为通用组件轮廓。禁用态通过降低不透明度与不可操作光标提示，键盘焦点用可见外描边和偏移。

## Components

基础组件紧凑、清楚、状态明确；把真实操作状态放在可读文字与一致控件反馈里。

### Page Heading
- 四个业务路由共用 `PageHeading`：标题、简短说明和可选的右侧操作/辅助信息采用同一内容结构。
- 页面继续提供原有的 `className`、标题 ID、说明样式与语义容器（`div`/`header`）；共享组件不规定页面间不同的间距、断点或操作样式。
- 标题保持页面级 `<h1>`；说明紧邻标题，右侧内容只承载当前页面已有操作或元数据。不要把业务专属状态、表单或逻辑塞入此组件。

### Buttons
- **形态：** 紧凑矩形、小圆角（6px）；常规操作最小高度 39px，水平内距 15px。
- **主要：** 深海蓝填充、白字，用于明确的主操作。
- **次要：** 白底、细蓝灰边线、深蓝文字；不改变禁用条件。
- **Hover / Disabled / Focus：** 可用悬停以背景或边框变化反馈；禁用态降低不透明度、显示不可操作光标；键盘焦点使用全局清晰外描边，不移除原生可达性。

### Chips
- **样式：** 紧凑矩形标签（4px 圆角、4px 7px 内距），浅色底与深色文字。
- **状态：** 方向参考用淡蓝，等待用中性灰，数据不可用/无资格用淡朱红，符合策略资格用淡绿。每种状态同时给出文本；颜色不替代语义。

### Cards / Containers
- **形态与背景：** 白色表面、细蓝灰边线，小圆角（7–8px）；信号状态面板内距 24px，工具条内距约 13px 16px。
- **阴影：** 常驻容器无阴影；参照 Elevation & Depth 的浮层规则。
- **状态：** 加载、空结果、认证要求与错误各自有明确文本；错误面板以浅暖底色和深朱红错误文本区分。

### Inputs / Fields
- **形态：** 轻量边线与浅色表面；标的筛选使用 5px 圆角、8px 11px 内距；原生复选框用深蓝 accent-color。
- **Focus / Disabled / Error：** 使用可见键盘焦点；不可操作控件明确禁用；错误状态以文本说明并提供可用恢复操作。

### Navigation
- **形态：** 白色共享顶栏，链接使用 13px 界面字体；常态文字为石板蓝灰，悬停浅灰蓝底，当前页使用深蓝文字、强调字重及 2px 底边线。
- **响应式：** 900px 以下导航换到下一行并横向滚动，链接最小高度 44px；600px 以下收紧品牌字标与外侧留白。
- **连接状态：** 文字状态配合 8px 圆点；圆点的灰色表示未连接，绿色表示已连接。未连接时提供明确的安全连接操作。

### Data Table / Signal Row

桌面信号数据用完整宽表呈现，最小宽度 1020px；白色表面、细行分隔、浅灰蓝表头。单元格内距约 15px 14px，说明内容使用紧凑字号。可用行悬停时使用微弱底色；不可用行不以悬停效果掩盖风险状态。窄屏改为带标签的两列信息卡，每项数据仍保留明确字段名；原因和事件时间跨列显示。

## Do's and Don'ts

### Do:
- **Do** 用浅灰蓝工作区承托白色内容面，保持深蓝为导航和主要交互强调色。
- **Do** 保持状态、理由、事件时间、时效和可用性可查；错误、加载、空结果和未连接状态均提供清楚文案。
- **Do** 以细边线和表面色组织常驻内容，只为实际浮层使用阴影。
- **Do** 在窄屏保留桌面信息顺序，将表格转成带字段标签的卡片。
- **Do** 使用键盘可见焦点、可操作控件的明确禁用样式、中文标签及等宽数字。

### Don't:
- **Don't** 从旧 Main 页面的遗留样式提取配色、布局、组件或氛围规则。
- **Don't** 只凭颜色表达状态，或把过期/不可用数据呈现成当前有效建议。
- **Don't** 把策略信号、执行资格或参考数量视觉化成已下单、已成交或自动交易。
- **Don't** 为常驻卡片、面板或内容表格添加默认投影、装饰性渐变或与核查无关的图表。
- **Don't** 在手机尺寸强迫信号表整体水平滚动，或省略桌面字段语义。
