#!/bin/bash
# Deploy News AI Agent to AWS Lambda

set -e

# Always run from project root
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "🚀 Deploying News AI Agent to AWS Lambda"
echo "========================================"

# Check AWS CLI
if ! command -v aws &> /dev/null; then
    echo "❌ AWS CLI not found. Please install it first."
    exit 1
fi

# Check Terraform
if ! command -v terraform &> /dev/null; then
    echo "❌ Terraform not found. Please install it first."
    exit 1
fi

# Step 1: Build Lambda package
echo ""
echo "📦 Step 1: Building Lambda deployment package..."
./scripts/build_lambda.sh

if [ ! -f "lambda_deployment.zip" ]; then
    echo "❌ Lambda deployment package not created!"
    exit 1
fi

# Step 2: Create Secrets in AWS Secrets Manager
echo ""
echo "🔐 Step 2: Setting up AWS Secrets Manager..."
echo "Would you like to create/update secrets now? (y/n)"
read -r response

if [[ "$response" =~ ^([yY][eE][sS]|[yY])$ ]]; then
    echo ""
    echo "Please provide the following credentials:"
    echo ""
    
    read -p "ANTHROPIC_API_KEY: " ANTHROPIC_KEY
    read -p "INSTAGRAM_ACCESS_TOKEN: " INSTAGRAM_TOKEN
    read -p "INSTAGRAM_ACCOUNT_ID: " INSTAGRAM_ACCOUNT
    
    # Create secret JSON
    SECRET_JSON=$(cat <<EOF
{
  "ANTHROPIC_API_KEY": "$ANTHROPIC_KEY",
  "INSTAGRAM_ACCESS_TOKEN": "$INSTAGRAM_TOKEN",
  "INSTAGRAM_ACCOUNT_ID": "$INSTAGRAM_ACCOUNT"
}
EOF
)
    
    echo ""
    echo "Creating secret in AWS Secrets Manager..."
    aws secretsmanager create-secret \
        --name news-ai-agent/credentials \
        --description "News AI Agent credentials" \
        --secret-string "$SECRET_JSON" \
        --region eu-west-1 2>/dev/null || \
    aws secretsmanager update-secret \
        --secret-id news-ai-agent/credentials \
        --secret-string "$SECRET_JSON" \
        --region eu-west-1
    
    echo "✅ Secrets configured"
else
    echo "⚠️  Skipping secrets setup. Make sure to configure them manually!"
fi

# Step 3: Initialize and apply Terraform
echo ""
echo "🏗️  Step 3: Deploying infrastructure with Terraform..."
cd infrastructure/terraform

echo "Initializing Terraform..."
terraform init

echo ""
echo "Planning deployment..."
terraform plan -out=tfplan

echo ""
echo "Applying infrastructure changes..."
terraform apply tfplan

echo ""
echo "✅ Deployment complete!"
echo ""

# Get outputs
echo "📊 Deployment Information:"
echo "=========================="
terraform output

echo ""
echo "🎉 News AI Agent is now deployed!"
echo ""
echo "📅 Scheduled posts (Amsterdam time):"
echo "   - Morning:   08:00"
echo "   - Afternoon: 12:30"
echo "   - Evening:   17:30"
echo ""
echo "📝 To view logs:"
echo "   aws logs tail /aws/lambda/news-ai-agent --follow --region eu-west-1"
echo ""
echo "🧪 To test Lambda manually:"
echo "   aws lambda invoke --function-name news-ai-agent --region eu-west-1 response.json"
echo ""
