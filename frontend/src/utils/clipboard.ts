/**
 * 剪贴板写入（2026-10-05）：navigator.clipboard 仅在安全上下文（https / localhost）
 * 可用——通过 http://IP:端口 访问时为 undefined，直接调用同步抛 TypeError 且无提示。
 * 非安全上下文降级为隐藏 textarea + document.execCommand('copy')（已废弃但仍被
 * 全部主流浏览器支持，是 http 环境唯一可用通道）。
 *
 * 返回是否成功，由调用方决定提示文案。
 */
export async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // 权限拒绝等场景落到 execCommand 兜底
    }
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  document.body.removeChild(ta);
  return ok;
}
