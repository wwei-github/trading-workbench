import {
  Table,
  Tag,
  Tooltip,
  Empty,
  Button,
  Modal,
  Checkbox,
  message,
} from "antd";
import {
  CopyOutlined,
  ReloadOutlined,
  StarOutlined,
  StarFilled,
} from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import type { ReactNode } from "react";
import { useScanStore } from "../stores/scanStore";
import { scanApi } from "../api/scan";
import { schemeTag } from "../utils/scheme";
import { copyText } from "../utils/clipboard";
import {
  SIGNAL_TYPE_MAP,
  SIGNAL_TYPE_FILTERS,
  PATTERN_FILTERS,
  EMA_STATE_FILTERS,
  emaStateTag,
  patternStyle,
} from "../constants/labels";
import { useState, useMemo, useEffect, useCallback, useRef } from "react";
import KlineChart from "./KlineChart";
import AiExpandContent from "./AiExpandContent";
import type { AIAnalysis, ScanResult } from "../types";

// 交易状态中文名（"开单状态"列 tooltip 用，与交易记录 Tab 口径一致）
const TRADE_STATUS_LABEL: Record<string, string> = {
  OPENED: "运行中",
  TP1_HIT: "部分止盈",
  TP2_HIT: "已止盈·跟踪止损",
  CLOSED: "已平仓",
  FAILED: "开单失败",
};

// 回踩触点（docs/22 触点体系，与后端 trigger.TOUCH_LABELS 同源）：
// 颜色/悬停说明固定，展示文案由 touchLabel 按方向生成（EMA21 / 前高回测 / 摆动低点…）
const TOUCH_TAG_MAP: Record<string, { color: string; tip: string }> = {
  retest: { color: "geekblue", tip: "突破回测位：破轨后横向验证，结构意义最强" },
  swing: { color: "purple", tip: "摆动结构位：最近 HL（多）/LH（空）防守位" },
  ema21: { color: "gold", tip: "回踩 EMA21 趋势价值区企稳" },
  ema55: { color: "orange", tip: "回踩 EMA55 深回撤价值区（数据/排列/斜率门已过）" },
  ema144: { color: "volcano", tip: "回踩 EMA144 大周期深回撤价值区（数据/排列/斜率门已过）" },
};

// 回踩位置展示文案：EMA 触点直接显示周期；retest/swing 按方向给 前高/前低 措辞。
// 方向推断：指标多数决净票（程序判定方向的真实依据）→ 趋势斜率符号兜底
function touchLabel(ref: string, record: ScanResult): string {
  if (ref === "ema21") return "EMA21";
  if (ref === "ema55") return "EMA55";
  if (ref === "ema144") return "EMA144";
  const votes = record.votes_json;
  const dir =
    votes && votes.net !== 0
      ? votes.net > 0
        ? 1
        : -1
      : record.trend_slope > 0
        ? 1
        : record.trend_slope < 0
          ? -1
          : 0;
  if (ref === "retest") return dir > 0 ? "前高回测" : dir < 0 ? "前低回测" : "突破回测位";
  if (ref === "swing") return dir > 0 ? "摆动低点" : dir < 0 ? "摆动高点" : "摆动结构位";
  return ref;
}

