# 06-App 移动端方案（筛选币种 + AI 分析，iOS / Android）

> 版本：2026-10-19（**设计稿，未实施**）
>
> 上游：现行 Web 前端 `frontend/`（[App.tsx](../../frontend/src/App.tsx) / [ScanResult.tsx](../../frontend/src/pages/ScanResult.tsx) / [ResultTable.tsx](../../frontend/src/components/ResultTable.tsx) / [AiAnalysisCard.tsx](../../frontend/src/components/AiAnalysisCard.tsx)）、后端 `backend/app/api/scan.py`（只读接口已现成）
>
> 核心代码：`frontend/src/api/scan.ts`（`scanApi`）、`frontend/src/types/index.ts`、`frontend/src/constants/labels.ts`、`frontend/src/utils/scheme.ts`、`backend/app/api/scan.py`
>
> 性质：**新增独立移动端 App 设计稿**。App 只读，复制 Web 端「筛选币种」「AI 分析」两个功能；**不改后端接口**——「每页最多 10 条」与「只出现正式网」在 App 端实现。
>
> 范围：仅做两个功能——**① 筛选币种结果列表（只读，最多 10 条）② AI 分析结果展示（只读）+ K 线图查看**。**不做**：手动触发/重新触发 AI 分析、手动开单、交易记录、复盘、现货、系统配置、日志、任务等。

## 一、需求与范围

### 1.1 目标

在 Web 工作台之外，提供 **iOS + Android 一套代码** 的轻量只读 App，让用户在手机上看：

1. **筛选币种**：最新一轮扫描命中的币种，**端上固定最多展示 10 条**；
2. **AI 分析**：点开某币种查看 AI 决策结论、三价、推荐度、推理文本，并可看 **K 线图**。

### 1.2 功能范围（拍板口径）

| 维度 | 口径 |
|---|---|
| 只读/可写 | **纯只读**——App 不发起任何写操作（不触发扫描、不触发 AI 分析、不开单） |
| 列表条数 | **最多 10 条**，不翻页（调用 `page_size=10`，只取第一页） |
| 网络口径 | **App 内完全不出现测试网**——无测试网开关、无测试网账号、无测试网单据/状态 |
| 平台 | iOS + Android（单套代码） |
| 登录/鉴权 | 本期不做（见 §九 风险 1，建议后续加只读 token） |

### 1.3 明确不做（防止范围蔓延）

- 手动触发 AI 分析（Web 端 `POST /scans/{id}/ai-analyses`、`POST /scans/analyze` 不在 App 内）；
- 「开单」按钮与开单状态列（该列会 join `trade_records`，是引入测试网概念的唯一入口，故整体移除）；
- 关注列表、待确认池、复盘、交易记录、资金分析、现货、策略/系统配置、日志、任务记录、CODE WIKI。

## 二、技术选型

### 2.1 结论

**React Native 0.7x + Expo（managed workflow）+ TypeScript**，一套代码出 iOS/Android 包。

### 2.2 逐项选型

| 层 | 选型 | 理由 / 备注 |
|---|---|---|
| 框架 | React Native + Expo（managed） | 与现有 Web 前端同属 React/TS 心智，复用度最高；EAS 一套代码出双端 |
| 语言 | TypeScript | 与现有 `frontend/src` 类型体系一致，类型可直接裁剪复用 |
| 导航 | `@react-navigation/native`（native-stack） | **不采用 expo-router**——避开顶层 `app/` 目录与 expo-router 路由目录 `app/` 同名冲突 |
| 状态管理 | `zustand` | 与 [scanStore.ts](../../frontend/src/stores/scanStore.ts) 同源，精简移植 |
| 网络 | `axios` | 直接裁剪复用 [scan.ts](../../frontend/src/api/scan.ts) |
| 本地持久化 | `@react-native-async-storage/async-storage` | 替代 Web 端 `localStorage`（涨跌配色方案等） |
| K 线图 | 见 §七（首选 Skia 自绘 / wagmi-charts；兜底 WebView 内嵌） | `lightweight-charts` 是 Web Canvas 库，RN 不可用 |
| 构建/发布 | EAS Build + `expo-updates`（OTA 热更） | iOS 需 Apple 开发者账号（$99/年） |

### 2.3 为什么不是 Flutter / uni-app / 原生

