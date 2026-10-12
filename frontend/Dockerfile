# ===== 前端镜像：仅打包构建产物 =====
# 2026-10-12 构建外移：1.6G 内存服务器无法完成含 mermaid 的 vite build
# （峰值内存约 2.3GB，连续两次部署卡死 2h+ 后 SSH 断连失败；前身
#  oakscriptjs 全量打包已曾于 2026-10-09 OOM 一次）。
# dist 产物改由 GitHub Actions runner 构建，经部署通道以 stdin 传入，
# 由 scripts/release.sh 解压到 frontend/dist/ 后打包入本镜像（秒级）。
FROM nginx:1.27-alpine

COPY dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
