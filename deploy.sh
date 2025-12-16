#!/bin/bash
set -e

GREEN='\033[0;32m'
NC='\033[0m'

echo -e "${GREEN}Deploying to AWS...${NC}"

AWS_REGION=${AWS_REGION:-"eu-west-1"}
PROJECT_NAME="news-ai-agent"

check_deps() {
    command -v aws >/dev/null || { echo "AWS CLI required"; exit 1; }
    command -v docker >/dev/null || { echo "Docker required"; exit 1; }
}

get_account() {
    aws sts get-caller-identity --query Account --output text
}

create_ecr() {
    ACCOUNT_ID=$(get_account)
    ECR="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${PROJECT_NAME}"
    
    aws ecr describe-repositories --repository-names ${PROJECT_NAME} --region ${AWS_REGION} 2>/dev/null || \
        aws ecr create-repository --repository-name ${PROJECT_NAME} --region ${AWS_REGION}
    
    echo ${ECR}
}

build_push() {
    ECR=$1
    aws ecr get-login-password --region ${AWS_REGION} | docker login --username AWS --password-stdin ${ECR%/*}
    docker build -t ${PROJECT_NAME}:latest .
    docker tag ${PROJECT_NAME}:latest ${ECR}:latest
    docker push ${ECR}:latest
}

check_deps
ECR=$(create_ecr)
build_push ${ECR}

echo -e "${GREEN}✅ Deployed!${NC}"
