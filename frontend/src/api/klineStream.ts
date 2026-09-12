// 实时 K 线 SSE 客户端（原生 EventSource，无第三方库）
// 服务端：backend/app/api/kline_stream.py + kline_hub.py（docs/05）
// 断线由 EventSource 按服务端下发的 retry: 5000 自动重连，此处不手动重连。

export interface KlineBar {
  t: number; // bar 开始时间（ms）
  o: number;
  h: number;
  l: number;
  c: number;
  v: number; // base volume
  x: boolean; // 该 bar 是否已收盘
}

export interface KlineStreamHandlers {
  onSnapshot?: (bar: KlineBar | null) => void;
  onBar: (bar: KlineBar) => void;
  onDegraded?: (message: string) => void;
}

export function openKlineStream(
  symbol: string,
  interval: string,
  handlers: KlineStreamHandlers,
): () => void {
  const url = `/api/scans/klines/${encodeURIComponent(symbol)}/stream?interval=${encodeURIComponent(interval)}`;
  const es = new EventSource(url);

  const parse = (ev: MessageEvent): any => {
    try {
      return JSON.parse(ev.data);
    } catch {
      return null;
    }
  };

  es.addEventListener("snapshot", (ev) => {
    const d = parse(ev as MessageEvent);
    if (d) handlers.onSnapshot?.(d.bar ?? null);
  });
  es.addEventListener("bar", (ev) => {
    const d = parse(ev as MessageEvent);
    if (d) handlers.onBar(d);
  });
  es.addEventListener("degraded", (ev) => {
    const d = parse(ev as MessageEvent);
    if (d) handlers.onDegraded?.(d.message ?? "");
  });
  // 原生 error（连接中断）→ EventSource 自动重连；不与业务 degraded 事件混用
  es.onerror = () => {
    console.warn(`[kline-stream] ${symbol} 连接中断，5s 后自动重连`);
  };

  return () => es.close();
}