export default function ResultTable({ headerExtra }: { headerExtra?: ReactNode }) {
  const {
    results,
    total,
    loading,
    resultsPage,
    resultsPageSize,
    setFilters,
    currentScanId,
    aiAnalyses,
    analyzingMap,
    triggerAiAnalysis,
    fetchResults,
    fetchStatus,
    fetchTasks,
    aiConfig,
    watchlist,
    addToWatchlist,
    removeFromWatchlist,
  } = useScanStore();
  // 全局涨跌配色方案：多空、胜负标签随「红涨绿跌/绿涨红跌」翻转
  const colorScheme = useScanStore((s) => s.colorScheme);

  const aiEnabled = !!aiConfig?.ai_analysis_enabled;
  // 开单状态映射：ai_analysis_id -> { status: 最近交易状态, nets: 已开过的网络集合 }。
  // nets 用于「开单」按钮置灰判定：双网（testnet+live）都有记录才禁点，
  // 单网已开仍可点（另一网补开——后端按账号判重，docs/17 P1 多账号各开一单）。
  // 随结果列表刷新拉取（limit 500 足够覆盖当前页分析的近期交易）
  const [tradeMap, setTradeMap] = useState<Record<string, { status: string; nets: boolean[] }>>({});
  // tradeMap 拉取（"开单"按钮点击后手动调用刷新，与 results effect 共用）
  const refreshTradeMap = useCallback(async () => {
    try {
      const rows = await scanApi.trades.list(undefined, 500);
      const m: Record<string, { status: string; nets: boolean[] }> = {};
      for (const t of rows) {
        if (!t.ai_analysis_id) continue;
        const cur = m[t.ai_analysis_id] ?? { status: t.status, nets: [] };
        cur.status = t.status;
        if (!cur.nets.includes(t.testnet)) cur.nets.push(t.testnet);
        m[t.ai_analysis_id] = cur;
      }
      setTradeMap(m);
    } catch {
      // 静默：开单状态列缺失可接受，下次列表刷新重拉
    }
  }, []);
  useEffect(() => {
    refreshTradeMap();
  }, [results, currentScanId, refreshTradeMap]);
  const watchedSymbols = useMemo(
    () => new Set(watchlist.map((w) => w.symbol)),
    [watchlist],
  );

  // 展开行：同时控制左侧 AI 分析折叠和右侧图表
  const [expandedRowKeys, setExpandedRowKeys] = useState<string[]>([]);

  // 默认展开第一行
  useEffect(() => {
    if (results.length > 0) {
      setExpandedRowKeys((prev) => {
        // 保留当前展开的行（如果还在结果中）
        const stillValid = prev.filter((id) =>
          results.find((r) => r.id === id),
        );
        return stillValid.length > 0 ? stillValid : [results[0].id];
      });
    }
  }, [results]);

  // 按 scan_result_id 索引 AI 分析
  const aiMap: Record<string, AIAnalysis> = useMemo(() => {
    const m: Record<string, AIAnalysis> = {};
    for (const a of aiAnalyses) {
      if (a.scan_result_id) m[a.scan_result_id] = a;
    }
    return m;
  }, [aiAnalyses]);

  // 图表跟随展开的行
  const selectedRecord =
    results.find((r) => expandedRowKeys.includes(r.id)) || null;
  const selectedAi = selectedRecord ? aiMap[selectedRecord.id] : undefined;

  // 每行的 AI 补充说明输入（scanResultId -> 输入内容）
  const [userInputs, setUserInputs] = useState<Record<string, string>>({});

  // 刷新：先取最新扫描记录 ID，再拉取该次扫描的结果（原顶部工具栏的刷新按钮）
  const handleRefresh = async () => {
    await fetchStatus();
    const latestScanId = useScanStore.getState().status?.last_scan?.id;
    if (latestScanId) {
      await fetchResults(latestScanId);
    } else {
      fetchResults();
    }
    fetchTasks();
  };

  const handleReAnalyze = async (record: ScanResult) => {
    if (!currentScanId) {
      message.warning("无当前扫描记录");
      return;
    }
    try {
      await triggerAiAnalysis(
        currentScanId,
        record.id,
        userInputs[record.id]?.trim() || undefined,
      );
      message.success("AI 分析已提交，请稍候...");
    } catch (e: any) {
      message.error(e?.message || "AI 分析失败");
    }
  };

  // ── 手动开单（2026-09-27）─────────────────────────────────────────────
  // 推荐度门槛（前端启发式预判；后端 TRADING_MIN_RECOMMENDATION=60 为权威，
  // 不一致时以点击后端闸门判定为准）
  const MIN_RECOMMENDATION = 60;

  // 置灰判定：返回不可点原因文本；null = 可点。纯前端启发式——
  // 钱包/在跑上限等运行时条件由点击后端闸门权威判定（拦截原因落库展示）
  const getOpenDisabledReason = (record: ScanResult): string | null => {
    const ai = aiMap[record.id];
    if (analyzingMap[record.id]?.loading) return "AI 分析中，请稍候";
    if (!ai) return "无 AI 分析";
    if (ai.trade_decision !== "suggest") {
      return ai.skip_reason ? `AI 建议跳过：${ai.skip_reason}` : "AI 建议跳过";
    }
    if (!ai.direction || !ai.entry_price || !ai.stop_loss || !ai.take_profit_1) {
      return "AI 未给出完整开单位";
    }
    if ((ai.recommendation ?? 0) < MIN_RECOMMENDATION) {
      return `推荐度 ${Math.round(ai.recommendation ?? 0)} 低于门槛 ${MIN_RECOMMENDATION}`;
    }
    // 信号时效与自动开仓同口径（try_open_for_analysis：1 小时内有效）。
    // 后端 UTC-naive，末尾无 Z，必须补 Z 按 UTC 解析（本地解析会偏移 8h）
    if (ai.created_at) {
      const ageMin = (Date.now() - new Date(`${ai.created_at}Z`).getTime()) / 60000;
      if (ageMin > 60) return "信号已过期（分析超过 1 小时）";
    }
    // 置灰铁律（2026-10-09）：仅当测试网+正式网都有该单记录才禁点；
    // 单网已开仍可点（另一网补开，后端按账号判重兜底）
    const tradeInfo = tradeMap[ai.id];
    if (tradeInfo && tradeInfo.nets.includes(true) && tradeInfo.nets.includes(false)) {
      const label = TRADE_STATUS_LABEL[tradeInfo.status] || tradeInfo.status;
      return tradeInfo.status === "FAILED"
        ? `已有开单尝试（${label}），不支持重试`
        : `已开单（测试网+正式网，${label}）`;
    }
    return null;
  };

  // 行级 loading 防连点（scanResultId -> true）
  const [openLoadingMap, setOpenLoadingMap] = useState<Record<string, boolean>>({});

  // 手动开单网络勾选（2026-10-10 改版）：两个复选框按网选单（每网内部取该网
  // 第一个已配置账号下单），替代原 Radio 选账号。已开单的网禁选并标注，
  // 另一网默认选中（测试网已开 → 默认勾正式网）；两网都没开 → 只勾测试网
  //（实盘安全基线，同旧默认 testnet-1）。ref 收集勾选——Modal.confirm 命令式
  // 弹窗不响应组件级 state
  const openNetsRef = useRef<{ testnet: boolean; live: boolean }>({
    testnet: false,
    live: false,
  });

  const handleOpenTrade = (record: ScanResult) => {
    const ai = aiMap[record.id];
    if (!ai) return;
    const dirLabel = ai.direction === "long" ? "开多" : "开空";
    const accs = (aiConfig?.accounts ?? []).filter((a) => a.configured);
    const testnetAcc = accs.find((a) => a.testnet);
    const liveAcc = accs.find((a) => !a.testnet);
    const nets = tradeMap[ai.id]?.nets ?? [];
    const testnetOpened = nets.includes(true);
    const liveOpened = nets.includes(false);
    const init = { testnet: !testnetOpened, live: testnetOpened && !liveOpened };
    openNetsRef.current = { ...init };
    const netCheckbox = (
      net: "testnet" | "live",
      tag: string,
      acc: { key: string; name: string } | undefined,
      opened: boolean,
    ) => (
      <Checkbox
        defaultChecked={init[net]}
        disabled={opened || !acc}
        onChange={(e) => {
          openNetsRef.current[net] = e.target.checked;
        }}
      >
        <Tag color={net === "testnet" ? "orange" : "volcano"} style={{ marginInlineEnd: 4 }}>
          {tag}
        </Tag>
        {acc ? (
          <>
            {acc.name}
            <span style={{ color: "#999", marginLeft: 4 }}>{acc.key}</span>
          </>
        ) : (
          <span style={{ color: "#fa8c16" }}>无已配置账号</span>
        )}
        {opened && <span style={{ color: "#999", marginLeft: 4 }}>（已开单，禁选）</span>}
      </Checkbox>
    );
    Modal.confirm({
      title: `确认手动开单：${record.symbol} ${dirLabel}？`,
      content: (
        <div style={{ fontSize: 12, lineHeight: 1.9 }}>
          <div>入场价：{ai.entry_price}　止损：{ai.stop_loss}　止盈一：{ai.take_profit_1}</div>
          <div>推荐度：{Math.round(ai.recommendation ?? 0)}</div>
          <div style={{ marginTop: 8 }}>
            <div style={{ fontWeight: 600, marginBottom: 4 }}>开单网络（正式网为实盘，请确认）</div>
            {accs.length === 0 ? (
              <div style={{ color: "#fa8c16" }}>账号清单未加载，请先打开「系统配置」确认服务器 .env 已配置账号</div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {netCheckbox("testnet", "测试网", testnetAcc, testnetOpened)}
                {netCheckbox("live", "正式网", liveAcc, liveOpened)}
              </div>
            )}
          </div>
          {ai.open_block_reason && (
            <div style={{ color: "#fa8c16" }}>上次拦截：{ai.open_block_reason}</div>
          )}
        </div>
      ),
      okText: "确认开单",
      cancelText: "取消",
      onOk: async () => {
        const targets: Array<{ net: string; accKey: string }> = [];
        if (openNetsRef.current.testnet && testnetAcc)
          targets.push({ net: "测试网", accKey: testnetAcc.key });
        if (openNetsRef.current.live && liveAcc)
          targets.push({ net: "正式网", accKey: liveAcc.key });
        if (targets.length === 0) {
          message.warning("请至少勾选一个可开单的网络");
          return Promise.reject();
        }
        setOpenLoadingMap((m) => ({ ...m, [record.id]: true }));
        try {
          // 逐网顺序开单（共享 IP 权重，同自动扇出惯例）；单网失败不中断另一网
          const results: string[] = [];
          let okCount = 0;
          for (const t of targets) {
            try {
              const res = await scanApi.trades.openFromAnalysis(ai.id, t.accKey);
              if (res.opened) {
                okCount += 1;
                results.push(`${t.net}开单成功（${res.account_key}）`);
              } else {
                results.push(`${t.net}未开：${res.block_reason || "闸门未过"}`);
              }
            } catch (e: any) {
              results.push(`${t.net}失败：${e?.response?.data?.detail || e?.message || "请求异常"}`);
            }
          }
          const summary = `${record.symbol}：${results.join("；")}`;
          if (okCount === targets.length) message.success(summary, 5);
          else if (okCount > 0) message.warning(summary, 5);
          else message.error(summary, 5);
          await refreshTradeMap();
        } finally {
          setOpenLoadingMap((m) => ({ ...m, [record.id]: false }));
        }
      },
    });
  };

  // 服务端过滤（antd 官方模式）：列只声明 filters（非受控，不传 filteredValue），
  // 筛选/翻页统一在 Table 的 onChange 里按 extra.action 分发。
  // 注意1：antd 列级没有 onChange 属性，写在列上会被静默忽略。
  // 注意2：pagination 不要传 onChange——antd 筛选确认时会额外回调它，
  // 导致带着旧 filters 的重复请求竞态（UI 停留在重置前状态）。
  const filterProps = (
    key: "signal_type" | "pattern" | "ema_state",
    options: { text: string; value: string }[],
  ) => ({
    filters: options,
    filterMultiple: false,
  });

  const handleTableChange: NonNullable<
    React.ComponentProps<typeof Table<ScanResult>>["onChange"]
  > = (tablePagination, tableFilters, _sorter, extra) => {
    if (extra.action === "filter") {
      setFilters({
        signal_type: (tableFilters.signal_type?.[0] as string) || undefined,
        pattern: (tableFilters.pattern?.[0] as string) || undefined,
        ema_state: (tableFilters.ema_state?.[0] as string) || undefined,
      });
      return;
    }
    if (extra.action === "paginate") {
      fetchResults(undefined, tablePagination.current ?? 1, tablePagination.pageSize);
    }
    // sort 走客户端排序，无需处理
  };

  const handleCopySymbol = async (e: React.MouseEvent, symbol: string) => {
    e.stopPropagation();
    const text = symbol + ".P";
    // navigator.clipboard 在 http 非 localhost 下为 undefined，copyText 内降级 execCommand
    if (await copyText(text)) {
      message.success(`已复制: ${text}`);
    } else {
      message.error("复制失败（浏览器未授权剪贴板）");
    }
  };

  const handleToggleWatch = async (e: React.MouseEvent, record: ScanResult) => {
    e.stopPropagation();
    try {
      if (watchedSymbols.has(record.symbol)) {
        await removeFromWatchlist(record.symbol);
        message.info(`已取消关注 ${record.symbol}`);
      } else {
        await addToWatchlist(record.symbol);
        message.success(`已关注 ${record.symbol}`);
      }
    } catch (err: any) {
      message.error(err?.response?.data?.detail || "关注操作失败");
    }
  };

  const columns: ColumnsType<ScanResult> = [
    {
      // 操作列：手动开单（2026-09-27）。置灰条件=无 AI 分析/建议跳过/缺价位/
      // 推荐度不足/信号过期/已开单；钱包/在跑上限等运行时条件由点击后端闸门
      // 权威判定。disabled Button 不触发 hover——外层 span 承接 Tooltip
      title: "操作",
      key: "actions",
      width: 76,
      render: (_: unknown, record: ScanResult) => {
        const ai = aiMap[record.id];
        const reason = getOpenDisabledReason(record);
        const disabled = reason !== null;
        // 单网已开（按钮仍可点）：tooltip 提示已开/可补开的网络
        const nets = (ai ? tradeMap[ai.id]?.nets : undefined) ?? [];
        const netHint = !disabled && nets.length === 1
          ? `（${nets[0] ? "测试网" : "正式网"}已开，可再开${nets[0] ? "正式网" : "测试网"}）`
          : "";
        const tip = disabled
          ? reason!
          : `手动开单：${record.symbol} ${ai?.direction === "long" ? "开多" : "开空"}（当前账号，走完整开仓闸门）${netHint}`
            + (ai?.open_block_reason ? `　上次拦截：${ai.open_block_reason}` : "");
        return (
          <Tooltip title={tip}>
            <span>
              <Button
                type="link"
                size="small"
                style={{ padding: 0 }}
                disabled={disabled}
                loading={!!openLoadingMap[record.id]}
                onClick={(e) => {
                  e.stopPropagation();
                  handleOpenTrade(record);
                }}
              >
                开单
              </Button>
            </span>
          </Tooltip>
        );
      },
    },
    {
      title: "币种",
      dataIndex: "symbol",
      key: "symbol",
      width: 150,
      render: (v: string, record: ScanResult) => (
        <span>
          <strong>{v.replace("USDT", "")}/USDT</strong>
          <Tooltip title={watchedSymbols.has(v) ? "取消关注" : "加入关注列表"}>
            <Button
              type="text"
              size="small"
              icon={
                watchedSymbols.has(v) ? (
                  <StarFilled style={{ color: "#faad14" }} />
                ) : (
                  <StarOutlined />
                )
              }
              onClick={(e) => handleToggleWatch(e, record)}
              style={{ marginLeft: 2, padding: "0 4px" }}
            />
          </Tooltip>
          <Button
            type="text"
            size="small"
            icon={<CopyOutlined />}
            onClick={(e) => handleCopySymbol(e, v)}
            style={{ marginLeft: 2, padding: "0 4px" }}
          />
        </span>
      ),
    },
    {
      title: "质量分",
      dataIndex: "quality_score",
      key: "quality_score",
      width: 80,
      defaultSortOrder: "descend",
      sorter: (a: ScanResult, b: ScanResult) =>
        (a.quality_score ?? -1) - (b.quality_score ?? -1),
      render: (v: number | null, record: ScanResult) => {
        if (v == null) return <span style={{ color: "#999" }}>-</span>;
        const votes = record.votes_json;
        const detail = votes
          ? `指标多数决：多${votes.bull_n} vs 空${votes.bear_n}（净${votes.net > 0 ? "+" : ""}${votes.net} / 已投${votes.voted}）`
            + (votes.bull.length ? `\n看多：${votes.bull.join("、")}` : "")
            + (votes.bear.length ? `\n看空：${votes.bear.join("、")}` : "")
          : undefined;
        const color = v >= 75 ? "green" : v >= 60 ? "gold" : "default";
        return (
          <Tooltip title={detail}>
            <Tag color={color} style={{ marginInlineEnd: 0, cursor: "default" }}>
              {v}
            </Tag>
          </Tooltip>
        );
      },
    },
    {
      title: "信号类型",
      dataIndex: "signal_type",
      key: "signal_type",
      width: 108,
      ...filterProps("signal_type", SIGNAL_TYPE_FILTERS),
      render: (v: string, record: ScanResult) => {
        const cfg = SIGNAL_TYPE_MAP[v] || { label: v, color: "default" };
        // 触发原因挂信号类型悬停（突破/回踩行无 K线形态，原因只在这里可见）
        return (
          <Tooltip title={record.signal_reason || undefined}>
            <Tag color={cfg.color}>{cfg.label}</Tag>
          </Tooltip>
        );
      },
    },
    {
      // 位置（2026-10-01）：命中位置双口径——回踩确认=回踩企稳的触点位，
      // 突破触发=突破/跌破的结构前高前低或 20 根 Donchian 轨（附位价）；
      // 旧行无 position_ref 时回退 touch_ref→touchLabel
      title: (
        <Tooltip title="命中位置：回踩确认=回踩企稳的触点位（EMA21/前高回测/摆动低点等），突破触发=突破或跌破的结构前高/前低/20根Donchian轨，附突破位价格">
          <span>位置</span>
        </Tooltip>
      ),
      dataIndex: "position_ref",
      key: "position_ref",
      width: 150,
      render: (v: string | null, record: ScanResult) => {
        if (v) {
          const isPullback = v.startsWith("回踩");
          const tip = isPullback
            ? TOUCH_TAG_MAP[record.touch_ref || ""]?.tip
            : "突破/跌破的结构位或趋势轨，价格即突破位（信号K线收盘越过该位触发）";
          return (
            <Tooltip title={tip}>
              <Tag
                color={isPullback ? TOUCH_TAG_MAP[record.touch_ref || ""]?.color || "purple" : "geekblue"}
                style={{ marginInlineEnd: 0 }}
              >
                {v}
              </Tag>
            </Tooltip>
          );
        }
        const ref = record.touch_ref;
        if (!ref) return <span style={{ color: "#999" }}>-</span>;
        const touch = TOUCH_TAG_MAP[ref];
        return (
          <Tooltip title={touch?.tip}>
            <Tag color={touch?.color || "default"} style={{ marginInlineEnd: 0 }}>
              {touchLabel(ref, record)}
            </Tag>
          </Tooltip>
        );
      },
    },
    {
      // 4h方向列：扫描时 4h 背景级别（震荡/纠缠/趋势年龄期别），来自 votes_json.htf
      // 表头简称 4h（2026-09-30 用户拍板）；完整说明保留在悬停
      title: (
        <Tooltip title="扫描时 4h 高周期背景（r2 趋势年龄口径）：EMA21/55 交叉后根数分前期≤10/中期≤30/后期>30，柱线翻向=衰竭期禁开新单；EMA55 vs EMA144 定宏观同侧/逆侧；悬停查看完整判定细节">
          <span>4h</span>
        </Tooltip>
      ),
      key: "htf_direction",
      width: 72,
      render: (_: unknown, record: ScanResult) => {
        const htf = record.votes_json?.htf;
        if (!htf) return <span style={{ color: "#999" }}>-</span>;
        const isBull = htf.state === "bullish";
        const isBear = htf.state === "bearish";
        // 短标签三段式：r2 新行有 phase → 期别（前期/中期/后期/衰竭 + 多/空）；
        // 历史行无 phase → 旧强/弱口径回退（完整标签形如「强多头背景（…）」）
        const PHASE_SHORT: Record<string, string> = {
          launch: "前期", mid: "中期", late: "后期", exhausted: "衰竭",
        };
        const short = htf.state === "ranging"
          ? "震荡"
          : htf.state === "mixed"
            ? "纠缠"
            : htf.phase
              ? `${PHASE_SHORT[htf.phase] ?? ""}${isBull ? "多" : "空"}`
              : `${htf.label?.startsWith("强") ? "强" : "弱"}${isBull ? "多" : "空"}`;
        const color = isBull
          ? schemeTag(colorScheme).up
          : isBear
            ? schemeTag(colorScheme).down
            : "default";
        return (
          <Tooltip title={htf.label || undefined}>
            <Tag color={color} style={{ marginInlineEnd: 0 }}>{short}</Tag>
          </Tooltip>
        );
      },
    },
    {
      title: "K线形态",
      dataIndex: "pattern",
      key: "pattern",
      width: 116,
      ...filterProps("pattern", PATTERN_FILTERS),
      render: (v: string | null, record: ScanResult) => {
        if (!v) return <span style={{ color: "#999" }}>-</span>;
        const { label, color } = patternStyle(v, colorScheme);
        return (
          <Tooltip title={record.signal_reason || undefined}>
            <Tag color={color}>{label}</Tag>
          </Tooltip>
        );
      },
    },
    {
      title: "EMA",
      dataIndex: "ema_state",
      key: "ema_state",
      width: 100,
      ...filterProps("ema_state", EMA_STATE_FILTERS),
      render: (v: string | null) => {
        if (!v) return <span style={{ color: "#999" }}>-</span>;
        const cfg = emaStateTag(v, colorScheme);
        return <Tag color={cfg.color}>{cfg.label}</Tag>;
      },
    },
    {
      // AI 方向列：开多/开空。扫描完成后自动分析（按24h成交额取前 AI_MAX_PER_SCAN 个）
      title: (
        <Tooltip title="AI 分析方向：开多/开空。扫描完成后自动分析成交额前 10 的命中币种">
          <span>AI方向</span>
        </Tooltip>
      ),
      key: "ai_direction",
      width: 72,
      render: (_: unknown, record: ScanResult) => {
        const ai = aiMap[record.id];
        if (!ai) {
          return analyzingMap[record.id]?.loading ? (
            <Tag color="processing">分析中</Tag>
          ) : (
            <span style={{ color: "#999" }}>-</span>
          );
        }
        if (ai.trade_decision !== "suggest") {
          return (
            <Tooltip title={ai.skip_reason || undefined}>
              <Tag color="default">跳过</Tag>
            </Tooltip>
          );
        }
        if (ai.direction === "long") return <Tag color={schemeTag(colorScheme).up}>多</Tag>;
        if (ai.direction === "short") return <Tag color={schemeTag(colorScheme).down}>空</Tag>;
        return <span style={{ color: "#999" }}>-</span>;
      },
    },
    {
      // AI 推荐度列：0-100，仅 suggest 结论有值
      title: (
        <Tooltip title="AI 推荐度（0-100，仅建议开单时有值）。与质量分不同：质量分是程序侧指标多数决打分（决定进入 AI 配额的顺序），推荐度是 AI 分析后的结论">
          <span>推荐度</span>
        </Tooltip>
      ),
      key: "ai_recommendation",
      width: 64,
      render: (_: unknown, record: ScanResult) => {
        const ai = aiMap[record.id];
        if (!ai) {
          return analyzingMap[record.id]?.loading ? (
            <Tag color="processing">分析中</Tag>
          ) : (
            <span style={{ color: "#999" }}>-</span>
          );
        }
        if (ai.trade_decision !== "suggest") {
          return <span style={{ color: "#999" }}>-</span>;
        }
        const rec =
          ai.recommendation != null ? Math.round(ai.recommendation) : null;
        return rec != null ? <strong>{rec}</strong> : <span style={{ color: "#999" }}>-</span>;
      },
    },
    {
      // 开单状态列：suggest 被开仓闸门拦下时展示原因（ai_analyses.open_block_reason，
      // trade_engine._mark_open_block 落库）；进入下单序列后的成败见交易记录 Tab
      title: (
        <Tooltip title="建议开单但被开仓闸门拦下的原因（强平闸门/现价偏离/在跑上限/余额不足/待回踩等），悬停查看详情；成功开单或下单失败的见交易记录 Tab">
          <span>开单状态</span>
        </Tooltip>
      ),
      key: "ai_open_block",
      width: 100,
      render: (_: unknown, record: ScanResult) => {
        const ai = aiMap[record.id];
        if (!ai || ai.trade_decision !== "suggest") {
          return <span style={{ color: "#999" }}>-</span>;
        }
        // 已开单/开单失败：以 trade_records（按 ai_analysis_id 关联）为准，
        // 双网都有记录时展示两网标记
        const tradeInfo = tradeMap[ai.id];
        if (tradeInfo) {
          const bothNets = tradeInfo.nets.includes(true) && tradeInfo.nets.includes(false);
          const label = TRADE_STATUS_LABEL[tradeInfo.status] || tradeInfo.status;
          if (tradeInfo.status === "FAILED") {
            return (
              <Tooltip title={`下单序列失败（${label}），详见交易记录 Tab`}>
                <Tag color="red" style={{ marginInlineEnd: 0, cursor: "default" }}>
                  开单失败
                </Tag>
              </Tooltip>
            );
          }
          return (
            <Tooltip title={`已开单${bothNets ? "（测试网+正式网）" : ""}（${label}），详见交易记录 Tab`}>
              <Tag color="green" style={{ marginInlineEnd: 0, cursor: "default" }}>
                已开单{bothNets ? "·双网" : ""}
              </Tag>
            </Tooltip>
          );
        }
        if (ai.open_block_reason) {
          return (
            <Tooltip title={ai.open_block_reason}>
              <Tag color="orange" style={{ marginInlineEnd: 0, cursor: "default" }}>
                {ai.pullback_wait ? "⏳ 待回踩" : "未开单"}
              </Tag>
            </Tooltip>
          );
        }
        return <span style={{ color: "#999" }}>-</span>;
      },
    },
    {
      title: "当前价格",
      dataIndex: "current_price",
      key: "current_price",
      width: 104,
      render: (v: number) =>
        v < 1 ? v.toFixed(6) : v < 100 ? v.toFixed(4) : v.toFixed(2),
    },
    {
      title: "24h成交额",
      dataIndex: "volume_24h",
      key: "volume_24h",
      width: 104,
      sorter: (a, b) => a.volume_24h - b.volume_24h,
      render: (v: number) => {
        if (v >= 1e9) return (v / 1e9).toFixed(2) + "B";
        if (v >= 1e6) return (v / 1e6).toFixed(2) + "M";
        if (v >= 1e3) return (v / 1e3).toFixed(1) + "K";
        return v.toFixed(0);
      },
    },
    {
      title: "K线量能",
      key: "volume_type",
      width: 92,
      sorter: (a, b) => a.volume - b.volume,
      render: (_: unknown, r: ScanResult) => {
        const colorMap: Record<string, string> = {
          倍量: "red",
          放量: "orange",
          平量: "default",
          缩量: "blue",
          地量: "gray",
        };
        return (
          <Tag color={colorMap[r.volume_type] || "default"}>
            {r.volume_type}
          </Tag>
        );
      },
    },
    {
      title: "状态",
      dataIndex: "is_repeat",
      key: "is_repeat",
      width: 68,
      render: (v: boolean) =>
        v ? <Tag color="default">重复</Tag> : <Tag color="green">新</Tag>,
    },
  ];

  return (
    <div style={{ display: "flex", height: "100%", overflow: "hidden" }}>
      {/* 左侧：结果列表 + 折叠 AI 分析（固定高度，表格区内部滚动） */}
      <div
        style={{
          flex: "0 0 55%",
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
          paddingRight: 8,
        }}>
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, flexShrink: 0, marginBottom: 4 }}>
          {/* 立即扫描（外部传入，置于刷新左侧，2026-09-20 移入扫描结果 Tab） */}
          {headerExtra}
          <Button icon={<ReloadOutlined />} onClick={handleRefresh}>
            刷新
          </Button>
        </div>
        <Table
          rowKey="id"
          className="result-table"
          columns={columns}
          dataSource={results}
          loading={loading}
          size="small"
          tableLayout="fixed"
          // 表格总宽 x = 各列 width 之和（2026-09-30 列宽收窄后同步；改列宽须一并维护）
          scroll={{ x: 1444 }}
          onChange={handleTableChange}
          pagination={{
            current: resultsPage,
            pageSize: resultsPageSize,
            total,
            showTotal: (t) => `共 ${t} 条`,
            size: "small",
            position: ['bottomRight'],
          }}
          locale={{ emptyText: <Empty description="暂无命中币种" /> }}
          onRow={(record) => ({
            // 点击行 = 聚焦图表 + 展开该行（单开，与展开图标一致）；行内按钮已 stopPropagation
            onClick: () =>
              setExpandedRowKeys((prev) =>
                prev.includes(record.id) ? [] : [record.id],
              ),
            style: { cursor: "pointer" },
          })}
          expandable={{
            expandedRowKeys,
            onExpand: (expanded, record) => {
              setExpandedRowKeys(expanded ? [record.id] : []);
            },
            expandedRowRender: (record: ScanResult) => {
              const ai = aiMap[record.id];
              const rowState = analyzingMap[record.id];
              return (
                <AiExpandContent
                  aiEnabled={aiEnabled}
                  ai={ai}
                  loading={!!rowState?.loading}
                  error={rowState?.error}
                  scanResultId={record.id}
                  userInput={userInputs[record.id] ?? ""}
                  onUserInput={(v) =>
                    setUserInputs((m) => ({ ...m, [record.id]: v }))
                  }
                  onTrigger={() => handleReAnalyze(record)}
                />
              );
            },
            rowExpandable: () => true,
          }}
        />
      </div>

      {/* 右侧：K线图（填满剩余高度，与左栏对齐） */}
      <div
        style={{
          flex: "1 1 45%",
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
          paddingLeft: 8,
          borderLeft: "1px solid #f0f0f0",
        }}>
        {!selectedRecord ? (
          <Empty description="请点击一行查看图表" style={{ marginTop: 80 }} />
        ) : (
          <KlineChart
            symbol={selectedRecord.symbol}
            limit={500}
            ai={selectedAi}
          />
        )}
      </div>
    </div>
  );
}
