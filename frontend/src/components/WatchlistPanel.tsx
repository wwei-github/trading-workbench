/**
 * 关注页（2026-10-25 改版）：去掉左侧关注表格，改为「币种切换栏 + 铺满的 K 线图」。
 * - 币种来自后端 watchlist（启动时种子 BTC/ETH/BNB/OKB/SOL，只补缺失、不删用户已有）
 * - 图表拉 1000 根：Vegas 慢隧道 EMA676 需要 ≥676 根 K 线才有值
 * - 刷新 = 重拉关注列表；删除当前 = 取消关注该币并自动切到剩余第一个
 */
import { useEffect, useState } from "react";
import { Button, Empty, Popconfirm, Space, Typography, message } from "antd";
import { DeleteOutlined, ReloadOutlined } from "@ant-design/icons";
import { useScanStore } from "../stores/scanStore";
import KlineChart from "./KlineChart";

const { Text } = Typography;

// 展示名：BTCUSDT → BTC/USDT（非 USDT 计价原样保留）
const shortName = (symbol: string) => `${symbol.replace(/USDT$/, "")}/USDT`;

export default function WatchlistPanel() {
  const watchlist = useScanStore((s) => s.watchlist);
  const fetchWatchlist = useScanStore((s) => s.fetchWatchlist);
  const removeFromWatchlist = useScanStore((s) => s.removeFromWatchlist);

  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [deleting, setDeleting] = useState(false);

  const refresh = async () => {
    setLoading(true);
    try {
      await fetchWatchlist();
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 选中态跟随列表：默认选第一个；当前币种被删除后回落到剩余第一个
  useEffect(() => {
    if (!watchlist.length) {
      setSelectedSymbol(null);
      return;
    }
    if (!selectedSymbol || !watchlist.some((w) => w.symbol === selectedSymbol)) {
      setSelectedSymbol(watchlist[0].symbol);
    }
  }, [watchlist, selectedSymbol]);

  const handleDelete = async () => {
    if (!selectedSymbol) return;
    setDeleting(true);
    try {
      await removeFromWatchlist(selectedSymbol);
      message.success(`已取消关注 ${selectedSymbol}`);
    } catch (e: any) {
      message.error(e?.response?.data?.detail || "删除失败");
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100%",
        overflow: "hidden",
      }}>
      {/* 顶部：币种切换 + 刷新 / 删除当前（单行，不挤占图表高度） */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          marginBottom: 8,
          flexShrink: 0,
        }}>
        <div
          style={{
            display: "flex",
            gap: 4,
            flex: 1,
            minWidth: 0,
            overflowX: "auto",
            paddingBottom: 2,
          }}>
          {watchlist.map((w) => (
            <Button
              key={w.symbol}
              size="small"
              type={w.symbol === selectedSymbol ? "primary" : "default"}
              onClick={() => setSelectedSymbol(w.symbol)}
              style={{ flexShrink: 0 }}>
              {shortName(w.symbol)}
            </Button>
          ))}
        </div>
        <Space size={6} style={{ flexShrink: 0 }}>
          <Button
            size="small"
            icon={<ReloadOutlined />}
            loading={loading}
            onClick={refresh}>
            刷新
          </Button>
          <Popconfirm
            title={`取消关注 ${selectedSymbol ?? ""}？`}
            okText="删除"
            cancelText="取消"
            disabled={!selectedSymbol}
            onConfirm={handleDelete}>
            <Button
              size="small"
              danger
              icon={<DeleteOutlined />}
              disabled={!selectedSymbol}
              loading={deleting}>
              删除当前
            </Button>
          </Popconfirm>
        </Space>
      </div>

      {/* 图表铺满剩余空间 */}
      <div style={{ flex: 1, minHeight: 0 }}>
        {selectedSymbol ? (
          <KlineChart symbol={selectedSymbol} limit={1000} />
        ) : (
          <Empty
            description={
              <Text type="secondary">
                {loading
                  ? "加载关注列表…"
                  : "暂无关注币种，可在「扫描结果」中点击 ★ 添加"}
              </Text>
            }
            style={{ marginTop: 80 }}
          />
        )}
      </div>
    </div>
  );
}
