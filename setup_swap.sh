#!/usr/bin/env bash
# setup_swap.sh — 云服务器配置开机自动 Swap（幂等，可重复执行）
# 用途: 2C2G 轻量服务器跑 6 容器，加 swap 防止 AI 批量分析内存冲高时 OOM-kill celery_worker
# 用法: sudo bash setup_swap.sh [大小MB]    # 默认 2048 = 2G
set -euo pipefail

SIZE_MB="${1:-2048}"
SWAP_FILE="/swapfile"

[ "$(id -u)" -eq 0 ] || { echo "错误: 请用 sudo 运行"; exit 1; }

# 1. 创建并启用 swapfile（已启用则跳过）
if swapon --show=NAME --noheadings 2>/dev/null | grep -qw "$SWAP_FILE"; then
  echo "OK swap 已启用: $SWAP_FILE，跳过创建"
else
  # fallocate 在部分文件系统/老内核不支持，失败时退回 dd
  fallocate -l "${SIZE_MB}M" "$SWAP_FILE" 2>/dev/null \
    || dd if=/dev/zero of="$SWAP_FILE" bs=1M count="$SIZE_MB" status=progress
  chmod 600 "$SWAP_FILE"
  mkswap "$SWAP_FILE"
  swapon "$SWAP_FILE"
  echo "OK swap 已启用: $SWAP_FILE (${SIZE_MB}M)"
fi

# 2. 写入 /etc/fstab，重启后自动挂载（已存在则跳过）
if grep -q "^${SWAP_FILE}[[:space:]]" /etc/fstab; then
  echo "OK /etc/fstab 已有 swap 配置，跳过"
else
  echo "$SWAP_FILE none swap sw 0 0" >> /etc/fstab
  echo "OK 已写入 /etc/fstab（重启后自动生效）"
fi

# 3. swappiness=20: 平时优先用内存，紧张时才动 swap（默认 60 对服务器过于激进）
SYSCTL_FILE=/etc/sysctl.d/99-swap.conf
if grep -q "^vm.swappiness" "$SYSCTL_FILE" 2>/dev/null; then
  echo "OK swappiness 已配置，跳过"
else
  echo "vm.swappiness=20" > "$SYSCTL_FILE"
  sysctl -p "$SYSCTL_FILE"
  echo "OK swappiness=20 已持久化"
fi

echo "--- 当前内存与 swap 状态 ---"
free -h
swapon --show
