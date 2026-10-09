# ===== Stage 1: Build =====
FROM node:20-alpine AS builder

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .
# oakscriptjs（Pine 运行时，1290 指标全量进打包图）使 vite build 堆需求
# 超过 1.6G 内存服务器的 Node 默认上限（~855MB，2026-10-09 部署 OOM 实证）；
# 显式放宽到 2GB，超出部分由 swap 兜底（服务器 swap 3.9G，空闲 2.4G+）
ENV NODE_OPTIONS=--max-old-space-size=2048
RUN npm run build

# ===== Stage 2: Serve with nginx =====
FROM nginx:1.27-alpine

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