| 备选 | 否决理由 |
|---|---|
| Flutter | 性能与图表一致性最好，但与现有 React 前端**零代码复用**，需 Dart 重写全部逻辑 |
| uni-app（Vue） | 可一套出双端+小程序，但需新引入 Vue 技术栈，与现有 React 体系割裂 |
| 原生 Swift + Kotlin | 双端两套代码，开发与维护成本最高，不适合双功能只读小 App |

## 三、目录结构（新增顶层 `app/`）

```
trading-workbench/
├── frontend/                     # 现有 Web 工作台（不动）
├── backend/                      # 现有后端（不动）
└── app/                          # ★ 新增：Expo 移动端工程
    ├── app.json                  # Expo 配置（名称/图标/包名 com.xxx.workbench）
    ├── eas.json                  # EAS 构建配置（development/preview/production）
    ├── package.json
    ├── tsconfig.json
    ├── babel.config.js
    └── src/
        ├── api/
        │   ├── client.ts         # axios 实例：baseURL → 生产后端 HTTPS
        │   └── scan.ts           # 裁剪版 scanApi（只保留 §五 的只读接口）
        ├── types/index.ts        # 裁剪：ScanResult / AIAnalysis / KlineData / ScanStatus / ListResponse
        ├── constants/labels.ts   # 直接拷贝（纯常量映射）
        ├── store/scanStore.ts    # 精简版：results / aiAnalyses / latest / status
        ├── utils/
        │   ├── scheme.ts         # 直接拷贝（hexToRgba / schemeTag）
        │   ├── dayjs.ts          # 直接拷贝（北京时间格式化）
        │   └── aiText.ts         # ★ 从 AiAnalysisCard 抽出的纯解析函数（无 UI）
        ├── navigation/index.tsx  # native-stack：List → Detail
        ├── screens/
        │   ├── ScanListScreen.tsx    # 筛选结果列表（最多 10 条卡片）
        │   └── CoinDetailScreen.tsx  # AI 分析卡 + K 线图
        └── components/
            ├── ResultCard.tsx        # 行卡片（替代宽表格）
            ├── AiAnalysisCard.tsx    # 移植解析逻辑 + RN 视图
            └── KlineChart.tsx        # K 线（§七 选型）
```

> 命名说明：顶层目录用 `app/`（用户约定），内部到 `src/`，导航用 React Navigation，因此不会与 expo-router 的 `app/` 路由约定冲突。

## 四、复用与重写清单

### 4.1 可直接拷贝（纯 TS，无 DOM / 无 antd 依赖）

| 现有文件 | 用途 | 改动 |
|---|---|---|
| `frontend/src/constants/labels.ts` | 信号类型/形态/EMA/拦截原因等标签映射 | 0 |
| `frontend/src/utils/scheme.ts` | 涨跌配色（`hexToRgba` / `schemeTag` / `SCHEME_UP_DOWN`） | 0 |
| `frontend/src/utils/dayjs.ts` | 北京时间格式化 `bj()` | 0 |
| `frontend/src/api/scan.ts` | 只保留 §五 的 5 个只读方法 | 裁剪 |
| `frontend/src/types/index.ts` | 只保留 `ScanResult` / `AIAnalysis` / `KlineData` / `ScanStatus` / `ListResponse` | 裁剪 |
| `frontend/src/components/AiAnalysisCard.tsx` 纯函数 | `getEffectiveDecision` / `parseAnalysisSections` / `analysisLines` / `parseItem` | 抽到 `utils/aiText.ts`（无 UI） |

### 4.2 必须重写（RN 无对应）

| Web 实现 | RN 实现 | 说明 |
|---|---|---|
| antd `<Table>`（结果表，16 列） | `FlatList` + `ResultCard` | 移动端不放宽表格，改**卡片式列表**，只留核心字段 |
| antd `Tag / Tooltip / Modal / Button` | RN `View / Text / Pressable` | 长文本改**折叠展示**；Tooltip 改为可点击的详情或长按说明 |
| `lightweight-charts`（K 线） | RN 图表库（§七） | 不可复用 |
| 双栏（左表右图）布局 | 两屏：列表屏 → 详情屏 | 移动端改路由跳转 |
| `localStorage` | AsyncStorage | 配色方案等 |
| `AiExpandContent` / `AiProgressPanel` 的流式进度 | 静态结果卡（本期不需要流式） | 只读展示通常分析已完成；如列表存在「分析中」行，可在详情页做一次轻量轮询 |

