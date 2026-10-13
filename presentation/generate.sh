#!/usr/bin/env bash
set -e

# Base directory
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$DIR"

echo "=========================================="
echo "🚀 RieltorBot Longread Generator (1600x3900)"
echo "=========================================="

# Run Playwright rendering
echo "📸 Рендеринг зображення через Playwright..."
(cd "$DIR/.." && .venv/bin/python presentation/render.py)

OUTPUT_PNG="$DIR/rieltorbot_longread.png"

# Optional optimization with pngquant or ImageMagick
if command -v pngquant &> /dev/null; then
    echo "🗜 Оптимізація розміру файлу через pngquant..."
    pngquant --quality=80-95 --ext .png --force "$OUTPUT_PNG"
    echo "✔ Оптимізовано!"
fi

echo "=========================================="
echo "✅ Готово! Результат збережено:"
echo "   $OUTPUT_PNG"
echo "=========================================="
