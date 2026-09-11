import {
  Table,
  Tag,
  Tooltip,
  Empty,
  Button,
  message,
} from "antd";
import {
  CopyOutlined,
  StarOutlined,
  StarFilled,
} from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import { useScanStore } from "../stores/scanStore";
import {
  SIGNAL_TYPE_MAP,
  SIGNAL_TYPE_FILTERS,
  POSITION_FILTERS,
  PATTERN_FILTERS,
  EMA_STATE_MAP,
  EMA_STATE_FILTERS,
  patternStyle,
  positionTag,
} from "../constants/labels";
import { useState, useMemo, useEffect } from "react";
import KlineChart from "./KlineChart";
import AiExpandContent from "./AiExpandContent";
import type { AIAnalysis, ScanResult } from "../types";

export default function ResultTable() {
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
    aiConfig,
    watchlist,
    addToWatchlist,
    removeFromWatchlist,
  } = useScanStore();

  const aiEnabled = !!aiConfig?.ai_analysis_enabled;
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

  // 服务端过滤（antd 官方模式）：列只声明 filters（非受控，不传 filteredValue），
  // 筛选/翻页统一在 Table 的 onChange 里按 extra.action 分发。
  // 注意1：antd 列级没有 onChange 属性，写在列上会被静默忽略。
  // 注意2：pagination 不要传 onChange——antd 筛选确认时会额外回调它，
  // 导致带着旧 filters 的重复请求竞态（UI 停留在重置前状态）。
  const filterProps = (
    key: "signal_type" | "position" | "pattern" | "ema_state",
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
        position: (tableFilters.position?.[0] as string) || undefined,
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

  const handleCopySymbol = (e: React.MouseEvent, symbol: string) => {
    e.stopPropagation();
    const text = symbol + ".P";
    navigator.clipboard
      .writeText(text)
      .then(() => {
        message.success(`已复制: ${text}`);
      })
      .catch(() => {
        message.error("复制失败");
      });
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
      title: "币种",
      dataIndex: "symbol",
      key: "symbol",
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
      title: "信号类型",
      dataIndex: "signal_type",
      key: "signal_type",
      ...filterProps("signal_type", SIGNAL_TYPE_FILTERS),
      render: (v: string) => {
        const cfg = SIGNAL_TYPE_MAP[v] || { label: v, color: "default" };
        return <Tag color={cfg.color}>{cfg.label}</Tag>;
      },
    },
    {
      title: "位置",
      dataIndex: "position",
      key: "position",
      ...filterProps("position", POSITION_FILTERS),
      render: (v: string | null, record: ScanResult) => {
        if (!v) return <span style={{ color: "#999" }}>-</span>;
        // 颜色/标签跟随关键位实际角色（跌破的支撑位显示"支撑位→压力"并标红）
        const { label, color, tip } = positionTag(v, record.key_levels);
        return (
          <Tooltip title={tip}>
            <Tag color={color}>{label}</Tag>
          </Tooltip>
        );
      },
    },
    {
      title: "EMA",
      dataIndex: "ema_state",
      key: "ema_state",
      ...filterProps("ema_state", EMA_STATE_FILTERS),
      render: (v: string | null) => {
        if (!v) return <span style={{ color: "#999" }}>-</span>;
        const cfg = EMA_STATE_MAP[v] || { label: v, color: "default" };
        return <Tag color={cfg.color}>{cfg.label}</Tag>;
      },
    },
    {
      title: "K线形态",
      dataIndex: "pattern",
      key: "pattern",
      ...filterProps("pattern", PATTERN_FILTERS),
      render: (v: string | null, record: ScanResult) => {
        if (!v) return <span style={{ color: "#999" }}>-</span>;
        const { label, color } = patternStyle(v);
        return (
          <Tooltip title={record.signal_reason || undefined}>
            <Tag color={color}>{label}</Tag>
          </Tooltip>
        );
      },
    },
    {
      title: "当前价格",
      dataIndex: "current_price",
      key: "current_price",
      render: (v: number) =>
        v < 1 ? v.toFixed(6) : v < 100 ? v.toFixed(4) : v.toFixed(2),
    },
    {
      title: "24h成交额",
      dataIndex: "volume_24h",
      key: "volume_24h",
      sorter: (a, b) => a.volume_24h - b.volume_24h,
      defaultSortOrder: "descend",
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
      render: (v: boolean) =>
        v ? <Tag color="default">重复</Tag> : <Tag color="green">新</Tag>,
    },
    {
      // 复盘列：AI 建议开单 24h 后逐K回放定论（数据来自 aiMap，AI 开关关闭时为空显示 -）
      title: (
        <Tooltip title="AI 建议开单 24h 后逐K回放结果：先触止损=负，触止盈=胜，24h内未触任何价位=超时">
          <span>复盘</span>
        </Tooltip>
      ),
      key: "review",
      render: (_: unknown, record: ScanResult) => {
        const status = aiMap[record.id]?.review_status;
        if (!status) return <span style={{ color: "#999" }}>-</span>;
        switch (status) {
          case "win_tp1":
            return <Tag color="green">胜·TP1</Tag>;
          case "win_tp2":
            return <Tag color="green">胜·TP2</Tag>;
          case "loss":
            return <Tag color="red">负·止损</Tag>;
          case "expired":
            return <Tag color="default">超时</Tag>;
          default:
            return <span style={{ color: "#999" }}>-</span>;
        }
      },
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
        <Table
          rowKey="id"
          className="result-table"
          columns={columns}
          dataSource={results}
          loading={loading}
          size="small"
          onChange={handleTableChange}
          pagination={{
            current: resultsPage,
            pageSize: resultsPageSize,
            total,
            showTotal: (t) => `共 ${t} 条`,
            size: "small",
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
            keyLevels={selectedRecord.key_levels ?? undefined}
          />
        )}
      </div>
    </div>
  );
}
