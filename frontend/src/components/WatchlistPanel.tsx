import { useEffect, useMemo, useRef, useState } from "react";
import {
  Table,
  Tag,
  Tooltip,
  Empty,
  Button,
  Typography,
  message,
  Popconfirm,
} from "antd";
import {
  CopyOutlined,
  DeleteOutlined,
  ReloadOutlined,
  SyncOutlined,
  StarFilled,
} from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import { useScanStore } from "../stores/scanStore";
import { scanApi } from "../api/scan";
import {
  SIGNAL_TYPE_MAP,
  SIGNAL_TYPE_FILTERS,
  POSITION_LABEL_MAP,
  POSITION_FILTERS,
  POSITION_SUPPORT_KINDS,
  PATTERN_FILTERS,
  EMA_STATE_MAP,
  EMA_STATE_FILTERS,
  patternStyle,
} from "../constants/labels";
import KlineChart from "./KlineChart";
import AiExpandContent from "./AiExpandContent";
import type { AIAnalysis, ScanResult, WatchlistItem } from "../types";

const { Text } = Typography;

interface WatchRow extends WatchlistItem {
  price: number | null;
  volume_24h: number | null;
  scan: ScanResult | null;
}

const fmtVol = (v: number) => {
  if (v >= 1e9) return (v / 1e9).toFixed(2) + "B";
  if (v >= 1e6) return (v / 1e6).toFixed(2) + "M";
  if (v >= 1e3) return (v / 1e3).toFixed(1) + "K";
  return v.toFixed(0);
};

const fmtPrice = (v: number) =>
  v < 1 ? v.toFixed(6) : v < 100 ? v.toFixed(4) : v.toFixed(2);

