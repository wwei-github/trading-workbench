import { useEffect } from "react";
import { Table, Button, Empty, Popconfirm, message, Typography } from "antd";
import { DeleteOutlined, ReloadOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import { bj } from "../utils/dayjs";
import { useScanStore } from "../stores/scanStore";
import type { WatchlistItem } from "../types";

const { Text } = Typography;

export default function WatchlistPanel() {
  const { watchlist, fetchWatchlist, removeFromWatchlist } = useScanStore();

  useEffect(() => {
    fetchWatchlist();
  }, [fetchWatchlist]);

  const handleDelete = async (symbol: string) => {
    try {
      await removeFromWatchlist(symbol);
      message.success(`已删除 ${symbol}`);
    } catch (e: any) {
      message.error(e?.response?.data?.detail || "删除失败");
    }
  };

  const columns: ColumnsType<WatchlistItem> = [
    {
      title: "币种",
      dataIndex: "symbol",
      key: "symbol",
      render: (v: string) => (
        <Text strong>
          {v.replace(/USDT$/, "")}/USDT
        </Text>
      ),
    },
    {
      title: "备注",
      dataIndex: "note",
      key: "note",
      render: (v: string | null) => v || <Text type="secondary">-</Text>,
    },
    {
      title: "添加时间",
      dataIndex: "created_at",
      key: "created_at",
      render: (v: string) => bj(v).format("YYYY-MM-DD HH:mm:ss"),
    },
    {
      title: "操作",
      key: "action",
      width: 100,
      render: (_: unknown, record: WatchlistItem) => (
        <Popconfirm
          title="确认删除该关注？"
          onConfirm={() => handleDelete(record.symbol)}
        >
          <Button type="text" danger size="small" icon={<DeleteOutlined />}>
            删除
          </Button>
        </Popconfirm>
      ),
    },
  ];

  return (
    <div style={{ padding: 12, height: "100%", overflow: "auto" }}>
      <div style={{ marginBottom: 12 }}>
        <Button
          icon={<ReloadOutlined />}
          size="small"
          onClick={() => fetchWatchlist()}
        >
          刷新
        </Button>
        <Text type="secondary" style={{ marginLeft: 12 }}>
          在"扫描结果"中点击币种旁的 ★ 即可加入关注
        </Text>
      </div>
      <Table
        rowKey="id"
        size="small"
        columns={columns}
        dataSource={watchlist}
        loading={false}
        pagination={false}
        locale={{ emptyText: <Empty description="暂无关注币种" /> }}
      />
    </div>
  );
}
