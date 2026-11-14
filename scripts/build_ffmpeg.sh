#!/bin/bash
# Bouwt de eigen LGPL-build van ffmpeg en ffprobe in build/ffmpeg-out (geen externe bibliotheken).
set -e
VERSION=9.0.2
SHA256=8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e
HERE="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$HERE/build/ffmpeg-src"
cd "$HERE/build/ffmpeg-src"
if [ ! -d "ffmpeg-$VERSION" ]; then
  curl -fLO "https://ffmpeg.org/releases/ffmpeg-$VERSION.tar.xz"
  echo "$SHA256  ffmpeg-$VERSION.tar.xz" | shasum -a 256 -c -
  tar -xf "ffmpeg-$VERSION.tar.xz"
fi
cd "ffmpeg-$VERSION"
# ffmpeg bakt zijn prefix in het programma (zichtbaar in `ffmpeg -version` en als pad voor presets). Daarom
# een neutrale prefix en installeren via DESTDIR, zodat het pad van de bouwmap niet in de dmg komt.
PREFIX=/opt/local-transcribe/ffmpeg
STAGE="$HERE/build/ffmpeg-stage"
./configure --prefix="$PREFIX" --disable-gpl --disable-nonfree --disable-autodetect \
  --enable-zlib --disable-doc --disable-ffplay --disable-debug --disable-network
make -j"$(sysctl -n hw.ncpu)"
rm -rf "$STAGE" "$HERE/build/ffmpeg-out"
make install DESTDIR="$STAGE"
mv "$STAGE$PREFIX" "$HERE/build/ffmpeg-out"
rm -rf "$STAGE"
"$HERE/build/ffmpeg-out/bin/ffmpeg" -version | head -1
