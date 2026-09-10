#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

# 0. 检查并安装 pyenv 和 direnv（如尚未安装）
if ! command -v pyenv &> /dev/null; then
    echo "🧩 正在安装 pyenv..."
    brew install pyenv
fi

if ! command -v direnv &> /dev/null; then
    echo "🧩 正在安装 direnv..."
    brew install direnv
fi

# 1. 安装 Python 3.11（使用 pyenv）
echo "🐍 安装 Python 3.11.9（如已存在则跳过）..."
pyenv install 3.11.9 -s

# 2. 设置本地 Python 版本
pyenv local 3.11.9

# 3. 创建虚拟环境
echo "📦 创建虚拟环境 .venv"
python3 -m venv .venv

# 4. 激活虚拟环境
source .venv/bin/activate

# 5. 升级 pip & 安装依赖
pip install --upgrade pip setuptools wheel

# 6. 安装本地 TikTokLive 项目为开发包
pip install -r requirements.lock
pip install --no-deps -e '.[server]'

# 7. 创建 .envrc 文件供 direnv 自动加载
cat > .envrc <<EOF
export PYTHONPATH=$(pwd)
source .venv/bin/activate
EOF

# 8. 激活 direnv（首次需要手动允许）
echo "✅ 初始化完成，请运行：direnv allow"
