#!/bin/bash
# AWS Deployment - Quick Start
# Bu script seni adım adım AWS'ye deploy etme sürecinde yönlendirir

set -e

# Use virtual environment's AWS CLI
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"
AWS_CMD="$PROJECT_ROOT/.venv/bin/aws"

echo "🚀 News AI Agent - AWS Deployment Wizard"
echo "========================================="
echo ""
echo "Bu wizard seni AWS'ye güvenli ve maliyet-optimized deployment yapmak için yönlendirecek."
echo ""

# Step 1: Check AWS CLI
echo "📋 Adım 1/8: AWS CLI Kontrolü"
if [ ! -f "$AWS_CMD" ]; then
    echo "❌ AWS CLI bulunamadı!"
    echo ""
    echo "Kurulum:"
    echo "  .venv/bin/pip install awscli"
    echo ""
    echo "Kurulum sonrası bu scripti tekrar çalıştır."
    exit 1
fi
echo "✅ AWS CLI kurulu: $($AWS_CMD --version)"
echo ""

# Step 2: Check AWS Credentials
echo "📋 Adım 2/8: AWS Credentials Kontrolü"
if ! $AWS_CMD sts get-caller-identity &> /dev/null; then
    echo "❌ AWS credentials yapılandırılmamış!"
    echo ""
    echo "Yapılandırma:"
    echo "  aws configure"
    echo ""
    echo "Gerekli bilgiler:"
    echo "  - AWS Access Key ID (IAM User'dan)"
    echo "  - AWS Secret Access Key (IAM User'dan)"
    echo "  - Default region: eu-central-1"
    echo ""
    echo "Detaylı rehber: AWS_SETUP_GUIDE.md"
    exit 1
fi
echo "✅ AWS Credentials yapılandırılmış"
$AWS_CMD sts get-caller-identity
echo ""

# Step 3: Check Terraform
echo "📋 Adım 3/8: Terraform Kontrolü"
if ! command -v terraform &> /dev/null; then
    echo "❌ Terraform bulunamadı!"
    echo ""
    echo "Kurulum:"
    echo "  brew install terraform"
    echo ""
    exit 1
fi
echo "✅ Terraform kurulu: $(terraform --version | head -1)"
echo ""

# Step 4: Local Lambda Test
echo "📋 Adım 4/8: Lambda Lokal Test"
echo "Lambda handler'ı lokal olarak test etmek ister misin?"
echo "⚠️  Bu gerçek Instagram post atacak!"
echo ""
read -p "Lokal test yap? (y/n): " test_local

if [[ "$test_local" =~ ^([yY][eE][sS]|[yY])$ ]]; then
    echo ""
    echo "🧪 Lambda handler lokal test ediliyor..."
    python test_lambda_local.py
    echo ""
    echo "Test tamamlandı. Instagram'da postu kontrol et!"
    echo ""
    read -p "Test başarılı, devam et? (y/n): " continue_after_test
    if [[ ! "$continue_after_test" =~ ^([yY][eE][sS]|[yY])$ ]]; then
        echo "Deployment iptal edildi."
        exit 0
    fi
else
    echo "⏭️  Lokal test atlandı"
fi
echo ""

# Step 5: Cost Review
echo "📋 Adım 5/8: Maliyet Tahmini"
echo ""
echo "💰 Beklenen Aylık Maliyetler (EU West 1):"
echo "   Lambda (256MB, 3min, 90x/ay):  ~$0.20"
echo "   EventBridge (3 rules):         $0.00 (free)"
echo "   S3 Storage (5 MB):             ~$0.01"
echo "   Secrets Manager (1 secret):    ~$0.40"
echo "   CloudWatch Logs (7 days):      ~$0.20"
echo "   ────────────────────────────────────"
echo "   TOPLAM:                        ~$0.80/ay"
echo ""
echo "📊 Optimizasyonlar:"
echo "   ✅ Minimum Lambda memory (256 MB)"
echo "   ✅ Kısa timeout (180 saniye)"
echo "   ✅ Kısa log retention (7 gün)"
echo "   ✅ Only 3 executions/day"
echo ""
read -p "Devam et? (y/n): " continue_cost
if [[ ! "$continue_cost" =~ ^([yY][eE][sS]|[yY])$ ]]; then
    echo "Deployment iptal edildi."
    exit 0
fi
echo ""

# Step 6: Build Lambda Package
echo "📋 Adım 6/8: Lambda Package Build"
echo "Lambda deployment package oluşturuluyor..."
./scripts/build_lambda.sh
echo ""

# Step 7: Setup Secrets
echo "📋 Adım 7/8: AWS Secrets Manager Setup"
echo ""
echo "Şimdi API credentials'ı AWS Secrets Manager'a ekleyeceğiz."
echo "Bu bilgiler .env dosyanda mevcut."
echo ""
read -p "Secrets'ı şimdi ekle? (y/n): " setup_secrets

