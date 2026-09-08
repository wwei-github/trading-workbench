import { Table, Tag, Empty } from "antd";
import type { ColumnsType } from "antd/es/table";
import dayjs from "dayjs";
import { useScanStore } from "../stores/scanStore";
import type { ScanResult } from "../types";

export default function ResultTable() {
  const { results, total, loading } = useScanStore();

  const columns: ColumnsType<ScanResult> = [
    {
      title: "币种",
      dataIndex: "symbol",
      key: "symbol",
      width: 120,
      render: (v: string) => <strong>{v.replace("USDT", "")}/USDT</strong>,
    },
    {
      title: "信号类型",
      dataIndex: "signal_type",
      key: "signal_type",
      width: 110,
      filters: [
        { text: "下跌突破", value: "downtrend_breakout" },
        { text: "区间震荡", value: "range_bound" },
        { text: "上涨回调", value: "uptrend_pullback" },
      ],
      onFilter: (value, record) => record.signal_type === value,
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
      title: "当前价格",
      dataIndex: "current_price",
      key: "current_price",
      width: 140,
      render: (v: number) =>
        v < 1 ? v.toFixed(6) : v < 100 ? v.toFixed(4) : v.toFixed(2),
    },
    {
      title: "突破幅度",
      dataIndex: "breakout_pct",
      key: "breakout_pct",
      width: 120,
      sorter: (a, b) => a.breakout_pct - b.breakout_pct,
      defaultSortOrder: "descend",
      render: (v: number) => (
        <span style={{ color: "#52c41a", fontWeight: 600 }}>
          +{v.toFixed(2)}%
        </span>
      ),
    },
    {
      title: "趋势线斜率",
      dataIndex: "trend_slope",
      key: "trend_slope",
      width: 130,
      render: (v: number) => v.toExponential(2),
    },
    {
      title: "R²",
      dataIndex: "r_squared",
      key: "r_squared",
      width: 100,
      render: (v: number) => v.toFixed(3),
    },
    {
      title: "状态",
      dataIndex: "is_repeat",
      key: "is_repeat",
      width: 100,
      render: (v: boolean) =>
        v ? (
          <Tag color="default">重复命中</Tag>
        ) : (
          <Tag color="green">新命中</Tag>
        ),
    },
    {
      title: "命中时间",
      dataIndex: "created_at",
      key: "created_at",
      width: 160,
      render: (v: string) => dayjs(v).format("YYYY-MM-DD HH:mm:ss"),
    },
  ];

  return (
    <Table
      rowKey="id"
      columns={columns}
      dataSource={results}
      loading={loading}
      pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 条` }}
      locale={{ emptyText: <Empty description="暂无命中币种" /> }}
      scroll={{ x: 900 }}
    />
  );
}
