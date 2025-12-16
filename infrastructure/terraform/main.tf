terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

provider "aws" {
  region = "eu-west-1"
}

resource "aws_s3_bucket" "results" {
  bucket = "news-ai-agent-results-${data.aws_caller_identity.current.account_id}"
}

resource "aws_secretsmanager_secret" "creds" {
  name = "news-ai-agent/credentials"
}

data "aws_caller_identity" "current" {}

output "bucket" { value = aws_s3_bucket.results.id }
