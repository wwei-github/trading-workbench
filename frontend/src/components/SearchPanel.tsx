import { useState } from "react";
import { Button, Input, Empty, Alert, Typography, Space } from "antd";
import { SearchOutlined, RobotOutlined, LoadingOutlined } from "@ant-design/icons";
import KlineChart from "./KlineChart";
import AiAnalysisCard, { getEffectiveDecision } from "./AiAnalysisCard";
import { scanApi } from "../api/scan";
import type { AIAnalysis } from "../types";

const { Text } = Typography;

// 规范币种输入：与后端一致（去空格大写，未带 USDT 后缀自动补全）
function normalizeInput(raw: string): string {
  const s = (raw || "").trim().toUpperCase().replace("/", "").replace("-", "");
  if (!s) return "";
  if (!s.endsWith("USDT") && !s.endsWith("USDC") && !s.endsWith("BUSD") && !s.endsWith("USD")) {
    return s + "USDT";
  }
  return s;
}

export default function SearchPanel() {
  const [input, setInput] = useState("");
  const [symbol, setSymbol] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  const [analyzing, setAnalyzing] = useState(false);
  const [analysis, setAnalysis] = useState<AIAnalysis | null>(null);
  const [analyzeError, setAnalyzeError] = useState<string | null>(null);

  const doSearch = async () => {
    const s = normalizeInput(input);
    if (!s) return;
    setSearching(true);
    setSearchError(null);
    setSymbol(s);
    // 预取 K 线验证币种存在（图表组件内部也会拉取）
    try {
      await scanApi.klines(s, 300);
    } catch {
      setSearchError(`未找到币种 ${s}，请确认名称后重试`);
      setSymbol(null);
    } finally {
      setSearching(false);
    }
  };

  const doAnalyze = async () => {
    if (!symbol) return;
    setAnalyzing(true);
    setAnalyzeError(null);
    setAnalysis(null);
    try {
      const result = await scanApi.analyzeCoin(symbol);
      setAnalysis(result);
    } catch (e: any) {
      setAnalyzeError(e?.response?.data?.detail || "AI 分析失败，请重试");
    } finally {
      setAnalyzing(false);
    }
  };

  // 分析完成后刷新图表（带上价格线标注）
  const chartAi = analyzing ? null : analysis;
  const chartKey = symbol ? `${symbol}-${analysis ? "ai" : "plain"}` : "empty";

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", overflow: "hidden" }}>
      {/* 搜索栏：搜索 + AI 分析 */}
      <div style={{ flexShrink: 0, padding: "12px 12px 0" }}>
        <Space>
          <Input
            placeholder="输入币种名称，如 BTC / ETH / SOL"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onPressEnter={doSearch}
            style={{ width: 260 }}
            allowClear
          />
          <Button
            type="primary"
            icon={<SearchOutlined />}
            loading={searching}
            onClick={doSearch}
          >
            搜索
          </Button>
          <Button
            icon={<RobotOutlined />}
            disabled={!symbol}
            loading={analyzing}
            onClick={doAnalyze}
          >
            AI 分析
          </Button>
          {symbol && (
            <Text type="secondary">
              当前：{symbol.replace(/USDT$/, "")}/USDT（1h · 300 根 K 线）
            </Text>
          )}
        </Space>
      </div>

      {searchError && (
        <Alert
          type="warning"
          message={searchError}
          showIcon
          style={{ margin: "8px 12px 0", flexShrink: 0 }}
        />
      )}

      {/* 主体：图表在左，AI 分析结果在右 */}
      <div style={{ flex: 1, minHeight: 0, display: "flex", padding: 12, gap: 12 }}>
        {/* 左：K线图 */}
        <div style={{ flex: "1 1 55%", minWidth: 0, display: "flex", flexDirection: "column" }}>
          {symbol ? (
            <KlineChart key={chartKey} symbol={symbol} limit={500} ai={chartAi || undefined} />
          ) : (
            <Empty description="输入币种名称开始搜索" style={{ marginTop: 120 }} />
          )}
        </div>

        {/* 右：AI 分析结果 */}
        <div
          style={{
            flex: "0 0 40%",
            overflow: "auto",
            borderLeft: "1px solid #f0f0f0",
            paddingLeft: 12,
          }}
        >
          {analyzing ? (
            <div style={{ textAlign: "center", padding: "60px 0" }}>
              <LoadingOutlined style={{ fontSize: 28 }} />
              <div style={{ marginTop: 12 }}>
                <Text type="secondary">AI 正在分析 {symbol}，请稍候（约 1 分钟）...</Text>
              </div>
            </div>
          ) : analyzeError ? (
            <Alert type="error" message="AI 分析失败" description={analyzeError} showIcon />
          ) : analysis ? (
            <>
              <AiAnalysisCard ai={analysis} />
              {getEffectiveDecision(analysis).decision === "skip" && !analysis.analysis && (
                <Alert
                  type="info"
                  message="AI 未返回额外推理内容"
                  showIcon
                  style={{ marginTop: 8 }}
                />
              )}
            </>
          ) : (
            <Empty
              description={symbol ? "点击上方「AI 分析」获取开单建议" : "搜索币种后可进行 AI 分析"}
              style={{ marginTop: 60 }}
            />
          )}
        </div>
      </div>
    </div>
  );
}
