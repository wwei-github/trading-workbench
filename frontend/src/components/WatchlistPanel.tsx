import { useEffect, useMemo, useState } from "react";
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
import type { ScanResult, WatchlistItem } from "../types";

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
  } = useScanStore();

  const [quotes, setQuotes] = useState<
    Record<string, { price: number; volume_24h: number }>
  >({});
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null);

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

  // 组装行数据：watchlist + 行情 + 最新扫描信号
  const rows: WatchRow[] = useMemo(
    () =>
      watchlist.map((w) => ({
        ...w,
        price: quotes[w.symbol]?.price ?? null,
        volume_24h: quotes[w.symbol]?.volume_24h ?? null,
        scan: results.find((r) => r.symbol === w.symbol) || null,
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
      title: "添加时间",
      dataIndex: "created_at",
      key: "created_at",
      render: (v: string) => (
        <Text type="secondary">{v.slice(0, 16).replace("T", " ")}</Text>
      ),
    },
    {
      title: "操作",
      key: "action",
      width: 90,
      render: (_: unknown, r: WatchRow) => (
        <Popconfirm
          title="确认删除该关注？"
          onConfirm={(e) => {
            e?.stopPropagation();
            handleDelete(r.symbol);
          }}
          onCancel={(e) => e?.stopPropagation()}
        >
          <Button
            type="text"
            danger
            size="small"
            icon={<DeleteOutlined />}
            onClick={(e) => e.stopPropagation()}
          >
            删除
          </Button>
        </Popconfirm>
      ),
    },
  ];

  return (
    <div style={{ display: "flex", height: "100%", overflow: "hidden" }}>
      {/* 左侧：关注列表（与扫描结果同布局） */}
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
            在"扫描结果"中点击币种旁的 ★ 即可加入关注
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
            onClick: () => setSelectedSymbol(record.symbol),
            style: { cursor: "pointer" },
          })}
          rowClassName={(record) =>
            record.symbol === selectedSymbol ? "ant-table-row-selected" : ""
          }
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
