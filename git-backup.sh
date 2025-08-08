#!/bin/bash

cd "$(dirname "$0")"
BRANCH="main"

echo "🔄 Pulling latest from origin/$BRANCH..."
git pull origin "$BRANCH"

echo "📦 Adding and committing changes..."
git add .
git commit -m "Backup from $(hostname) at $(date +'%Y-%m-%d %H:%M:%S')" || echo "Nothing to commit."

echo "⬆️  Pushing to GitHub..."
git push origin "$BRANCH"

echo "✅ Backup complete!"
