#!/bin/bash
# Delivery Zone Manager — Mac Launcher
# Double-click this file in Finder to start the app.

cd "$(dirname "$0")"

echo "============================================"
echo "  Delivery Zone Manager"
echo "============================================"
echo ""

# Check Node.js
if ! command -v node &>/dev/null; then
  echo "ERROR: Node.js is not installed."
  echo ""
  echo "Please download and install Node.js from:"
  echo "  https://nodejs.org  (choose the LTS version)"
  echo ""
  read -p "Press Enter to exit..."
  exit 1
fi

NODE_VERSION=$(node -v)
echo "Node.js: $NODE_VERSION"
echo ""

# Install dependencies if needed
if [ ! -d "node_modules" ]; then
  echo "Installing dependencies (first run only)..."
  npm install
  echo ""
fi

# Kill any existing instance on port 3000
lsof -ti tcp:3000 | xargs kill -9 2>/dev/null

echo "Starting server on http://localhost:3000 ..."
echo "Opening login page in your browser..."
echo ""
echo "Press Ctrl+C to stop the app."
echo ""

# Start server in background, open browser after short delay
node server.js &
SERVER_PID=$!

sleep 2
open "http://localhost:3000/login"

# Wait for server process
wait $SERVER_PID