### 4.3 列表卡片字段设计（移动端信息密度）

从 [ResultTable.tsx](../../frontend/src/components/ResultTable.tsx) 的 16 列中裁剪为核心字段：

| 字段 | 来源列 | 展示 |
|---|---|---|
| 币种 | `symbol`（去 USDT 后缀） | 标题 |
| 质量分 | `quality_score` | 右侧徽标（含方向票 tooltip 可点开） |
| 信号类型 | `signal_type` | Tag（含 `signal_reason`） |
| 位置 | `position_ref` / `touch_ref` | Tag（可省略或长按说明） |
| 4h 背景 | `votes_json.htf` | Tag（短标签） |
| K线形态 / EMA | `pattern` / `ema_state` | Tag |
| AI 方向 | `ai_analyses.direction` | 多/空 Tag |
| AI 推荐度 | `ai_analyses.recommendation` | 数值 |
| 当前价 / 24h 成交额 | `current_price` / `volume_24h` | 次要信息 |

**移除**：操作列（开单）、开单状态列（测试网概念入口）、关注/复制按钮。

## 五、接口契约与数据流（App 端只读，零后端改动）

### 5.1 只读接口清单（全部现成）

| 用途 | 方法 | 接口 | 关键参数 |
|---|---|---|---|
| 扫描状态 | GET | `/api/scans/status` | 取 `last_scan.id` |
| 筛选结果（10 条） | GET | `/api/scans/latest/results` | `page=1&page_size=10&sort_by=quality_score&order=desc` |
| 该轮 AI 分析 | GET | `/api/scans/{scanId}/ai-analyses` | 按 `scan_result_id` 建 `aiMap` |
| K 线 | GET | `/api/scans/klines/{symbol}` | `limit=500`，`interval=1h`（详情页可切 `4h`） |
| 分析进度（可选） | GET | `/api/scans/ai-progress/{scanResultId}` | 仅当列表存在「分析中」行时轻量轮询 |

### 5.2 数据流

```
App 启动
  └─ GET /scans/status              → last_scan.id
  └─ GET /scans/latest/results?page_size=10
                                    → 列表（≤10 条）
  └─ GET /scans/{scanId}/ai-analyses
                                    → aiMap: scan_result_id → AIAnalysis
点击某币种（Detail）
  └─ GET /scans/klines/{symbol}?limit=500&interval=1h
                                    → K 线 + 用 aiMap[symbol行] 画入场/止损/止盈标线
```

`aiMap` 构建逻辑与 [ResultTable.tsx](../../frontend/src/components/ResultTable.tsx) 中 `aiMap`（按 `scan_result_id` 索引）一致，直接移植。

## 六、正式网口径与 10 条上限（关键设计）

### 6.1 「只出现正式网」如何达成——结论：App 端不加过滤即天然成立

- [scan.py](../../backend/app/api/scan.py) 的 `latest/results`、`ai-analyses`、`klines` 返回的都是**行情/信号数据**，字段里**没有 `testnet` 维度**；
- 测试网/正式网只在**交易维度**区分（`trade_records.testnet`，见 [config.py](../../backend/app/config.py) 的 live/testnet 账号体系与 `trading_enabled_live/testnet`）；
- 因此唯一会把测试网概念带进 App 的是**「开单状态」列**（join 了 `trade_records`）。**App 列表卡片整体移除该列 → App 内不再出现任何测试网信息，无需后端改动。**

> 若后续要给 App 增加「已开单」只读展示，必须调用 `GET /api/trades?scope=live`（[scan.ts](../../frontend/src/api/scan.ts) 的 `trades.list(status, limit, scope='live')`）**显式限定正式网**，绝不可用 `scope=all`。

### 6.2 「最多 10 条」如何达成——结论：App 端固定 `page_size=10`

- 列表固定传 `page_size=10` 且**不渲染分页/加载更多**；
- 注意 `ListResponse.total` 是全部命中数（可能 >10），App **不展示 total、不做翻页**，感官上就是「最多 10 条」；
- 与后端 `AI_MAX_PER_SCAN=10`（[config.py](../../backend/app/config.py) 单次扫描按 24h 成交额取前 10 做 AI 分析）口径天然对齐——列表前 10 条基本都有 AI 分析结果。

