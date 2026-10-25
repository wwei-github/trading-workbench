/**
 * 关注页（2026-10-25 改版）：去掉左侧关注表格，改为「币种切换栏 + 铺满的 K 线图」。
 * - 币种来自后端 watchlist（启动时种子 BTC/ETH/BNB/OKB/SOL，只补缺失、不删用户已有）
 * - 图表默认日K（1d），首屏 1500 根：Vegas 慢隧道 EMA676 需要 ≥676 根 K 线；
 *   开启 IndexedDB 缓存（1h/4h/1d），左滑到左沿继续加载更早历史
 * - 后台低并发预缓存前 5 个关注币 × 1h/4h/1d（新鲜项按 meta 跳过）
 * - 刷新 = 重拉关注列表；删除当前 = 取消关注该币并自动切到剩余第一个
 */
import { useEffect, useState } from "react";
import { Button, Empty, Popconfirm, Space, Typography, message } from "antd";
import { DeleteOutlined, ReloadOutlined } from "@ant-design/icons";
import { useScanStore } from "../stores/scanStore";
import { scanApi } from "../api/scan";
import {
  getMeta,
  isTailFresh,
  saveTail,
  type KlineInterval,
} from "../utils/klineDb";
import KlineChart from "./KlineChart";

const { Text } = Typography;

// 首屏/预缓存根数（Vegas EMA676 需要 ≥676 根）
const PRELOAD_LIMIT = 1500;
// 预缓存周期与并发（低并发避免抢占交易所与后端配额）
const PRELOAD_INTERVALS: KlineInterval[] = ["1h", "4h", "1d"];
const PRELOAD_SYMBOLS = 5;
const PRELOAD_CONCURRENCY = 2;

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

  // 后台预缓存（关注页首屏之外）：前 5 个关注币 × 1h/4h/1d，低并发；
  // meta 尾端新鲜（1h/4h 60s、1d 10min）直接跳过，避免重复请求
  useEffect(() => {
    if (!watchlist.length) return;
    let cancelled = false;
    const tasks = watchlist
      .slice(0, PRELOAD_SYMBOLS)
      .flatMap((w) => PRELOAD_INTERVALS.map((iv) => ({ symbol: w.symbol, iv })));
    const run = async () => {
      const queue = [...tasks];
      const worker = async () => {
        while (!cancelled) {
          const t = queue.shift();
          if (!t) return;
          try {
            const meta = await getMeta(t.symbol, t.iv);
            if (isTailFresh(meta, t.iv)) continue;
            const d = await scanApi.klines(t.symbol, PRELOAD_LIMIT, false, t.iv);
            if (d.klines.length) await saveTail(t.symbol, t.iv, d.klines);
          } catch {
            /* 预缓存失败不影响看图（打开时还会再拉一次） */
          }
        }
      };
      await Promise.all(
        Array.from({ length: PRELOAD_CONCURRENCY }, () => worker()),
      );
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [watchlist]);

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
          <KlineChart
            symbol={selectedSymbol}
            limit={PRELOAD_LIMIT}
            defaultInterval="1d"
            cacheHistory
            pageSize={1000}
          />
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
