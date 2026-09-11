// 技术指标计算库（前端本地计算，与图表 K 线同源数据）
// 所有函数输入按时间升序的数组，未形成周期的位置输出 null

export type Line = (number | null)[]

/** SMA 简单移动平均 */
export function calcSma(values: number[], period: number): Line {
  const out: Line = new Array(values.length).fill(null)
  if (period < 2 || values.length < period) return out
  let sum = 0
  for (let i = 0; i < values.length; i++) {
    sum += values[i]
    if (i >= period) sum -= values[i - period]
    if (i >= period - 1) out[i] = sum / period
  }
  return out
}

/** EMA 指数移动平均（SMA 种子 + 递推） */
export function calcEma(values: number[], period: number): Line {
  const out: Line = new Array(values.length).fill(null)
  if (period < 2 || values.length < period) return out
  let sum = 0
  for (let i = 0; i < period; i++) sum += values[i]
  out[period - 1] = sum / period
  const k = 2 / (period + 1)
  for (let i = period; i < values.length; i++) {
    const prev = out[i - 1]
    if (prev != null) out[i] = values[i] * k + prev * (1 - k)
  }
  return out
}

/** BOLL 布林带（默认 20, 2）：中轨 = SMA，上下轨 = 中轨 ± mult 倍总体标准差 */
export function calcBoll(
  closes: number[],
  period = 20,
  mult = 2,
): { mid: Line; upper: Line; lower: Line } {
  const n = closes.length
  const mid: Line = new Array(n).fill(null)
  const upper: Line = new Array(n).fill(null)
  const lower: Line = new Array(n).fill(null)
  if (period < 2 || n < period) return { mid, upper, lower }
  let sum = 0
  for (let i = 0; i < n; i++) {
    sum += closes[i]
    if (i >= period) sum -= closes[i - period]
    if (i < period - 1) continue
    const avg = sum / period
    let sq = 0
    for (let j = i - period + 1; j <= i; j++) {
      const d = closes[j] - avg
      sq += d * d
    }
    const std = Math.sqrt(sq / period)
    mid[i] = avg
    upper[i] = avg + mult * std
    lower[i] = avg - mult * std
  }
  return { mid, upper, lower }
}

/** MACD（默认 12, 26, 9）：DIF = EMA快 - EMA慢；DEA = DIF 的 EMA；柱 = (DIF-DEA)*2 */
export function calcMacd(
  closes: number[],
  fast = 12,
  slow = 26,
  signal = 9,
): { dif: Line; dea: Line; hist: Line } {
  const emaFast = calcEma(closes, fast)
  const emaSlow = calcEma(closes, slow)
  const difRaw: number[] = []
  const difIdx: number[] = []
  for (let i = 0; i < closes.length; i++) {
    if (emaFast[i] != null && emaSlow[i] != null) {
      difRaw.push(emaFast[i]! - emaSlow[i]!)
      difIdx.push(i)
    }
  }
  const n = closes.length
  const dif: Line = new Array(n).fill(null)
  const dea: Line = new Array(n).fill(null)
  const hist: Line = new Array(n).fill(null)
  if (difRaw.length < signal) {
    difRaw.forEach((v, j) => (dif[difIdx[j]] = v))
    return { dif, dea, hist }
  }
  // DEA = DIF 连续段的 EMA（首值取前 signal 个 DIF 的均值）
  let seed = 0
  for (let j = 0; j < signal; j++) seed += difRaw[j]
  let deaVal = seed / signal
  dea[difIdx[signal - 1]] = deaVal
  difRaw.forEach((v, j) => (dif[difIdx[j]] = v))
  for (let j = signal; j < difRaw.length; j++) {
    deaVal = difRaw[j] * (2 / (signal + 1)) + deaVal * (1 - 2 / (signal + 1))
    dea[difIdx[j]] = deaVal
  }
  for (let j = 0; j < difRaw.length; j++) {
    const d = dea[difIdx[j]]
    if (d != null) hist[difIdx[j]] = (dif[difIdx[j]]! - d) * 2
  }
  return { dif, dea, hist }
}

/** RSI（默认 14，Wilder 平滑） */
export function calcRsi(closes: number[], period = 14): Line {
  const n = closes.length
  const out: Line = new Array(n).fill(null)
  if (period < 2 || n <= period) return out
  let gain = 0
  let loss = 0
  for (let i = 1; i <= period; i++) {
    const ch = closes[i] - closes[i - 1]
    if (ch > 0) gain += ch
    else loss -= ch
  }
  gain /= period
  loss /= period
  out[period] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss)
  for (let i = period + 1; i < n; i++) {
    const ch = closes[i] - closes[i - 1]
    gain = (gain * (period - 1) + (ch > 0 ? ch : 0)) / period
    loss = (loss * (period - 1) + (ch < 0 ? -ch : 0)) / period
    out[i] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss)
  }
  return out
}

/** KDJ 随机指标（默认 9, 3, 3） */
export function calcKdj(
  highs: number[],
  lows: number[],
  closes: number[],
  n = 9,
  kP = 3,
  dP = 3,
): { k: Line; d: Line; j: Line } {
  const len = closes.length
  const k: Line = new Array(len).fill(null)
  const d: Line = new Array(len).fill(null)
  const j: Line = new Array(len).fill(null)
  if (n < 2 || len < n) return { k, d, j }
  let kVal = 50
  let dVal = 50
  for (let i = n - 1; i < len; i++) {
    let hh = -Infinity
    let ll = Infinity
    for (let m = i - n + 1; m <= i; m++) {
      if (highs[m] > hh) hh = highs[m]
      if (lows[m] < ll) ll = lows[m]
    }
    const rsv = hh === ll ? 50 : ((closes[i] - ll) / (hh - ll)) * 100
    kVal = (kVal * (kP - 1) + rsv) / kP
    dVal = (dVal * (dP - 1) + kVal) / dP
    k[i] = kVal
    d[i] = dVal
    j[i] = 3 * kVal - 2 * dVal
  }
  return { k, d, j }
}
