#!/bin/bash
# Build Lambda deployment package

set -e

echo "🏗️  Building Lambda deployment package..."

# Create build directory
BUILD_DIR="lambda_build"
rm -rf $BUILD_DIR
mkdir -p $BUILD_DIR

echo "📦 Copying Python files..."
cp *.py $BUILD_DIR/

# Copy prompt templates
if [ -d "prompts" ]; then
	cp -r prompts $BUILD_DIR/
	echo "📝 Copied prompts/"
fi

REQ_FILE="requirements.txt"
if [ -f "requirements_lambda.txt" ]; then
	REQ_FILE="requirements_lambda.txt"
fi
cp "$REQ_FILE" $BUILD_DIR/requirements.txt

echo "📚 Installing dependencies..."
cd $BUILD_DIR

PIP_CMD=""
if [ -x "../.venv/bin/pip" ]; then
	PIP_CMD="../.venv/bin/pip"
elif command -v pip &> /dev/null; then
	PIP_CMD="pip"
else
	echo "❌ pip bulunamadı. Önce virtualenv'i kur veya pip'i yükle."
	exit 1
fi

$PIP_CMD install -r requirements.txt -t . \
	--platform manylinux2014_x86_64 \
	--only-binary=:all:

echo "🗜️  Creating deployment package..."
zip -r ../lambda_deployment.zip . -x "*.pyc" -x "__pycache__/*" -x "*.dist-info/*"

cd ..
echo "✅ Deployment package created: lambda_deployment.zip"
echo "📊 Package size: $(du -h lambda_deployment.zip | cut -f1)"

# Cleanup
rm -rf $BUILD_DIR
echo "🧹 Cleaned up build directory"