export default function WatchlistPanel() {
  const {
    watchlist,
    fetchWatchlist,
    removeFromWatchlist,
    results,
    loading,
    fetchResults,
    aiConfig,
  } = useScanStore();

  const [quotes, setQuotes] = useState<
    Record<string, { price: number; volume_24h: number }>
  >({});
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null);
  // 折叠行（单开，与扫描结果一致）
  const [expandedKeys, setExpandedKeys] = useState<string[]>([]);

  // ===== K 线手动刷新 =====
  const [refreshing, setRefreshing] = useState<Record<string, boolean>>({});
  // 每币种的图表重拉计数（刷新成功后 +1，触发 KlineChart 重新拉数据）
  const [refreshKeys, setRefreshKeys] = useState<Record<string, number>>({});

  // ===== AI 分析（按币种管理，与扫描结果行展开布局一致） =====
  const aiEnabled = !!aiConfig?.ai_analysis_enabled;
  const [aiMap, setAiMap] = useState<Record<string, AIAnalysis>>({});
  const [analyzing, setAnalyzing] = useState<
    Record<string, { loading: boolean; error: string | null }>
  >({});
  const [userInputs, setUserInputs] = useState<Record<string, string>>({});
  const pollTimersRef = useRef<Record<string, ReturnType<typeof setInterval>>>({});

  const loadData = async () => {
    fetchWatchlist();
    scanApi.watchlist
      .quotes()
      .then((d) => {
        const m: Record<string, { price: number; volume_24h: number }> = {};
        for (const it of d.items) m[it.symbol] = it;
        setQuotes(m);
      })
      .catch(() => undefined);
    // 最新扫描结果（信号信息），为空时才拉取
    if (results.length === 0) fetchResults().catch(() => undefined);
  };

  useEffect(() => {
    loadData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 组件卸载时清理 AI 轮询计时器
  useEffect(() => {
    const timers = pollTimersRef.current;
    return () => {
      for (const id of Object.keys(timers)) {
        clearInterval(timers[id]);
        delete timers[id];
      }
    };
  }, []);

  // 组装行数据：行情 + 信号信息（当前扫描结果优先，其次后端嵌入的最近扫描结果）
  const rows: WatchRow[] = useMemo(
    () =>
      watchlist.map((w) => ({
        ...w,
        price: quotes[w.symbol]?.price ?? null,
        volume_24h: quotes[w.symbol]?.volume_24h ?? null,
        scan: results.find((r) => r.symbol === w.symbol) || w.latest_scan || null,
      })),
    [watchlist, quotes, results],
  );

  const handleDelete = async (symbol: string) => {
    try {
      await removeFromWatchlist(symbol);
      message.success(`已删除 ${symbol}`);
    } catch (e: any) {
      message.error(e?.response?.data?.detail || "删除失败");
    }
  };

  const handleCopySymbol = (e: React.MouseEvent, symbol: string) => {
    e.stopPropagation();
    const text = symbol + ".P";
    navigator.clipboard
      .writeText(text)
      .then(() => message.success(`已复制: ${text}`))
      .catch(() => message.error("复制失败"));
  };

  // ===== K 线手动刷新：强制拉取最新 K 线写回缓存 =====
  const handleRefresh = async (e: React.MouseEvent, symbol: string) => {
    e.stopPropagation();
    setRefreshing((prev) => ({ ...prev, [symbol]: true }));
    try {
      const d = await scanApi.watchlist.refresh(symbol);
      setRefreshKeys((prev) => ({ ...prev, [symbol]: (prev[symbol] ?? 0) + 1 }));
      message.success(`${symbol} K线已更新（${d.kline_count}根）`);
    } catch (err: any) {
      message.error(err?.response?.data?.detail || "K线刷新失败");
    } finally {
      setRefreshing((prev) => ({ ...prev, [symbol]: false }));
    }
  };

  // ===== AI 分析 =====
  const stopPolling = (symbol: string) => {
    if (pollTimersRef.current[symbol]) {
      clearInterval(pollTimersRef.current[symbol]);
      delete pollTimersRef.current[symbol];
    }
  };

  const setRowState = (
    symbol: string,
    state: { loading: boolean; error: string | null },
  ) => {
    setAnalyzing((prev) => ({ ...prev, [symbol]: state }));
  };

  // 触发后轮询：进度事件（Redis）判定分析进程结束（done/error）立即停止，
  // 兜底每 5 轮查一次结果列表（进度事件丢失时仍能拿到结果）
  const startPolling = (
    symbol: string,
    scanRecordId: string,
    scanResultId: string,
  ) => {
    stopPolling(symbol);
    setRowState(symbol, { loading: true, error: null });
    let attempts = 0;
    // Agent 管线单次分析可达 2~5 分钟，放宽到 10 分钟
    const maxAttempts = 200; // 200 次 × 3 秒 = 600 秒
    pollTimersRef.current[symbol] = setInterval(async () => {
      attempts++;
      try {
        const prog = await scanApi.aiProgress(scanResultId).catch(() => null);
        const errEv = prog?.events.find((e) => e.t === "error");
        if (errEv || prog?.status === "done") {
          stopPolling(symbol);
          if (errEv) {
            setRowState(symbol, {
              loading: false,
              error: errEv.note || "AI 分析失败",
            });
          } else {
            const d = await scanApi.aiAnalyses(scanRecordId);
            const found = d.items.find((a) => a.scan_result_id === scanResultId);
            if (found) setAiMap((prev) => ({ ...prev, [symbol]: found }));
            setRowState(symbol, { loading: false, error: null });
          }
          return;
        }
        if (attempts % 5 === 0) {
          const d = await scanApi.aiAnalyses(scanRecordId);
          const found = d.items.find((a) => a.scan_result_id === scanResultId);
          if (found) {
            setAiMap((prev) => ({ ...prev, [symbol]: found }));
            stopPolling(symbol);
            setRowState(symbol, { loading: false, error: null });
            return;
          }
        }
        if (attempts >= maxAttempts) {
          stopPolling(symbol);
          setRowState(symbol, { loading: false, error: "AI 分析超时，请重试" });
        }
      } catch {
        // 忽略轮询错误
      }
    }, 3000);
  };

  // 触发分析：有扫描结果走异步任务 + 轮询；无扫描结果走同步手动分析
  const handleAnalyze = async (r: WatchRow) => {
    if (!r.scan?.id || !r.scan?.scan_record_id) {
      // 从未被扫描命中过：走手动搜索的同步分析（拉最新 K 线 + AI）
      setRowState(r.symbol, { loading: true, error: null });
      try {
        const ai = await scanApi.analyzeCoin(r.symbol);
        setAiMap((prev) => ({ ...prev, [r.symbol]: ai }));
        setRowState(r.symbol, { loading: false, error: null });
      } catch (e: any) {
        setRowState(r.symbol, {
          loading: false,
          error: e?.response?.data?.detail || "AI 分析失败",
        });
      }
      return;
    }

    const scanRecordId = r.scan.scan_record_id;
    const scanResultId = r.scan.id;
    try {
      await scanApi.triggerAi(scanRecordId, scanResultId, userInputs[r.symbol]?.trim() || undefined);
      startPolling(r.symbol, scanRecordId, scanResultId);
    } catch (e: any) {
      setRowState(r.symbol, {
        loading: false,
        error: e?.response?.data?.detail || "触发 AI 分析失败",
      });
    }
  };

  // 展开某行（显式单开）：选中图表 + 首次展开时预加载该币种已有的 AI 分析
  const expandRow = (symbol: string) => {
    setSelectedSymbol(symbol);
    setExpandedKeys([symbol]);
    if (!aiMap[symbol]) {
      const scan = rows.find((r) => r.symbol === symbol)?.scan;
      if (scan?.id && scan?.scan_record_id) {
        scanApi
          .aiAnalyses(scan.scan_record_id)
          .then((d) => {
            const found = d.items.find((a) => a.scan_result_id === scan.id);
            if (found) setAiMap((prev) => ({ ...prev, [symbol]: found }));
          })
          .catch(() => undefined);
      }
    }
  };

  // 行点击：已展开则折叠，否则展开（显式判断，避免与展开图标双触发）
  const handleRowClick = (symbol: string) => {
    if (expandedKeys.includes(symbol)) {
      setExpandedKeys([]);
    } else {
      expandRow(symbol);
    }
  };

  const columns: ColumnsType<WatchRow> = [
    {
      title: "币种",
      dataIndex: "symbol",
      key: "symbol",
      render: (v: string) => (
        <span>
          <StarFilled style={{ color: "#faad14", marginRight: 4 }} />
          <strong>{v.replace("USDT", "")}/USDT</strong>
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
      title: "信号类型",
      key: "signal_type",
      filters: SIGNAL_TYPE_FILTERS,
      filterMultiple: false,
      onFilter: (value, r) => r.scan?.signal_type === value,
      render: (_: unknown, r: WatchRow) => {
        const v = r.scan?.signal_type;
        if (!v) return <span style={{ color: "#999" }}>-</span>;
        const cfg = SIGNAL_TYPE_MAP[v] || { label: v, color: "default" };
        return <Tag color={cfg.color}>{cfg.label}</Tag>;
      },
    },
    {
      title: "位置",
      key: "position",
      filters: POSITION_FILTERS,
      filterMultiple: false,
      onFilter: (value, r) => r.scan?.position === value,
      render: (_: unknown, r: WatchRow) => {
        const v = r.scan?.position;
        if (!v) return <span style={{ color: "#999" }}>-</span>;
        const label = POSITION_LABEL_MAP[v] || v;
        const color = POSITION_SUPPORT_KINDS.has(v) ? "green" : "red";
        const hit = r.scan?.key_levels?.find((lv) => lv.kind === v);
        const tip = hit
          ? `${POSITION_LABEL_MAP[hit.kind] || hit.kind} ${hit.price}，${hit.role === "support" ? "支撑" : "压力"}，触及 ${hit.touches} 次`
          : undefined;
        return (
          <Tooltip title={tip}>
            <Tag color={color}>{label}</Tag>
          </Tooltip>
        );
      },
    },
    {
      title: "EMA",
      key: "ema_state",
      filters: EMA_STATE_FILTERS,
      filterMultiple: false,
      onFilter: (value, r) => r.scan?.ema_state === value,
      render: (_: unknown, r: WatchRow) => {
        const v = r.scan?.ema_state;
        if (!v) return <span style={{ color: "#999" }}>-</span>;
        const cfg = EMA_STATE_MAP[v] || { label: v, color: "default" };
        return <Tag color={cfg.color}>{cfg.label}</Tag>;
      },
    },
    {
      title: "K线形态",
      key: "pattern",
      filters: PATTERN_FILTERS,
      filterMultiple: false,
      onFilter: (value, r) => r.scan?.pattern === value,
      render: (_: unknown, r: WatchRow) => {
        const v = r.scan?.pattern;
        if (!v) return <span style={{ color: "#999" }}>-</span>;
        const { label, color } = patternStyle(v);
        return (
          <Tooltip title={r.scan?.signal_reason || undefined}>
            <Tag color={color}>{label}</Tag>
          </Tooltip>
        );
      },
    },
    {
      title: "当前价格",
      key: "price",
      render: (_: unknown, r: WatchRow) =>
        r.price != null ? fmtPrice(r.price) : "-",
    },
    {
      title: "24h成交额",
      dataIndex: "volume_24h",
      key: "volume_24h",
      sorter: (a, b) => (a.volume_24h ?? 0) - (b.volume_24h ?? 0),
      render: (_: unknown, r: WatchRow) =>
        r.volume_24h != null ? fmtVol(r.volume_24h) : "-",
    },
    {
      title: "K线量能",
      key: "volume_type",
      render: (_: unknown, r: WatchRow) => {
        const v = r.scan?.volume_type;
        if (!v) return "-";
        const colorMap: Record<string, string> = {
          倍量: "red",
          放量: "orange",
          平量: "default",
          缩量: "blue",
          地量: "gray",
        };
        return <Tag color={colorMap[v] || "default"}>{v}</Tag>;
      },
    },
    {
      title: "操作",
      key: "action",
      width: 130,
      render: (_: unknown, r: WatchRow) => (
        <span onClick={(e) => e.stopPropagation()}>
          <Tooltip title="拉取最新K线并缓存">
            <Button
              type="text"
              size="small"
              icon={<SyncOutlined spin={!!refreshing[r.symbol]} />}
              loading={refreshing[r.symbol]}
              onClick={(e) => handleRefresh(e, r.symbol)}
              style={{ padding: "0 6px" }}>
              更新
            </Button>
          </Tooltip>
          <Popconfirm
            title="确认删除该关注？"
            onConfirm={(e) => {
              e?.stopPropagation();
              handleDelete(r.symbol);
            }}
            onCancel={(e) => e?.stopPropagation()}>
            <Button
              type="text"
              danger
              size="small"
              icon={<DeleteOutlined />}
              onClick={(e) => e.stopPropagation()}>
              删除
            </Button>
          </Popconfirm>
        </span>
      ),
    },
  ];

  return (
    <div style={{ display: "flex", height: "100%", overflow: "hidden" }}>
      {/* 左侧：关注列表（与扫描结果同布局，行可折叠看 AI 分析） */}
      <div
        style={{
          flex: "0 0 55%",
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
          paddingRight: 8,
        }}>
        <div style={{ marginBottom: 8, flexShrink: 0 }}>
          <Button
            icon={<ReloadOutlined />}
            size="small"
            onClick={loadData}
            loading={loading}>
            刷新
          </Button>
          <Text type="secondary" style={{ marginLeft: 12 }}>
            在"扫描结果"中点击币种旁的 ★ 即可加入关注；点击行展开 AI 分析
          </Text>
        </div>
        <Table
          rowKey="symbol"
          className="result-table"
          columns={columns}
          dataSource={rows}
          size="small"
          pagination={false}
          locale={{ emptyText: <Empty description="暂无关注币种" /> }}
          onRow={(record) => ({
            onClick: () => handleRowClick(record.symbol),
            style: { cursor: "pointer" },
          })}
          rowClassName={(record) =>
            record.symbol === selectedSymbol ? "ant-table-row-selected" : ""
          }
          expandable={{
            expandedRowKeys: expandedKeys,
            onExpand: (expanded, record) => {
              if (expanded) handleRowClick(record.symbol);
              else
                setExpandedKeys((prev) =>
                  prev.filter((s) => s !== record.symbol),
                );
            },
            expandedRowRender: (record: WatchRow) => (
              <AiExpandContent
                aiEnabled={aiEnabled}
                ai={aiMap[record.symbol]}
                loading={!!analyzing[record.symbol]?.loading}
                error={analyzing[record.symbol]?.error}
                scanResultId={record.scan?.id}
                userInput={userInputs[record.symbol] ?? ""}
                onUserInput={(v) =>
                  setUserInputs((m) => ({ ...m, [record.symbol]: v }))
                }
                onTrigger={() => handleAnalyze(record)}
              />
            ),
            rowExpandable: () => true,
          }}
        />
      </div>

      {/* 右侧：K线图（与扫描结果同布局） */}
      <div
        style={{
          flex: "1 1 45%",
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
          paddingLeft: 8,
          borderLeft: "1px solid #f0f0f0",
        }}>
        {!selectedSymbol ? (
          <Empty description="点击一行查看图表" style={{ marginTop: 80 }} />
        ) : (
          <KlineChart
            symbol={selectedSymbol}
            limit={500}
            refreshKey={refreshKeys[selectedSymbol] ?? 0}
            keyLevels={
              rows.find((r) => r.symbol === selectedSymbol)?.scan?.key_levels ??
              undefined
            }
          />
        )}
      </div>
    </div>
  );
}