if [[ "$setup_secrets" =~ ^([yY][eE][sS]|[yY])$ ]]; then
    echo ""
    
    # Load from .env if exists
    if [ -f ".env" ]; then
        echo "📄 .env dosyasından credentials okunuyor..."
        source .env
        
        ANTHROPIC_KEY="${ANTHROPIC_API_KEY}"
        INSTAGRAM_TOKEN="${INSTAGRAM_ACCESS_TOKEN}"
        INSTAGRAM_ACCOUNT="${INSTAGRAM_ACCOUNT_ID}"
        AI_PROMPT_BATCH="${AI_PROMPT_BATCH_SELECTION}"
        AI_PROMPT_SINGLE="${AI_PROMPT_SINGLE_ARTICLE}"
        
        echo "✅ Credentials ve AI promptları .env'den yüklendi"
    else
        echo "⚠️  .env dosyası bulunamadı, manuel giriş gerekli"
        read -p "ANTHROPIC_API_KEY: " ANTHROPIC_KEY
        read -p "INSTAGRAM_ACCESS_TOKEN: " INSTAGRAM_TOKEN
        read -p "INSTAGRAM_ACCOUNT_ID: " INSTAGRAM_ACCOUNT
        echo "⚠️  AI promptları .env'den okunamadı - varsayılan değerler kullanılacak"
        AI_PROMPT_BATCH=""
        AI_PROMPT_SINGLE=""
    fi
    
    # Create secret JSON with prompts
    if [ -n "$AI_PROMPT_BATCH" ] && [ -n "$AI_PROMPT_SINGLE" ]; then
        SECRET_JSON=$(cat <<EOF
{
  "ANTHROPIC_API_KEY": "$ANTHROPIC_KEY",
  "INSTAGRAM_ACCESS_TOKEN": "$INSTAGRAM_TOKEN",
  "INSTAGRAM_ACCOUNT_ID": "$INSTAGRAM_ACCOUNT",
  "AI_PROMPT_BATCH_SELECTION": "$AI_PROMPT_BATCH",
  "AI_PROMPT_SINGLE_ARTICLE": "$AI_PROMPT_SINGLE"
}
EOF
)
        echo "✅ Secrets API keys ve AI promptları içeriyor"
    else
        SECRET_JSON=$(cat <<EOF
{
  "ANTHROPIC_API_KEY": "$ANTHROPIC_KEY",
  "INSTAGRAM_ACCESS_TOKEN": "$INSTAGRAM_TOKEN",
  "INSTAGRAM_ACCOUNT_ID": "$INSTAGRAM_ACCOUNT"
}
EOF
)
        echo "⚠️  AI promptları dahil edilmedi (sadece API keys)"
    fi
    
    echo ""
    echo "🔐 AWS Secrets Manager'a ekleniyor..."
    $AWS_CMD secretsmanager create-secret \
        --name news-ai-agent/credentials \
        --description "News AI Agent credentials" \
        --secret-string "$SECRET_JSON" \
        --region eu-central-1 2>/dev/null || \
    $AWS_CMD secretsmanager update-secret \
        --secret-id news-ai-agent/credentials \
        --secret-string "$SECRET_JSON" \
        --region eu-central-1
    
    echo "✅ Secrets yapılandırıldı"
else
    echo "⚠️  Secrets manuel olarak eklemen gerekecek:"
    echo "   aws secretsmanager create-secret --name news-ai-agent/credentials ..."
fi
echo ""

# Step 8: Terraform Deployment
echo "📋 Adım 8/8: Terraform Deployment"
echo ""
echo "Şimdi Terraform ile altyapı deploy edilecek:"
echo "  - Lambda Function"
echo "  - EventBridge Rules (3 schedule)"
echo "  - S3 Bucket"
echo "  - IAM Roles"
echo "  - CloudWatch Log Groups"
echo ""
read -p "Deploy et? (y/n): " do_deploy

if [[ ! "$do_deploy" =~ ^([yY][eE][sS]|[yY])$ ]]; then
    echo "Deployment iptal edildi."
    echo ""
    echo "💡 Manuel deployment:"
    echo "   cd infrastructure/terraform"
    echo "   terraform init"
    echo "   terraform plan"
    echo "   terraform apply"
    exit 0
fi

cd infrastructure/terraform

echo ""
echo "🔧 Terraform initialize..."
terraform init

echo ""
echo "📝 Terraform plan..."
terraform plan -out=tfplan

echo ""
echo "🚀 Terraform apply..."
terraform apply tfplan

echo ""
echo "✅ DEPLOYMENT TAMAMLANDI!"
echo ""
echo "═══════════════════════════════════════════════════════"
echo "🎉 News AI Agent AWS'de Çalışıyor!"
echo "═══════════════════════════════════════════════════════"
echo ""

# Show outputs
echo "📊 Deployment Bilgileri:"
terraform output

echo ""
echo "📅 Otomatik Paylaşım Saatleri (Amsterdam):"
echo "   🌅 Sabah:    08:00"
echo "   🏙️  Öğle:     12:30"
echo "   🌆 Akşam:    17:30"
echo ""
echo "🧪 Manuel Test:"
echo "   aws lambda invoke --function-name news-ai-agent --region eu-central-1 response.json"
echo ""
echo "📝 Logları Görüntüle:"
echo "   aws logs tail /aws/lambda/news-ai-agent --follow --region eu-central-1"
echo ""
echo "⏸️  Schedule'ı Geçici Durdur (test için):"
echo "   aws events disable-rule --name news-ai-agent-morning --region eu-central-1"
echo "   aws events disable-rule --name news-ai-agent-afternoon --region eu-central-1"
echo "   aws events disable-rule --name news-ai-agent-evening --region eu-central-1"
echo ""
echo "▶️  Schedule'ı Tekrar Başlat:"
echo "   aws events enable-rule --name news-ai-agent-morning --region eu-central-1"
echo "   aws events enable-rule --name news-ai-agent-afternoon --region eu-central-1"
echo "   aws events enable-rule --name news-ai-agent-evening --region eu-central-1"
echo ""
echo "💰 Maliyetleri Takip Et:"
echo "   AWS Console → Billing → Cost Explorer"
echo ""
echo "🗑️  Tamamen Kaldır:"
echo "   cd infrastructure/terraform && terraform destroy"
echo ""
echo "Detaylı bilgi: AWS_SETUP_GUIDE.md"
echo ""
