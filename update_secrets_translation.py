#!/usr/bin/env python3
"""Update AWS Secrets Manager with credentials and prompt templates."""

import os
import json
from pathlib import Path
import boto3
from botocore.exceptions import ClientError

SCRIPT_DIR = Path(__file__).parent

# Read .env file for credentials
env_vars = {}
with open(SCRIPT_DIR / '.env', 'r') as f:
    for line in f:
        line = line.rstrip()
        if not line or line.startswith('#'):
            continue
        if '=' in line:
            key, value = line.split('=', 1)
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            env_vars[key] = value

# Read prompt templates from files
prompts_dir = SCRIPT_DIR / 'prompts'
batch_prompt = (prompts_dir / 'batch_selection.txt').read_text(encoding='utf-8')
single_prompt = (prompts_dir / 'single_article.txt').read_text(encoding='utf-8')
quality_prompt = (prompts_dir / 'quality_check.txt').read_text(encoding='utf-8')

# Prepare secret payload
secret_payload = {
    'ANTHROPIC_API_KEY': env_vars.get('ANTHROPIC_API_KEY'),
    'INSTAGRAM_ACCESS_TOKEN': env_vars.get('INSTAGRAM_ACCESS_TOKEN'),
    'INSTAGRAM_ACCOUNT_ID': env_vars.get('INSTAGRAM_ACCOUNT_ID'),
    'AI_PROMPT_BATCH_SELECTION': batch_prompt,
    'AI_PROMPT_SINGLE_ARTICLE': single_prompt,
    'AI_PROMPT_QUALITY_CHECK': quality_prompt,
}

# Update AWS Secrets Manager
try:
    client = boto3.client('secretsmanager', region_name='eu-central-1')
    response = client.update_secret(
        SecretId='news-ai-agent/credentials',
        SecretString=json.dumps(secret_payload, ensure_ascii=False)
    )
    
    print("✅ AWS Secrets Manager güncellendi!")
    print(f"   Secret ARN: {response['ARN']}")
    print(f"   Version: {response['VersionId']}")
    print(f"\n✨ Changes:")
    print(f"   ✓ AI_PROMPT_SINGLE_ARTICLE: {len(secret_payload['AI_PROMPT_SINGLE_ARTICLE'])} chars")
    print(f"   ✓ AI_PROMPT_BATCH_SELECTION: {len(secret_payload['AI_PROMPT_BATCH_SELECTION'])} chars")
    print(f"   ✓ AI_PROMPT_QUALITY_CHECK: {len(secret_payload['AI_PROMPT_QUALITY_CHECK'])} chars")
    print(f"\n📋 Next steps:")
    print(f"   1. Rebuild Lambda: ./build_lambda.sh")
    print(f"   2. Deploy: cd infrastructure/terraform && terraform apply -auto-approve")
    print(f"   3. Test: aws lambda invoke --function-name news-ai-agent ...")
    
except ClientError as e:
    if e.response['Error']['Code'] == 'ResourceNotFoundException':
        print("❌ Secrets Manager secret bulunamadı")
        print("   Komutu çalıştır: ./deploy.sh")
    else:
        print(f"❌ Hata: {e}")
        exit(1)
except Exception as e:
    print(f"❌ Hata: {e}")
    exit(1)
