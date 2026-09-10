import {
  Table,
  Tag,
  Tooltip,
  Empty,
  Button,
  Typography,
  message,
  Alert,
  Space,
  Input,
} from "antd";
import {
  RobotOutlined,
  CopyOutlined,
  LoadingOutlined,
  StarOutlined,
  StarFilled,
} from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import { useScanStore } from "../stores/scanStore";
import {
  SIGNAL_TYPE_MAP,
  SIGNAL_TYPE_FILTERS,
  POSITION_LABEL_MAP,
  POSITION_FILTERS,
  POSITION_SUPPORT_KINDS,
  PATTERN_FILTERS,
  patternStyle,
} from "../constants/labels";
import { useState, useMemo, useEffect } from "react";
import KlineChart from "./KlineChart";
import AiAnalysisCard from "./AiAnalysisCard";
import type { AIAnalysis, ScanResult } from "../types";

const { Text } = Typography;

export default function ResultTable() {
  const {
    results,
    total,
    loading,
    resultsPage,
    resultsPageSize,
    filters,
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

  // 服务端过滤：antd 列头筛选 onChange -> store -> 重新请求
  const filterProps = (
    key: "signal_type" | "position" | "pattern",
    options: { text: string; value: string }[],
  ) => ({
    filters: options,
    filterMultiple: false,
    filteredValue: filters[key] ? [filters[key]] : null,
    onChange: (vals: React.Key[] | null) =>
      setFilters({ ...filters, [key]: (vals?.[0] as string) || undefined }),
  });

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
        const label = POSITION_LABEL_MAP[v] || v;
        const color = POSITION_SUPPORT_KINDS.has(v) ? "green" : "red";
        const hit = record.key_levels?.find((lv) => lv.kind === v);
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
      title: "距关键位",
      dataIndex: "breakout_pct",
      key: "breakout_pct",
      sorter: (a, b) => a.breakout_pct - b.breakout_pct,
      render: (v: number) => (
        <span
          style={{
            color: v >= 0 ? "#52c41a" : "#ef5350",
            fontWeight: 600,
          }}>
          {v >= 0 ? "+" : ""}
          {v.toFixed(2)}%
        </span>
      ),
    },
    {
      title: "状态",
      dataIndex: "is_repeat",
      key: "is_repeat",
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
        <Table
          rowKey="id"
          className="result-table"
          columns={columns}
          dataSource={results}
          loading={loading}
          size="small"
          pagination={{
            current: resultsPage,
            pageSize: resultsPageSize,
            total,
            showTotal: (t) => `共 ${t} 条`,
            size: "small",
            onChange: (page, pageSize) =>
              fetchResults(undefined, page, pageSize),
          }}
          locale={{ emptyText: <Empty description="暂无命中币种" /> }}
          onRow={(record) => ({
            onClick: () => {
              setExpandedRowKeys((prev) =>
                prev.includes(record.id)
                  ? prev.filter((k) => k !== record.id)
                  : [record.id],
              );
            },
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
              const isLoading = !!rowState?.loading;
              const rowError = rowState?.error;
              return !aiEnabled ? (
                <div style={{ textAlign: "center", padding: "20px 0" }}>
                  <Text type="secondary">AI 分析未开启</Text>
                </div>
              ) : isLoading && !ai ? (
                <div style={{ textAlign: "center", padding: "20px 0" }}>
                  <LoadingOutlined style={{ fontSize: 24 }} />
                  <div style={{ marginTop: 8 }}>
                    <Text type="secondary">AI 正在分析中，请稍候...</Text>
                  </div>
                </div>
              ) : !ai ? (
                <div style={{ textAlign: "center", padding: "20px 0" }}>
                  {rowError && (
                    <Alert
                      type="error"
                      message="AI 分析失败"
                      description={rowError}
                      showIcon
                      style={{ marginBottom: 12, textAlign: "left" }}
                    />
                  )}
                  <Input.TextArea
                    rows={2}
                    placeholder="补充说明（可选）：你的判断或对 AI 的要求，将随分析一起提交"
                    value={userInputs[record.id] ?? ""}
                    onChange={(e) =>
                      setUserInputs((m) => ({ ...m, [record.id]: e.target.value }))
                    }
                    style={{ marginBottom: 8 }}
                  />
                  <Button
                    type="primary"
                    ghost
                    size="small"
                    icon={<RobotOutlined />}
                    loading={isLoading}
                    onClick={() => handleReAnalyze(record)}>
                    AI 分析
                  </Button>
                </div>
              ) : (
                <div>
                  {isLoading && (
                    <Alert
                      type="info"
                      message={
                        <Space>
                          <LoadingOutlined />
                          <span>AI 重新分析中...</span>
                        </Space>
                      }
                      style={{ marginBottom: 8 }}
                    />
                  )}
                  <AiAnalysisCard ai={ai} />
                  {rowError && (
                    <Alert
                      type="error"
                      message="上次重新分析失败"
                      description={rowError}
                      showIcon
                      style={{ marginTop: 8, textAlign: "left" }}
                    />
                  )}
                  <Input.TextArea
                    rows={2}
                    placeholder="补充说明（可选）：你的判断或对 AI 的要求，将随分析一起提交"
                    value={userInputs[record.id] ?? ""}
                    onChange={(e) =>
                      setUserInputs((m) => ({ ...m, [record.id]: e.target.value }))
                    }
                    style={{ marginTop: 8, marginBottom: 8 }}
                  />
                  <div style={{ textAlign: "center" }}>
                    <Button
                      type="primary"
                      ghost
                      size="small"
                      icon={<RobotOutlined />}
                      loading={isLoading}
                      onClick={() => handleReAnalyze(record)}>
                      重新分析
                    </Button>
                  </div>
                </div>
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
            limit={250}
            ai={selectedAi}
            keyLevels={selectedRecord.key_levels ?? undefined}
          />
        )}
      </div>
    </div>
  );
}