> 备选（不推荐）：后端为 App 新增 `max_items` 参数。属多余复杂度，App 端固定 `page_size` 已满足需求。

## 七、K 线图选型与备选

`lightweight-charts` 是 Web Canvas 库，RN 不可用，必须替换。三个方案按推荐顺序：

| 方案 | 库 | 优点 | 缺点 |
|---|---|---|---|
| **A（推荐起步）** | `react-native-webview` 内嵌现有 `KlineChart` 页面 | 复用现有 K 线逻辑最快、风险最低 | WebView 性能/交互略逊，标线需通信 |
| **B** | `@shopify/react-native-skia` 自绘蜡烛图 | 性能最好、完全可控 | 自绘成本高（坐标轴/十字光标/缩放） |
| **C** | `react-native-wagmi-charts` | 现成 candlestick 组件 | 功能偏简，标线/多周期需自行扩展 |

**建议**：先做方案 A 跑通端到端（列表→详情→K 线→三价标线），再评估是否换 B。**不要一上来就自绘**。

## 八、打包与发布

- **EAS Build**：`eas.json` 配 development / preview / production 三档 profile；
- **iOS**：需 Apple 开发者账号；TestFlight 内测 → App Store 上架（注意加密货币类目审核较严，见 §九）；
- **Android**：EAS 生成签名 APK/AAB；内测侧载 APK，正式走 Google Play；
- **OTA**：`expo-updates` 对纯 JS 改动做热更，无需重新过审（原生依赖变更仍需重新构建）；
- **后端地址**：App 内固化为生产后端 HTTPS（复用现有 nginx `location /api/` 反代，见 [nginx.conf](../../frontend/nginx.conf)）；开发期用环境变量切本地。

## 九、风险与缓解

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| 1 | **后端无鉴权**：[main.py](../../backend/app/main.py) CORS `allow_origins=["*"]`、无 token，写接口（触发扫描/改配置/开单）公开 | 后端地址固化进 App 后，暴露面风险上升 | App 纯只读，不含写接口；建议后续为写接口加只读/写权限分离的 API Key 校验 |
| 2 | **K 线库选型不确定** | 详情页图表可能返工 | 按 §七 先用 WebView 兜底验证 |
| 3 | **上架合规** | iOS 审核可能被拒 | 提前确认加密货币类目政策；准备仅行情/分析、无交易的定位说明 |
| 4 | **信息密度** | 16 列宽表在手机不可读 | 按 §4.3 重设计卡片，只留核心字段 |
| 5 | **AI 文本过长** | 推理文本溢出 | 默认折叠 + 展开全文；复用 `parseAnalysisSections` 分区（公共检查/决策结论） |
| 6 | **无登录** | 任何拿到地址的人可看 | 本期接受（只读）；后续加 token |

## 十、里程碑与验收标准

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| M1 骨架 | Expo 工程 + 导航 + axios client + 类型/常量移植 | 真机/模拟器可跑，能打通 `/scans/status` |
| M2 列表 | `latest/results` 固定 10 条卡片 | 列表恰好 ≤10 条、无分页、卡片字段与 §4.3 一致 |
| M3 详情 | AI 分析卡（移植解析纯函数 + RN 视图） | 决策/三价/推荐度/推理分区展示正确 |
| M4 K 线 | 图表接入 + 三价标线 + 1h/4h 切换 | 标线与 AI 三价一致，切换周期正常 |
| M5 打包 | EAS 出 iOS/Android 包 + OTA | 双端可安装，正式网数据口径复核通过 |

**端到端验收铁律**：App 内**不出现任何**测试网标识、`testnet` 字样、测试网单据；列表条数**恒 ≤10**；无任何写操作请求。

## 十一、待拍板 / 未决

1. **App 名称、图标、包名**（`com.xxx.workbench`）与 App Store/Play 上架主体；
2. **后端访问地址**：复用现有域名 `/api` 还是新开 `api.` 子域；
3. **是否需要只读 token**（风险 1 的缓解是否本期做）；
4. **K 线方案最终定档**（A 验证后再定 B/C）；
5. 是否保留「4h 背景」等次级字段（信息密度取舍）。

---

> 附：本文为**设计稿**。实施顺序建议严格按 §十 M1→M5；每阶段完成后按验收标准复核，尤其是 §6 「正式网口径」与「10 条上限」的端到端复核。
