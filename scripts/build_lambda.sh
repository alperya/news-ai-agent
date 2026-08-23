#!/bin/bash
# Build Lambda deployment package

set -e

# Always run from project root
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "🏗️  Building Lambda deployment package..."

# Remove old ZIP first — zip -r updates in-place, so deleted files would survive otherwise
rm -f lambda_deployment.zip

# Create build directory
BUILD_DIR="lambda_build"
rm -rf $BUILD_DIR
mkdir -p $BUILD_DIR

echo "📦 Copying Python files..."
cp src/*.py $BUILD_DIR/
cp -r src/video $BUILD_DIR/video
cp -r src/music $BUILD_DIR/music
[ -d src/logo ] && cp -r src/logo $BUILD_DIR/logo  # brand logo for fact-carousel CTA slide
cp src/fonts/Montserrat-Bold.ttf $BUILD_DIR/Montserrat-Bold.ttf
cp src/fonts/Poppins-Bold.ttf $BUILD_DIR/Poppins-Bold.ttf
cp src/fonts/Poppins-SemiBold.ttf $BUILD_DIR/Poppins-SemiBold.ttf
cp src/fonts/Poppins-Regular.ttf $BUILD_DIR/Poppins-Regular.ttf
cp lambda_handler.py $BUILD_DIR/
cp token_refresher.py $BUILD_DIR/

# Copy prompt templates
if [ -d "prompts" ]; then
	cp -r prompts $BUILD_DIR/
	echo "📝 Copied prompts/"
fi

REQ_FILE="requirements/base.txt"
if [ -f "requirements/lambda.txt" ]; then
	REQ_FILE="requirements/lambda.txt"
fi
cp "$REQ_FILE" $BUILD_DIR/requirements.txt

echo "📚 Installing dependencies..."
cd $BUILD_DIR

PIP_CMD=""
if [ -x "$PROJECT_ROOT/.venv/bin/pip" ]; then
	PIP_CMD="$PROJECT_ROOT/.venv/bin/pip"
elif command -v pip &> /dev/null; then
	PIP_CMD="pip"
else
	echo "❌ pip not found. Install virtualenv or pip first."
	exit 1
fi

$PIP_CMD install -r requirements.txt -t . \
	--platform manylinux2014_x86_64 \
	--only-binary=:all:

echo "🔪 Stripping files that exceed the 250 MB Lambda unzipped limit..."
# zstandard (~23 MB): optional botocore dep for zstd encoding — unused by us.
rm -rf zstandard/ zstandard*.dist-info/
# Google API discovery documents (~96 MB): keep only youtube.v3.json (372 KB),
# delete the other 581 API definitions. cache_discovery=False at build() time
# means Lambda reads from this local file instead of making a network call.
find googleapiclient/discovery_cache/documents/ -type f ! -name 'youtube.v3.json' -delete

echo "🗜️  Creating deployment package..."
zip -r ../lambda_deployment.zip . \
	-x "*.pyc" -x "__pycache__/*" \
	-x "*.dist-info/RECORD" -x "*.dist-info/LICENSE*" -x "*.dist-info/AUTHORS*" \
	-x "*.dist-info/top_level.txt" -x "*.dist-info/WHEEL" -x "*.dist-info/INSTALLER" \
	-x "*/tests/*" -x "*/test/*" \
	-x "numpy/f2py/*" -x "numpy/testing/*" -x "numpy/doc/*"

cd ..
echo "✅ Deployment package created: lambda_deployment.zip"
UNZIPPED_BYTES=$(unzip -l lambda_deployment.zip | tail -1 | awk '{print $1}')
UNZIPPED_MB=$((UNZIPPED_BYTES / 1024 / 1024))
echo "📊 Package: $(du -h lambda_deployment.zip | cut -f1) ZIP / ${UNZIPPED_MB} MB unzipped"
if [ "$UNZIPPED_MB" -gt 240 ]; then
  echo "❌ Package too large: ${UNZIPPED_MB} MB exceeds 240 MB safety threshold (Lambda hard limit: 250 MB)"
  echo "   Consider migrating to Container Image deployment (no size limit)."
  exit 1
fi

# Cleanup
rm -rf $BUILD_DIR
echo "🧹 Cleaned up build directory"
