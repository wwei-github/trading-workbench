import { Table, Tag, Tooltip, Empty, Button, Descriptions, Typography, message, Alert, Space } from 'antd'
import { RobotOutlined, CopyOutlined, LoadingOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { bj } from '../utils/dayjs'
import { useScanStore } from '../stores/scanStore'
import { useState, useMemo, useEffect } from 'react'
import KlineChart from './KlineChart'
import type { AIAnalysis, ScanResult } from '../types'

const { Text, Paragraph } = Typography

function fmtPrice(v: number | null | undefined): string {
  if (v == null) return '-'
  if (v < 1) return v.toFixed(6)
  if (v < 100) return v.toFixed(4)
  return v.toFixed(2)
}

export default function ResultTable() {
  const {
    results,
    total,
    loading,
    resultsPage,
    resultsPageSize,
    currentScanId,
    aiAnalyses,
    analyzingMap,
    triggerAiAnalysis,
    fetchResults,
    aiConfig,
  } = useScanStore();

  const aiEnabled = !!aiConfig?.ai_analysis_enabled;

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
      m[a.scan_result_id] = a;
    }
    return m;
  }, [aiAnalyses]);

  // 图表跟随展开的行
  const selectedRecord =
    results.find((r) => expandedRowKeys.includes(r.id)) || null;
  const selectedAi = selectedRecord ? aiMap[selectedRecord.id] : undefined;

  // 计算实际决策：综合 trade_decision、recommendation、direction 兜底判断
  // - AI 明确 skip → skip
  // - recommendation ≤ 30 → skip（分数太低不值得做）
  // - direction 为空 且 trade_decision 也不是 suggest → skip（无明确方向）
  // - 以上都不是 → suggest
  const getEffectiveDecision = (
    ai?: AIAnalysis,
  ): { decision: string; reason: string } => {
    if (!ai) return { decision: "", reason: "" };
    if (ai.trade_decision === "skip") {
      return { decision: "skip", reason: ai.skip_reason || "AI 不建议开单" };
    }
    if (ai.recommendation != null && ai.recommendation <= 30) {
      return {
        decision: "skip",
        reason: `推荐程度仅 ${ai.recommendation} 分，不值得开单${ai.skip_reason ? "；" + ai.skip_reason : ""}`,
      };
    }
    if (!ai.direction && ai.trade_decision !== "suggest") {
      return { decision: "skip", reason: "AI 未给出明确交易方向" };
    }
    return { decision: "suggest", reason: "" };
  };

  const handleReAnalyze = async (record: ScanResult) => {
    if (!currentScanId) {
      message.warning("无当前扫描记录");
      return;
    }
    try {
      await triggerAiAnalysis(currentScanId, record.id);
      message.success("AI 分析已提交，请稍候...");
    } catch (e: any) {
      message.error(e?.message || "AI 分析失败");
    }
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

  const columns: ColumnsType<ScanResult> = [
    {
      title: "币种",
      dataIndex: "symbol",
      key: "symbol",
      render: (v: string) => (
        <span>
          <strong>{v.replace("USDT", "")}/USDT</strong>
          <Button
            type="text"
            size="small"
            icon={<CopyOutlined />}
            onClick={(e) => handleCopySymbol(e, v)}
            style={{ marginLeft: 4, padding: "0 4px" }}
          />
        </span>
      ),
    },
    {
      title: "信号类型",
      dataIndex: "signal_type",
      key: "signal_type",
      render: (v: string) => {
        const map: Record<string, { label: string; color: string }> = {
          downtrend_breakout: { label: "下跌突破", color: "red" },
          range_bound: { label: "区间震荡", color: "orange" },
          uptrend_pullback: { label: "上涨回调", color: "green" },
        };
        const cfg = map[v] || { label: v, color: "default" };
        return <Tag color={cfg.color}>{cfg.label}</Tag>;
      },
    },
    {
      title: "K线形态",
      dataIndex: "pattern",
      key: "pattern",
      render: (v: string | null, record: ScanResult) => {
        if (!v) return <span style={{ color: "#999" }}>-</span>;
        const map: Record<string, string> = {
          hammer: "锤形线",
          inverted_hammer: "倒锤形线",
          bullish_engulfing: "看涨吞没",
          bearish_engulfing: "看跌吞没",
          morning_star: "启明星",
          piercing_line: "刺透线",
          close_above_prev_high: "收盘破前高",
        };
        const label = map[v] || v;
        return (
          <Tooltip title={record.signal_reason || undefined}>
            <Tag color="blue">{label}</Tag>
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
      title: "突破幅度",
      dataIndex: "breakout_pct",
      key: "breakout_pct",
      sorter: (a, b) => a.breakout_pct - b.breakout_pct,
      render: (v: number) => (
        <span style={{ color: "#52c41a", fontWeight: 600 }}>
          +{v.toFixed(2)}%
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
      {/* 左侧：结果列表 + 折叠 AI 分析 */}
      <div style={{ flex: "0 0 55%", overflow: "auto", paddingRight: 8 }}>
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
                  {/* 计算实际决策 */}
                  {(() => {
                    const eff = getEffectiveDecision(ai);
                    const isSkip = eff.decision === "skip";
                    return (
                      <>
                        {/* AI 决策标签 */}
                        {isSkip ? (
                          <Alert
                            type="error"
                            showIcon
                            message={
                              <strong style={{ fontSize: 14 }}>
                                ❌ 不建议开单
                              </strong>
                            }
                            description={eff.reason}
                            style={{ marginBottom: 8 }}
                          />
                        ) : (
                          <Alert
                            type="success"
                            showIcon
                            message={
                              <strong style={{ fontSize: 14 }}>
                                ✅ 建议开单
                              </strong>
                            }
                            description={ai.skip_reason || ""}
                            style={{ marginBottom: 8 }}
                          />
                        )}

                        {/* 交易细节：仅 suggest 时展示 */}
                        {!isSkip && (
                          <Descriptions bordered size="small" column={2}>
                            <Descriptions.Item label="方向">
                              {ai.direction === "long" ? (
                                <Tag color="green">做多 (Long)</Tag>
                              ) : ai.direction === "short" ? (
                                <Tag color="red">做空 (Short)</Tag>
                              ) : (
                                <span style={{ color: "#999" }}>-</span>
                              )}
                            </Descriptions.Item>
                            <Descriptions.Item label="推荐程度">
                              {ai.recommendation != null ? (
                                <span
                                  style={{
                                    fontWeight: 700,
                                    color:
                                      ai.recommendation >= 80
                                        ? "#ff4d4f"
                                        : ai.recommendation >= 60
                                          ? "#fa8c16"
                                          : ai.recommendation >= 40
                                            ? "#faad14"
                                            : "#8c8c8c",
                                  }}>
                                  {ai.recommendation}分
                                </span>
                              ) : (
                                <span style={{ color: "#999" }}>-</span>
                              )}
                            </Descriptions.Item>
                            <Descriptions.Item label="盈亏比">
                              {ai.risk_reward_ratio != null
                                ? `${ai.risk_reward_ratio.toFixed(2)}`
                                : "-"}
                            </Descriptions.Item>
                            <Descriptions.Item label="入场价">
                              {fmtPrice(ai.entry_price)}
                            </Descriptions.Item>
                            <Descriptions.Item label="仓位建议">
                              {ai.position_pct != null
                                ? `${ai.position_pct}%`
                                : "-"}
                            </Descriptions.Item>
                            <Descriptions.Item label="止损价">
                              <span style={{ color: "#ff4d4f" }}>
                                {fmtPrice(ai.stop_loss)}
                              </span>
                            </Descriptions.Item>
                            <Descriptions.Item label="止盈1">
                              <span style={{ color: "#52c41a" }}>
                                {fmtPrice(ai.take_profit_1)}
                              </span>
                            </Descriptions.Item>
                            <Descriptions.Item label="止盈2" span={2}>
                              <span style={{ color: "#52c41a" }}>
                                {fmtPrice(ai.take_profit_2)}
                              </span>
                            </Descriptions.Item>
                            <Descriptions.Item label="分析时间" span={2}>
                              {bj(ai.created_at).format("YYYY-MM-DD HH:mm:ss")}
                            </Descriptions.Item>
                            <Descriptions.Item label="AI 推理" span={2}>
                              <Paragraph style={{ margin: 0 }}>
                                {ai.analysis || "-"}
                              </Paragraph>
                            </Descriptions.Item>
                          </Descriptions>
                        )}

                        {/* skip 时只显示 AI 推理 */}
                        {isSkip && ai.analysis && (
                          <Descriptions bordered size="small" column={1}>
                            <Descriptions.Item label="AI 推理">
                              <Paragraph style={{ margin: 0 }}>
                                {ai.analysis}
                              </Paragraph>
                            </Descriptions.Item>
                          </Descriptions>
                        )}
                      </>
                    );
                  })()}
                  {rowError && (
                    <Alert
                      type="error"
                      message="上次重新分析失败"
                      description={rowError}
                      showIcon
                      style={{ marginTop: 8, textAlign: "left" }}
                    />
                  )}
                  <div style={{ marginTop: 8, textAlign: "center" }}>
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

      {/* 右侧：K线图 */}
      <div
        style={{
          flex: "1 1 45%",
          overflow: "auto",
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
          />
        )}
      </div>
    </div>
  );
}
